"""FastAPI control layer — the web UI listening on localhost:8099.

Serves the static control surface, exposes a websocket for live state sync and
commands, runs the control-rate modulation loop as a background task, and proxies
the live audio stream from the SuperCollider container to the browser (since
Docker-on-macOS cannot reach CoreAudio, the engine renders to a JACK sink that is
encoded and streamed over the network).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .osc_bridge import OSCBridge
from .persistence import load_patch, patch_from_dict, patch_to_dict, save_patch
from .snapshot import full_snapshot
from .state import StateManager

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_DIR = Path(os.environ.get("ATELIER_DATA", "/data"))
PATCH_DIR = DATA_DIR / "patches"
STREAM_DIR = DATA_DIR / "stream"      # HLS playlist + segments written by the SC container
STREAM_DIR.mkdir(parents=True, exist_ok=True)

SC_HOST = os.environ.get("SC_HOST", "127.0.0.1")
SC_PORT = int(os.environ.get("SC_PORT", "57130"))
STREAM_URL = os.environ.get("SC_STREAM_URL", "http://127.0.0.1:8200")
CONTROL_RATE = float(os.environ.get("ATELIER_CONTROL_RATE", "60"))  # Hz

app = FastAPI(title="Wildrider-style Electroacoustic Instrument")

bridge = OSCBridge(sc_host=SC_HOST, sc_port=SC_PORT)
state = StateManager(bridge=bridge)


class Hub:
    """Fan-out of state events to all connected websocket clients."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def push(self, event: dict) -> None:
        if not self.loop:
            return
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(self._send(ws, event), self.loop)

    async def _send(self, ws: WebSocket, event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:
            self.clients.discard(ws)


hub = Hub()


@app.on_event("startup")
async def _startup() -> None:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    hub.loop = asyncio.get_running_loop()
    state.subscribe(hub.push)
    bridge.start()
    state.init_default()
    # Rebuild the DSP graph whenever the engine (re)boots: the controller may
    # start before scsynth is ready, and module/new is idempotent server-side.
    bridge.on("ready", lambda *_: (state.build_graph(),
                                   hub.push({"type": "engine", "connected": True,
                                             "cpu": bridge.cpu, "cpu_warn": state.cpu_warn,
                                             "meters": bridge.meters, "analysis": bridge.analysis})))
    state.build_graph()
    asyncio.create_task(_modulation_loop())
    asyncio.create_task(_meter_loop())


async def _modulation_loop() -> None:
    period = 1.0 / CONTROL_RATE
    while True:
        t0 = time.monotonic()
        try:
            state.tick_modulation()
        except Exception:
            pass
        await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))


async def _meter_loop() -> None:
    """Push lightweight engine telemetry at a modest rate (section 10: control
    processing must never depend on heavy visualisation update rates)."""
    while True:
        hub.push({"type": "engine", "connected": bridge.connected,
                  "cpu": bridge.cpu, "cpu_warn": state.cpu_warn,
                  "meters": bridge.meters, "analysis": bridge.analysis})
        await asyncio.sleep(0.1)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(full_snapshot(state))


@app.get("/api/patches")
async def list_patches() -> JSONResponse:
    return JSONResponse(sorted(p.stem for p in PATCH_DIR.glob("*.json")))


def _patch_name(raw: str) -> str:
    """Normalize patch name by stripping a trailing .json extension."""
    return raw[:-5] if raw.lower().endswith(".json") else raw


@app.post("/api/patch/save")
async def api_save(body: dict) -> JSONResponse:
    name = _patch_name(body.get("name", state.patch.name or "untitled"))
    state.patch.name = name
    save_patch(state, str(PATCH_DIR / f"{name}.json"))
    return JSONResponse({"ok": True, "name": name})


@app.post("/api/patch/load")
async def api_load(body: dict) -> JSONResponse:
    name = _patch_name(body["name"])
    load_patch(state, str(PATCH_DIR / f"{name}.json"))
    hub.push({"type": "reload", "snapshot": full_snapshot(state)})
    return JSONResponse({"ok": True})


@app.get("/api/stream/status")
async def stream_status() -> JSONResponse:
    """Whether the HLS playlist is live (segments being written)."""
    pl = STREAM_DIR / "atelier.m3u8"
    live = pl.exists() and (time.time() - pl.stat().st_mtime) < 5.0
    return JSONResponse({"live": live, "url": "/hls/atelier.m3u8"})


# --------------------------------------------------------------------------- #
# WebSocket command channel
# --------------------------------------------------------------------------- #
COMMANDS = {}


def command(name: str):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


@command("set_param")
def _c_set_param(d):
    state.set_param(d["module"], d["param"], d.get("node"), float(d["value"]))


