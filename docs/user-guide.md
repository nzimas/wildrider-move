# Wildrider for Move — User Guide

Wildrider turns your Ableton Move into a standalone electroacoustic instrument.
There is no computer, no browser, no app — the Move's **32 pads**, **nine
encoders**, **screen**, and **transport / track / step buttons** are the whole
interface. This guide walks you through playing it.

> New to the architecture or curious how it runs on the device? See the
> [README](../README.md). For the multicore audio engine, see
> [docs/supernova.md](supernova.md).

---

## 1. The hardware at a glance

```
   ┌───────────────────────────── screen ─────────────────────────────┐
   │  parameter bars · module names · live CPU · morph & perf browsers │
   └───────────────────────────────────────────────────────────────────┘

   [E1] [E2] [E3] [E4]   [E5] [E6] [E7] [E8]            (◎ master volume)
   density pitch cut res  morph  ── macros ──              (◎ jog wheel)

   ┌──── 8 × 4 pad grid ────┐        Track 1  Track 2  Track 3  Track 4
   │  row 1   ● ● ● ● ● ● ● ● │        new      rewire   scenes   sampler
   │  row 2   ● ● ● ● ● ● ● ● │
   │  row 3   ● ● ● ● ● ● ● ● │        Play  Rec  Shift  Menu  Back  ← →  X
   │  row 4   ● ● ● ● ● ● ● ● │
   └────────────────────────┘        [ 16 step buttons ]
```

- **Pads** are numbered 0–31, left-to-right, top-to-bottom (row 1 = 0–7, row 2 =
  8–15, row 3 = 16–23, row 4 = 24–31).
- **A "long press" is ≥ 0.4 s.** Many pads and track buttons do one thing on a
  short tap and another on a long hold.
- **Modifier buttons** — **Shift**, **Play**, **Rec**, **X (Delete)** — change what
  the *next* pad/encoder does while you hold them.

---

## 2. First sound in 30 seconds

1. Launch **Wildrider** from the Move's Schwung menu (overtake runners).
2. Press **Track 1** — this generates a fresh, guided-random patch. Modules light
   up across rows 1 and 2.
3. **The patch starts silent.** Press **Play** to un-mute it. Press **Play** again
   to silence.
4. Turn **Encoder 1 (Density)** and **Encoder 2 (Pitch)** to shape the generators.
   Turn **Encoders 3 / 4** for the global filter.
5. Don't like it? **Track 1** again for a new patch, or **Track 2** to rewire the
   one you have.

That's the loop. Everything below is depth.

---

## 3. The views

Wildrider has **seven views**. Only one owns the pad grid at a time. You switch
with the track buttons and Menu; two views (Chains, Morph-time) are modal editors
that take over until you leave them.

| View | Enter with | What it's for |
|---|---|---|
| **Patch** (default) | — (launch default) | Play modules, browse & assign the catalog, macros, LFOs |
| **Scenes** | **Track 3** | Store & recall up to 32 snapshots, with morphing |
| **Transformers** | **Track 4** | Generate CDP + Csound variations into free sample slots |
| **Recorder** | **Shift + Track 4** | Long-form performance recorder — 8 takes per project, downloadable at move.local:7180 |
| **Performances** | **Menu** | Save / load / delete whole projects (32 slots) |
| **Chains** (modal) | **Shift + Track 1** | Build a patch by hand from the module list |
| **Morph-time** (modal) | **Shift + Track 3** | Set the scene-morph duration (1–99 s) |

Pressing the same track button again returns to Patch. **Back** arms exit
("EXIT YES?"); **push the jog wheel** to confirm leaving Wildrider, or press
**Back** again to cancel.

---

## 4. Patch view — the heart of the instrument

The grid splits into two halves:

```
 rows 1–2  →  the MODULE CANVAS   (your live patch)
 rows 3–4  →  the MODULE BROWSER  (the palette you build from)
```

### The canvas (rows 1 & 2, pads 0–15)

Row 1 holds **generators** (sound sources); row 2 holds **processors** (effects).

| Gesture | Action |
|---|---|
| **Tap** a lit pad | Toggle that module **on / off** (mute) |
| **Long-press** a pad | Show the module's name on screen (no toggle) |
| **Tap an empty pad** | Grow the patch — add a random generator (row 1) or effect (row 2) |
| **Play + empty pad** | Add **RINGS** · **Rec + empty** → **FMTONE** · **Play+Rec + empty** → **WAVIARY** |
| **Shift + pad** | Randomize that module's parameters |
| **X (Delete) + pad** | Remove that module |
| **Hold a pad + turn the jog wheel** | Set that module's level (0–2×) |
| **Hold a generator pad + E1 / E2** | Density / pitch for *that generator only* |

If the audio core is saturated, adding is refused and the screen flashes
**"CPU LIMIT – free a slot."**

### The browser / palette (rows 3 & 4, pads 16–31)

