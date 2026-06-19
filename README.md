# Wildrider — Electroacoustic Instrument

A dockerized, semi-modular sound-design workbench in the spirit of **INA-GRM
Tools Wildrider**, implementing the *SuperCollider Modular Sound-Design Synthesizer
Blueprint*. SuperCollider carries the DSP; a Python control layer holds the
authoritative patch state and serves a web control surface on
**http://localhost:8099**. Audio is streamed from the engine to the browser.

This is not a clone of any proprietary product. It reproduces the *experience*
described in the blueprint: modular real-time manipulation, multi-node generators
and processors, a polyadic modulation model, scenes/morphing, multichannel
spatialisation, and deterministic patch recall.

---

## Architecture — five planes

The blueprint insists the system be separated into five planes; they are separate
modules here even though one UI exposes them (`controller/atelier/`):

| Plane | Responsibility | Module |
|-------|----------------|--------|
| **DSP graph** | audio-rate execution, busses, groups, recorders | `supercollider/` (`engine.scd`, `synthdefs.scd`) |
| **Parameter graph** | canonical values + metadata, scaling, clamps, randomize | `params.py`, `catalog.py` |
| **Modulation graph** | polyadic sources/routes, per-node decorrelation, seeds | `modulation.py` |
| **Scene graph** | snapshots, morph, exclusions, constrained mutation | `scenes.py` |
| **Control graph** | macros, pages, MIDI/OSC learn, accessibility | `control.py`, `server.py`, `web/` |

Ordering principle: **the DSP graph is disposable; the patch data model is
authoritative.** On load the engine is built from patch state (`state.build_graph`);
on edit, state updates first, then a graph diff is sent over OSC.

```
 browser ──ws/http──► controller (FastAPI :8099)
                          │  authoritative state, modulation control loop @60Hz
                          │  ──OSC──► supercollider engine (sclang :57120)
                          │  ◄─OSC── meters / analysis / cpu  (:57140)
                          ▼
                      patch JSON (/data/patches)
 browser ◄──MP3 stream── controller ◄──proxy── supercollider ffmpeg (:8200)
```

## The eight modules (catalog-driven)

`PLAY` sampler/looper/resampler · `GEN` generator bank · `BAND` spectral/band
sculpting · `PITCH` pitch-granular delay · `TIME` multi-tap delay lab · `COMB`
resonator/waveguide · `GAIN` movement & presence · `VIZ` metering/scopes. Every
module supports multiple **nodes** (voices/bands/taps/comb lines…). All
parameters are declared once, with full metadata, in `catalog.py`; the UI,
randomizer, modulation targets, save format and validation all derive from it.

## Polyadic modulation (the core idea)

A connection is not a wire — it is a *derived instance* of a source. One source
feeding many destinations (and many nodes) yields independent, decorrelated,
**reproducible** streams via seed derivation:

```
derivedSeed = hash(rootSeed, sourceID, routeID, destinationParamID, nodeID)
```

so reloading a patch reproduces the exact motion. Node scopes (`allDecorrelated`,
`allSame`, `alternating`, `spatiallyWeighted`, …) and depth (−200%…+200% in
normalised parameter space) match the GRM modulation semantics. Modulators sum at
a destination; safety clamps prevent modulation pushing feedback/loudness into
unsafe ranges unless an expert override is armed.

## Run it

```bash
docker compose up -d --build      # detached; build first time only
docker compose logs -f            # follow logs (optional)
# open http://localhost:8099  — click "▶ audio" for the live stream
docker compose down               # stop the stack
```

Use `-d` so the stack runs in the background; `docker compose up --build`
without it stays attached to your terminal. After the first build, plain
`docker compose up -d` is enough (rebuild only when source changes).

- **Multichannel:** set `ATELIER_CHANNELS` (2 / 4 / 8 / N) on the `supercollider`
  service in `docker-compose.yml`. SynthDefs rebuild for the configured count and
  signals spread via `PanAz` for N > 2.
- **Patches** persist to `./data/patches`. Save/load is deterministic; version
  migration is in `persistence.py`.

### Audio on macOS

Docker on macOS cannot reach CoreAudio, so the engine renders against a **JACK
dummy backend** and ffmpeg streams the result as MP3 to the browser (some
latency). The control layer and UI are fully functional regardless of the audio
path — if you want true low-latency monitoring, run SuperCollider natively on the
host and point the controller's `SC_HOST`/`SC_PORT` at it.

## Develop / test the control layer locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r controller/requirements.txt pytest
cd controller
pytest -q                       # 21 tests, blueprint section-13 matrix
uvicorn atelier.server:app --port 8099   # runs UI even with no SC engine
```

The control layer degrades gracefully when SuperCollider is unreachable: OSC
sends become no-ops, so the data model, modulation, scenes and UI all run
headless (useful for CI and design work).

## Test matrix coverage (section 13)

Implemented as automated tests (`controller/tests/test_atelier.py`): patch
determinism & transport-reset reproducibility, safety clamps / expert override,
parameter scaling round-trips & formatters, constrained randomization, scene
morph (numeric + discrete + locks + exclusions), persistence round-trip &
migration, polyadic decorrelation, macro routing, panic. Routing, click-free hot
edits, controller soft-takeover, audio stability and the live audio stream
require the running engine and are verified in-container / on hardware.

## Status vs. the blueprint's MVP path

- **MVP 1 (core audio):** ✔ PLAY/GEN/COMB/GAIN + BAND/PITCH/TIME SynthDefs,
  lanes, macros, deterministic save/load.
- **MVP 2 (modulation):** ✔ all seven modulator types, polyadic per-node seeds,
  gesture/analysis plumbing.
- **MVP 3 (sound-design processors):** ✔ first-version BAND (IIR)/PITCH/TIME;
  FFT/`PV_*` BAND quality modes are a documented refinement.
- **MVP 4 (scene system):** ✔ capture/morph/exclusions/locks/constrained mutation.
- **MVP 5 (viz/control polish):** ✔ meters/analysis, CPU watchdog telemetry,
  high-contrast accessibility, OSC status, MIDI/OSC-learn with soft takeover.

Known first-version simplifications (called out in code comments): serial lane
busing is realized as ordered per-module busses summed to master; BAND uses an
IIR realization rather than full linear-phase FFT; the JACK→MP3 stream chain needs
on-hardware tuning. These do not affect the authoritative data model, which the
blueprint requires to be stable first.
