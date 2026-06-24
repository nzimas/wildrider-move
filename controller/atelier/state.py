"""Authoritative state manager + graph diffing (blueprint sections 2 & 10).

Ordering principle (section 2): the DSP graph is disposable; the patch data model
is authoritative. On load we build the graph from patch state. On edit we update
state first, then apply a graph diff. This object owns the patch, the modulation
engine, the scene engine and the control layer, and drives the control-rate
modulation loop that writes post-modulation values to SuperCollider.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable

from . import aesthetics
from .catalog import spec
from .control import ControlLayer, Macro, MacroTarget
from .params import RandomizePolicy
from .model import Lane, LaneMode, ModuleInstance, Patch
from .modulation import ModEngine, ModRoute, ModSource, ModType, NodeScope
from .osc_bridge import OSCBridge
from .scenes import SceneEngine
from .seq import SeqEngine

_EPS = 1e-4


class StateManager:
    def __init__(self, bridge: OSCBridge | None = None):
        self.patch = Patch()
        self.mod = ModEngine(root_seed=self.patch.root_seed)
        self.per_module_mod = ModEngine(root_seed=self.patch.root_seed)
        self.scenes = SceneEngine()
        self.seq = SeqEngine()
        self.control = ControlLayer()
        self.bridge = bridge or OSCBridge()
        self.rng = random.Random(self.patch.root_seed)
        self._last_sent: dict[tuple[str, str], list[float]] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._panic = False
        self.cpu_warn = False
        self.lfos_enabled = True          # global on/off for the LFO bank
        self._morph_state: dict[str, Any] | None = None
        self._scene_morph: dict[str, Any] | None = None   # active timed scene morph
        self._last_tick = time.monotonic()

    # -- change notification (for websocket push) -------------------------- #
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _notify(self, event: dict) -> None:
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Graph construction (build DSP from authoritative state).
    # ------------------------------------------------------------------ #
    def build_graph(self) -> None:
        # NB: do NOT send /atelier/boot here. The SC container self-boots via
        # boot.scd; sending boot would re-trigger /atelier/ready and loop. We
        # (re)create all modules on the running server, then push the routing
        # graph built from the connection edges.
        self.bridge.spatial(self.patch.spatial.n, self.patch.spatial.layout_name)
        for m in self.patch.modules.values():
            if not m.spec.is_audio:    # VIZ is a view layer, no DSP synth
                continue
            self.bridge.module_new(m.id, m.type, self.patch.channel_count, m.node_count, 0)
            self.bridge.module_bypass(m.id, m.bypass)
            self.bridge.module_wet(m.id, m.wet_dry)
            self._push_module(m)
        self._sync_graph()
        for rec in self.patch.recorders.values():
            self.bridge.recorder(rec.id, rec.target, rec.tap,
                                 rec.rolling_buffer_seconds, rec.armed)

    def _sync_graph(self) -> None:
        """Send the current connection graph (order/edges/terminals) to the engine."""
        audio = lambda mid: mid in self.patch.modules and self.patch.modules[mid].spec.is_audio
        order = [m for m in self.patch.topo_order() if audio(m)]
        edges = [(c["src"], c["dst"]) for c in self.patch.connections
                 if audio(c["src"]) and audio(c["dst"])]
        terminals = [t for t in self.patch.terminals() if audio(t)]
        self.bridge.graph(order, edges, terminals)

    def _push_module(self, m: ModuleInstance) -> None:
        """Push every effective value for a module (global + per-node vectors)."""
        if not m.spec.is_audio:    # VIZ has no server synth to push to
            return
        for pid, slot in m.global_slots.items():
            self.bridge.set_param(m.id, m.short_pid(pid), -1, slot.effective)
        # send node params as vectors (one value per node)
        if m.node_slots:
            for pid in m.node_slots[0].keys():
                vals = [nd[pid].effective for nd in m.node_slots]
                self.bridge.set_vector(m.id, m.short_pid(pid), vals)
                self._last_sent[(m.id, pid)] = vals

    # ------------------------------------------------------------------ #
    # Edits: update state, then diff to graph.
    # ------------------------------------------------------------------ #
    def add_lane(self, lane_id: str, name: str, mode: str = "serial") -> Lane:
        lane = Lane(id=lane_id, name=name, mode=LaneMode(mode))
        self.patch.add_lane(lane)
        self._notify({"type": "lane_added", "lane": lane.to_dict()})
        return lane

    def add_module(self, module_type: str, node_count: int = 1,
                   mid: str | None = None, x: float = 0.0, y: float = 0.0,
                   lane_id: str = "") -> ModuleInstance:
        mid = mid or self._gen_id(module_type)
        m = ModuleInstance(mid, module_type, lane_id, node_count)
        m.x, m.y = x, y
        self.patch.add_module(m)
        self._rebuild_per_module_lfos(m)
        if m.type == "SEQ":
            self.seq.add(m.id)
        if m.spec.is_audio:
            self.bridge.module_new(m.id, m.type, self.patch.channel_count, m.node_count, 0)
            self._push_module(m)
            self._sync_graph()   # the new module becomes a terminal -> audible
        self._notify({"type": "module_added", "module": m.to_dict()})
        return m

    def remove_module(self, mid: str) -> None:
        self.patch.remove_module(mid)        # also drops its audio + midi connections
        self.bridge.module_free(mid)
        for rid in [r.id for r in self.mod.routes.values() if r.dest_module_id == mid]:
            self.mod.remove_route(rid)
        self._remove_per_module_lfos(mid)
        self.seq.remove(mid)                 # if it was a SEQ
        for sid in self.seq.seqs:            # rebuild any SEQ that targeted it
            self._rebuild_seq_targets(sid)
        self._sync_graph()
        self._notify({"type": "module_removed", "id": mid})

    # ------------------------------------------------------------------ #
    # MIDI / control graph + sequencer.
    # ------------------------------------------------------------------ #
    def _pitch_param(self, spec) -> str | None:
        params = spec.node_params + spec.global_params
        for p in params:
            sid = p.id.split(".", 1)[-1].lower()
            if sid in ("note", "pitch", "pit") or sid.startswith("note") or sid.startswith("pitch"):
                return p.id
        for p in params:
            if "freq" in p.id.split(".", 1)[-1].lower():
                return p.id
        return None

    def _target_descriptor(self, mid: str) -> dict | None:
        m = self.patch.modules.get(mid)
        if not m:
            return None
        spec = m.spec
        pitch = self._pitch_param(spec)
        root = 48
        if pitch:
            pm = next((p for p in spec.node_params + spec.global_params if p.id == pitch), None)
            if pm and pm.rmax <= 127:
                root = int(pm.default)
        # CC defaults: first modulatable params that aren't the pitch or an enable
        ccs = [(p.id, p.label) for p in (spec.node_params + spec.global_params)
               if p.modulatable and p.id != pitch
               and not p.id.split(".", 1)[-1].lower().startswith(("enable", "nodeenable"))]
        vel = next((p.id for p in spec.node_params + spec.global_params
                    if "velocity" in p.id.split(".", 1)[-1].lower()
                    or p.id.split(".", 1)[-1].lower() == "vel"), None)
        return {"mid": mid, "generative": spec.generative_capable,
                "pitch_param": pitch, "vel_param": vel, "root": root, "cc_params": ccs}

    def _rebuild_seq_targets(self, seq_mid: str) -> None:
        if seq_mid not in self.seq.seqs:
            return
        targets = [d for t in self.patch.midi_targets(seq_mid)
                   if (d := self._target_descriptor(t))]
        self.seq.rebuild_targets(seq_mid, targets)

    def add_midi_connection(self, src: str, dst: str) -> None:
        c = self.patch.add_midi_connection(src, dst)
        if c:
            self._rebuild_seq_targets(src)
            self._notify({"type": "midi_connected", "src": src, "dst": dst})

    def remove_midi_connection(self, cid: str) -> None:
        src = next((c["src"] for c in self.patch.midi_connections if c["id"] == cid), None)
        self.patch.remove_midi_connection(cid)
        if src:
            self._rebuild_seq_targets(src)
        self._notify({"type": "midi_disconnected", "id": cid})

    def seq_set_clock(self, mid: str, **kw) -> None:
        self.seq.set_clock(mid, **kw)
        # when stopped, release the gates so note targets aren't left muted at gate 0
        if kw.get("running") is False:
            st = self.seq.seqs.get(mid)
            for tmid, lanes in (st.targets.items() if st else []):
                if any(ln.kind == "note" for ln in lanes):
                    self.bridge.set_param(tmid, "gate", -1, 1.0)
        self._notify({"type": "seq_clock", "id": mid})

    def seq_randomize(self, mid: str, target: str | None = None,
                      index: int | None = None) -> None:
        n = self.seq.randomize(mid, self.rng, target, index)
        self._notify({"type": "seq_randomized", "id": mid, "changed": n})

    def seq_set_lane(self, mid: str, target: str, index: int, **kw) -> None:
        lane = self.seq.lane(mid, target, index)
        if not lane:
            return
        for k, v in kw.items():
            if v is None or not hasattr(lane, k):
                continue
            cur = getattr(lane, k)
            setattr(lane, k, type(cur)(v) if isinstance(cur, (int, float)) and not isinstance(cur, bool)
                    else (bool(v) if isinstance(cur, bool) else v))
        self._notify({"type": "seq_lane", "id": mid, "target": target, "index": index})

    def _apply_seq_writes(self, writes: list) -> None:
        for ev in writes:
            kind, tgt = ev[0], ev[1]
            m = self.patch.modules.get(tgt)
            if not m:
                continue
            if kind == "cc":
                _, _, pid, norm = ev
                slot = m.global_slots.get(pid) or (m.node_slots[0].get(pid) if m.node_slots else None)
                if slot:
                    self.bridge.set_param(tgt, m.short_pid(pid), -1, slot.meta.to_value(norm))
            elif kind in ("note", "vel"):
                _, _, pid, val = ev
                if not pid:
                    continue
                slot = (m.node_slots[0].get(pid) if m.node_slots else None) or m.global_slots.get(pid)
                if slot:
                    val = max(slot.meta.rmin, min(slot.meta.rmax, float(val)))
                self.bridge.set_param(tgt, m.short_pid(pid), -1, float(val))
            elif kind == "gate":
                self.bridge.set_param(tgt, "gate", -1, float(ev[2]))

    def clear_patch(self) -> None:
        """Remove every module and connection, leaving lanes/LFOs/scenes intact."""
        self._teardown_modules()
        self.per_module_mod.sources.clear()
        self.per_module_mod.routes.clear()
        self.per_module_mod._instances.clear()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    # -- connection graph editing ----------------------------------------- #
    def add_connection(self, src: str, dst: str, cid: str | None = None):
        c = self.patch.add_connection(src, dst, cid)
        if c:
            self._sync_graph()
            self._notify({"type": "connection_added", "connection": c})
        else:
            ok, why = self.patch.connection_legal(src, dst)
            self._notify({"type": "connection_rejected", "src": src, "dst": dst, "reason": why})
        return c

    def remove_connection(self, cid: str) -> None:
        self.patch.remove_connection(cid)
        self._sync_graph()
        self._notify({"type": "connection_removed", "id": cid})

    def move_module(self, mid: str, x: float, y: float) -> None:
        m = self.patch.modules.get(mid)
        if m:
            m.x, m.y = x, y
            self._notify({"type": "module_moved", "id": mid, "x": x, "y": y})

    def set_node_count(self, mid: str, count: int) -> None:
        m = self.patch.modules.get(mid)
        if not m:
            return
        m.set_node_count(count)
        self.bridge.module_nodes(mid, m.node_count)
        self._push_module(m)
        # rebuild any modulation routes targeting this module (hot edit)
        for r in self.mod.routes.values():
            if r.dest_module_id == mid:
                self.mod.rebuild_route(r.id, m.node_count, m.node_positions())
        self._rebuild_per_module_lfos(m)
        self._notify({"type": "module_nodes", "id": mid, "count": m.node_count})

    def set_bypass(self, mid: str, on: bool) -> None:
        m = self.patch.modules.get(mid)
        if not m:
            return
        m.bypass = on
        self.bridge.module_bypass(mid, on)
        self._notify({"type": "module_bypass", "id": mid, "bypass": on})

    def set_wet(self, mid: str, wet: float) -> None:
        m = self.patch.modules.get(mid)
        if not m:
            return
        m.wet_dry = max(0.0, min(1.0, wet))
        self.bridge.module_wet(mid, m.wet_dry)
        self._notify({"type": "module_wet", "id": mid, "wet": m.wet_dry})

    # ------------------------------------------------------------------ #
    # Per-module LFO bank.
    # ------------------------------------------------------------------ #
    def _pmod_src_id(self, mid: str, pid: str) -> str:
        return f"{mid}_pmod_{pid}"

    def _pmod_rt_id(self, mid: str, pid: str) -> str:
        return f"{mid}_pmod_rt_{pid}"

    def _apply_lfo_shape(self, src: ModSource, shape: str) -> None:
        if shape == "triangle":
            src.type, src.shape = ModType.CLOCK, "tri"
        elif shape in ("sh", "s&h", "random", "randstep"):
            src.type = ModType.RANDOM_STEPPED
        else:
            src.type, src.shape = ModType.CLOCK, "sine"

    def _sync_per_module_lfo(self, m: ModuleInstance, pid: str) -> None:
        """Create/update/remove the ModSource/ModRoute for one per-module LFO."""
        cfg = m.per_module_lfos.get(pid)
        if cfg is None:
            return
        sid = self._pmod_src_id(m.id, pid)
        rid = self._pmod_rt_id(m.id, pid)
        active = m.per_module_lfos_enabled and cfg.get("enabled", False)
        if not active:
            self.per_module_mod.remove_route(rid)
            self.per_module_mod.remove_source(sid)
            return
        src = self.per_module_mod.sources.get(sid)
        shape = cfg.get("shape", "sine")
        rate = cfg.get("rate", 0.5)
        depth = cfg.get("depth", 0.3)
        if src is None:
            src = ModSource(
                id=sid, type=ModType.CLOCK, label=f"{m.id} {pid}",
                rate=rate, shape="sine", depth=1.0, bipolar=True, is_lfo=True)
            self.per_module_mod.add_source(src)
        self._apply_lfo_shape(src, shape)
        src.rate = float(rate)
        route = self.per_module_mod.routes.get(rid)
        if route is None:
            self.per_module_mod.add_route(
                ModRoute(id=rid, source_id=sid, dest_module_id=m.id,
                         dest_param_id=pid, node_scope=NodeScope.ALL_DECORRELATED,
                         depth=float(depth)),
                m.node_count)
        else:
            route.depth = float(depth)
            route.enable = True
            self.per_module_mod.rebuild_route(rid, m.node_count, m.node_positions())

    def _remove_per_module_lfos(self, mid: str) -> None:
        m = self.patch.modules.get(mid)
        if m is not None:
            for pid in list(m.per_module_lfos or []):
                self.per_module_mod.remove_route(self._pmod_rt_id(mid, pid))
                self.per_module_mod.remove_source(self._pmod_src_id(mid, pid))
            return
        # module already gone: tear down its sources/routes by id prefix
        pre_s, pre_r = self._pmod_src_id(mid, ""), self._pmod_rt_id(mid, "")
        for rid in [r for r in list(self.per_module_mod.routes) if r.startswith(pre_r)]:
            self.per_module_mod.remove_route(rid)
        for sid in [s for s in list(self.per_module_mod.sources) if s.startswith(pre_s)]:
            self.per_module_mod.remove_source(sid)

    def _rebuild_per_module_lfos(self, m: ModuleInstance) -> None:
        for pid in m.per_module_lfos:
            self._sync_per_module_lfo(m, pid)

    def set_per_module_lfos_enabled(self, mid: str, enabled: bool) -> None:
        m = self.patch.modules.get(mid)
        if not m:
            return
        m.per_module_lfos_enabled = bool(enabled)
        self._rebuild_per_module_lfos(m)
        self._notify({"type": "per_module_lfos_enabled", "id": mid, "enabled": m.per_module_lfos_enabled})

    def set_per_module_lfo(self, mid: str, pid: str,
                           enabled: bool | None = None,
                           shape: str | None = None,
                           rate: float | None = None,
                           depth: float | None = None) -> None:
        m = self.patch.modules.get(mid)
        if not m or pid not in m.per_module_lfos:
            return
        cfg = m.per_module_lfos[pid]
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if shape is not None:
            cfg["shape"] = shape
        if rate is not None:
            cfg["rate"] = float(rate)
        if depth is not None:
            cfg["depth"] = float(depth)
        self._sync_per_module_lfo(m, pid)
        self._notify({"type": "per_module_lfo", "id": mid, "param": pid, "cfg": dict(cfg)})

    def randomize_per_module_lfos(self, mid: str) -> None:
        m = self.patch.modules.get(mid)
        if not m:
            return
        shapes = ["sine", "triangle", "sh"]
        for pid, cfg in m.per_module_lfos.items():
            cfg["enabled"] = self.rng.random() < 0.5
            cfg["shape"] = self.rng.choice(shapes)
            cfg["rate"] = 0.03 * ((4.0 / 0.03) ** self.rng.random())
            cfg["depth"] = self.rng.uniform(0.2, 0.8)
        self._rebuild_per_module_lfos(m)
        self._notify({"type": "per_module_lfos_randomized", "id": mid})

    def set_param(self, mid: str, pid: str, node: int | None, base: float) -> None:
        slot = self.patch.find_slot(mid, pid, node)
        if not slot or slot.locked:
            return
        slot.base = base
        m = self.patch.modules[mid]
        if node is None:
            self.bridge.set_param(mid, m.short_pid(pid), -1, slot.effective)
        else:
            self.bridge.set_param(mid, m.short_pid(pid), node, slot.effective)
        self._notify({"type": "param", "id": mid, "param": pid, "node": node,
                      "base": base, "display": slot.display()})

    def set_lock(self, mid: str, pid: str, node: int | None, locked: bool) -> None:
        slot = self.patch.find_slot(mid, pid, node)
        if slot:
            slot.locked = locked
            self._notify({"type": "param_lock", "id": mid, "param": pid, "node": node, "locked": locked})

    def _gen_id(self, prefix: str) -> str:
        i = 1
        while f"{prefix.lower()}{i}" in self.patch.modules:
            i += 1
        return f"{prefix.lower()}{i}"

    # ------------------------------------------------------------------ #
    # Modulation control loop.
    # ------------------------------------------------------------------ #
    def tick_modulation(self) -> None:
        now = time.monotonic()
        dt = max(1e-4, min(0.1, now - self._last_tick))
        self._last_tick = now
        # advance an in-progress timed scene morph (current state -> target scene)
        if self._scene_morph:
            self._advance_scene_morph(dt)
        # feed analysis followers from the latest SC analysis
        patch_offsets = self.mod.tick(dt, self.bridge.analysis)
        module_offsets = self.per_module_mod.tick(dt, self.bridge.analysis)
        # Per-module LFOs override patch-matrix LFOs for conflicting targets.
        offsets = dict(patch_offsets)
        offsets.update(module_offsets)
        # group offsets per (module, param) so we can write node vectors
        touched: dict[tuple[str, str], dict[int, float]] = {}
        for (mid, pid, node), off in offsets.items():
            touched.setdefault((mid, pid), {})[node] = off
        # also clear stale offsets (params that had modulation last tick but not now)
        for (mid, pid), pernode in touched.items():
            m = self.patch.modules.get(mid)
            if not m:
                continue
            short = m.short_pid(pid)
            vals = []
            for node in range(m.node_count):
                slot = m.node_slots[node].get(pid) if node < len(m.node_slots) else None
                if slot is None:
                    # could be a global param targeted with node 0
                    slot = m.global_slots.get(pid)
                if slot is None:
                    vals.append(0.0)
                    continue
                # A locked param is frozen: randomize/morph already skip it, and it
                # must not be moved by modulation either (a lock means "hold here").
                slot.mod_norm = 0.0 if slot.locked else pernode.get(node, 0.0)
                vals.append(slot.effective)
            last = self._last_sent.get((mid, pid))
            if last is None or any(abs(a - b) > _EPS for a, b in zip(vals, last)):
                self.bridge.set_vector(mid, short, vals)
                self._last_sent[(mid, pid)] = vals
        # MIDI sequencers (clock source): read their (modulatable) tempo/density/
        # mutation slots — so LFOs can wobble them — then advance + write to targets.
        if self.seq.seqs:
            for mid, st in self.seq.seqs.items():
                m = self.patch.modules.get(mid)
                if not m:
                    continue
                gs = m.global_slots
                if "seq.tempo" in gs:
                    st.tempo = gs["seq.tempo"].effective
                if "seq.density" in gs:
                    st.dens_scale = gs["seq.density"].effective
                if "seq.mutation" in gs:
                    st.mut_scale = gs["seq.mutation"].effective
            writes = self.seq.tick(dt)
            if writes:
                self._apply_seq_writes(writes)
        # CPU watchdog awareness
        total_nodes = sum(m.node_count for m in self.patch.modules.values()
                          if spec(m.type).is_audio)
        self.cpu_warn = total_nodes > 96 or self.bridge.cpu.get("peak", 0) > 0.9

    # ------------------------------------------------------------------ #
    # Modulation editing.
    # ------------------------------------------------------------------ #
    def add_mod_source(self, sid: str, mtype: str, **kw) -> ModSource:
        src = ModSource(id=sid, type=ModType(mtype), label=kw.pop("label", sid), **kw)
        self.mod.add_source(src)
        self._notify({"type": "mod_source_added", "source": src.to_dict()})
        return src

    def add_mod_route(self, rid: str, source_id: str, dest_module: str,
                      dest_param: str, scope: str = "allDecorrelated",
                      depth: float = 0.5, **kw) -> ModRoute:
        route = ModRoute(id=rid, source_id=source_id, dest_module_id=dest_module,
                         dest_param_id=dest_param, node_scope=NodeScope(scope),
                         depth=depth, **kw)
        m = self.patch.modules.get(dest_module)
        ncount = m.node_count if m else 1
        self.mod.add_route(route, ncount)
        if m:
            self.mod.rebuild_route(rid, ncount, m.node_positions())
        self._notify({"type": "mod_route_added", "route": route.to_dict()})
        return route

    def remove_mod_route(self, rid: str) -> None:
        self.mod.remove_route(rid)
        self._notify({"type": "mod_route_removed", "id": rid})

    # ------------------------------------------------------------------ #
    # LFO bank: 8 by default, expandable to 30. Each LFO is a ModSource +
    # (optional) single ModRoute; shape sine/triangle/s&h, rate, depth, target.
    # ------------------------------------------------------------------ #
    LFO_DEFAULT_RATES = [0.05, 0.08, 0.13, 0.21, 0.34, 0.55, 0.89, 1.44]
    LFO_MAX = 30

    def lfo_ids(self) -> list[str]:
        ids = [sid for sid, s in self.mod.sources.items() if s.is_lfo]
        return sorted(ids, key=lambda x: int("".join(c for c in x if c.isdigit()) or 0))

    def ensure_lfos(self, n: int) -> None:
        n = max(1, min(self.LFO_MAX, int(n)))
        cur = self.lfo_ids()
        if len(cur) < n:
            for i in range(len(cur), n):
                rate = self.LFO_DEFAULT_RATES[i % len(self.LFO_DEFAULT_RATES)]
                self.mod.add_source(ModSource(
                    id=f"lfo{i + 1}", type=ModType.CLOCK, label=f"LFO {i + 1}",
                    rate=rate, shape="sine", depth=1.0, bipolar=True, is_lfo=True))
        elif len(cur) > n:
            for sid in cur[n:]:
                self.mod.remove_source(sid)   # also removes its route
        self._notify({"type": "lfos_changed", "count": n})

    @staticmethod
    def _apply_lfo_shape(src: ModSource, shape: str) -> None:
        if shape == "triangle":
            src.type, src.shape = ModType.CLOCK, "tri"
        elif shape in ("sh", "s&h", "random", "randstep"):
            src.type = ModType.RANDOM_STEPPED
        else:  # sine
            src.type, src.shape = ModType.CLOCK, "sine"

    def set_lfo(self, lfo_id: str, shape: str | None = None, rate: float | None = None,
                depth: float | None = None, target: str | None = None) -> None:
        src = self.mod.sources.get(lfo_id)
        if not src or not src.is_lfo:
            return
        if shape is not None:
            self._apply_lfo_shape(src, shape)
        if rate is not None:
            src.rate = float(rate)
        rt = f"{lfo_id}_rt"
        if target is not None:
            if target in ("", "none"):
                self.mod.remove_route(rt)
            else:
                module_id, param_id = target.split("|", 1)
                mod = self.patch.modules.get(module_id)
                if mod:
                    d = (float(depth) if depth is not None
                         else (self.mod.routes[rt].depth if rt in self.mod.routes else 0.5))
                    if rt in self.mod.routes:
                        r = self.mod.routes[rt]
                        r.dest_module_id, r.dest_param_id, r.depth = module_id, param_id, d
                        self.mod.rebuild_route(rt, mod.node_count, mod.node_positions())
                    else:
                        self.add_mod_route(rt, lfo_id, module_id, param_id,
                                           scope="allDecorrelated", depth=d)
        elif depth is not None and rt in self.mod.routes:
            self.mod.routes[rt].depth = float(depth)
        self._notify({"type": "lfo_set", "id": lfo_id})

    def all_mod_targets(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for mid, mod in self.patch.modules.items():
            for p in mod.spec.node_params + mod.spec.global_params:
                if p.modulatable:
                    out.append((mid, p.id))
        return out

    def randomize_lfos(self) -> None:
        targets = self.all_mod_targets()
        if not targets:
            return
        shapes = ["sine", "triangle", "sh"]
        for lid in self.lfo_ids():
            rate = 0.03 * ((4.0 / 0.03) ** self.rng.random())   # 0.03..4 Hz, log
            mid, pid = self.rng.choice(targets)
            self.set_lfo(lid, shape=self.rng.choice(shapes), rate=rate,
                         depth=self.rng.uniform(0.25, 0.9), target=f"{mid}|{pid}")
        self._notify({"type": "lfos_randomized"})

    # -- Move global-LFO bank (16 step buttons) ---------------------------- #
    def _rand_one_lfo(self, lid: str, style: str | None = None) -> None:
        """Randomize one LFO's shape/rate/depth/target. Artist-aware when a style
        is given (rate band, shapes, role-appropriate destinations)."""
        targets = self.all_mod_targets()
        if not targets:
            return
        prof = aesthetics.LFO_PROFILE.get(style) if style else None
        # Depth is deliberately STRONG for the Move global LFOs: they are user-
        # toggled and must be obviously audible when switched on (the artist
        # profiles' "modest" depths were inaudible). -200..200% range; 0.45-0.95
        # is a clear, musical swing without going fully unhinged.
        depth = self.rng.uniform(0.45, 0.95)
        if prof:
            shape = self.rng.choice(prof["shapes"])
            rate = aesthetics._logrand(self.rng, *prof["rate"])
            roles = prof.get("roles", set())
            pref = [(m, p) for (m, p, role) in self._classified_mod_targets() if role in roles]
            mid, pid = self.rng.choice(pref) if pref else self.rng.choice(targets)
        else:
            shape = self.rng.choice(["sine", "triangle", "sh"])
            rate = 0.03 * ((4.0 / 0.03) ** self.rng.random())
            mid, pid = self.rng.choice(targets)
        self.set_lfo(lid, shape=shape, rate=rate, depth=depth, target=f"{mid}|{pid}")

    def randomize_all_lfos(self, style: str | None = None) -> None:
        """Re-randomize ALL 16 global LFOs (shape/rate/depth/target), preserving
        each one's on/off state (set_lfo keeps the existing route's enable)."""
        for lid in self.lfo_ids()[:16]:
            self._rand_one_lfo(lid, style)
        self._notify({"type": "lfos_randomized"})

    def init_global_lfos(self, style: str | None = None, n: int = 16) -> None:
        """Every patch loads `n` global LFOs: randomized but OFF (disabled). The
        Move's 16 step buttons toggle them on; shift+button re-randomizes one."""
        self.ensure_lfos(n)
        for lid in self.lfo_ids()[:n]:
            self._rand_one_lfo(lid, style)
            rt = self.mod.routes.get(f"{lid}_rt")
            if rt:
                rt.enable = False
        self._notify({"type": "lfos_randomized"})

    def set_lfo_enabled(self, lid: str, on: bool) -> bool:
        rt = self.mod.routes.get(f"{lid}_rt")
        if not rt:
            return False
        rt.enable = bool(on)
        self._notify({"type": "lfo_set", "id": lid})
        return rt.enable

    def rerandomize_lfo(self, lid: str, style: str | None = None) -> None:
        """Re-randomize one LFO, preserving its on/off state (set_lfo keeps the
        existing route's enable flag)."""
        self._rand_one_lfo(lid, style)

    # ------------------------------------------------------------------ #
    # Scenes (performance snapshots: modules + params + LFOs).
    # ------------------------------------------------------------------ #
    def _snapshot_lfos(self) -> dict[str, dict[str, Any]]:
        """Current LFO bank state: shape / rate / depth / target per LFO."""
        out: dict[str, dict[str, Any]] = {}
        for sid in self.lfo_ids():
            src = self.mod.sources.get(sid)
            if not src:
                continue
            rt = self.mod.routes.get(f"{sid}_rt")
            shape = ("sh" if src.type is ModType.RANDOM_STEPPED
                     else ("triangle" if src.shape == "tri" else "sine"))
            out[sid] = {
                "shape": shape, "rate": src.rate,
                "depth": rt.depth if rt else 0.5,
                "target": f"{rt.dest_module_id}|{rt.dest_param_id}" if rt else "",
            }
        return out

    def _restore_lfos(self, lfos: dict[str, dict[str, Any]]) -> None:
        for sid, cfg in (lfos or {}).items():
            if self.mod.sources.get(sid):
                self.set_lfo(sid, shape=cfg.get("shape"), rate=cfg.get("rate"),
                             depth=cfg.get("depth"), target=cfg.get("target", ""))

    def _new_scene_id(self) -> str:
        i = 1
        while f"scene_{i}" in self.scenes.scenes:
            i += 1
        return f"scene_{i}"

    def add_scene(self) -> None:
        """Create a new empty scene slot and select it (capture stores into it)."""
        sid = self._new_scene_id()
        self.scenes.add_empty(sid)
        self.scenes.active = sid
        self._notify({"type": "scene_added", "id": sid})

    def capture_scene(self, scene_id: str | None = None, name: str = "") -> None:
        """Store a COMPLETE snapshot of the performance (modules, wiring, MIDI graph,
        LFO bank + modulation topology, sequencer, positions, all params) into the
        SELECTED scene slot. Scenes are clips: faithful, recall-anywhere captures."""
        sid = scene_id or self.scenes.active
        if not sid or sid not in self.scenes.scenes:
            sid = sid or self._new_scene_id()
        self.scenes.store(sid, name or sid, self._capture_snapshot())
        self.scenes.active = sid
        self._notify({"type": "scene_captured", "id": sid})

    def remove_scene(self, scene_id: str) -> None:
        self.scenes.remove(scene_id)   # also clears active if it was this one
        if self._scene_morph and self._scene_morph.get("dst_id") == scene_id:
            self._scene_morph = None
        self._notify({"type": "scene_removed", "id": scene_id})

    # -- full-state snapshot helpers --------------------------------------- #
    def _capture_snapshot(self) -> dict[str, Any]:
        """A complete patch snapshot (the persistence shape minus the scene bank /
        control maps, which must not be nested inside a scene)."""
        from .persistence import patch_to_dict
        d = patch_to_dict(self)
        d.pop("scenes", None)
        d.pop("control", None)
        return d

    def _full_snapshot(self) -> dict[str, Any]:
        from .snapshot import full_snapshot
        return full_snapshot(self)

    def load_scene(self, scene_id: str, morph: float = 0.0) -> None:
        """Load a scene. Empty slots just select. Filled slots: morph<=~0 = instant
        rebuild; else a timed morph from the CURRENT live state — params/wet/positions
        glide continuously while structural change (re-wiring, modules added/removed,
        whole-patch regeneration) commits at the midpoint. The UI is streamed live
        throughout so the canvas and panels update as it unfolds."""
        sc = self.scenes.scenes.get(scene_id)
        if not sc:
            return
        self.scenes.active = scene_id
        if not sc.filled:         # empty slot — just select it (nothing to apply)
            self._scene_morph = None
            self._notify({"type": "scene_loaded", "id": scene_id})
            return
        if morph <= 0.05:
            self._apply_snapshot(sc.snapshot)
            self._scene_morph = None
            self._notify({"type": "reload", "snapshot": self._full_snapshot()})
            self._notify({"type": "scene_loaded", "id": scene_id})
            return
        src = self._capture_snapshot()
        dst = sc.snapshot
        self._scene_morph = {
            "dst_id": scene_id, "src": src, "dst": dst,
            "elapsed": 0.0, "duration": float(morph), "since_frame": 0.0,
            "committed": False, "structural": self._morph_is_structural(src, dst),
            "exclusions": sc.exclusions, "policy": sc.discrete_policy,
        }
        self._notify({"type": "scene_loading", "id": scene_id, "morph": morph,
                      "structural": self._scene_morph["structural"]})

    def _morph_is_structural(self, src: dict, dst: dict) -> bool:
        """Does morphing src->dst require rebuilding structure (not just sliding
        params)? True if the module set, wiring, MIDI graph, modulation topology,
        sequencer or any module's node-count / per-module-LFO config differs."""
        smods = {m["id"]: m for m in src.get("modules", [])}
        dmods = {m["id"]: m for m in dst.get("modules", [])}
        if set(smods) != set(dmods):
            return True
        for key in ("connections", "midi_connections", "mod_sources", "mod_routes", "seq"):
            if src.get(key) != dst.get(key):
                return True
        for mid, a in smods.items():
            b = dmods[mid]
            if (a.get("node_count") != b.get("node_count")
                    or a.get("per_module_lfos_enabled") != b.get("per_module_lfos_enabled")
                    or a.get("per_module_lfos") != b.get("per_module_lfos")):
                return True
        return False

    def _apply_snapshot(self, snap: dict) -> None:
        """Rebuild the live patch (modules, wiring, modulation, sequencer, engine)
        from a full snapshot, leaving the scene bank / control maps untouched."""
        from .persistence import _source_from_dict, _route_from_dict
        from .model import FeedbackEdge
        for mid in list(self.patch.modules):
            self.bridge.module_free(mid)
        self.patch.modules.clear()
        self.patch.connections = [dict(c) for c in snap.get("connections", [])]
        self.patch.midi_connections = [dict(c) for c in snap.get("midi_connections", [])]
        self.patch.feedback_edges = {fb["id"]: FeedbackEdge(**fb)
                                     for fb in snap.get("feedback_edges", [])}
        for md in snap.get("modules", []):
            self.patch.modules[md["id"]] = ModuleInstance.from_dict(md)
        self.mod.sources.clear(); self.mod.routes.clear(); self.mod._instances.clear()
        for s in snap.get("mod_sources", []):
            self.mod.add_source(_source_from_dict(s))
        for r in snap.get("mod_routes", []):
            route = _route_from_dict(r)
            m = self.patch.modules.get(route.dest_module_id)
            self.mod.add_route(route, m.node_count if m else 1)
            if m:
                self.mod.rebuild_route(route.id, m.node_count, m.node_positions())
        self.per_module_mod.sources.clear(); self.per_module_mod.routes.clear()
        self.per_module_mod._instances.clear()
        for m in self.patch.modules.values():
            self._rebuild_per_module_lfos(m)
        self.seq.seqs.clear()
        self.seq.load_dict(snap.get("seq", {}))
        for sid in list(self.seq.seqs):
            self._rebuild_seq_targets(sid)
        self.build_graph()

    def _advance_scene_morph(self, dt: float) -> None:
        sm = self._scene_morph
        if not sm:
            return
        from .scenes import interp_params
        sm["elapsed"] += dt
        t = min(1.0, sm["elapsed"] / max(1e-4, sm["duration"]))
        # glide every comparable param/wet/position toward the destination
        interp_params(self.patch, sm["src"], sm["dst"], t,
                      exclusions=sm["exclusions"], policy=sm["policy"])
        # commit structural change once, at the midpoint
        if sm["structural"] and not sm["committed"] and t >= 0.5:
            self._morph_commit(sm["dst"])
            sm["committed"] = True
            interp_params(self.patch, sm["src"], sm["dst"], t,
                          exclusions=sm["exclusions"], policy=sm["policy"])
            self._resync_all()
            self._notify_morph_frame(t)        # push the new structure immediately
        self._resync_all()                      # audio follows every tick
        if t >= 1.0:
            self._finalize_scene_morph(sm)
            return
        sm["since_frame"] += dt
        if sm["since_frame"] >= 0.07:           # stream the canvas/panels ~14 fps
            sm["since_frame"] = 0.0
            self._notify_morph_frame(t)

    def _finalize_scene_morph(self, sm: dict) -> None:
        from .scenes import interp_params
        if sm["structural"] and not sm["committed"]:
            self._morph_commit(sm["dst"])
            sm["committed"] = True
        interp_params(self.patch, sm["src"], sm["dst"], 1.0,
                      exclusions=sm["exclusions"], policy=sm["policy"])
        self._resync_all()
        self._scene_morph = None
        self._notify({"type": "reload", "snapshot": self._full_snapshot()})
        self._notify({"type": "scene_loaded", "id": sm["dst_id"]})

    def _notify_morph_frame(self, t: float) -> None:
        """A light, catalog-free live frame: enough for the UI to redraw the canvas,
        detail panel, modulation and scene highlight mid-morph (merged into its
        existing state, which already holds the static catalog)."""
        from .snapshot import module_state, lfos_dict, mod_targets
        p = self.patch
        frame = {
            "global": {"name": p.name, "root_seed": p.root_seed,
                       "expert_override": p.expert_override},
            "connections": list(p.connections),
            "midi_connections": list(p.midi_connections),
            "seq": self.seq.to_dict(),
            "modules": [module_state(self, mid) for mid in p.modules],
            "mod_sources": [s.to_dict() for s in self.mod.sources.values()],
            "mod_routes": [r.to_dict() for r in self.mod.routes.values()],
            "lfos": lfos_dict(self),
            "mod_targets": mod_targets(self),
            "active_scene": self.scenes.active,
        }
        self._notify({"type": "morph_frame", "t": t, "frame": frame})

    def _morph_commit(self, dst: dict) -> None:
        """Reconcile structure to the destination snapshot mid-morph: add/remove
        modules, re-wire audio + MIDI, rebuild the modulation topology and sequencer.
        Surviving modules keep their (interpolated) param values so the glide
        continues seamlessly across the structural switch."""
        from .persistence import _source_from_dict, _route_from_dict
        from .model import FeedbackEdge
        target = {md["id"]: md for md in dst.get("modules", [])}
        cur, tgt = set(self.patch.modules), set(target)
        for mid in cur - tgt:                       # gone in the destination
            self.patch.remove_module(mid)
            self.bridge.module_free(mid)
            self._remove_per_module_lfos(mid)
            self.seq.remove(mid)
        for mid in tgt - cur:                       # new in the destination
            m = ModuleInstance.from_dict(target[mid])
            self.patch.modules[mid] = m
            self._rebuild_per_module_lfos(m)
            if m.type == "SEQ":
                self.seq.add(mid)
            if m.spec.is_audio:
                self.bridge.module_new(mid, m.type, self.patch.channel_count, m.node_count, 0)
                self._push_module(m)
        for mid in cur & tgt:                       # surviving: structural bits only
            m = self.patch.modules[mid]
            md = target[mid]
            nc = md.get("node_count", m.node_count)
            if nc != m.node_count:
                m.set_node_count(nc)
                if m.spec.is_audio:
                    self.bridge.module_nodes(mid, nc)
            m.per_module_lfos_enabled = md.get("per_module_lfos_enabled",
                                               m.per_module_lfos_enabled)
            for k, cfg in md.get("per_module_lfos", {}).items():
                if k in m.per_module_lfos:
                    m.per_module_lfos[k].update(cfg)
            self._remove_per_module_lfos(mid)
            self._rebuild_per_module_lfos(m)
        self.patch.connections = [dict(c) for c in dst.get("connections", [])]
        self.patch.midi_connections = [dict(c) for c in dst.get("midi_connections", [])]
        self.patch.feedback_edges = {fb["id"]: FeedbackEdge(**fb)
                                     for fb in dst.get("feedback_edges", [])}
        self.mod.sources.clear(); self.mod.routes.clear(); self.mod._instances.clear()
        for s in dst.get("mod_sources", []):
            self.mod.add_source(_source_from_dict(s))
        for r in dst.get("mod_routes", []):
            route = _route_from_dict(r)
            m = self.patch.modules.get(route.dest_module_id)
            self.mod.add_route(route, m.node_count if m else 1)
            if m:
                self.mod.rebuild_route(route.id, m.node_count, m.node_positions())
        self.seq.seqs.clear()
        self.seq.load_dict(dst.get("seq", {}))
        for sid in list(self.seq.seqs):
            self._rebuild_seq_targets(sid)
        self._sync_graph()

    def mutate(self, amount: float, expert: bool = False,
               module_filter: set[str] | None = None) -> int:
        n = self.scenes.mutate(self.patch, amount, self.rng, module_filter,
                               expert or self.patch.expert_override)
        self._resync_all()
        self._notify({"type": "mutated", "amount": amount, "changed": n})
        return n

    # -- guided (aesthetic) generation ------------------------------------- #
    @staticmethod
    def _molly_scope(m) -> str:
        slot = m.global_slots.get("molly.scope")
        idx = int(round(slot.base)) if slot else 0
        return aesthetics.MOLLY_SCOPES[idx] if 0 <= idx < len(aesthetics.MOLLY_SCOPES) else "lead"

    def _guided_value(self, slot, m, style: str, expert: bool) -> float:
        """Guided value for one slot. MOLLY voices follow their `scope` (lead/pad/
        percussion) sound type; everything else follows the artist aesthetic."""
        if m.type == "MOLLY":
            v = aesthetics.molly_value(self.rng, slot.meta, self._molly_scope(m))
            if v is not None:
                return v
        return aesthetics.guided_value(self.rng, slot.meta, style, expert)

    def _gen_value(self, meta, m, style: str | None,
                   base: float, amount: float, expert: bool) -> float:
        """A value either freely randomized or shaped (guided) for this module."""
        if style and style != "free":
            if meta.randomize_policy is RandomizePolicy.OFF:
                return base
            if meta.randomize_policy is RandomizePolicy.EXPERT and not expert:
                return base
            slot = type("S", (), {"meta": meta})()    # lightweight carrier
            return self._guided_value(slot, m, style, expert)
        return meta.randomize(self.rng, base, amount, expert)

    def _apply_style(self, mid: str | None, style: str, expert: bool) -> int:
        """Apply an artist aesthetic to every (non-locked, randomizable) slot of one
        module (mid) or the whole patch (mid None). MOLLY voices are steered by the
        artist's preferred scope (lead/pad/percussion) then drawn from that recipe."""
        mods = [self.patch.modules[mid]] if mid else list(self.patch.modules.values())
        n = 0
        for m in mods:
            # bias a MOLLY toward the artist's sound type before generating
            if m.type == "MOLLY":
                sc = aesthetics.pick_molly_scope(self.rng, style)
                ss = m.global_slots.get("molly.scope")
                if sc and ss is not None:
                    ss.base = float(aesthetics.MOLLY_SCOPES.index(sc))
            slots = list(m.global_slots.values())
            for nd in m.node_slots:
                slots.extend(nd.values())
            for slot in slots:
                if slot.locked or slot.meta.randomize_policy is RandomizePolicy.OFF:
                    continue
                if slot.meta.randomize_policy is RandomizePolicy.EXPERT and not expert:
                    continue
                slot.base = self._guided_value(slot, m, style, expert)
                n += 1
            # wet/dry of character effects — the biggest perceptual lever per artist
            wet = aesthetics.guided_wet(self.rng, style, m.type)
            if wet is not None:
                m.wet_dry = wet
        return n

    # -- comprehensive guided generation (structure / motion / rhythm) ----- #
    def _classified_mod_targets(self) -> list[tuple[str, str, str]]:
        """(module_id, param_id, aesthetic role) for every modulatable param —
        lets the guided modulation planner aim LFOs at role-appropriate knobs."""
        out: list[tuple[str, str, str]] = []
        for mid, m in self.patch.modules.items():
            for p in m.spec.node_params + m.spec.global_params:
                if p.modulatable:
                    role, _inv = aesthetics.classify(p)
                    out.append((mid, p.id, role))
        return out

    def _apply_guided_lfos(self, style: str) -> None:
        """Configure the modulation bank for the aesthetic: count, shapes, rate
        band and (role-aware) routing. This is the 'motion' half of an aesthetic."""
        plans = aesthetics.lfo_plan(self.rng, style, self._classified_mod_targets())
        if not plans:
            return
        self.ensure_lfos(min(len(plans), self.LFO_MAX))
        for lid, plan in zip(self.lfo_ids(), plans):
            self.set_lfo(lid, shape=plan["shape"], rate=plan["rate"],
                         depth=plan["depth"], target=plan["target"])

    def _apply_guided_per_module_lfos(self, mid: str, style: str) -> None:
        """Module-wide motion: enable a subset of a module's per-module LFOs with
        the aesthetic's rate/depth/shape character (used at module scope)."""
        prof = aesthetics.LFO_PROFILE.get(style)
        m = self.patch.modules.get(mid)
        if not prof or not m or not m.per_module_lfos:
            return
        m.per_module_lfos_enabled = True
        pids = list(m.per_module_lfos)
        k = max(1, int(round(len(pids) * 0.4)))
        on = set(self.rng.sample(pids, min(k, len(pids))))
        for pid, cfg in m.per_module_lfos.items():
            cfg["enabled"] = pid in on
            cfg["shape"] = self.rng.choice(prof["shapes"])
            cfg["rate"] = aesthetics._logrand(self.rng, *prof["rate"])
            cfg["depth"] = self.rng.uniform(*prof["depth"])
            self._sync_per_module_lfo(m, pid)

    # Rhythm in guided patches is provided by the GATE module (it lives in the
    # rhythmic artists' palettes), not by an auto-added SEQ — so there is no guided
    # sequencer step; GATE's own clock supplies the pulse.

    def randomize(self, amount: float, scope: str = "global",
                  mid: str | None = None, pid: str | None = None,
                  node: int | None = None, expert: bool = False,
                  style: str | None = None) -> int:
        """Constrained randomizer at three scopes (GRM: param / module / patch).
        ``amount`` 0 keeps results near current, 1 is fully random within each
        param's policy range. ``style`` None/"free" = full randomization; an artist
        id = guided generation reflecting that aesthetic. Locked params and
        modulator topology are preserved."""
        ex = expert or self.patch.expert_override
        if scope == "param":
            slot = self.patch.find_slot(mid, pid, node)
            if not slot or slot.locked:
                return 0
            m = self.patch.modules[mid]
            slot.base = self._gen_value(slot.meta, m, style, slot.base, amount, ex)
            if m.spec.is_audio:
                self.bridge.set_param(mid, m.short_pid(pid),
                                      node if node is not None else -1, slot.effective)
            self._notify({"type": "param", "id": mid, "param": pid, "node": node,
                          "base": slot.base, "display": slot.display()})
            return 1
        if style and style != "free":
            n = self._apply_style(mid if scope == "module" else None, style, ex)
            # comprehensive guidance: motion at module scope, motion + rhythm at
            # global scope (global reconfigures existing modules; it never adds new
            # structure, so the SEQ is configured only if one is already present).
            if scope == "module" and mid:
                self._apply_guided_per_module_lfos(mid, style)
            elif scope == "global":
                self._apply_guided_lfos(style)
        else:
            flt = {mid} if (scope == "module" and mid) else None
            n = self.scenes.mutate(self.patch, amount, self.rng, flt, ex)
        self._resync_all()
        self._notify({"type": "randomized", "scope": scope, "module": mid, "changed": n})
        return n

    def _resync_all(self) -> None:
        for m in self.patch.modules.values():
            self.bridge.module_bypass(m.id, m.bypass)
            self.bridge.module_wet(m.id, m.wet_dry)
            self._push_module(m)

    # ------------------------------------------------------------------ #
    # Macros / control.
    # ------------------------------------------------------------------ #
    def set_macro(self, macro_id: str, value: float) -> None:
        changed = self.control.set_macro(self.patch, macro_id, value)
        for (mid, pid, node, _base) in changed:
            slot = self.patch.find_slot(mid, pid, node)
            if slot:
                self.bridge.set_param(mid, self.patch.modules[mid].short_pid(pid),
                                      node if node is not None else -1, slot.effective)
        self._notify({"type": "macro", "id": macro_id, "value": value})

    # -- macro bank (user-defined macros, each targeting many destinations) -- #
    def _macro_candidates(self) -> list[tuple[str, str, int | None]]:
        """Every randomizable, modulatable destination across the patch. Node
        params target all voices (-1); globals target node None."""
        out: list[tuple[str, str, int | None]] = []
        for mid, m in self.patch.modules.items():
            for p in m.spec.node_params:
                if p.modulatable and p.randomize_policy is not RandomizePolicy.OFF:
                    out.append((mid, p.id, -1))
            for p in m.spec.global_params:
                if p.modulatable and p.randomize_policy is not RandomizePolicy.OFF:
                    out.append((mid, p.id, None))
        return out

    def _assign_macro_targets(self, macro: Macro, count: int) -> None:
        cands = self._macro_candidates()
        macro.targets = []
        if not cands:
            return
        k = max(0, min(int(count), len(cands)))
        for mid, pid, node in self.rng.sample(cands, k):
            macro.targets.append(MacroTarget(
                module_id=mid, param_id=pid, node=node,
                lo=round(self.rng.uniform(0.0, 0.35), 3),
                hi=round(self.rng.uniform(0.6, 1.0), 3),
                invert=self.rng.random() < 0.25))

    def _next_macro_index(self) -> int:
        i = 1
        while f"macro_{i}" in self.control.macros:
            i += 1
        return i

    def set_macro_count(self, n: int) -> None:
        """Grow/shrink the macro bank to n macros (new ones get a few random
        destinations; existing macros are kept)."""
        n = max(0, min(16, int(n)))
        while len(self.control.macros) < n:
            i = self._next_macro_index()
            mac = Macro(id=f"macro_{i}", name=f"M{i}", page="Macro", value=0.0)
            self._assign_macro_targets(mac, self.rng.randint(2, 4))
            self.control.add_macro(mac)
        while len(self.control.macros) > n:
            self.control.macros.pop(list(self.control.macros)[-1])
        self._notify({"type": "macros"})

    def set_macro_targets(self, macro_id: str, count: int) -> None:
        """Re-pick a macro's destinations to `count` of them."""
        m = self.control.macros.get(macro_id)
        if not m:
            return
        self._assign_macro_targets(m, count)
        for (mid, pid, node, _b) in m.apply(self.patch):
            self._push_param(mid, pid, node)
        self._notify({"type": "macros"})

    def randomize_macro(self, macro_id: str) -> None:
        """Per-macro randomizer: re-pick this macro's destination count, its actual
        destinations, and its slider value."""
        m = self.control.macros.get(macro_id)
        if not m:
            return
        m.value = round(self.rng.random(), 3)
        self._assign_macro_targets(m, self.rng.randint(2, 6))
        for (mid, pid, node, _b) in m.apply(self.patch):
            self._push_param(mid, pid, node)
        self._notify({"type": "macros"})

    def randomize_macros(self) -> None:
        """Block randomizer: random macro count, random destination count + actual
        destinations per macro, and random slider values."""
        self.control.macros.clear()
        for i in range(1, self.rng.randint(2, 6) + 1):
            mac = Macro(id=f"macro_{i}", name=f"M{i}", page="Macro",
                        value=round(self.rng.random(), 3))
            self._assign_macro_targets(mac, self.rng.randint(2, 6))
            for (mid, pid, node, _b) in mac.apply(self.patch):
                pass
            self.control.add_macro(mac)
        self._resync_all()
        self._notify({"type": "macros"})

    def _push_param(self, mid: str, pid: str, node: int | None) -> None:
        m = self.patch.modules.get(mid)
        slot = self.patch.find_slot(mid, pid, node)
        if m and slot:
            self.bridge.set_param(mid, m.short_pid(pid),
                                  node if node is not None else -1, slot.effective)

    def set_lfos_enabled(self, enabled: bool) -> None:
        """Global on/off for the LFO bank — gate its routes and reset any lingering
        offset so params return to base when turned off."""
        self.lfos_enabled = bool(enabled)
        for r in self.mod.routes.values():
            src = self.mod.sources.get(r.source_id)
            if src and getattr(src, "is_lfo", False):
                r.enable = self.lfos_enabled
        if not self.lfos_enabled:
            for m in self.patch.modules.values():
                for slot in m.global_slots.values():
                    slot.mod_norm = 0.0
                for nd in m.node_slots:
                    for slot in nd.values():
                        slot.mod_norm = 0.0
            self._resync_all()
        self._notify({"type": "lfos_enabled", "enabled": self.lfos_enabled})

    def handle_controller(self, transport: str, selector: str, channel: int, value: float) -> None:
        changed = self.control.handle_incoming(transport, selector, channel, value, self.patch)
        for item in changed:
            if len(item) == 4:
                mid, pid, node, _ = item
                slot = self.patch.find_slot(mid, pid, node)
                if slot:
                    self.bridge.set_param(mid, self.patch.modules[mid].short_pid(pid),
                                          node if node is not None else -1, slot.effective)
        self._notify({"type": "controller", "selector": selector})

    # ------------------------------------------------------------------ #
    # Safety.
    # ------------------------------------------------------------------ #
    def panic(self) -> None:
        """Global fade, free feedback synths, limiter reset, node cap, loudness
        guard (section 10 panic policy)."""
        self._panic = True
        for fb in self.patch.feedback_edges.values():
            fb.armed = False
            self.bridge.feedback(fb.source_module, fb.dest_module, False,
                                 fb.gain_limit, fb.delay_samples_min)
        self.bridge.panic()
        self._notify({"type": "panic"})

    def reseed(self, seed: int) -> None:
        self.patch.root_seed = seed
        self.rng = random.Random(seed)
        self.mod.set_root_seed(seed, self.patch.node_counts())
        self.per_module_mod.set_root_seed(seed, self.patch.node_counts())
        self.bridge.reseed(seed)
        self._notify({"type": "reseed", "seed": seed})

    def reset_transport(self) -> None:
        self.mod.reset_phases()
        self._notify({"type": "transport_reset"})

    def reconnect_engine(self) -> None:
        """Re-handshake with the engine and rebuild the DSP graph on it. Recovers
        the common case where the controller restarted after the engine booted
        (so it missed the one-shot /atelier/ready) or the graph drifted."""
        self.bridge.ping()
        self.build_graph()
        self._notify({"type": "engine_reconnect", "connected": self.bridge.connected})

    # ------------------------------------------------------------------ #
    # Default patch (a usable starting instrument).
    # ------------------------------------------------------------------ #
    def init_default(self) -> None:
        """Build the default as a free-form graph: a serial chain
        DX7 -> PITCH -> TIME -> COMB -> GAIN -> SDLY -> VERB."""
        chain = ["DX7", "PITCH", "TIME", "COMB", "GAIN", "SDLY", "VERB"]
        prev = None
        for i, t in enumerate(chain):
            mod = self.add_module(t, node_count=1, x=40 + i * 210, y=60)
            if prev:
                self.patch.add_connection(prev, mod.id)
            prev = mod.id

        def m(t):
            return next(mm for mm in self.patch.modules.values() if mm.type == t)

        # DX7: a wide 3-voice stack on one factory preset — notes spread across an
        # octave triad and panned across the stereo field, so the source is rich
        # and wide rather than a single centred tone. A polyadic source
        # decorrelates per-voice pan motion below.
        gen = m("DX7")
        gen.set_node_count(3)
        spread = [-0.6, 0.0, 0.6]             # symmetric L / centre / R
        notes = [36.0, 48.0, 55.0]            # low octave + fifth, held as a drone
        gen.global_slots["dx7.preset"].base = 0.0
        for i, nd in enumerate(gen.node_slots):
            nd["dx7.note"].base = notes[i]
            nd["dx7.pan"].base = spread[i]
            nd["dx7.amp"].base = 0.4

        m("PITCH").wet_dry = 0.3
        m("TIME").wet_dry = 0.3
        m("COMB").node_slots[0]["comb.freqOrDelay"].base = 110.0
        m("COMB").wet_dry = 0.4
        m("GAIN").wet_dry = 1.0
        # stereo delay + spacious reverb at the tail (mostly wet for space/width)
        m("SDLY").wet_dry = 0.32
        m("VERB").wet_dry = 0.38

        # Polyadic modulation: one slow random source whose derived per-voice
        # instances both wobble pitch and drift the pan independently, widening
        # and animating the stereo image.
        self.add_mod_source("agitation", "random_continuous", label="Agitation",
                            rate=0.4, smooth=0.4, depth=1.0)
        # Decorrelated per-voice pan drift for a living stereo image. Depth kept
        # modest so each voice stays in its own hemisphere (base ±0.6 ± 0.4): the
        # image animates but never collapses to one side (was 0.45, which could
        # align all voices hard to one channel and momentarily mute the other).
        self.add_mod_route("rt_pan", "agitation", gen.id, "dx7.pan",
                           scope="allDecorrelated", depth=0.2)
        # LFO bank: 8 assignable LFOs by default (expandable to 30).
        self.ensure_lfos(8)

        self._auto_layout()          # distribute evenly on the canvas (not one row)
        self._sync_graph()
        self.reseed(self.patch.root_seed)

    # ------------------------------------------------------------------ #
    # Random patch: auto-wire a legal graph (<=8 modules, each type <=2)
    # and randomize all params within musical ranges.
    # ------------------------------------------------------------------ #
    def _teardown_modules(self) -> None:
        # Free EVERY engine module first (orphans included): ids are reused across
        # patches, so a single dropped /module/free would leave a synth still
        # sounding but absent from the model/grid. Then free the known ones too.
        self.bridge.send("/atelier/free_modules")
        for mid in list(self.patch.modules):
            self.bridge.module_free(mid)
        self.patch.modules.clear()
        self.patch.connections.clear()
        for rid in [r.id for r in self.mod.routes.values() if not r.id.startswith("lfo")]:
            self.mod.remove_route(rid)

    def _wire_random(self, ids: list[str]) -> None:
        """Wire a legal DAG: each input-capable module pulls from 1-2 upstreams."""
        outputs = [mid for mid in ids if self.patch.can_output(mid)]
        for mid in ids:
            if not self.patch.can_input(mid):
                continue
            cands = [u for u in outputs if u != mid and self.patch.connection_legal(u, mid)[0]]
            if not cands:
                continue
            k = 1 if self.rng.random() < 0.7 else 2
            for u in self.rng.sample(cands, min(k, len(cands))):
                self.patch.add_connection(u, mid)

    def _wire_guided(self, ids: list[str]) -> None:
        """Deliberate signal flow: the pure generators (heads) sum once into an
        ordered serial effect chain, whose end is the single terminal -> master.
        This reads like a real processing chain (tone -> dirt -> modulation ->
        pitch/texture -> time -> space) instead of random parallel summing, which
        is the main cause of cacophony and of an aesthetic never coming through."""
        heads = [mid for mid in ids if not self.patch.can_input(mid)]   # pure sources
        chain = [mid for mid in ids if self.patch.can_input(mid)]       # effects (+RINGS/FBANK)
        if not heads:
            # No pure generator picked (e.g. only RINGS/FBANK): promote one
            # generative chain module to be the self-oscillating head.
            gen = next((mid for mid in chain
                        if self.patch.modules[mid].spec.generative_capable), None)
            if gen is not None:
                heads = [gen]
                chain = [m for m in chain if m != gen]
            elif chain:
                heads, chain = [chain[0]], chain[1:]
        # order the effect chain by canonical signal flow (a coherent foundation);
        # variety between patches comes from the module set + params. Free-form
        # chaos is the job of rewire (_wire_random), not of the guided foundation.
        chain.sort(key=lambda mid: (aesthetics.chain_rank(self.patch.modules[mid].type),
                                    self.rng.random()))
        if chain:
            for h in heads:
                self.patch.add_connection(h, chain[0])
            for a, b in zip(chain, chain[1:]):
                self.patch.add_connection(a, b)
        # else: the few heads are the terminals and sum straight to master

    def random_patch(self, amount: float | None = None, style: str | None = None) -> None:
        """Patch scope: rewire a fresh legal graph AND generate all params (Free
        randomization, or Guided in the chosen artist's aesthetic)."""
        from .catalog import CATALOG
        rng = self.rng
        guided = bool(style and style != "free")
        self._teardown_modules()
        audio_types = [t for t, s in CATALOG.items() if s.is_audio]   # excludes VIZ
        chosen: list[str] = []

        # Guided: assemble a module set from the artist's palette (their defining
        # generators + effects). Free: a generic legal mix.
        if guided:
            chosen = aesthetics.pick_modules(rng, style) or []
        if not chosen:
            n = rng.randint(4, 10)
            used: dict[str, int] = {}

            def take(t: str) -> None:
                if used.get(t, 0) < 2 and len(chosen) < 16:    # never >16 total, none >2x
                    chosen.append(t)
                    used[t] = used.get(t, 0) + 1

            take("DX7")            # DX7 self-sounds; COMB/PLAY need excitation/buffer
            guard = 0
            while len(chosen) < n and guard < 200:
                guard += 1
                take(rng.choice(audio_types))

        # Guided patches keep voice counts low (more nodes = more simultaneous
        # detuned voices = denser/cacophonic); Free stays adventurous.
        node_opts = [1, 1, 1, 2] if guided else [1, 1, 2, 3]
        ids = [self.add_module(t, node_count=rng.choice(node_opts)).id for t in chosen]
        self._wire_guided(ids) if guided else self._wire_random(ids)
        if guided:
            self._apply_style(None, style, self.patch.expert_override)
            self._apply_guided_lfos(style)      # motion (rhythm comes from GATE modules)
        else:
            amt = amount if amount is not None else rng.uniform(0.6, 0.9)
            self.scenes.mutate(self.patch, amt, rng, expert=self.patch.expert_override)
        self._auto_layout()
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def rewire_patch(self, style: str | None = None) -> None:
        """Regenerate ONLY the audio connection graph. Everything else — params,
        wet/dry, node counts, MIDI links, modulation — is left exactly as is; rewire
        changes wiring, never sound-per-module. (`style` is accepted but ignored.)"""
        ids = list(self.patch.modules.keys())
        if not ids:
            return
        self.patch.connections.clear()       # keep modules + params; drop only wiring
        # Rewire is FREE-FORM: guided generation builds the solid foundation at the
        # patch level; rewire then throws it into anything-goes territory (random
        # DAG, multiple parallel paths) — a big, audible change every time.
        self._wire_random(ids)
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def chain_patch(self, types: list[str], style: str | None = None) -> None:
        """CHAINS (Move): build a guided patch from an EXPLICIT module list (the
        user picked the modules + instance counts). Like random_chain but wired
        with the deliberate _wire_guided (a coherent foundation, since the user
        chose the parts) and capped to the 32-pad grid, up to 8 of a type."""
        from .catalog import CATALOG
        used: dict[str, int] = {}
        chosen: list[str] = []
        for t in types:
            if (t in CATALOG and CATALOG[t].is_audio
                    and used.get(t, 0) < 8 and len(chosen) < 32):
                chosen.append(t)
                used[t] = used.get(t, 0) + 1
        if not chosen:
            return
        self._teardown_modules()
        ids = [self.add_module(t, node_count=1).id for t in chosen]
        self._wire_guided(ids)               # coherent foundation
        if style and style != "free":
            self._apply_style(None, style, self.patch.expert_override)
            self._apply_guided_lfos(style)
        self._auto_layout()
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def random_chain(self, types: list[str], style: str | None = None) -> None:
        """Chain scope: build a new random legal graph from the user-selected
        modules. Free = new modules keep defaults; Guided = each module's params are
        generated module-aware in the chosen artist's aesthetic (no global-structure
        decision — purely module/section level). <=24 modules, none used >5x."""
        from .catalog import CATALOG
        used: dict[str, int] = {}
        chosen: list[str] = []
        for t in types:
            if (t in CATALOG and CATALOG[t].is_audio
                    and used.get(t, 0) < 5 and len(chosen) < 24):
                chosen.append(t)
                used[t] = used.get(t, 0) + 1
        if not chosen:
            return
        self._teardown_modules()
        ids = [self.add_module(t, node_count=1).id for t in chosen]
        self._wire_random(ids)               # rewire
        if style and style != "free":        # guided: per-module aesthetic params
            self._apply_style(None, style, self.patch.expert_override)
            self._apply_guided_lfos(style)   # motion (rhythm comes from GATE modules)
        self._auto_layout()
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def _auto_layout(self) -> None:
        """Lay modules out as a wrapping serpentine grid so a patch fills the canvas
        evenly instead of stretching into one long off-screen row. Signal order is
        preserved left-to-right, snaking down each row so wiring stays readable."""
        import math
        order = self.patch.topo_order()
        n = len(order)
        if not n:
            return
        cols = max(2, min(6, round(math.sqrt(n * 1.6))))   # squarish, capped width
        gx, gy, x0, y0 = 220, 150, 40, 40
        for i, mid in enumerate(order):
            r, c = divmod(i, cols)
            c = c if (r % 2 == 0) else (cols - 1 - c)       # serpentine: keep flow continuous
            self.patch.modules[mid].x = x0 + c * gx
            self.patch.modules[mid].y = y0 + r * gy