Row 3 is the **generator palette**, row 4 the **processor palette**. Use **← / →**
to scroll when there are more modules than pads (the edge pad tints cyan when more
are hidden).

| Gesture | Action |
|---|---|
| **Tap a generator palette pad** | **Audition** it — it self-sounds *and speaks its name*; tap again to stop |
| **Tap a processor palette pad** | Hear its **name spoken** (the device says it aloud) |
| **Hold a palette pad + tap a canvas slot** | **Assign / replace** — a held generator drops into a row-1 slot, a held processor into a row-2 slot |

The auditioning item glows brightly so you can always see what you're hearing.

### Encoders (Patch view)

| Encoder | Function |
|---|---|
| **E1** | **Density** — scales every generator's internal clock (bipolar, ±). Hold a generator pad to scope it to that one. |
| **E2** | **Pitch shift** — transpose the generators (bipolar, up to ±24 semitones). Per-module when a gen pad is held. |
| **E3 / E4** | **Master filter** — cutoff / resonance (global lowpass). |
| **E5** | **Morph macro** — bipolar "morph everything"; nudges every patch parameter along a persistent random direction. Touch it first to re-centre and snapshot the baseline. |
| **E6 / E7 / E8** | **Macro bank** — each drives many destinations at once. |
| **Master volume** | Left to the Move's own host volume. |
| **Jog wheel** | Per-module level (hold a pad first). |

Touching any encoder pops its bar onto the screen without changing the value.

### Step buttons (Patch view) = the 16 LFOs

The 16 step buttons are a bank of global LFOs.

| Gesture | Action |
|---|---|
| **Tap a step** | Toggle that LFO on / off (lit yellow = on) |
| **Shift + step** | Re-randomize that LFO (keeps it on) |
| **Shift + touch the volume knob + step 1** | Randomize **all** LFOs at once |

### Track & transport buttons (Patch view)

| Button | Short press | Long press / modified |
|---|---|---|
| **Track 1** | New guided-random patch | **Shift** → Chains (manual build) |
| **Track 2** | Rewire the patch | **Long** → rewire **and** re-randomize all params |
| **Track 3** | Scenes view | **Shift** → Morph-time editor |
| **Track 4** | Transformers view | **Shift** → Recorder view |
| **Play** | Play / silence the whole patch | Hold = "force RINGS" modifier for empty pads |
| **Rec** | (sampler wiring) | Hold = "force FMTONE" modifier for empty pads |
| **Menu** | Performances view | |
| **Back** | Arm exit → confirm with jog-click | |

---

## 5. Chains — build a patch by hand

**Shift + Track 1** opens the Chains editor (modal). Turn the **jog wheel** to scroll
the full module list, **click the jog** to select a module, use the **top pads 1–8**
to set how many instances, and **Shift + jog-click** to build the chain. **Back**
leaves without building.

---

## 6. Scenes — snapshots with morphing

**Track 3.** The 32 pads are scene slots.

| Gesture | Action |
|---|---|
| **Shift + pad** | Store the current performance into that slot |
| **Tap a filled pad** | Recall it — Wildrider **morphs** from the current state into the stored one (gapless) |

Stored slots glow blue; the currently loaded scene is white; a morph destination
shows violet. To set the morph duration, use **Shift + Track 3** (Morph-time
editor): turn the jog to scan 1–99 s, click to confirm.

---

## 7. Transformers — generate variations from the mix

**Track 4.** The grid dims to a cool grey wash so you always know you're here.
Rows 1–3 (24 pads) are the Transformers' **own sample bank** — **entirely separate**
from the Recorder's; takes and variations never mix. The **bottom-left pad** (the
red one) is the generator.

- **Tap the red pad** → Wildrider captures a live snippet and spawns **16
  variations into the free slots** of rows 1–3 — **8 from CDP** (phase-vocoder /
  waveset) then **8 from Csound** (ATS resynthesis, LPC formants, spectral
  morphing, modal resonators), back to back. Occupied slots are never overwritten;
  the pad flashes bright red while the job runs.
- **While a job runs** the filling slots (and the generator pad) **breathe** fast
  and gently — a soft bright↔dim pulse, not a hard blink; once it finishes they go
  **steady** — freshly generated content sits **bright green** until you audition
  it, then reverts to the normal colour.
- **A playing slot breathes gently at 120 BPM**, so you can see what's sounding.
- **Shift + pad selects a slot for editing** — it turns solid **red** for contrast.

The slot gestures, per-slot encoder editing, and armed insert FX are identical to
the Recorder (below).

---

## 8. Recorder — capture full performances (for publishing)

**Shift + Track 4.** A **long-form performance recorder**: it captures the master
output straight to a stereo WAV on disk (up to 10 minutes), so you can record a
whole take and publish it. Each **project has its own 8 recording slots** (row 1,
the 8 leftmost pads) — the active project is whichever you last loaded or saved.

