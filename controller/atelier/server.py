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
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .osc_bridge import OSCBridge
from .persistence import load_patch, patch_from_dict, patch_to_dict, save_patch
from .snapshot import full_snapshot
from .state import StateManager

SESSION_SECRET = os.environ.get("SESSION_SECRET", "wildrider-dev-secret-change-me")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_DIR = Path(os.environ.get("ATELIER_DATA", "/data"))
PATCH_DIR = DATA_DIR / "patches"
STREAM_DIR = DATA_DIR / "stream"      # HLS playlist + segments written by the SC container
STREAM_DIR.mkdir(parents=True, exist_ok=True)
REC_DIR = DATA_DIR / "recordings"     # CD-quality WAV captures (written by the SC container)
REC_DIR.mkdir(parents=True, exist_ok=True)

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


# --------------------------------------------------------------------------- #
# Recorder — CD-quality WAV capture. The bytes are written inside the SC
# container (which owns JACK); here we only choose the timestamped filename,
# trigger start/stop over OSC, and serve the resulting files.
# --------------------------------------------------------------------------- #
REC = {"active": False, "name": None, "started": 0.0}
_REC_RE = re.compile(r"^rec_\d{8}_\d{6}\.wav$")


def _rec_state() -> dict:
    return {"active": REC["active"], "name": REC["name"],
            "started": REC["started"], "elapsed": (time.time() - REC["started"]) if REC["active"] else 0.0}


def _start_recording() -> dict:
    if REC["active"]:
        return _rec_state()
    name = "rec_" + time.strftime("%Y%m%d_%H%M%S") + ".wav"
    REC.update(active=True, name=name, started=time.time())
    bridge.send("/atelier/record/start", str(REC_DIR / name))
    hub.push({"type": "record", **_rec_state()})
    return _rec_state()


def _stop_recording() -> dict:
    if not REC["active"]:
        return _rec_state()
    bridge.send("/atelier/record/stop")
    REC.update(active=False, name=None, started=0.0)
    st = _rec_state()
    hub.push({"type": "record", **st})
    return st


@app.on_event("startup")
async def _startup() -> None:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    # Application DB (users / login wall). Run the blocking setup off the loop.
    await asyncio.to_thread(db.init_db)
    await asyncio.to_thread(db.seed_admin)
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


@app.get("/api/recordings")
async def list_recordings() -> JSONResponse:
    """Recorded WAVs, newest first, with size + download URL."""
    items = []
    for p in REC_DIR.glob("rec_*.wav"):
        try:
            st = p.stat()
        except OSError:
            continue
        # don't list the file that's still being written (current take)
        if REC["active"] and p.name == REC["name"]:
            continue
        items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                      "url": f"/recordings/{p.name}"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse({"recordings": items, "recording": _rec_state()})


@app.get("/recordings/{name}")
async def download_recording(name: str) -> FileResponse:
    """Download a single recording (forced as an attachment)."""
    if not _REC_RE.match(name):
        return JSONResponse({"error": "bad name"}, status_code=400)
    path = REC_DIR / name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="audio/wav", filename=name)


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


@command("clear_patch")
def _c_clear_patch(_d):
    state.clear_patch()


@command("add_connection")
def _c_add_connection(d):
    state.add_connection(d["src"], d["dst"])


@command("remove_connection")
def _c_remove_connection(d):
    state.remove_connection(d["id"])


@command("add_midi_connection")
def _c_add_midi_connection(d):
    state.add_midi_connection(d["src"], d["dst"])


@command("remove_midi_connection")
def _c_remove_midi_connection(d):
    state.remove_midi_connection(d["id"])


@command("seq_clock")
def _c_seq_clock(d):
    state.seq_set_clock(d["module"], tempo=d.get("tempo"), division=d.get("division"),
                        running=d.get("running"), swing=d.get("swing"))


@command("seq_lane")
def _c_seq_lane(d):
    kw = {k: v for k, v in d.items() if k not in ("cmd", "module", "target", "index")}
    state.seq_set_lane(d["module"], d["target"], int(d["index"]), **kw)


@command("seq_randomize")
def _c_seq_randomize(d):
    idx = d.get("index")
    state.seq_randomize(d["module"], target=d.get("target"),
                        index=int(idx) if idx is not None else None)


@command("move_module")
def _c_move_module(d):
    state.move_module(d["module"], float(d["x"]), float(d["y"]))


@command("random_patch")
def _c_random_patch(d):
    a = d.get("amount")
    state.random_patch(float(a) if a is not None else None, style=d.get("style"))


@command("random_chain")
def _c_random_chain(d):
    state.random_chain(list(d.get("types", [])), style=d.get("style"))


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


@command("set_per_module_lfos_enabled")
def _c_set_per_module_lfos_enabled(d):
    state.set_per_module_lfos_enabled(d["module"], bool(d["enabled"]))


@command("set_per_module_lfo")
def _c_set_per_module_lfo(d):
    state.set_per_module_lfo(
        d["module"], d["param"],
        enabled=d.get("enabled") if "enabled" in d else None,
        shape=d.get("shape"),
        rate=float(d["rate"]) if "rate" in d else None,
        depth=float(d["depth"]) if "depth" in d else None,
    )


