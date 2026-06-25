"""Headless on-device Wildrider controller daemon (P1, Move takeover).

This is the controller stripped of FastAPI / Postgres / web / login: just the
authoritative :class:`StateManager`, the 60 Hz modulation control loop, and the
OSC bridge to a *local* sclang engine (127.0.0.1:57120). It adds two things the
Schwung overtake ``ui.js`` needs (the "controller renders, ui.js blits" model):

  * a thin **control channel** — a small OSC server (default :57150) the ui.js
    pokes to set macros / params / launch scenes / panic;
  * a **snapshot file** — the full machine-describable patch state written to
    ``share/snapshot.json`` a few times a second, which the ui.js reads to draw
    the Move screen. (A real framebuffer renderer lands in P3 once the screen
    layout is specified; here we keep the plumbing + a placeholder.)

Run on the device with the engine already up:
    PYTHONPATH=.../controller python3 -m atelier.headless

Degrades gracefully: if the engine is unreachable, OSC sends are no-ops and the
state model / control loop still run (same contract as the desktop controller).
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

from . import aesthetics
from .osc_bridge import OSCBridge
from .snapshot import full_snapshot
from .state import StateManager

# Guided "artist-inspired" styles a new patch is generated in (Track 1).
ARTISTS = list(aesthetics.MODULE_PALETTE.keys()) or ["vidna_obmana"]


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


SC_HOST = _env("SC_HOST", "127.0.0.1")
SC_PORT = int(_env("SC_PORT", "57120"))            # sclang langPort (engine.scd)
TELEMETRY_PORT = int(_env("CONTROLLER_PORT", "57140"))  # engine -> controller
CONTROL_PORT = int(_env("WR_CONTROL_PORT", "57150"))    # ui.js -> controller
CONTROL_RATE = float(_env("ATELIER_CONTROL_RATE", "60"))
SHARE = Path(_env("WR_SHARE", "/data/UserData/wildrider/share"))
SNAP_FILE = SHARE / "snapshot.json"
STATUS_FILE = SHARE / "status.json"
# ui.js -> controller: the JS sandbox has file IO but no UDP socket, so the
# overtake ui.js writes commands/macro values here and the controller polls it.
CONTROL_FILE = SHARE / "control.json"
SNAP_HZ = float(_env("WR_SNAPSHOT_HZ", "8"))           # status.json rate (cheap)
CONTROL_HZ = float(_env("WR_CONTROL_HZ", "60"))
# The full 180KB snapshot.json is unused by the ui.js today and writing it often
# caused JACK XRuns; write it sparsely (or disable) to protect the audio thread.
WRITE_FULL_SNAPSHOT = _env("WR_FULL_SNAPSHOT", "1") != "0"
FULL_SNAPSHOT_EVERY_S = float(_env("WR_FULL_SNAPSHOT_EVERY_S", "3"))


class HeadlessController:
    def __init__(self) -> None:
        self.bridge = OSCBridge(sc_host=SC_HOST, sc_port=SC_PORT,
                                listen_port=TELEMETRY_PORT)
        self.state = StateManager(bridge=self.bridge)
        self._stop = threading.Event()
        self._built = threading.Event()
        self._ctrl_server = None
        self._pad_map: dict = {}        # module id -> stable pad cell (0-31)
        self._style = ARTISTS[0]        # current patch's artist (for LFO re-rand)
        self._swap_lock = threading.Lock()   # serialize click-free patch swaps

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        SHARE.mkdir(parents=True, exist_ok=True)
        self._write_modules_list()
        self.bridge.start()
        # Start with an EMPTY canvas — no default patch. The user builds via Track1
        # (new patch), CHAINS, or by pressing empty pads (grow). But the 16 global
        # LFOs (step buttons) + 8 macros must ALWAYS exist for consistent control,
        # so create the (empty/untargeted, off) banks now; they get targeted as
        # modules appear (retarget_lfos on grow, full re-roll on new patch).
        self._style = self.state.rng.choice(ARTISTS)
        self._gen_macros()
        self.state.init_global_lfos(self._style)
        # Build the DSP graph only once the engine signals readiness. The engine
        # sets ~masterBus etc. at the *end* of its async boot block and then
        # sends /atelier/ready; building before that races (nil bus -> errors).
        # We may have started after the engine's one-shot ready, so we also ping
        # (engine replies /atelier/ready) until we're connected. Idempotent.
        self.bridge.on("ready", self._on_ready)
        threading.Thread(target=self._handshake_loop, daemon=True).start()
        self._start_control_channel()
        threading.Thread(target=self._snapshot_loop, daemon=True).start()
        threading.Thread(target=self._control_file_loop, daemon=True).start()
        print(f"[wildrider] headless controller up — engine {SC_HOST}:{SC_PORT}, "
              f"control :{CONTROL_PORT}, snapshot {SNAP_FILE}", flush=True)

    def run(self) -> None:
        """Block in the modulation loop until signalled."""
        period = 1.0 / CONTROL_RATE
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._safe(self.state.tick_modulation)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _on_ready(self, *_a) -> None:
        # (Re)build the graph on every readiness signal; idempotent server-side.
        self._safe(self.state.build_graph)
        self._built.set()

    def _handshake_loop(self) -> None:
        """Ping the engine until it answers /atelier/ready and we build the
        graph. (Generic telemetry like CPU also marks the bridge 'connected',
        so we gate on an actual ready/build, not mere connectivity.)"""
        while not self._stop.is_set() and not self._built.is_set():
            self._safe(self.bridge.ping)
            time.sleep(1.0)

    def stop(self, *_a) -> None:
        self._stop.set()
        try:
            self.bridge.stop()
        except Exception:
            pass
        if self._ctrl_server is not None:
            try:
                self._ctrl_server.shutdown()
            except Exception:
                pass

    # -- control channel (ui.js -> controller) ----------------------------- #
    def _start_control_channel(self) -> None:
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
        except Exception:
            print("[wildrider] pythonosc missing — control channel disabled", flush=True)
            return
        disp = Dispatcher()
        disp.map("/wr/macro", self._h_macro)         # knob i (0-7) -> macro value
        disp.map("/wr/newpatch", self._h_newpatch)   # track 1: new guided patch
        disp.map("/wr/rewire", self._h_rewire)       # track 2: rewire connections
        disp.map("/wr/pad/toggle", self._h_pad_toggle)  # short-press: module on/off
        disp.map("/wr/pad/delete", self._h_pad_delete)  # track3 + pad: delete module
        disp.map("/wr/param", self._h_param)
        disp.map("/wr/scene", self._h_scene)
        disp.map("/wr/panic", self._h_panic)
        disp.map("/wr/snapshot", lambda *_: self._write_full_snapshot())
        try:
            self._ctrl_server = ThreadingOSCUDPServer(("127.0.0.1", CONTROL_PORT), disp)
            threading.Thread(target=self._ctrl_server.serve_forever, daemon=True).start()
        except Exception as e:
            print(f"[wildrider] control channel bind failed: {e}", flush=True)

    def _h_macro(self, _addr, index=0, value=0.0) -> None:
        # The 8 encoders map to macro_1..macro_8 by index.
        self._safe(lambda: self.state.set_macro(f"macro_{int(index) + 1}", float(value)))

    def _h_newpatch(self, _addr, *_a) -> None:
        self._safe(self.new_patch)

    def _h_rewire(self, _addr, *_a) -> None:
        self._safe(self.state.rewire_patch)

    def _h_pad_toggle(self, _addr, pad=-1) -> None:
        self._safe(lambda: self.toggle_pad(int(pad)))

    def _h_pad_delete(self, _addr, pad=-1) -> None:
        self._safe(lambda: self.delete_pad(int(pad)))

    def _h_param(self, _addr, mid="", pid="", node=-1, value=0.0) -> None:
        n = None if int(node) < 0 else int(node)
        self._safe(lambda: self.state.set_param(str(mid), str(pid), n, float(value)))

    def _h_scene(self, _addr, scene_id="", morph=0.0) -> None:
        self._safe(lambda: self.state.load_scene(str(scene_id), float(morph)))

    def _h_panic(self, _addr, *_a) -> None:
        fn = getattr(self.state, "panic", None) or self.bridge.panic
        self._safe(fn)

    # -- patch / grid / macros (the Move canvas model) --------------------- #
    def new_patch(self) -> None:
        """Track 1: generate a fresh guided artist-inspired patch + 8 macros +
        16 global LFOs (randomized but off)."""
        def build():
            self._style = self.state.rng.choice(ARTISTS)
            self.state.random_patch(style=self._style)
            self.state.init_global_lfos(self._style)
            self._gen_macros()
            self._pad_map = {}          # reflow the grid for the new module set
        self._swap_patch(build)

    def _swap_patch(self, build) -> None:
        """Click-free patch swap. Old code rebuilt at full gain, then unity-measured
        the patch AUDIBLY before jumping the makeup — so the teardown burst rang out
        and the new patch came in quiet then lurched up. Instead: fade the master
        OUT (the \\gain control is lagged in the engine), rebuild while silent,
        measure the PRE-gain level (the engine's analysis taps ~masterBus, so it
        reads the patch regardless of the muted output), then fade back IN straight
        at the normalized makeup. No teardown artifact, no quiet-then-loud ramp.
        Runs in a worker so the control loop never blocks; serialized by a lock."""
        def worker():
            with self._swap_lock:
                try:
                    self.bridge.send("/atelier/mastergain", 0.0)   # fade out (lagged)
                    time.sleep(0.07)                                # let the ramp reach ~0
                    build()                                         # teardown + rebuild (silent)
                    time.sleep(0.45)                                # envelopes open + meters settle
                    peak = 0.0
                    for _ in range(5):
                        m = self.bridge.meters or []
                        peak = max([peak] + [float(x) for x in m])
                        time.sleep(0.06)
                    # Target pre-gain peak ~0.3 so the post-shadow (~2x) signal lands
                    # ~0.6, not clipping. Min 0.4 attenuates loud patches below unity;
                    # max 6 lifts quiet ambient ones.
                    gain = 6.0 if peak < 1e-3 else max(0.4, min(6.0, 0.3 / peak))
                    self.bridge.send("/atelier/mastergain", round(gain, 2))  # fade in at level
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _write_modules_list(self) -> None:
        """Static list of selectable module types for the CHAINS picker ui.js."""
        try:
            from .catalog import CATALOG
            types = [t for t in CATALOG if CATALOG[t].is_audio]
            (SHARE / "modules.json").write_text(json.dumps(types))
        except Exception:
            pass

    def chains_patch(self, selection) -> None:
        """CHAINS generate: selection = [[type, count], ...] -> guided patch built
        from exactly those modules/instances. + macros + 16 LFOs + normalize."""
        types = []
        for item in selection or []:
            try:
                t, n = str(item[0]), int(item[1])
            except Exception:
                continue
            types += [t] * max(0, min(8, n))
        if not types:
            return
        def build():
            self._style = self.state.rng.choice(ARTISTS)
            self.state.chain_patch(types, self._style)
            self.state.init_global_lfos(self._style)
            self._gen_macros()
            self._pad_map = {}
        self._swap_patch(build)

    REWIRE_MORPH_S = 2.0                 # gapless crossfade time for a rewire

    def rewire(self) -> None:
        """Track 2 short-press: rewire the connections (new signal flow), keep
        params. A 2 s cord CROSSFADE — no master fade, no gap — so it's usable live."""
        self._safe(lambda: self.state.rewire_patch(morph=self.REWIRE_MORPH_S))

    def rewire_randomize(self) -> None:
        """Track 2 long-press: rewire AND re-roll every module's parameters in the
        current artist aesthetic. Push the new params, then morph the wiring (the
        routing crossfades gaplessly; params change in place)."""
        def go():
            self.state._apply_style(None, self._style, self.state.patch.expert_override)
            self.state._resync_all()                       # push the new params
            self.state.rewire_patch(morph=self.REWIRE_MORPH_S)
        self._safe(go)

    def toggle_lfo(self, i: int) -> None:
        if 0 <= i < 16:
            lid = f"lfo{i + 1}"
            rt = self.state.mod.routes.get(f"{lid}_rt")
            self.state.set_lfo_enabled(lid, not (rt and rt.enable))

    def rerandomize_lfo(self, i: int) -> None:
        if 0 <= i < 16:
            self.state.rerandomize_lfo(f"lfo{i + 1}", self._style)

    def randomize_all_lfos(self) -> None:
        self.state.randomize_all_lfos(self._style)

    def _lfos_status(self) -> list:
        out = []
        for i in range(16):
            rt = self.state.mod.routes.get(f"lfo{i + 1}_rt")
            out.append(bool(rt and rt.enable))
        return out

    def _gen_macros(self) -> None:
        """Every patch loads 8 macros, each with 1-5 random destinations and a
        random modulation direction (MacroTarget.invert)."""
        from .control import Macro
        st = self.state
        st.control.macros.clear()
        for i in range(1, 9):
            mac = Macro(id=f"macro_{i}", name=f"M{i}", page="Macro", value=0.0)
            st._assign_macro_targets(mac, st.rng.randint(1, 5))
            st.control.add_macro(mac)
        st._resync_all()

    def toggle_pad(self, pad: int) -> None:
        """Short-press a module pad: flip the module on/off (bypass)."""
        mid = self._pad_to_mid(pad)
        if not mid:
            return
        m = self.state.patch.modules[mid]
        m.bypass = not m.bypass
        self.state.bridge.module_bypass(mid, m.bypass)

    def set_pad_level(self, pad: int, level: float) -> None:
        """Set the audio level (the module synth's `amp` arg) of the module at a
        pad. Every module synthdef has an `amp` arg; the engine .set's it on all
        the module's nodes (node -1)."""
        mid = self._pad_to_mid(pad)
        if not mid:
            return
        self.state.bridge.set_param(mid, "amp", -1, max(0.0, min(2.0, float(level))))

    def randomize_module(self, pad: int) -> None:
        """Shift + module pad: re-roll that module's params (no LFOs touched). Uses
        a FRESH RANDOM artist each time (not the patch's fixed one) so repeated
        randomizing actually surprises — different aesthetic, wide spread — instead
        of clustering around one artist's centres."""
        mid = self._pad_to_mid(pad)
        if mid:
            self.state.randomize_module_params(mid, self.state.rng.choice(ARTISTS))

    def add_module_at(self, cell: int, mtype: str = "") -> None:
        """Press an EMPTY pad -> drop a module onto it and wire it into the patch at
        random (the 'growing maze' build). `mtype` forces a specific module (Play/Rec
        gestures load RINGS/DX7/BEN); otherwise a random module of that row's class
        (GEN row -> generator/hybrid, FX row -> processor)."""
        from .catalog import CATALOG
        if not (0 <= cell < 32) or cell in self._pad_map.values():
            return                                      # occupied / out of range
        if mtype and mtype in CATALOG and CATALOG[mtype].is_audio:
            t = mtype                                   # forced (Play/Rec + empty pad)
        else:
            gen_row = cell in self.GEN_CELLS
            pool = [tt for tt, s in CATALOG.items()
                    if s.is_audio and self._is_gen(s) == gen_row]
            if not pool:
                return
            t = self.state.rng.choice(pool)
        # Build the module + wiring WITHOUT a graph rebuild, then fade JUST its new
        # cords in (grow) — no rebuild click, no onset pop.
        m = self.state.add_module(t, node_count=1, sync=False)
        self._pad_map[m.id] = cell                      # pin to the pressed pad
        # Start RANDOMIZED (fresh random artist), not at defaults — so a grown
        # module lands varied from the get-go instead of always the same sound.
        self.state.randomize_module_params(m.id, self.state.rng.choice(ARTISTS))
        self.state.wire_in_module(m.id, sync=False)
        self.state._sync_graph(grow_mid=m.id)
        self.state.retarget_lfos(self._style)           # give dead/empty LFOs a live target

    def delete_pad(self, pad: int) -> None:
        """Track3 + pad: remove the module at that pad; its cell clears."""
        mid = self._pad_to_mid(pad)
        if not mid:
            return
        self.state.remove_module(mid)
        self._pad_map.pop(mid, None)
        self.state.retarget_lfos(self._style)           # rescue LFOs that pointed at it

    @staticmethod
    def _category(spec) -> str:
        if not spec.is_audio or spec.type == "SEQ":
            return "midi"
        if spec.generative_capable and spec.insert_capable:
            return "both"            # RINGS / FBANK — generator AND processor
        if spec.generative_capable:
            return "gen"
        return "fx"

    # Canvas contract: generators + hybrids live in the GEN rows (1 & 3),
    # processors in the FX rows (2 & 4). Pads top->bottom are cells 0-7, 8-15,
    # 16-23, 24-31. Layout only — it never affects wiring.
    GEN_CELLS = [0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19, 20, 21, 22, 23]
    FX_CELLS = [8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 26, 27, 28, 29, 30, 31]

    def _is_gen(self, spec) -> bool:
        return self._category(spec) in ("gen", "both")   # generator OR hybrid (RINGS)

    def _ensure_pad_map(self) -> None:
        """Assign each module a STABLE pad cell. Generators/hybrids take a free GEN
        cell (rows 1 & 3), processors a free FX cell (rows 2 & 4); a full row falls
        back to any free cell. Existing assignments are kept, so toggling/deleting/
        adding never reflows the others."""
        live = list(self.state.patch.modules.keys())
        for mid in [m for m in self._pad_map if m not in live]:
            del self._pad_map[mid]
        used = set(self._pad_map.values())
        for mid in self.state.patch.topo_order():
            if mid in self._pad_map:
                continue
            m = self.state.patch.modules[mid]
            pref = self.GEN_CELLS if self._is_gen(m.spec) else self.FX_CELLS
            for cell in pref + [c for c in range(32) if c not in pref]:
                if cell not in used:
                    self._pad_map[mid] = cell
                    used.add(cell)
                    break

    def _pad_to_mid(self, pad: int):
        for mid, cell in self._pad_map.items():
            if cell == pad:
                return mid
        return None

    def _grid(self) -> list:
        self._ensure_pad_map()
        out = []
        for mid, cell in self._pad_map.items():
            m = self.state.patch.modules.get(mid)
            if not m:
                continue
            out.append({"pad": cell, "type": m.type,
                        "cat": self._category(m.spec), "on": not m.bypass})
        return out

    def _macros_status(self) -> list:
        out = []
        for i in range(1, 9):
            mac = self.state.control.macros.get(f"macro_{i}")
            out.append({"id": f"M{i}",
                        "val": round(getattr(mac, "value", 0.0), 3) if mac else 0.0,
                        "targets": len(mac.targets) if mac else 0})
        return out

    # -- control file (ui.js -> controller, polled) ------------------------ #
    def _control_file_loop(self) -> None:
        """Poll control.json written by the overtake ui.js. Shape:
            {"seq": <int>, "cmd": "newpatch|rewire|toggle|delete", "arg": <int>,
             "macros": [8 floats 0..1]}
        macros are applied every poll (idempotent); cmd only when seq advances."""
        period = 1.0 / max(10.0, CONTROL_HZ)
        last_seq = -1
        last_macros = [None] * 8
        last_level = None
        last_gain = None
        while not self._stop.is_set():
            time.sleep(period)
            try:
                if not CONTROL_FILE.exists():
                    continue
                doc = json.loads(CONTROL_FILE.read_text() or "{}")
            except Exception:
                continue  # partial write / parse error — skip this frame
            macros = doc.get("macros")
            if isinstance(macros, list):
                for i, v in enumerate(macros[:8]):
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if last_macros[i] is None or abs(fv - last_macros[i]) > 1e-4:
                        last_macros[i] = fv
                        self._safe(lambda i=i, fv=fv:
                                   self.state.set_macro(f"macro_{i + 1}", fv))
            # Per-module level: hold a pad + main knob -> that module's amp.
            lvl = doc.get("level")
            if isinstance(lvl, dict):
                key = (lvl.get("pad"), round(float(lvl.get("val", 1.0)), 3))
                if key != last_level:
                    last_level = key
                    self._safe(lambda p=lvl.get("pad"), v=lvl.get("val"):
                               self.set_pad_level(int(p), float(v)))
            # Master gain: main knob with no pad held -> the engine makeup gain.
            mg = doc.get("mastergain")
            if mg is not None:
                try:
                    mgf = round(float(mg), 3)
                except (TypeError, ValueError):
                    mgf = None
                if mgf is not None and mgf != last_gain:
                    last_gain = mgf
                    self._safe(lambda v=mgf: self.bridge.send("/atelier/mastergain", v))
            seq = doc.get("seq")
            if isinstance(seq, int) and seq != last_seq:
                last_seq = seq
                cmd = doc.get("cmd")
                if cmd == "chainsgen":          # carries a variable-length selection
                    self._safe(lambda: self.chains_patch(doc.get("chains", [])))
                elif cmd == "addmod":           # carries an optional forced module type
                    a = doc.get("arg", -1)
                    self._safe(lambda: self.add_module_at(
                        int(a) if isinstance(a, (int, float)) else -1, str(doc.get("mtype", ""))))
                else:
                    arg = doc.get("arg", -1)
                    self._dispatch_cmd(str(cmd), int(arg) if isinstance(arg, (int, float)) else -1)

    def _dispatch_cmd(self, cmd: str, arg: int) -> None:
        if cmd == "newpatch":
            self._safe(self.new_patch)
        elif cmd == "rewire":
            self._safe(self.rewire)
        elif cmd == "rewirerand":
            self._safe(self.rewire_randomize)
        elif cmd == "toggle":
            self._safe(lambda: self.toggle_pad(arg))
        elif cmd == "delete":
            self._safe(lambda: self.delete_pad(arg))
        elif cmd == "randmod":
            self._safe(lambda: self.randomize_module(arg))
        elif cmd == "lfotoggle":
            self._safe(lambda: self.toggle_lfo(arg))
        elif cmd == "lforand":
            self._safe(lambda: self.rerandomize_lfo(arg))
        elif cmd == "lforandall":
            self._safe(self.randomize_all_lfos)
        elif cmd == "panic":
            self._safe(getattr(self.state, "panic", None) or self.bridge.panic)

    # -- snapshot (controller -> ui.js screen) ----------------------------- #
    def _snapshot_loop(self) -> None:
        # status.json is small + cheap (the ui.js reads it every frame); write it
        # often. snapshot.json is the full 180 KB picture (nothing reads it yet) —
        # building + writing it 5x/s was starving scsynth's audio thread and
        # causing JACK XRuns (clicks/pops), so write it rarely.
        period = 1.0 / max(0.5, SNAP_HZ)
        full_every = max(1, int(round(SNAP_HZ * FULL_SNAPSHOT_EVERY_S)))
        i = 0
        while not self._stop.is_set():
            self._write_status()
            if WRITE_FULL_SNAPSHOT and (i % full_every == 0):
                self._write_full_snapshot()
            i += 1
            time.sleep(period)

    def _write_status(self) -> None:
        """Tiny status file the overtake ui.js reads every frame. Built DIRECTLY
        from state (no full_snapshot) so the hot path stays cheap."""
        try:
            status = {
                "ready": self._built.is_set(),
                "engine": self.bridge.connected,
                "cpu": round(self.bridge.cpu.get("avg", 0.0), 1),  # SC avgCPU is already %
                "nodes": self.bridge.cpu.get("nodes", 0),
                "modules": len(self.state.patch.modules),
                "meters": [round(m, 3) for m in (self.bridge.meters or [])[:2]],
                "grid": self._grid(),       # pad cell -> {type, cat, on}
                "macros": self._macros_status(),
                "lfos": self._lfos_status(),   # 16 bools: step-button LFO on/off
            }
            tmp = STATUS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(status, separators=(",", ":")))
            tmp.replace(STATUS_FILE)
        except Exception:
            pass

    def _write_full_snapshot(self) -> None:
        try:
            snap = full_snapshot(self.state)
            snap["_engine"] = {"connected": self.bridge.connected,
                               "cpu": self.bridge.cpu, "meters": self.bridge.meters}
            tmp = SNAP_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, separators=(",", ":")))
            tmp.replace(SNAP_FILE)
        except Exception:
            pass

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass


def main() -> None:
    ctl = HeadlessController()
    signal.signal(signal.SIGTERM, ctl.stop)
    signal.signal(signal.SIGINT, ctl.stop)
    ctl.start()
    ctl.run()


if __name__ == "__main__":
    main()