| Gesture | Action |
|---|---|
| **Tap an empty slot** | Start recording the master output |
| **Tap the recording slot** | Stop the take (it's saved to disk) |
| **Tap a filled slot** | Play it back (audition) |
| **X (Delete) + pad** | Delete that recording |

A recording slot **breathes red**; a playing slot **breathes green**; a slot that
holds a take is **steady green**. The screen shows the active project, elapsed time
while recording, and the download address.

### Getting recordings off the device

Recordings download from a small **web page** the instrument serves on your
network:

```
http://move.local:7180
```

Open it from any phone or laptop on the same Wi-Fi. It lists every project that has
recordings; each row has **▶ Play** (streams in the browser), **Download** (the
WAV), and **Del**. This is the intended path for publishing a performance.

---

## 9. Performances — whole-project save/load

**Menu.** A *performance* is an entire project: the patch, all scenes, modulation,
samples, and macros. The 32 pads are project slots.

| Gesture | Action |
|---|---|
| **Shift + pad** | Save the current project into that slot |
| **Tap a filled pad** | Load that project |
| **X (Delete) + pad** | Delete it |

Filled slots glow green; the last saved/loaded slot is white.

---

## 10. LED colour legend

| Where | Colour | Meaning |
|---|---|---|
| Canvas | bright vs dim | module **on** vs **off**; each generator has its own identity hue |
| Canvas | white | a module with no engine colour, currently off |
| Palette | dim colour / cyan edge | available module / "more hidden this way" |
| Palette | bright glow | currently auditioning |
| Step buttons | yellow | LFO on (Patch) |
| Step buttons | amber | armed insert FX (Recorder / Transformers) |
| Scenes | blue / white / violet | stored / loaded / morph destination |
| Performances | green / white | saved / last used |
| Transformers | grey wash | you're in the Transformers view (its own slot bank) |
| Transformers | steady white/green | a filled slot (green = fresh, unauditioned) |
| Transformers | gentle breathe, 120 BPM | a **playing** slot (soft bright↔dim, not on/off) |
| Transformers | fast gentle breathe | a job is generating |
| Transformers | solid red | selected-for-edit (high contrast) |
| Recorder | red breathe / green breathe / steady green | recording / playing / holds a take (8 slots, row 1) |
| Screen | "CPU LIMIT" | audio core saturated; adds refused |

---

## 11. The module catalog

### Generators (17)

`FMTONE` · `FM7` · `BUCHLOID` · `MOLLY` · `BEN` · `NOIZEOP` · `CHAOS` · `ICARUS` ·
`PLAITS` · `SHAKER` · `MEMBRANE` · `PLUCK` · `TUBE` · `WTABLE` · `BYTEBEAT` ·
`RINGS` · `WAVIARY`

FMTONE is a bespoke Digitone-style 4-operator FM voice; RINGS is a
modal/sympathetic-string resonator (and is the one module that also works as an
effect); WAVIARY is a morphing-wavetable voice. The rest are the "poundhard fleet"
of clock-articulated character voices. Each generator runs its own drifting
internal clock, which Density (E1) and Pitch (E2) steer.

### Processors (22)

`PITCH` · `TIME` · `COMB` · `GAIN` · `SDLY` · `VERB` · `CLOUDS` · `GRAINS` ·
`RINGS` · `ENV` · `GATE` · `DISTORT` · `OVERDRIVE` · `AMPSIM` · `EQUALIZER` ·
`FLANGER` · `PHASER` · `RINGMOD` · `BITCRUSHER` · `LOFI` · `TREMOLO` · `WAVEFOLDER`

`VERB` is a lush algorithmic reverb, `PHASER` an authentic 1970s string-machine
phaser, `CLOUDS`/`GRAINS` granular processors. `DISTORT` is level-compensated so
its wet/dry actually blends.

---

## 12. The engine (supernova, multicore)

Wildrider runs on the **multithreaded `supernova` server by default**, spreading
each patch across all four of the Move's cores — roughly 1.5× the module capacity
of the old single-core engine, with fewer XRuns under load. There's nothing to
turn on.

If you ever need the original single-core `scsynth` engine (for example to
A/B a suspected engine issue), change the default in `move/run-engine.sh` from
`3` to `0` — a thread count below 1 selects scsynth — and relaunch.

Full details, tuning, and the one caveat (coexistence with the Move's own audio)
are in [docs/supernova.md](supernova.md).

---

## 13. Quick troubleshooting

| Symptom | Fix |
|---|---|
| **No sound after generating a patch** | The patch starts **silent** — press **Play**. |
| **"CPU LIMIT – free a slot"** | The audio core is maxed. Delete a module (X + pad) or generate a lighter patch. |
| **Generators barely audible** | Raise **Density (E1)**; hold a gen pad + jog to raise its level. |
| **A processor does nothing** | Make sure a generator feeds it — try **Track 2** (rewire) or reassign from the palette. |
| **Wildrider isn't in the Schwung menu after a Move update** | A Move OS auto-update can wipe the overtake hook — re-run the post-update step (see README). |
