# Wildrider for Move

A standalone **electroacoustic / experimental instrument that runs entirely on the
Ableton Move**. Wildrider takes over the Move as a **Schwung** *overtake*
runner: a SuperCollider engine and a headless Python control layer run
**on the device itself** (the Move is a Raspberry Pi CM4 / ARM64), and the Move's
8×4 pad grid, nine endless encoders, screen, transport buttons and built-in
speaker become the whole interface. No browser, no Docker, no host computer.

> This is **not** the desktop Wildrider. It was forked from it (see
> [`HANDOFF.md`](HANDOFF.md) for provenance) and shares the engine/catalog/
> modulation heritage, but the web UI and container stack are gone — the surface
> is now Move hardware. The old desktop/Docker instructions do not apply here.

---

## Documentation

| Doc | What |
|---|---|
| [`docs/user-guide.md`](docs/user-guide.md) | **Playing Wildrider** — every view, pad, encoder and gesture, view by view. |
| [`docs/supernova.md`](docs/supernova.md) | **Multicore audio** — the `supernova` engine: why, how it's built, the ParGroup design, realtime scheduling, tuning & troubleshooting. |
| [`HANDOFF.md`](HANDOFF.md) | Fork provenance and desktop-vs-Move history. |

---

## How it runs on the Move

The chosen architecture is an **on-device full stack**:

```
  Move pads / 9 encoders / screen / transport
                 │
                 ▼
  ui.js  (Schwung JS sandbox: draws the screen, reads pads/encoders, lights LEDs)
                 │  share/control.json  ◄─►  share/status.json     (file IPC — the
                 │                                                  sandbox has no UDP)
                 ▼
  headless controller  (Python: authoritative patch/scene/perf state,
                 │       60 Hz modulation loop, macro logic)
                 │  ──OSC──►  sclang :57120   ◄─OSC──  meters / analysis / CPU
                 ▼
  SuperCollider engine  (scsynth :57110)
                 │
                 ▼
  shadow JACK (jackd -R, realtime)  ──►  Schwung shadow mixer  ──►  DAC / speaker
                                          44.1 kHz · 128-sample block
```

- **`ui.js`** is the hardware UI, running in the Schwung overtake sandbox. It can't
  open a socket, so it exchanges state with the controller through JSON files under
  `share/` that the controller polls.
- The **controller** holds all state and drives the engine over OSC, exactly as the
  desktop version did — but headless (no FastAPI/Postgres/web).
- Audio is realtime-scheduled (`jackd -R`) into the Move's shadow-JACK mixer, which
  is the fix for the clicks/pops that a non-RT chain produced. The engine outputs a
  conservative peak so the shadow mixer's post-gain doesn't clip the DAC.

## The control surface

| Control | Role |
|---|---|
| **32 pads** | The module canvas — generators in rows 1 & 3, processors in rows 2 & 4. Also scene launch and the 32-slot sampler. |
| **Encoder 1** | **Density** — scales every generator's internal clock (bipolar; hold a generator pad to scope it to that module). |
| **Encoder 2** | **Pitch shift** — transposes the generators (bipolar, low end floored to avoid sub gargle; per-module when a pad is held). |
| **Encoders 3 / 4** | **Global master filter** — cutoff / resonance. |
| **Encoder 5** | **Morph macro** — bipolar "morph everything": nudges every patch param up/down by a random-but-persistent direction (generator pitch excluded). |
| **Encoders 6–8** | Macro bank (each drives many destinations). |
| **Master encoder** | Main volume. |
| **Step buttons** | The 16-slot global LFO bank; in the sampler view, arm the step-button insert FX. |
| **Transport / track / Shift / Rec** | Scene + performance launch/capture, sampler record, morph-time and page gestures. |
| **Screen** | Bipolar parameter bars, module names, live CPU, the morph editor and the Performances browser. |

Every on-screen bar activates the moment its encoder is touched.

## What's inside

- **Generators** (17, unstable-clock voices): **FMTONE** (a bespoke Digitone-style
  4-operator FM voice), **WAVIARY** (morphing-wavetable), **RINGS** (modal /
  sympathetic-string resonator), plus the clock-articulated "poundhard fleet" —
  FM7, BUCHLOID, MOLLY, BEN, NOIZEOP, CHAOS, ICARUS, PLAITS, SHAKER, MEMBRANE,
  PLUCK, TUBE, WTABLE, BYTEBEAT. Each runs its own drifting internal clock.
