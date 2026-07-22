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
import random
import signal
import threading
import time
from pathlib import Path

from . import aesthetics
from .catalog import (GATE as _GATE_SPEC, DISTORT as _DISTORT_SPEC,
                      COMB as _COMB_SPEC, CLOUDS as _CLOUDS_SPEC)
from .osc_bridge import OSCBridge
from .params import Curve as _Curve, RandomizePolicy as _RandPol, Rate as _Rate
from .snapshot import full_snapshot
from .state import StateManager

# Sampler step-button FX reuse the real module specs (full param sets, musical
# randomization ranges). `which` is the position in the engine's ~sampFxChain.
_SAMP_FX_SPEC = {"gate": _GATE_SPEC, "dist": _DISTORT_SPEC,
                 "comb": _COMB_SPEC, "clouds": _CLOUDS_SPEC}
_SAMP_FX_WHICH = {"gate": 0, "dist": 1, "comb": 2, "clouds": 3}
_SAMP_FX_ORDER = ["gate", "dist", "comb", "clouds"]


def _fresh_samp_slot() -> dict:
    return {"state": "empty", "t0": 0.0, "frames": 0,
            "vol": 1.0, "pan": 0.0, "ls": 0.0, "le": 1.0,
            "cut": 1.0, "res": 0.0, "pit": 0.0,
            "gate_on": 0, "gate_params": {}, "dist_on": 0, "dist_params": {},
            "comb_on": 0, "comb_params": {}, "clouds_on": 0, "clouds_params": {}}
# args the chain manages (not randomized): enable/wet/bypass set by the engine, amp
# kept at the module default, nodeEnable has no synthdef arg. Spatial pinned neutral.
_SAMP_FX_SKIP = {"nodeEnable", "enable", "wet", "bypass", "amp"}
_SAMP_FX_PIN0 = {"pan", "spatialPos"}
# Context bias: as a foreground sampler FX, CLOUDS must be reliably audible — its
# catalog ranges go sparse/quiet, and a random freeze freezes an empty buffer
# (silence). Pin freeze off and bias grain density/size/gain up.
_SAMP_FX_FIX = {"clouds": {"freeze": 0},
                # DISTORT: catalog drive (->24x) saturates everything to a near-square
                # wave far louder than dry, so even a sliver of wet overwhelms. Tame the
                # output level and keep the wet roughly in the range of the dry signal.
                "dist": {"amp": 0.45}}
