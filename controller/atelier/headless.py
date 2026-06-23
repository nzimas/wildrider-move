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

from .osc_bridge import OSCBridge
from .snapshot import full_snapshot
from .state import StateManager


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
SNAP_HZ = float(_env("WR_SNAPSHOT_HZ", "5"))


class HeadlessController:
    def __init__(self) -> None:
        self.bridge = OSCBridge(sc_host=SC_HOST, sc_port=SC_PORT,
                                listen_port=TELEMETRY_PORT)
        self.state = StateManager(bridge=self.bridge)
        self._stop = threading.Event()
        self._built = threading.Event()
        self._ctrl_server = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        SHARE.mkdir(parents=True, exist_ok=True)
        self.bridge.start()
        self.state.init_default()
        # Build the DSP graph only once the engine signals readiness. The engine
        # sets ~masterBus etc. at the *end* of its async boot block and then
        # sends /atelier/ready; building before that races (nil bus -> errors).
        # We may have started after the engine's one-shot ready, so we also ping
        # (engine replies /atelier/ready) until we're connected. Idempotent.
        self.bridge.on("ready", self._on_ready)
        threading.Thread(target=self._handshake_loop, daemon=True).start()
        self._start_control_channel()
        threading.Thread(target=self._snapshot_loop, daemon=True).start()
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
        disp.map("/wr/macro", self._h_macro)
        disp.map("/wr/param", self._h_param)
        disp.map("/wr/scene", self._h_scene)
        disp.map("/wr/panic", self._h_panic)
        disp.map("/wr/snapshot", lambda *_: self._write_snapshot())
        try:
            self._ctrl_server = ThreadingOSCUDPServer(("127.0.0.1", CONTROL_PORT), disp)
            threading.Thread(target=self._ctrl_server.serve_forever, daemon=True).start()
        except Exception as e:
            print(f"[wildrider] control channel bind failed: {e}", flush=True)

    def _h_macro(self, _addr, macro_id="m0", value=0.0) -> None:
        self._safe(lambda: self.state.set_macro(str(macro_id), float(value)))

    def _h_param(self, _addr, mid="", pid="", node=-1, value=0.0) -> None:
        n = None if int(node) < 0 else int(node)
        self._safe(lambda: self.state.set_param(str(mid), str(pid), n, float(value)))

    def _h_scene(self, _addr, scene_id="", morph=0.0) -> None:
        self._safe(lambda: self.state.load_scene(str(scene_id), float(morph)))

    def _h_panic(self, _addr, *_a) -> None:
        fn = getattr(self.state, "panic", None) or self.bridge.panic
        self._safe(fn)

    # -- snapshot (controller -> ui.js screen) ----------------------------- #
    def _snapshot_loop(self) -> None:
        period = 1.0 / max(0.5, SNAP_HZ)
        while not self._stop.is_set():
            self._write_snapshot()
            time.sleep(period)

    def _write_snapshot(self) -> None:
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
