# Wildrider — Move · Handoff notes

This repo is a **fork of the desktop Wildrider** ("eap-mc2"), created to be the
foundation for a version of Wildrider that **takes over the Ableton Move** as its
control/performance surface.

- **Forked from:** `AbsoluteManagement/wildrider` at commit `6f0e9f6`
  (full history is preserved — desktop improvements can be cherry-picked across).
- **Desktop dev continues** in `~/development/eap-mc2` (repo `wildrider`).
  **Move dev happens here** (`~/development/wildrider-move`, repo `wildrider-move`).

> Read this whole file before changing anything. The rest of the codebase is, at
> fork time, byte-identical to desktop Wildrider — nothing Move-specific exists yet.

---

## 1. What Wildrider is (today)

A dockerized **electroacoustic instrument**. Three services (`docker-compose.yml`):

| Service        | Role |
|----------------|------|
| `supercollider`| Headless SuperCollider DSP engine (JACK dummy backend). Builds Mutable-Instruments UGens (Clouds/Rings/Plaits) from source. Streams audio out as HLS (ffmpeg off JACK). |
| `controller`   | Python **FastAPI** app on `:8099` — serves the web UI, holds the authoritative patch/modulation/scene state, runs the 60 Hz control loop, and talks **OSC** to the SC engine. |
| `postgres`     | App DB (users now; the platform's business logic grows here). |

**Audio path:** SC → JACK → ffmpeg → HLS playlist in the shared `/data` volume →
browser `<audio>` (hls.js). **Control path:** browser ⇄ controller over a
websocket; controller ⇄ SC over OSC (`atelier/osc_bridge.py`).

### Run it
```bash
docker compose up -d --build           # supercollider + controller + postgres
# open http://localhost:8099  (redirects to /login)
# login: nzimas  /  d26661e1156d01931fe1e6d6f4868333   (seeded admin)
```
Rebuild after controller changes: `docker compose build controller && docker compose up -d --force-recreate controller`. The web assets are versioned with `?v=N` in `controller/web/index.html` — **bump it on every app.js/style.css change** or the browser caches stale files. Watch for a transient `DeadlineExceeded` on `docker compose build` (retry; it sometimes silently leaves the old image).

### Key code map
- `controller/atelier/catalog.py` — every module is a `ModuleSpec` with `P()` params. Adding a module = catalog spec (`synthdef="lowercasetype"`) + a `("type"++suf)` SynthDef in `supercollider/synthdefs.scd`; the UI is fully catalog-driven (auto-appears).
- `supercollider/synthdefs.scd` / `engine.scd` / `boot.scd` / `dx7.scd` — the DSP. Generic insert wrapper: mono-sum → process → `~spread(pan)` → wet/bypass.
- `controller/atelier/state.py` — `StateManager`: the brain. add/remove/wire modules, params, modulation tick, scenes/morph, macros, SEQ, recorder.
- `controller/atelier/modulation.py` — LFO/mod engine (`ModEngine`, routes, node scopes, deterministic seeds).
- `controller/atelier/scenes.py` — scenes are **full-snapshot clips**; structural-aware morph lives in `state.py` (`_advance_scene_morph`, `_morph_commit`) and streams live frames to the canvas.
- `controller/atelier/seq.py` — Turing-machine MIDI sequencer (clock + note/CC lanes, polymeter).
- `controller/atelier/control.py` — **macros** (a high-level control → many `MacroTarget`s) + MIDI/OSC learn bindings (`LearnBinding`, soft-takeover). **This is the seam the Move maps onto.**
- `controller/atelier/aesthetics.py` — guided ("artist profile") randomization.
- `controller/atelier/db.py` + login wall in `server.py` — Postgres users, PBKDF2, session cookies.
- `controller/web/{index.html,app.js,style.css}` — the single-page UI (no framework). Renders entirely from the server snapshot; drives state over the websocket.

### Feature inventory (all working on desktop)
Modules (catalog): DX7, **MOLLY** (Molly-the-Poly port, lead/pad/perc scope), FBANK,
PITCH, TIME, COMB, GAIN, SDLY, VERB, CLOUDS, **GRAINS** (maximalist granulator),
RINGS, BEN, BUCHLOID, ENV, GATE, PLAITS, DISTORT, + **10 pedal FX** (OVERDRIVE,
AMPSIM, EQUALIZER, FLANGER, PHASER, RINGMOD, BITCRUSHER, LOFI, TREMOLO, WAVEFOLDER),
and **SEQ** (MIDI sequencer). Plus: per-module pan, per-param lock (freezes
randomize+morph+modulation), guided randomization, rewire, scenes + structural
morph, CD-quality WAV recorder, **8-encoder-friendly Macros bank** with per-macro
and block randomizers, an enable/disable-able LFO bank.

---

## 2. The Move target — what "takeover" means

The **Ableton Move** is an ARM64 Linux groovebox: an **8×4 (32) RGB pad grid**, **9
endless encoders** (8 + 1), a small color screen, transport/util buttons, and a
built-in speaker/battery. It runs an embedded Linux ("MoveOS"); there is
community SSH/dev access. "Takeover" = Wildrider drives the Move hardware (pads +
encoders + screen + audio out) instead of (or alongside) the native Move app.

### Three plausible architectures (decide first!)
1. **On-device full stack** — run the SC engine + controller on the Move's ARM
   Linux, output to the Move's audio, drive pads/encoders/screen directly.
   - Pros: standalone instrument. Cons: ARM builds (the SC image already builds
     mi-UGens under qemu — see `supercollider/Dockerfile`; needs an arm64 path),
     CPU/RAM limits, no browser — the web UI must be replaced by a hardware UI.
2. **Move as a control surface** for desktop Wildrider — Move sends MIDI/OSC to
   the existing controller; the `control.py` learn/macro layer already supports
   this. Fastest path to "playable on Move."
3. **Move-native lightweight app** — a trimmed engine + a hardware UI layer, no
   Docker, no browser.

### Natural mappings (Wildrider → Move)
- **8 encoders ↔ the Macros bank** (we just built it: a user-defined set of
  macros, each driving many destinations). This is the headline control surface
  and the reason Macros exist. 1 macro per encoder; the 9th encoder = scene morph
  / page select.
- **32 pads ↔** scene launch (`scenes.py` clips) + module select + SEQ step
  entry (`seq.py` is already step-based). The pad grid maps cleanly to the SEQ
  lanes and the scene bank.
- **Screen ↔** a minimal renderer of the selected module's params / current scene
  (replace `controller/web` with a hardware display layer, or a tiny framebuffer
  UI). The server snapshot already contains everything needed to render.
- **Transport buttons ↔** play/stop (SEQ clock + scene), record (the WAV
  recorder), capture (scene capture).
- **MIDI/OSC ↔** `control.py LearnBinding` already does soft-takeover; route Move
  controls through `handle_controller`.

### First steps (suggested)
1. Pick an architecture (likely start with **#2**: Move as a MIDI/OSC surface, to
   get playing fast, then evolve toward #1).
2. Stand up an **arm64 build** of the stack (test `docker compose` on Apple
   Silicon first; the SC Dockerfile's qemu/mi-UGens build is the risky part).
3. Map the Move's MIDI map (pads/encoders/buttons) → controller commands; the
   Macros + scenes + SEQ already give you the three core surfaces.
4. Replace/augment the browser UI with a screen layer driven by the same snapshot.

### Open questions to resolve early
- How are we accessing the Move (SSH? official SDK? MIDI only?).
- Audio out: on-device JACK vs. native ALSA vs. streaming.
- Does the engine run on-device (CPU budget?) or stays on a host?

---

## 3. Practical notes / gotchas
- Web cache: bump `?v=N` in `index.html` every UI change.
- SC engine gotchas (see desktop memory): SC package names, `QT_QPA_PLATFORM=offscreen`, JACK `shm_size: 1gb`, sclang OSC string args arrive as Symbols (`.asString`), periodic tasks must use **SystemClock** not AppClock (headless), `s.startAliveThread` needed for live CPU/`numSynths`.
- Auth/ports: controller `:8099` (localhost-bound); Postgres in-network; session secret + admin creds are in `docker-compose.yml` env (**replace for any real deployment**; enable `https_only` behind TLS).
- Tests: `cd controller && python -m pytest tests/test_atelier.py -q` (run from the `controller/` dir for imports).

---

## 4. Provenance
Fork point: `wildrider@6f0e9f6` (2026-06). The desktop project keeps evolving in
`eap-mc2`; pull/cherry-pick from it as needed. Keep this file updated as the Move
port takes shape.
