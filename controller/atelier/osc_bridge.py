"""OSC bridge to the SuperCollider engine (blueprint section 10: OSC/MIDI).

The controller is the authoritative state manager; SuperCollider owns the DSP and
audio-rate modulation only for explicitly audio-rate params. This bridge speaks a
small private OSC protocol to ``supercollider/engine`` and degrades gracefully
when the server is not reachable (so the control layer and UI run even with no
audio backend, satisfying 'controllers must remain responsive even if audio CPU
is high', and aiding headless/CI use).

Protocol (controller -> engine), all under /atelier:
    /atelier/boot   sr blockSize channels
    /atelier/module/new   mid type channels nodeCount laneIndex
    /atelier/module/free  mid
    /atelier/module/nodes mid count
    /atelier/module/bypass mid 0|1
    /atelier/module/wet   mid wetDry
    /atelier/set    mid pname node value      (node -1 == global)
    /atelier/setvec mid pname f0 f1 ...        (one value per node)
    /atelier/feedback mid->mid armed gainLimit delaySamples
    /atelier/recorder rid target tap seconds armed
    /atelier/spatial  n layout
    /atelier/panic
    /atelier/reseed  rootSeed

Engine -> controller, handled by :class:`OSCBridge.on_message`:
    /atelier/ready
    /atelier/analysis  source feature value ...
    /atelier/meters    f0 f1 ...
    /atelier/cpu       avg peak nodeCount
    /atelier/status    text
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable


def _finite(x: Any, default: float = 0.0) -> float:
    """Coerce a telemetry value to a finite float. SpecCentroid/Amplitude on
    silence or unstable audio can return NaN/Inf, which would otherwise poison
    the snapshot JSON (NaN is not JSON-compliant)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default

try:
    from pythonosc.udp_client import SimpleUDPClient
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    _HAVE_OSC = True
except Exception:  # pragma: no cover - optional at import time
    _HAVE_OSC = False


class OSCBridge:
    def __init__(self, sc_host: str = "127.0.0.1", sc_port: int = 57130,
                 listen_host: str = "0.0.0.0", listen_port: int = 57140):
        self.sc_host = sc_host
        self.sc_port = sc_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.client: Any = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, list[Callable]] = {}
        # latest analysis features for the modulation engine's analysis followers
        self.analysis: dict[str, float] = {}
        self.meters: list[float] = []
        self.cpu = {"avg": 0.0, "peak": 0.0, "nodes": 0}
        # Liveness is a heartbeat: the engine is "connected" if we have seen any
        # telemetry recently. (Relying on the one-shot /atelier/ready breaks when
        # the controller restarts after the engine has already booted.)
        self._last_rx = 0.0
        self.heartbeat_timeout = 4.0

    def _mark_rx(self) -> None:
        self._last_rx = time.monotonic()

    @property
    def connected(self) -> bool:
        return (time.monotonic() - self._last_rx) < self.heartbeat_timeout

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if not _HAVE_OSC:
            return
        try:
            self.client = SimpleUDPClient(self.sc_host, self.sc_port)
        except Exception:
            self.client = None
        disp = Dispatcher()
        disp.map("/atelier/ready", self._on_ready)
        disp.map("/atelier/analysis", self._on_analysis)
        disp.map("/atelier/meters", self._on_meters)
        disp.map("/atelier/cpu", self._on_cpu)
        disp.map("/atelier/status", self._on_status)
        try:
            self._server = ThreadingOSCUDPServer((self.listen_host, self.listen_port), disp)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        except Exception:
            self._server = None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def on(self, addr: str, fn: Callable) -> None:
        self._handlers.setdefault(addr, []).append(fn)

    def _emit(self, addr: str, *args) -> None:
        for fn in self._handlers.get(addr, []):
            try:
                fn(*args)
            except Exception:
                pass

    # -- inbound ----------------------------------------------------------- #
    def _on_ready(self, _addr, *_a):
        self._mark_rx()
        self._emit("ready")

    def _on_analysis(self, _addr, *args):
        self._mark_rx()
        # args = source, feature, value, source, feature, value, ...
        for i in range(0, len(args) - 2, 3):
            try:
                v = _finite(args[i + 2])
                self.analysis[f"{args[i]}.{args[i+1]}"] = v
                self.analysis[str(args[i + 1])] = v
            except Exception:
                pass

    def _on_meters(self, _addr, *args):
        self._mark_rx()
        self.meters = [_finite(a) for a in args]
        self._emit("meters", self.meters)

    def _on_cpu(self, _addr, avg=0.0, peak=0.0, nodes=0):
        self._mark_rx()
        self.cpu = {"avg": _finite(avg), "peak": _finite(peak), "nodes": int(_finite(nodes))}
        self._emit("cpu", self.cpu)

    def _on_status(self, _addr, text=""):
        self._emit("status", text)

    # -- outbound (no-op if SC unreachable) -------------------------------- #
    def send(self, addr: str, *args) -> None:
        if self.client is None:
            return
        try:
            self.client.send_message(addr, list(args))
        except Exception:
            pass

    def boot(self, sr: int, block: int, channels: int) -> None:
        self.send("/atelier/boot", sr, block, channels)

    def module_new(self, mid: str, mtype: str, channels: int, nodes: int, lane_index: int) -> None:
        self.send("/atelier/module/new", mid, mtype, channels, nodes, lane_index)

    def module_free(self, mid: str) -> None:
        self.send("/atelier/module/free", mid)

    def module_nodes(self, mid: str, count: int) -> None:
        self.send("/atelier/module/nodes", mid, count)

    def module_bypass(self, mid: str, on: bool) -> None:
        self.send("/atelier/module/bypass", mid, 1 if on else 0)

    def module_wet(self, mid: str, wet: float) -> None:
        self.send("/atelier/module/wet", mid, float(wet))

    def set_param(self, mid: str, pname: str, node: int, value: float) -> None:
        self.send("/atelier/set", mid, pname, node, float(value))

    def set_vector(self, mid: str, pname: str, values: list[float]) -> None:
        self.send("/atelier/setvec", mid, pname, *[float(v) for v in values])

    def feedback(self, src: str, dst: str, armed: bool, gain_limit: float, delay: int) -> None:
        self.send("/atelier/feedback", src, dst, 1 if armed else 0, float(gain_limit), int(delay))

    def recorder(self, rid: str, target: str, tap: str, seconds: float, armed: bool) -> None:
        self.send("/atelier/recorder", rid, target, tap, float(seconds), 1 if armed else 0)

    def spatial(self, n: int, layout: str) -> None:
        self.send("/atelier/spatial", n, layout)

    def panic(self) -> None:
        self.send("/atelier/panic")

    def reseed(self, root_seed: int) -> None:
        self.send("/atelier/reseed", root_seed)

    def ping(self) -> None:
        """Ask the engine to re-announce readiness (re-handshake)."""
        self.send("/atelier/ping")

    def graph(self, order: list[str], edges: list[tuple[str, str]],
              terminals: list[str]) -> None:
        """Push the full routing graph and commit it atomically."""
        self.send("/atelier/graph/order", *order)
        flat: list[str] = []
        for s, d in edges:
            flat += [s, d]
        self.send("/atelier/graph/edges", *flat)
        self.send("/atelier/graph/terminals", *terminals)
        self.send("/atelier/graph/commit")