- **Processors** (22): PITCH, TIME, COMB, GAIN, SDLY, VERB, CLOUDS, GRAINS, RINGS
  (the one module that is both a generator and an insert), ENV, GATE, DISTORT
  (level-compensated so its wet/dry actually blends), OVERDRIVE, AMPSIM, EQUALIZER,
  FLANGER, **PHASER** (an authentic 1970s string-machine phaser), RINGMOD,
  BITCRUSHER, LOFI, TREMOLO, WAVEFOLDER.
- **On-device module browser** — audition generators and hear processor names
  spoken, then assign them to the canvas (palette rows 3 & 4).
- **CDP variations** — capture a live snippet and fan Composers-Desktop-Project
  transformations across the free sample slots.
- **Guided generative patches** — artist-profile randomization with a CPU budget so
  a generated patch always leaves DSP headroom (and stays processor-forward rather
  than drowning in generators).
- **Scenes** with gapless structural morphing; **Performances** (full project
  save / recall / delete — patch, scenes, samples, macros, master level).
- **Sampler** — 32 slots with per-slot pitch, multi-select, and step-button insert
  FX that reuse the real GATE / DISTORT / COMB / CLOUDS modules.
- **Modulation** — per-module and global LFO banks over a deterministic,
  catalog-driven parameter model.

## Repository layout

| Path | What |
|---|---|
| `supercollider/` | The DSP — `engine.scd`, `synthdefs.scd`, `boot.scd`, `dx7.scd`, `DX7.afx`. |
| `controller/atelier/` | Headless controller: `headless.py` (daemon + control loop), `state.py` (StateManager), `catalog.py` (module specs), `aesthetics.py` (guided randomization), `params.py`, `modulation.py`, `scenes.py`, `seq.py`. |
| `move/schwung-module/wildrider/` | The overtake module: `ui.js` (hardware UI), `module.json` (manifest), `exit-hook.sh`. |
| `move/` | Deploy + run scripts (`deploy-controller.sh`, `deploy-module.sh`, `run-stack.sh`, `run-engine.sh`, `run-controller.sh`), vendored `pythonosc`, and `build/` (the cross-build recipe for the ARM `scsynth` + Mutable-Instruments UGens). |
| `HANDOFF.md` | Original fork / provenance notes. |

## Deploy to a Move

The device must already have the cross-built `scsynth` + UGens + `sclang` installed
under `/data/UserData/wildrider` (see `move/build/`). Access is over SSH as `root`;
the default host is `move.local`.

```bash
# push the engine (.scd), the controller, and the run scripts
move/deploy-controller.sh [host]

# push the Schwung overtake module (ui.js / module.json / exit-hook)
move/deploy-module.sh [host]
```

Then launch **Wildrider** from the Move's Schwung menu (overtake runners). The menu
runs `run-stack.sh`, which brings up realtime `jackd`, the SuperCollider engine
(`run-engine.sh`), and the headless controller (`run-controller.sh`). The stack
daemonizes on the device — it is meant to be launched from the menu, not held open
over an SSH session.

## Notes

- **Runtime env:** engine on `:57110` (scsynth) / `:57120` (sclang), controller
  telemetry `:57140`, control channel `:57150`; 44.1 kHz, 128-sample block, stereo.
  `HOME` is pointed at an Ableton-writable dir so sclang boots from the menu.
- **CPU:** the Move is a shared, load-heavy CM4. Generated patches are DSP-budgeted
  and the audio chain is pinned to SCHED_FIFO to keep XRuns at zero.
- **Multicore (optional):** the DSP engine can switch from single-core `scsynth` to
  the multithreaded **`supernova`** server, spreading a patch across all four cores
  (~1.5× the module capacity, 0 XRuns under load). Opt-in and fully reversible —
  see [`docs/supernova.md`](docs/supernova.md).
- **Recovery:** a Move OS auto-update can wipe the Schwung shim hook — re-run the
  post-update step as root and restart the Move service if the overtake stops
  appearing.