_SAMP_FX_RANGE = {"clouds": {"dens": (0.8, 0.98), "size": (0.5, 0.85), "inGain": (1.3, 1.6)},
                  "dist": {"drive": (1.2, 4.0), "feedback": (0.0, 0.2), "bias": (0.0, 0.3)}}

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
PERF_DIR = SHARE.parent / "performances"   # top-level projects: patch + scenes + samples
# ui.js -> controller: the JS sandbox has file IO but no UDP socket, so the
# overtake ui.js writes commands/macro values here and the controller polls it.
CONTROL_FILE = SHARE / "control.json"
SNAP_HZ = float(_env("WR_SNAPSHOT_HZ", "5"))           # status.json rate (cheap)
CONTROL_HZ = float(_env("WR_CONTROL_HZ", "60"))
# The full 180KB snapshot.json is unused by the ui.js today and writing it (even
# every 3s) spikes the Python process to ~20% CPU and starves scsynth's audio
# thread -> JACK XRuns (clicks/pops). Nothing reads it, so it is OFF by default;
# set WR_FULL_SNAPSHOT=1 only if a future UI consumer needs it.
WRITE_FULL_SNAPSHOT = _env("WR_FULL_SNAPSHOT", "0") != "0"
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
        # Sampler: 32 slots. Each has playback state + per-slot params (loop region,
        # volume, pan). loop ls/le default full take; vol unity; pan centre.
        self._samp = [{"state": "empty", "t0": 0.0, "frames": 0,
                       "vol": 1.0, "pan": 0.0, "ls": 0.0, "le": 1.0,
                       "cut": 1.0, "res": 0.0, "pit": 0.0,         # cut/res 0..1, pit semis
                       # per-slot insert FX (step buttons): real modules, on + params each
                       "gate_on": 0, "gate_params": {}, "dist_on": 0, "dist_params": {},
                       "comb_on": 0, "comb_params": {}, "clouds_on": 0, "clouds_params": {}}
                      for _ in range(32)]
        # per-FX dry/wet balance (0..1, 0.5 = 50/50), shared by all slots that carry it.
        # DISTORT defaults to 0.1 (10 wet / 90 dry). Order = _SAMP_FX_ORDER.
        self._fx_wet = [0.5, 0.1, 0.5, 0.5]
        self._perf_reload = 0      # bumps on performance load so the ui re-reads macros
        self._master_gain = 6.0    # current master makeup (saved/restored per performance)
        self._muted = False        # Play-button toggle: master output silenced?
        self._cdp_busy = False     # CDP view: a capture+process job is running
        self._perf_xfade = 2.0     # seconds to crossfade between performances (gapless)
        self._density = 0.0        # knob 1: global trigger density (-1..1, 0 = as generated)
        self._density_override = {}   # mid -> per-generator density (held-pad edits)
        self._pitch = 0.0          # knob 2: global pitch shift (-1..1, 0 = as generated)
        self._pitch_override = {}     # mid -> per-generator pitch shift (held-pad edits)
        self._master_cut = 1.0     # knobs 3/4: global master lowpass (normalised 0..1)
        self._master_res = 0.0
        self._macro5 = 0.0         # knob 5: bipolar "morph everything" macro (-1..1, 0 = baseline)
        self._macro5_dirs = {}     # (mid,pid,node) -> +/-1 direction (persists until new patch)
        self._macro5_base = {}     # (mid,pid,node) -> baseline value captured at gesture start
        self._macro5_targets = []  # cached [(mid,pid,node,meta,dir,base_norm)] for the hot apply
        self._macro5_epoch = -1    # patch epoch the current directions belong to
        self._patch_epoch = 0      # bumped on every patch rebuild (invalidates macro-5 directions)
        self._loop_start = 0.0          # CTRL-ALL loop region (0..1), applied to all slots
        self._loop_end = 1.0

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        SHARE.mkdir(parents=True, exist_ok=True)
        self._write_modules_list()
        self.bridge.start()
        # Seed the generator from real entropy so every session produces a DIFFERENT
        # sequence of patches. The default root_seed is a fixed constant (1), which
        # made every launch replay the identical patches in the identical order.
        self.state.reseed(int.from_bytes(os.urandom(4), "big") & 0x7fffffff)
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
                    self._patch_epoch += 1                          # new patch -> fresh macro-5 directions
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
                    self._master_gain = round(gain, 2)
                    self.bridge.send("/atelier/mastergain", self._master_gain)  # fade in at level
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

    # -- scenes (track-3 view: 32 pads = 32 scene slots) ------------------- #
    SCENE_MORPH_S = 10.0                 # default morph between scenes (seconds)
    SCENE_PREFIX = "padscene_"           # pad index -> scene id (no collision with add_scene)

    def _scene_id(self, pad: int) -> str:
        return f"{self.SCENE_PREFIX}{int(pad)}"

    def _scene_pad(self, sid) -> int:
        """Scene id -> pad index (or -1 if it is not a pad-bank scene)."""
        if isinstance(sid, str) and sid.startswith(self.SCENE_PREFIX):
            try:
                p = int(sid[len(self.SCENE_PREFIX):])
                return p if 0 <= p < 32 else -1
            except ValueError:
                return -1
        return -1

    def store_scene_pad(self, pad: int) -> None:
        """Shift + pad in the scenes view: capture the WHOLE performance (patch +
        LFOs + macros) into this slot."""
        if not (0 <= pad < 32):
            return
        sid = self._scene_id(pad)
        if sid not in self.state.scenes.scenes:
            self.state.scenes.add_empty(sid)
        self.state.capture_scene(sid)

    def load_scene_pad(self, pad: int) -> None:
        """Pad in the scenes view: recall a stored scene with a gradual 10 s morph
        of every module param, LFO and macro. Empty slots are ignored."""
        if not (0 <= pad < 32):
            return
        sc = self.state.scenes.scenes.get(self._scene_id(pad))
        if not sc or not sc.filled:
            return
        self.state.load_scene(self._scene_id(pad),
                              morph=getattr(self, "_morph_s", self.SCENE_MORPH_S))

    def _scenes_status(self) -> dict:
        """Scene-bank state for the ui.js scenes view: which of the 32 pads hold a
        filled scene, the active one, and the morph destination (while morphing)."""
        sc = self.state.scenes
        filled = [False] * 32
        for i in range(32):
            s = sc.scenes.get(self._scene_id(i))
            if s and s.filled:
                filled[i] = True
        sm = self.state._scene_morph
        return {"filled": filled,
                "active": self._scene_pad(sc.active),
                "morphTo": self._scene_pad(sm.get("dst_id")) if sm else -1}

    # -- sampler (track-4 view: 32 pads = 32 sample slots) ----------------- #
    SAMP_SR = 44100.0                    # Move shadow rate (fixed)
    SAMP_MAX_S = 30.0                    # must match the engine's ~sampMaxFrames

    def sampler_pad(self, pad: int) -> None:
        """Short-press a sample slot. empty -> start recording the master mix;
        recording -> stop (slot becomes playable); filled -> start looping playback;
        playing -> stop."""
        if not (0 <= pad < 32):
            return
        sl = self._samp[pad]
        st = sl["state"]
        if st == "empty":
            self.bridge.send("/atelier/sampler/rec", pad)
            sl["state"] = "recording"; sl["t0"] = time.monotonic()
        elif st == "recording":
            dur = min(time.monotonic() - sl["t0"], self.SAMP_MAX_S)
            frames = max(1, int(dur * self.SAMP_SR))
            self.bridge.send("/atelier/sampler/recstop", pad, frames)
            sl["state"] = "filled"; sl["frames"] = frames
        elif st == "filled":
            self.bridge.send("/atelier/sampler/play", pad)
            sl["state"] = "playing"
        elif st == "playing":
            self.bridge.send("/atelier/sampler/stop", pad)
            sl["state"] = "filled"

    def sampler_del(self, pad: int) -> None:
        """X + pad in the sampler view: free the slot's buffer + synths."""
        if not (0 <= pad < 32):
            return
        self.bridge.send("/atelier/sampler/free", pad)
        self._samp[pad] = {"state": "empty", "t0": 0.0, "frames": 0,
                           "vol": 1.0, "pan": 0.0, "ls": 0.0, "le": 1.0,
                           "cut": 1.0, "res": 0.0, "pit": 0.0,
                           "gate_on": 0, "gate_params": {}, "dist_on": 0, "dist_params": {},
                           "comb_on": 0, "comb_params": {}, "clouds_on": 0, "clouds_params": {}}

    # -- Performances: top-tier project = patch + scenes + modulation + samples - #
    def _perf_dir(self, pad: int):
        return PERF_DIR / f"perf_{int(pad):02d}"

    def _perf_status(self) -> dict:
        filled = [False] * 32
        try:
            for pad in range(32):
                if (self._perf_dir(pad) / "patch.json").exists():
                    filled[pad] = True
        except Exception:
            pass
        return {"filled": filled}

    def save_performance(self, pad: int) -> None:
        """Save the WHOLE project into a performance slot: the full patch (modules,
        wiring, MIDI, modulation, sequencer, macros) + the scene bank via save_patch,
        plus the sampler slot settings/FX and each take's audio as a WAV."""
        pad = int(pad)
        if not (0 <= pad < 32):
            return
        from .persistence import save_patch
        d = self._perf_dir(pad)
        (d / "samples").mkdir(parents=True, exist_ok=True)
        save_patch(self.state, str(d / "patch.json"))
        slots = []
        for s in self._samp:
            c = dict(s)
            c.pop("t0", None)
            if c["state"] == "recording":
                c["state"] = "filled"
            slots.append(c)
        with open(d / "sampler.json", "w") as f:
            json.dump({"slots": slots, "fx_wet": self._fx_wet,
                       "master_gain": self._master_gain}, f)
        for i, s in enumerate(self._samp):
            if s["state"] in ("filled", "playing"):
                self.bridge.send("/atelier/sampler/save", i,
                                 str(d / "samples" / f"slot_{i:02d}.wav"))

    def load_performance(self, pad: int) -> None:
        """Recall a performance: rebuild the patch + scenes + modulation (load_patch),
        then restore the sampler — free all slots, reload each take's WAV, and re-apply
        its settings/FX (pushed BEFORE the async buffer load so the resumed playback
        comes up with the saved per-slot state)."""
        import time
        from .scenes import DiscretePolicy
        d = self._perf_dir(int(pad))
        if not (d / "patch.json").exists():
            return
        patch_json = json.loads((d / "patch.json").read_text())
        samp = {}
        try:
            samp = json.loads((d / "sampler.json").read_text())
        except Exception:
            pass

        # --- GAPLESS PATCH TRANSITION ---------------------------------------- #
        # Crossfade the live patch into the saved one (no mute/rebuild gap): set up
        # the scene-morph engine with dst = the performance's patch snapshot. Audio
        # keeps flowing; structural change commits mid-crossfade with cord fades.
        dst = dict(patch_json)
        dst.pop("scenes", None)
        ctrl = dst.pop("control", None) or {}
        dst["macros"] = ctrl.get("macros", [])
        self.state.scenes.load_list(patch_json.get("scenes", []))   # the project's scene bank
        src = self.state._capture_snapshot()
        self.state._scene_morph = {
            "dst_id": None, "src": src, "dst": dst, "start": time.monotonic(),
            "duration": self._perf_xfade, "since_frame": 0.0, "committed": False,
            "structural": self.state._morph_is_structural(src, dst),
            "exclusions": [], "policy": DiscretePolicy.THRESHOLD,
        }
        self._perf_reload += 1                       # ui re-syncs its macro knobs
        # restore this project's calibrated master makeup (lagged in the engine = smooth)
        mg = samp.get("master_gain")
        if isinstance(mg, (int, float)):
            self._master_gain = float(mg)
            self.bridge.send("/atelier/mastergain", round(self._master_gain, 2))

        # --- SAMPLER: crossfade old takes -> new ----------------------------- #
        wet = samp.get("fx_wet") or [0.5, 0.1, 0.5, 0.5]
        self._fx_wet = (list(wet) + [0.5, 0.1, 0.5, 0.5])[:4]
        saved = samp.get("slots") or []
        old_playing = [self._samp[i]["state"] == "playing" for i in range(32)]
        new = []
        for i in range(32):
            sl = _fresh_samp_slot()
            if i < len(saved) and isinstance(saved[i], dict):
                sl.update(saved[i])
                sl["t0"] = 0.0
            new.append(sl)
        self._samp = new
        for i, s in enumerate(self._samp):
            wav = d / "samples" / f"slot_{i:02d}.wav"
            if s["state"] in ("filled", "playing") and wav.exists():
                self._push_slot(i)                   # stored settings (applied on resumed play)
                for fx in _SAMP_FX_ORDER:
                    if s.get(fx + "_on"):
                        self._push_slotfx(i, fx)
                autoplay = 1 if s["state"] == "playing" else 0
                self.bridge.send("/atelier/sampler/load", i, str(wav), autoplay)  # gapless crossfade
            else:
                s["state"] = "empty"
                if old_playing[i]:                   # fade out a take the new project drops
                    self.bridge.send("/atelier/sampler/stop", i)

    def delete_performance(self, pad: int) -> None:
        """X + pad in the Performances view: remove a saved project from disk."""
        import shutil
        d = self._perf_dir(int(pad))
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # -- Knob 1: global density (scales every generator's internal clock) -------- #
    _CLOCKED_GENS = {"FMTONE": None, "WAVIARY": None, "RINGS": None}

    @staticmethod
    def _dens_mul(d: float) -> float:
        return 2.0 ** (max(-1.0, min(1.0, float(d))) * 5.0)    # -1 -> ~x0.03 (sparse), 0 -> x1, +1 -> ~x32 (saturated)

    def set_density(self, d: float, target: int = -1) -> None:
        """Trigger-density knob (bipolar; 0 = generated rate). target < 0 = GLOBAL (all
        generators); target = a pad cell = only the generator on that held pad. Applied
        as a `densityMul` on each generator's internal clock (orthogonal to its rate
        base, so it survives modulation + new patches)."""
        d = max(-1.0, min(1.0, float(d)))
        if int(target) < 0:
            self._density = d
        else:
            mid = next((m for m, c in self._pad_map.items() if c == int(target)), None)
            if mid and self.state.patch.modules.get(mid) \
                    and self.state.patch.modules[mid].type in self._CLOCKED_GENS:
                self._density_override[mid] = d      # this generator diverges from global
        self._apply_density()

    def _apply_density(self) -> None:
        live = self.state.patch.modules
        for mid in [m for m in self._density_override if m not in live]:
            del self._density_override[mid]          # drop overrides for gone modules
        for mid, mod in list(live.items()):
            if mod.type in self._CLOCKED_GENS:
                d = self._density_override.get(mid, self._density)
                self.bridge.set_param(mid, "densityMul", -1, self._dens_mul(d))

    # -- Knob 2: pitch shift (transposes every generator; floored to dodge sub gargle) -- #
    _PITCH_RANGE = 24.0    # +/- semitones at full knob throw (2 octaves each way)

    @staticmethod
    def _pitch_semis(p: float) -> float:
        return max(-1.0, min(1.0, float(p))) * HeadlessController._PITCH_RANGE

    def set_pitch(self, p: float, target: int = -1) -> None:
        """Pitch-shift knob (bipolar; 0 = as generated). target < 0 = GLOBAL; target = a
        held pad cell = only that generator. Applied as a `pitchShift` (semitones) arg on
        each generator; the synthdefs floor the resulting frequency to avoid sub gargle."""
        p = max(-1.0, min(1.0, float(p)))
        if int(target) < 0:
            self._pitch = p
        else:
            mid = next((m for m, c in self._pad_map.items() if c == int(target)), None)
            if mid and self.state.patch.modules.get(mid) \
                    and self.state.patch.modules[mid].type in self._CLOCKED_GENS:
                self._pitch_override[mid] = p
        self._apply_pitch()

    def _apply_pitch(self) -> None:
        live = self.state.patch.modules
        for mid in [m for m in self._pitch_override if m not in live]:
            del self._pitch_override[mid]
        for mid, mod in list(live.items()):
            if mod.type in self._CLOCKED_GENS:
                p = self._pitch_override.get(mid, self._pitch)
                self.bridge.set_param(mid, "pitchShift", -1, self._pitch_semis(p))

    # -- Knob 5: bipolar "morph everything" macro ------------------------------ #
    # Offsets EVERY morphable param of the current patch in normalised space by the
    # knob position. Each param carries a random +/-1 direction that persists until a
    # new patch is generated, so CW pushes a fixed random half up and the rest down;
    # CCW inverts. Generator PITCH params are never touched. The baseline (knob-centre
    # state) is captured when a gesture begins (knob touch) so centre == the state the
    # patch is in at that moment.
    _MACRO5_SPAN = 0.5    # normalised offset applied at full knob throw (+/-)

    def _macro5_slots(self):
        """Yield (mid, pid, node, slot) for every param eligible for the knob-5 morph:
        unlocked + randomizable + continuous, and NOT a pitch param on a generator."""
        for mid, m in list(self.state.patch.modules.items()):
            gen = self._is_gen(m.spec)
            groups = [(None, m.global_slots)]
            for node, nd in enumerate(m.node_slots):
                groups.append((node, nd))
            for node, slots in groups:
                for pid, slot in slots.items():
                    if slot.locked or slot.meta.randomize_policy is _RandPol.OFF:
                        continue
                    if slot.meta.curve is _Curve.ENUM or slot.meta.rate is _Rate.DISCRETE:
                        continue      # discrete/enum params don't morph continuously
                    if gen and aesthetics.classify(slot.meta)[0] == aesthetics.PITCH:
                        continue      # never modulate pitch on generators
                    yield mid, pid, node, slot

    def begin_macro5(self) -> None:
        """Start a knob-5 gesture: re-centre the macro and snapshot the current param
        state as the baseline. Directions are (re)assigned only when the patch changed
        since they were last set, so they persist across gestures within a patch. The
        resolved targets are cached so the per-tick apply skips re-classification."""
        if self._patch_epoch != self._macro5_epoch:
            self._macro5_dirs = {}
            self._macro5_epoch = self._patch_epoch
        self._macro5 = 0.0
        self._macro5_base = {}
        targets = []
        for mid, pid, node, slot in self._macro5_slots():
            key = (mid, pid, node)
            self._macro5_base[key] = slot.base
            if key not in self._macro5_dirs:
                self._macro5_dirs[key] = 1.0 if random.random() < 0.5 else -1.0
            # cache (mid, pid, node, meta, direction, baseline-in-norm-space)
            targets.append((mid, pid, node, slot.meta,
                            self._macro5_dirs[key], slot.meta.to_norm(slot.base)))
        self._macro5_targets = targets

    def set_macro5(self, v: float) -> None:
        """Apply the bipolar morph at knob position v (-1..1). 0 restores the baseline."""
        self._macro5 = max(-1.0, min(1.0, float(v)))
        if not self._macro5_targets:   # no gesture started yet (e.g. value before a touch)
            self.begin_macro5()
        span = self._MACRO5_SPAN * self._macro5
        setp = self.state.set_param
        for mid, pid, node, meta, direction, base_norm in self._macro5_targets:
            pos = base_norm + (direction * span)
            setp(mid, pid, node, meta.to_value(0.0 if pos < 0.0 else 1.0 if pos > 1.0 else pos))

    def set_loop_range(self, start: float, end: float) -> None:
        """CTRL-ALL loop region (knob 1 = start, knob 2 = end) for ALL takes,
        normalised 0..1 of each take's length. Applied live to every playing slot
        + stored on every slot so new playbacks inherit it."""
        s = max(0.0, min(1.0, float(start)))
        e = max(0.0, min(1.0, float(end)))
        if e < s + 0.01:                 # keep a minimum loop window
            e = min(1.0, s + 0.01)
        self._loop_start, self._loop_end = s, e
        for sl in self._samp:
            sl["ls"], sl["le"] = s, e
        self.bridge.send("/atelier/sampler/looprange", s, e)

    # Filter mapping: normalised knob 0..1 -> cutoff Hz (exp 20..18k) / resonance 0..4.
    @staticmethod
    def _cut_hz(n: float) -> float:
        return 20.0 * (900.0 ** max(0.0, min(1.0, n)))     # 20 Hz .. 18 kHz, exponential

    @staticmethod
    def _res_amt(n: float) -> float:
        return max(0.0, min(1.0, n))                       # normalised; engine maps to RLPF rq

    # per-param clamps used by the multi-slot editor
    _SAMP_CLAMP = {"vol": (0.0, 1.3), "pan": (-1.0, 1.0), "ls": (0.0, 1.0),
                   "le": (0.0, 1.0), "cut": (0.0, 1.0), "res": (0.0, 1.0),
                   "pit": (-24.0, 24.0)}

    def _push_slot(self, slot: int) -> None:
        """Send a slot's current stored params to its play synth (+ for next play)."""
        if not (0 <= slot < 32):
            return
        sl = self._samp[slot]
        if sl["le"] < sl["ls"] + 0.01:                 # keep a minimum loop window
            sl["le"] = min(1.0, sl["ls"] + 0.01)
        self.bridge.send("/atelier/sampler/slot", slot, sl["vol"], sl["pan"], sl["ls"],
                         sl["le"], self._cut_hz(sl["cut"]), self._res_amt(sl["res"]), sl["pit"])

    def edit_slots(self, slots: list, param: str, value: float) -> None:
        """Slot-selection mode (one or MANY slots): set `param` to `value` on every
        selected slot, then push each. Same value applied to all selected."""
        if param not in self._SAMP_CLAMP:
            return
        lo, hi = self._SAMP_CLAMP[param]
        v = max(lo, min(hi, float(value)))
        for s in slots:
            s = int(s)
            if 0 <= s < 32:
                self._samp[s][param] = v
                self._push_slot(s)

    def _rand_fx(self, fx: str) -> dict:
        """Randomize the FULL param set of the real GATE/DISTORT module, exactly the
        way the patch generator does (musical ranges + danger clamps via the catalog
        ParamMetadata). Returns {synth_arg: value}. amp/pan/enable/wet/bypass are
        managed by the chain, not randomized; spatial stays neutral so the slot's own
        pan/level stay authoritative."""
        spec = _SAMP_FX_SPEC[fx]
        rng = self.state.rng
        fixed = _SAMP_FX_FIX.get(fx, {})
        ranges = _SAMP_FX_RANGE.get(fx, {})
        params: dict[str, float] = {}
        for p in spec.node_params:
            arg = p.id.split(".")[-1]
            if arg in fixed:                # fixed value wins (even over the skip list, e.g. a tamed amp)
                params[arg] = fixed[arg]
                continue
            if arg in _SAMP_FX_SKIP:
                continue
            if arg in _SAMP_FX_PIN0:
                params[arg] = 0.0
                continue
            if arg in ranges:
                lo, hi = ranges[arg]
                params[arg] = round(rng.uniform(lo, hi), 5)
                continue
            if p.curve is _Curve.ENUM:
                # enum range is [0, n-1], not the [0,1] musical band the scalar
                # randomizer assumes — pick across the full index set (or keep default
                # for OFF-policy enums like gate shape/invert).
                if p.randomize_policy is _RandPol.OFF or not p.enum_values:
                    params[arg] = int(p.default)
                else:
                    params[arg] = rng.randint(0, len(p.enum_values) - 1)
                continue
            params[arg] = round(p.randomize(rng, p.default, 1.0, expert=False), 5)
        return params

    def _push_slotfx(self, slot: int, fx: str) -> None:
        sl = self._samp[slot]
        which = _SAMP_FX_WHICH[fx]
        on = 1 if sl[fx + "_on"] else 0
        flat: list = ["wet", self._fx_wet[which]]   # per-FX dry/wet balance
        for k, v in sl[fx + "_params"].items():
            flat += [k, v]
        # /fxset <slot> <which> <on> name val ... — engine builds/teardowns the module
        self.bridge.send("/atelier/sampler/fxset", slot, which, on, *flat)

    def set_fx_wet(self, fxi: int, wet: float) -> None:
        """Set one FX's dry/wet (held step + jog). Updates every slot that carries it."""
        fxi = int(fxi)
        if not (0 <= fxi < len(_SAMP_FX_ORDER)):
            return
        self._fx_wet[fxi] = max(0.0, min(1.0, float(wet)))
        fx = _SAMP_FX_ORDER[fxi]
        for s in range(32):
            if self._samp[s][fx + "_on"]:
                self._push_slotfx(s, fx)

    def sampler_fx_sync(self, slots: list, armed: list, rerand: list) -> None:
        """Stamp the armed FX set onto the given slots: engage each armed FX (with
        fresh random params when newly engaged, or when its rerand flag is set),
        and disengage the rest. Slots not listed are left untouched (FX persist)."""
        for s in slots:
            s = int(s)
            if not (0 <= s < 32):
                continue
            sl = self._samp[s]
            for fxi, fx in enumerate(_SAMP_FX_ORDER):
                want = 1 if (fxi < len(armed) and armed[fxi]) else 0
                rer = 1 if (rerand and fxi < len(rerand) and rerand[fxi]) else 0
                cur = sl[fx + "_on"]
                if want and (not cur or rer):
                    sl[fx + "_on"] = 1
                    sl[fx + "_params"] = self._rand_fx(fx)
                    self._push_slotfx(s, fx)
                elif cur and not want:
                    sl[fx + "_on"] = 0
                    self._push_slotfx(s, fx)
                # want and cur and not rer -> already engaged, leave as-is

    def set_pitch_all(self, semis: float) -> None:
        """CTRL-ALL pitch (knob 5, no selection): semitone shift for ALL takes."""
        p = max(-24.0, min(24.0, float(semis)))
        for sl in self._samp:
            sl["pit"] = p
        self.bridge.send("/atelier/sampler/pitchall", p)

    def set_filter_all(self, cut: float, res: float) -> None:
        """CTRL-ALL filter (knob 3 = cutoff, knob 4 = resonance) for ALL takes."""
        cn = max(0.0, min(1.0, float(cut)))
        rn = max(0.0, min(1.0, float(res)))
        for sl in self._samp:
            sl["cut"], sl["res"] = cn, rn
        self.bridge.send("/atelier/sampler/filterall", self._cut_hz(cn), self._res_amt(rn))

    def set_master_filter(self, cut: float, res: float) -> None:
        """Patch-view knobs 3/4: global lowpass on the master bus (cutoff + resonance).
        Same normalised->Hz/res mapping as the sampler filter. The master synth persists
        across patch rebuilds, so this is set-and-forget (no re-apply needed)."""
        cn = max(0.0, min(1.0, float(cut)))
        rn = max(0.0, min(1.0, float(res)))
        self._master_cut, self._master_res = cn, rn
        self.bridge.send("/atelier/masterfilter", self._cut_hz(cn), self._res_amt(rn))

    def _sampler_status(self) -> dict:
        return {"states": [s["state"] for s in self._samp],
                "vol": [round(s["vol"], 3) for s in self._samp],
                "pan": [round(s["pan"], 3) for s in self._samp],
                "ls": [round(s["ls"], 4) for s in self._samp],
                "le": [round(s["le"], 4) for s in self._samp],
                "cut": [round(s["cut"], 4) for s in self._samp],
                "res": [round(s["res"], 4) for s in self._samp],
                "pit": [round(s["pit"], 2) for s in self._samp],
                "gate": [s["gate_on"] for s in self._samp],
                "dist": [s["dist_on"] for s in self._samp],
                "comb": [s["comb_on"] for s in self._samp],
                "clouds": [s["clouds_on"] for s in self._samp]}

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
        pad. Goes through the param SLOT (state.set_param) — not a direct engine
        poke — so the level becomes part of the patch state and is CAPTURED by
        scenes. Every module has an `.amp` param; it is per-node, so set all nodes."""
        mid = self._pad_to_mid(pad)
        if not mid:
            return
        level = max(0.0, min(2.0, float(level)))
        m = self.state.patch.modules.get(mid)
        if not m:
            return
        spec = m.spec
        amp_pid = next((p.id for p in spec.node_params + spec.global_params
                        if p.id.endswith(".amp")), None)
        if amp_pid is None:                       # no amp param -> fall back to direct poke
            self.state.bridge.set_param(mid, "amp", -1, level)
            return
        if any(p.id == amp_pid for p in spec.node_params):
            for n in range(m.node_count):
                self.state.set_param(mid, amp_pid, n, level)
        else:
            self.state.set_param(mid, amp_pid, None, level)

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
        gestures load RINGS/DX7/WAVIARY); otherwise a random module of that row's class
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
        last_looprange = None
        last_slot = None
        last_filter = None
        last_pitch = None
        last_sync_n = None
        last_fxwet = None
        last_density = None
        last_densmod = None
        last_genpitch = None
        last_pitchmod = None
        last_pfilter = None
        last_macro5 = None
        last_macro5begin = None
        last_macro5_t = 0.0
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
                    self._master_gain = mgf
                    self._safe(lambda v=mgf: self.bridge.send("/atelier/mastergain", v))
            # Sampler CTRL-ALL loop region (knob 1/2 in the sampler view).
            lr = doc.get("looprange")
            if isinstance(lr, list) and len(lr) == 2:
                try:
                    key = (round(float(lr[0]), 4), round(float(lr[1]), 4))
                except (TypeError, ValueError):
                    key = None
                if key is not None and key != last_looprange:
                    last_looprange = key
                    self._safe(lambda a=key: self.set_loop_range(a[0], a[1]))
            # Slot-selection edit (one or MANY selected slots): {sels, p, v}.
            se = doc.get("samedit")
            if isinstance(se, dict) and isinstance(se.get("sels"), list):
                try:
                    sk = (tuple(int(x) for x in se["sels"]), str(se.get("p")),
                          round(float(se.get("v", 0.0)), 4))
                except (TypeError, ValueError):
                    sk = None
                if sk is not None and sk != last_slot:
                    last_slot = sk
                    self._safe(lambda a=sk: self.edit_slots(a[0], a[1], a[2]))
            # CTRL-ALL filter (knob 3/4) + pitch (knob 5), no slot selected.
            fa = doc.get("filterall")
            if isinstance(fa, list) and len(fa) == 2:
                try:
                    fk = (round(float(fa[0]), 4), round(float(fa[1]), 4))
                except (TypeError, ValueError):
                    fk = None
                if fk is not None and fk != last_filter:
                    last_filter = fk
                    self._safe(lambda a=fk: self.set_filter_all(a[0], a[1]))
            pa = doc.get("pitchall")
            if pa is not None:
                try:
                    pk = round(float(pa), 3)
                except (TypeError, ValueError):
                    pk = None
                if pk is not None and pk != last_pitch:
                    last_pitch = pk
                    self._safe(lambda v=pk: self.set_pitch_all(v))
            # Sampler step-button FX: stamp the armed FX set onto the selected slots.
            # {slots, armed[4], rerand[4], n}. n monotonic so a re-stamp/re-randomize
            # with an identical mask still triggers.
            sync = doc.get("sampfxsync")
            if isinstance(sync, dict) and isinstance(sync.get("slots"), list):
                sn = int(sync.get("n", 0))
                if sn != last_sync_n:
                    last_sync_n = sn
                    self._safe(lambda d=sync: self.sampler_fx_sync(
                        d["slots"], d.get("armed", []), d.get("rerand", [])))
            # Knob 1: global trigger density (-1..1). Re-applied after rebuilds by the
            # snapshot loop, so a new patch inherits the current density.
            dens = doc.get("density")
            if dens is not None:
                try:
                    dv = round(float(dens), 4)
                except (TypeError, ValueError):
                    dv = None
                if dv is not None and dv != last_density:
                    last_density = dv
                    self._safe(lambda v=dv: self.set_density(v, -1))
            dm = doc.get("densmod")           # per-module density (a gen pad is held): {t, v, n}
            if isinstance(dm, dict):
                dmk = (int(dm.get("t", -1)), round(float(dm.get("v", 0.0)), 4), int(dm.get("n", 0)))
                if dmk != last_densmod:
                    last_densmod = dmk
                    self._safe(lambda a=dmk: self.set_density(a[1], a[0]))
            # Knob 2: global pitch shift for generators (-1..1). Also re-applied after
            # rebuilds by the snapshot loop.
            pit = doc.get("pitch")
            if pit is not None:
                try:
                    pv = round(float(pit), 4)
                except (TypeError, ValueError):
                    pv = None
                if pv is not None and pv != last_genpitch:
                    last_genpitch = pv
                    self._safe(lambda v=pv: self.set_pitch(v, -1))
            pm = doc.get("pitchmod")          # per-module pitch (a gen pad is held): {t, v, n}
            if isinstance(pm, dict):
                pmk = (int(pm.get("t", -1)), round(float(pm.get("v", 0.0)), 4), int(pm.get("n", 0)))
                if pmk != last_pitchmod:
                    last_pitchmod = pmk
                    self._safe(lambda a=pmk: self.set_pitch(a[1], a[0]))
            # Knobs 3/4: global master lowpass ([cut, res], both 0..1).
            pf = doc.get("pfilter")
            if isinstance(pf, list) and len(pf) == 2:
                pfk = (round(float(pf[0]), 4), round(float(pf[1]), 4))
                if pfk != last_pfilter:
                    last_pfilter = pfk
                    self._safe(lambda a=pfk: self.set_master_filter(a[0], a[1]))
            # Knob 5: bipolar morph-everything macro. A touch ({n}) snapshots the
            # baseline; the value (-1..1) sweeps it. Throttled to ~25 Hz because each
            # apply touches every patch param (a fast turn would otherwise flood OSC).
            mb = doc.get("macro5begin")
            if isinstance(mb, dict):
                mbn = int(mb.get("n", 0))
                if mbn != last_macro5begin:
                    last_macro5begin = mbn
                    self._safe(self.begin_macro5)
            mv = doc.get("macro5")
            if mv is not None:
                try:
                    mvr = round(float(mv), 4)
                except (TypeError, ValueError):
                    mvr = None
                now5 = time.monotonic()
                if mvr is not None and mvr != last_macro5 and (now5 - last_macro5_t) >= 0.04:
                    last_macro5 = mvr
                    last_macro5_t = now5
                    self._safe(lambda v=mvr: self.set_macro5(v))
            # FX dry/wet balance (held step + jog): {fx, wet, n}.
            fw = doc.get("fxwet")
            if isinstance(fw, dict):
                fwk = (int(fw.get("fx", -1)), round(float(fw.get("wet", 0.5)), 4), int(fw.get("n", 0)))
                if fwk != last_fxwet:
                    last_fxwet = fwk
                    self._safe(lambda a=fwk: self.set_fx_wet(a[0], a[1]))
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
        elif cmd == "storescene":
            self._safe(lambda: self.store_scene_pad(arg))
        elif cmd == "loadscene":
            self._safe(lambda: self.load_scene_pad(arg))
        elif cmd == "morphtime":            # Shift+Track3 editor: set scene morph seconds
            self._morph_s = max(1.0, min(99.0, float(arg)))
        elif cmd == "savep":                # Performances view: shift+pad = save project
            self._safe(lambda: self.save_performance(arg))
        elif cmd == "loadp":                # Performances view: pad = load project
            self._safe(lambda: self.load_performance(arg))
        elif cmd == "delp":                 # Performances view: X + pad = delete project
            self._safe(lambda: self.delete_performance(arg))
        elif cmd == "samppad":              # sampler view: record/play/stop toggle on a slot
            self._safe(lambda: self.sampler_pad(arg))
        elif cmd == "sampdel":              # sampler view: X + slot = delete
            self._safe(lambda: self.sampler_del(arg))
        elif cmd == "playtoggle":           # Play tap: silence / un-silence the whole patch
            self._safe(self.toggle_mute)
        elif cmd == "cdpgen":               # CDP view: capture a snippet + spawn 8 variations
            self._safe(self.cdp_generate)
        elif cmd == "panic":
            self._safe(getattr(self.state, "panic", None) or self.bridge.panic)

    def toggle_mute(self) -> None:
        """Play-button toggle: fade the master output out (silence) or back in at the
        current makeup. The engine's \\gain is lagged, so this is click-free."""
        self._muted = not self._muted
        self.bridge.send("/atelier/mastergain", 0.0 if self._muted else self._master_gain)

    # -- CDP: capture a live snippet -> 8 diverse CDP variations -> slots 0..7 --- #
    def cdp_generate(self) -> None:
        """CDP-view generator pad: fire the capture+process job in a worker so the
        control loop never blocks (the CDP chains take a couple of seconds)."""
        if self._cdp_busy:
            return
        self._cdp_busy = True
        threading.Thread(target=self._cdp_worker, daemon=True).start()

    def _cdp_worker(self) -> None:
        import time as _t
        from . import cdp as _cdp
        try:
            cdp_dir = SHARE.parent / "cdp"          # /data/UserData/wildrider/cdp
            work = cdp_dir / "work"
            work.mkdir(parents=True, exist_ok=True)
            src = work / "src.wav"
            dur = 4.0     # long enough to catch triggers from sparse patches, short enough to keep pvoc fast
            # 1. engine captures the live master bus to src.wav (deletes any stale one).
            self.bridge.send("/atelier/cdp/capture", float(dur), str(src))
            # 2. poll the shared filesystem for the file to appear + settle.
            deadline = _t.monotonic() + dur + 8.0
            last, stable = -1, 0
            while _t.monotonic() < deadline:
                _t.sleep(0.2)
                if src.exists():
                    sz = src.stat().st_size
                    if sz == last and sz > 2000:
                        stable += 1
                        if stable >= 3:
                            break
                    else:
                        last, stable = sz, 0
            if not (src.exists() and src.stat().st_size > 2000):
                return
            # 3. spawn diverse variations; load each into its slot the moment it is
            #    ready (progressive fill) so pads light up one-by-one instead of all
            #    at the end of the ~30 s batch.
            def _load(i, path):
                if i < 8:
                    self._samp[i]["state"] = "filled"
                    self.bridge.send("/atelier/sampler/load", i, str(path), 0)
            _cdp.generate(str(src), str(work / "vars"), count=8, on_ready=_load)
        except Exception:
            pass
        finally:
            self._cdp_busy = False

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
            if self._density != 0.0 or self._density_override:   # keep density applied across rebuilds
                self._safe(self._apply_density)
            if self._pitch != 0.0 or self._pitch_override:       # keep pitch shift applied across rebuilds
                self._safe(self._apply_pitch)
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
                "scenes": self._scenes_status(),  # {filled[32], active, morphTo}
                "performances": self._perf_status(),  # {filled[32]}
                "perfReload": self._perf_reload,
                "sampler": self._sampler_status(),  # {states[32]}
                "cdpBusy": self._cdp_busy,          # CDP view: capture+process in progress
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
