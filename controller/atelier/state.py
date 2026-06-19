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

from .catalog import spec
from .control import ControlLayer, Macro, MacroTarget
from .model import Lane, LaneMode, ModuleInstance, Patch
from .modulation import ModEngine, ModRoute, ModSource, ModType, NodeScope
from .osc_bridge import OSCBridge
from .scenes import SceneEngine

_EPS = 1e-4


class StateManager:
    def __init__(self, bridge: OSCBridge | None = None):
        self.patch = Patch()
        self.mod = ModEngine(root_seed=self.patch.root_seed)
        self.scenes = SceneEngine()
        self.control = ControlLayer()
        self.bridge = bridge or OSCBridge()
        self.rng = random.Random(self.patch.root_seed)
        self._last_sent: dict[tuple[str, str], list[float]] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._panic = False
        self.cpu_warn = False
        self._morph_state: dict[str, Any] | None = None
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
        if m.spec.is_audio:
            self.bridge.module_new(m.id, m.type, self.patch.channel_count, m.node_count, 0)
            self._push_module(m)
            self._sync_graph()   # the new module becomes a terminal -> audible
        self._notify({"type": "module_added", "module": m.to_dict()})
        return m

    def remove_module(self, mid: str) -> None:
        self.patch.remove_module(mid)        # also drops its connections
        self.bridge.module_free(mid)
        for rid in [r.id for r in self.mod.routes.values() if r.dest_module_id == mid]:
            self.mod.remove_route(rid)
        self._sync_graph()
        self._notify({"type": "module_removed", "id": mid})

    def clear_patch(self) -> None:
        """Remove every module and connection, leaving lanes/LFOs/scenes intact."""
        self._teardown_modules()
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
        # feed analysis followers from the latest SC analysis
        offsets = self.mod.tick(dt, self.bridge.analysis)
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
                slot.mod_norm = pernode.get(node, 0.0)
                vals.append(slot.effective)
            last = self._last_sent.get((mid, pid))
            if last is None or any(abs(a - b) > _EPS for a, b in zip(vals, last)):
                self.bridge.set_vector(mid, short, vals)
                self._last_sent[(mid, pid)] = vals
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

    # ------------------------------------------------------------------ #
    # Scenes.
    # ------------------------------------------------------------------ #
    def capture_scene(self, scene_id: str, name: str = "") -> None:
        self.scenes.capture(self.patch, scene_id, name)
        self._notify({"type": "scene_captured", "id": scene_id})

    def recall_scene(self, scene_id: str) -> None:
        self.scenes.recall(self.patch, scene_id)
        self._resync_all()
        self._notify({"type": "scene_recalled", "id": scene_id})

    def morph_scenes(self, scene_a: str, scene_b: str, t: float) -> None:
        self.scenes.morph(self.patch, scene_a, scene_b, t)
        self._resync_all()
        self._notify({"type": "scene_morph", "a": scene_a, "b": scene_b, "t": t})

    def mutate(self, amount: float, expert: bool = False,
               module_filter: set[str] | None = None) -> int:
        n = self.scenes.mutate(self.patch, amount, self.rng, module_filter,
                               expert or self.patch.expert_override)
        self._resync_all()
        self._notify({"type": "mutated", "amount": amount, "changed": n})
        return n

    def randomize(self, amount: float, scope: str = "global",
                  mid: str | None = None, pid: str | None = None,
                  node: int | None = None, expert: bool = False) -> int:
        """Constrained randomizer at three scopes (GRM: param / module / patch).
        ``amount`` 0 keeps results near current, 1 is fully random within each
        param's policy range. Locked params and modulator topology are preserved."""
        ex = expert or self.patch.expert_override
        if scope == "param":
            slot = self.patch.find_slot(mid, pid, node)
            if not slot or slot.locked:
                return 0
            slot.base = slot.meta.randomize(self.rng, slot.base, amount, ex)
            m = self.patch.modules[mid]
            if m.spec.is_audio:
                self.bridge.set_param(mid, m.short_pid(pid),
                                      node if node is not None else -1, slot.effective)
            self._notify({"type": "param", "id": mid, "param": pid, "node": node,
                          "base": slot.base, "display": slot.display()})
            return 1
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
        GEN -> BAND -> PITCH -> TIME -> COMB -> GAIN -> SDLY -> VERB laid out
        left-to-right on the canvas, plus a standalone PLAY source and VIZ."""
        chain = ["GEN", "BAND", "PITCH", "TIME", "COMB", "GAIN", "SDLY", "VERB"]
        prev = None
        for i, t in enumerate(chain):
            mod = self.add_module(t, node_count=1, x=40 + i * 210, y=60)
            if prev:
                self.patch.add_connection(prev, mod.id)
            prev = mod.id
        self.add_module("PLAY", node_count=1, x=40, y=260)
        self.add_module("VIZ", node_count=1, x=40 + 8 * 210, y=260)

        def m(t):
            return next(mm for mm in self.patch.modules.values() if mm.type == t)

        # GEN: a wide DX7-FM unison — several voices spread across the stereo
        # field with small detunes, so the source is rich and wide rather than a
        # single centred tone. Pans are distributed (clever panning), and a
        # polyadic source decorrelates per-voice motion below.
        gen = m("GEN")
        gen.set_node_count(3)
        spread = [-0.7, 0.05, 0.75]
        detune = [-0.09, 0.0, 0.11]
        for i, nd in enumerate(gen.node_slots):
            nd["gen.waveform"].base = 7        # DX7-style FM voice
            nd["gen.freq"].base = 110.0
            nd["gen.fmIndex"].base = 3.0
            nd["gen.fmRatioB"].base = 2.0
            nd["gen.detune"].base = detune[i]
            nd["gen.pan"].base = spread[i]
            nd["gen.amp"].base = 0.3

        b = m("BAND")
        b.node_slots[0]["band.centerHz"].base = 220.0
        b.node_slots[0]["band.bandwidth"].base = 600.0
        b.wet_dry = 0.5
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
        self.add_mod_route("rt_pan", "agitation", gen.id, "gen.pan",
                           scope="allDecorrelated", depth=0.45)
        self.add_mod_route("rt_detune", "agitation", gen.id, "gen.detune",
                           scope="allDecorrelated", depth=0.3)
        # LFO bank: 8 assignable LFOs by default (expandable to 30).
        self.ensure_lfos(8)

        # a performance macro: master presence via GAIN gain across all nodes
        mac = Macro(id="m_presence", name="Presence", page="Spatial")
        gain = m("GAIN")
        mac.targets.append(MacroTarget(module_id=gain.id, param_id="gain.gain", node=-1))
        self.control.add_macro(mac)
        self._sync_graph()
        self.reseed(self.patch.root_seed)

    # ------------------------------------------------------------------ #
    # Random patch: auto-wire a legal graph (<=8 modules, each type <=2)
    # and randomize all params within musical ranges.
    # ------------------------------------------------------------------ #
    def _teardown_modules(self) -> None:
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

    def random_patch(self, amount: float | None = None) -> None:
        """Patch scope: rewire a fresh legal graph AND randomize all params."""
        from .catalog import CATALOG
        rng = self.rng
        self._teardown_modules()
        audio_types = [t for t, s in CATALOG.items() if s.is_audio]   # excludes VIZ
        n = rng.randint(4, 8)
        used: dict[str, int] = {}
        chosen: list[str] = []

        def take(t: str) -> None:
            if used.get(t, 0) < 2 and len(chosen) < 8:    # never >8 total, none >2x
                chosen.append(t)
                used[t] = used.get(t, 0) + 1

        take("GEN")            # GEN self-sounds; COMB/PLAY need excitation/buffer
        guard = 0
        while len(chosen) < n and guard < 200:
            guard += 1
            take(rng.choice(audio_types))

        ids = [self.add_module(t, node_count=rng.choice([1, 1, 2, 3])).id for t in chosen]
        self._wire_random(ids)
        amt = amount if amount is not None else rng.uniform(0.6, 0.9)
        self.scenes.mutate(self.patch, amt, rng, expert=self.patch.expert_override)
        self._auto_layout()
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def random_chain(self, types: list[str]) -> None:
        """Chain scope: build a new random legal graph from the user-selected
        modules. Params are NOT randomized (new modules keep their defaults).
        Enforces the contract: <= 8 modules total, none used more than twice."""
        from .catalog import CATALOG
        used: dict[str, int] = {}
        chosen: list[str] = []
        for t in types:
            if (t in CATALOG and CATALOG[t].is_audio
                    and used.get(t, 0) < 2 and len(chosen) < 8):
                chosen.append(t)
                used[t] = used.get(t, 0) + 1
        if not chosen:
            return
        self._teardown_modules()
        ids = [self.add_module(t, node_count=1).id for t in chosen]
        self._wire_random(ids)               # rewire only — params left at defaults
        self._auto_layout()
        self._resync_all()
        self._sync_graph()
        self._notify({"type": "patch_replaced"})

    def _auto_layout(self) -> None:
        order = self.patch.topo_order()
        depth: dict[str, int] = {}
        for mid in order:
            ins = self.patch.incoming(mid)
            depth[mid] = 0 if not ins else 1 + max((depth.get(i, 0) for i in ins), default=0)
        rows: dict[int, int] = {}
        for mid in order:
            d = depth[mid]
            r = rows.get(d, 0)
            rows[d] = r + 1
            self.patch.modules[mid].x = 40 + d * 210
            self.patch.modules[mid].y = 40 + r * 150