@command("set_param_norm")
def _c_set_param_norm(d):
    slot = state.patch.find_slot(d["module"], d["param"], d.get("node"))
    if slot:
        state.set_param(d["module"], d["param"], d.get("node"),
                        slot.meta.to_value(float(d["value"])))


@command("set_lock")
def _c_set_lock(d):
    state.set_lock(d["module"], d["param"], d.get("node"), bool(d["locked"]))


@command("add_module")
def _c_add_module(d):
    state.add_module(d["type"], int(d.get("nodes", 1)),
                     x=float(d.get("x", 60)), y=float(d.get("y", 60)))


@command("remove_module")
def _c_remove_module(d):
    state.remove_module(d["module"])


@command("add_connection")
def _c_add_connection(d):
    state.add_connection(d["src"], d["dst"])


@command("remove_connection")
def _c_remove_connection(d):
    state.remove_connection(d["id"])


@command("move_module")
def _c_move_module(d):
    state.move_module(d["module"], float(d["x"]), float(d["y"]))


@command("random_patch")
def _c_random_patch(d):
    a = d.get("amount")
    state.random_patch(float(a) if a is not None else None)


@command("random_chain")
def _c_random_chain(d):
    state.random_chain(list(d.get("types", [])))


@command("set_nodes")
def _c_set_nodes(d):
    state.set_node_count(d["module"], int(d["count"]))


@command("bypass")
def _c_bypass(d):
    state.set_bypass(d["module"], bool(d["on"]))


@command("wet")
def _c_wet(d):
    state.set_wet(d["module"], float(d["value"]))


@command("add_lane")
def _c_add_lane(d):
    state.add_lane(d["id"], d["name"], d.get("mode", "serial"))


@command("add_mod_source")
def _c_add_mod_source(d):
    kw = {k: v for k, v in d.items() if k not in ("cmd", "id", "type", "label")}
    state.add_mod_source(d["id"], d["type"], label=d.get("label", d["id"]), **kw)


@command("add_mod_route")
def _c_add_mod_route(d):
    state.add_mod_route(d["id"], d["source"], d["module"], d["param"],
                        scope=d.get("scope", "allDecorrelated"),
                        depth=float(d.get("depth", 0.5)),
                        polarity=int(d.get("polarity", 1)))


@command("remove_mod_route")
def _c_remove_mod_route(d):
    state.remove_mod_route(d["id"])


@command("lfo_count")
def _c_lfo_count(d):
    state.ensure_lfos(int(d["count"]))


@command("lfo_set")
def _c_lfo_set(d):
    state.set_lfo(d["id"], shape=d.get("shape"), rate=d.get("rate"),
                  depth=d.get("depth"), target=d.get("target"))


@command("lfo_randomize")
def _c_lfo_randomize(_d):
    state.randomize_lfos()


@command("capture_scene")
def _c_capture_scene(d):
    state.capture_scene(d["id"], d.get("name", d["id"]))


@command("recall_scene")
def _c_recall_scene(d):
    state.recall_scene(d["id"])


@command("morph_scenes")
def _c_morph(d):
    state.morph_scenes(d["a"], d["b"], float(d["t"]))


@command("mutate")
def _c_mutate(d):
    state.mutate(float(d.get("amount", 0.3)), bool(d.get("expert", False)))


@command("randomize")
def _c_randomize(d):
    state.randomize(float(d.get("amount", 0.3)), scope=d.get("scope", "global"),
                    mid=d.get("module"), pid=d.get("param"), node=d.get("node"),
                    expert=bool(d.get("expert", False)))


@command("macro")
def _c_macro(d):
    state.set_macro(d["id"], float(d["value"]))


@command("controller")
def _c_controller(d):
    state.handle_controller(d.get("transport", "osc"), d["selector"],
                            int(d.get("channel", 0)), float(d["value"]))


@command("panic")
def _c_panic(_d):
    state.panic()


@command("reseed")
def _c_reseed(d):
    state.reseed(int(d["seed"]))


@command("reset_transport")
def _c_reset_transport(_d):
    state.reset_transport()


@command("engine_reconnect")
def _c_engine_reconnect(_d):
    state.reconnect_engine()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    await ws.send_json({"type": "snapshot", "snapshot": full_snapshot(state)})
    try:
        while True:
            msg = await ws.receive_json()
            fn = COMMANDS.get(msg.get("cmd"))
            if fn:
                try:
                    fn(msg)
                except Exception as exc:  # surface command errors, keep socket
                    await ws.send_json({"type": "error", "cmd": msg.get("cmd"), "error": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


# --------------------------------------------------------------------------- #
# Static web UI
# --------------------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
# Live audio: HLS playlist + segments written by the SC container to the shared
# volume. Served as static files (decoupled from ffmpeg lifetime).
app.mount("/hls", StaticFiles(directory=str(STREAM_DIR)), name="hls")