@command("randomize_per_module_lfos")
def _c_randomize_per_module_lfos(d):
    state.randomize_per_module_lfos(d["module"])


@command("add_scene")
def _c_add_scene(_d):
    state.add_scene()


@command("capture_scene")
def _c_capture_scene(d):
    state.capture_scene(d.get("id"), d.get("name", ""))


@command("load_scene")
def _c_load_scene(d):
    state.load_scene(d["id"], float(d.get("morph", 0.0)))


@command("remove_scene")
def _c_remove_scene(d):
    state.remove_scene(d["id"])


@command("mutate")
def _c_mutate(d):
    state.mutate(float(d.get("amount", 0.3)), bool(d.get("expert", False)))


@command("randomize")
def _c_randomize(d):
    state.randomize(float(d.get("amount", 0.3)), scope=d.get("scope", "global"),
                    mid=d.get("module"), pid=d.get("param"), node=d.get("node"),
                    expert=bool(d.get("expert", False)), style=d.get("style"))


@command("macro")
def _c_macro(d):
    state.set_macro(d["id"], float(d["value"]))


@command("macro_count")
def _c_macro_count(d):
    state.set_macro_count(int(d["count"]))


@command("macro_targets")
def _c_macro_targets(d):
    state.set_macro_targets(d["id"], int(d["count"]))


@command("macro_randomize")
def _c_macro_randomize(_d):
    state.randomize_macros()


@command("lfos_enabled")
def _c_lfos_enabled(d):
    state.set_lfos_enabled(bool(d["enabled"]))


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


@command("rewire_patch")
def _c_rewire_patch(d):
    state.rewire_patch(style=d.get("style"))


@command("record_start")
def _c_record_start(_d):
    _start_recording()


@command("record_stop")
def _c_record_stop(_d):
    _stop_recording()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not ws.session.get("uid"):          # auth wall covers the control channel too
        await ws.close(code=1008)
        return
    await ws.accept()
    hub.clients.add(ws)
    snap = full_snapshot(state)
    snap["recording"] = _rec_state()
    await ws.send_json({"type": "snapshot", "snapshot": snap})
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


# --------------------------------------------------------------------------- #
# Authentication (login wall). Session is a signed cookie; users live in Postgres.
# --------------------------------------------------------------------------- #
def _login_page(error: str = "") -> HTMLResponse:
    err = f'<p class="err">{error}</p>' if error else ""
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Wildrider — sign in</title><style>
*{{box-sizing:border-box}}body{{margin:0;height:100vh;display:flex;align-items:center;
justify-content:center;background:#14171c;color:#d7dee8;
font:14px/1.5 ui-monospace,"SF Mono",Menlo,monospace}}
.card{{background:#1d222b;border:1px solid #2c3340;border-radius:10px;padding:28px 26px;width:320px}}
.brand{{font-weight:700;letter-spacing:3px;color:#6fa8dc;font-size:20px}}
.sub{{color:#8b97a8;font-size:10px;letter-spacing:1px;margin-bottom:18px}}
label{{display:block;font-size:11px;color:#8b97a8;text-transform:uppercase;letter-spacing:1px;margin:12px 0 4px}}
input{{width:100%;padding:9px 10px;background:#232a35;border:1px solid #2c3340;border-radius:6px;color:#d7dee8;font:inherit}}
input:focus{{outline:none;border-color:#6fa8dc}}
button{{width:100%;margin-top:18px;padding:10px;background:#6fa8dc;color:#000;border:0;border-radius:6px;font-weight:700;cursor:pointer}}
button:hover{{background:#8bb9e3}}
.err{{color:#e06a6a;font-size:12px;margin:12px 0 0}}
</style></head><body>
<form class="card" method="post" action="/login">
  <div class="brand">WILDRIDER</div><div class="sub">electroacoustic workbench</div>
  <label for="u">username</label><input id="u" name="username" autofocus autocomplete="username"/>
  <label for="p">password</label><input id="p" name="password" type="password" autocomplete="current-password"/>
  {err}
  <button type="submit">Sign in</button>
</form></body></html>"""
    return HTMLResponse(html, status_code=200 if not error else 401)


@app.get("/login")
async def login_get(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    user = await asyncio.to_thread(db.authenticate, username, password)
    if not user:
        return _login_page("Invalid username or password.")
    request.session.update({"uid": user.id, "uname": user.username, "admin": user.is_admin})
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/api/me")
async def whoami(request: Request) -> JSONResponse:
    return JSONResponse({"username": request.session.get("uname"),
                         "is_admin": bool(request.session.get("admin"))})


# Paths reachable without a session. Everything else requires login.
_PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico"}


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or request.session.get("uid"):
        return await call_next(request)
    # Unauthenticated: APIs/assets get a hard 401; navigations go to the login page.
    if path.startswith(("/api", "/hls", "/recordings", "/static", "/ws")):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# SessionMiddleware is added LAST so it is the OUTERMOST layer — it populates
# request.session before the auth guard (and the websocket handler) reads it.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   same_site="lax", https_only=False, max_age=14 * 24 * 3600)
