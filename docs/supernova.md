# Multicore audio on the Move — the `supernova` engine

Wildrider's DSP can run on either of two SuperCollider servers:

| Server | Threads | When |
|---|---|---|
| **supernova** | multithreaded DSP (one thread per core) | **the default** — spreads the patch across the Move's four cores |
| **scsynth** | single-threaded DSP | fallback — the original single-core engine |

This document explains why supernova exists, how it was built, how the audio
graph is restructured to actually use the extra cores, how its DSP threads get
realtime priority on a locked-down device, and how to tune it and troubleshoot it.

> **TL;DR for operators.** Supernova is the default engine — there's nothing to
> turn on. To fall back to single-core scsynth, set `ATELIER_THREADS=0` in
> `run-engine.sh` (see §7). Everything else below is the *why*.

---

## 1. Why

The Move is a Raspberry Pi CM4 — **four ARM64 cores**. `scsynth` runs all of its
DSP on **one** of them. So no matter how many cores are idle, a heavy Wildrider
patch saturates a single core to ~85–90%, misses its 2.9 ms JACK block deadline,
and produces **XRuns** (audible clicks/dropouts). The other three cores sit
unused. A generated patch was DSP-budgeted to ~13 modules precisely to stay under
that single-core wall.

`supernova` is SuperCollider's multithreaded server. It runs a pool of DSP
threads and parallelizes work **across `ParGroup` nodes** — sibling nodes inside
a `ParGroup` may run on different threads simultaneously. The migration's goal:
let independent parts of a patch run on the idle cores, raising the ceiling before
XRuns and removing the single-core bottleneck.

**Result:** the same hardware now runs **19 modules with zero XRuns**, all four
cores at ~60% — roughly 1.5× the capacity of scsynth, with headroom to spare.

---

## 2. Design principles

Three constraints shaped every decision:

1. **Reversible.** supernova lands *alongside* scsynth, never replacing it. A
   single environment variable chooses at boot. The scsynth launch path is byte-for-byte
   untouched, so the proven instrument is always one `rm` away.
2. **Same sound.** A patch must sound identical on both servers (with one
   documented exception — the reverb, see §4).
3. **Surgical.** Match the device's existing SuperCollider 3.11.2 stack exactly,
   so the change is *adding one binary + its plugin variants*, not rebuilding the
   world.

The work came in four milestones (git history: `Phase 1`…`Phase 4`).

---

## 3. Phase 1 — building the binary

Debian/Ubuntu ship `scsynth` but **not** the `supernova` server binary, so it is
cross-compiled from the **matching 3.11.2 source**. Matching the version means the
device's existing `*_supernova.so` plugin ABI is compatible unchanged.

- **Recipe:** [`move/build/Dockerfile.supernova`](../move/build/Dockerfile.supernova).
- Built on an Apple-Silicon Mac via `docker buildx --platform linux/arm64`
  (native arm64 → fast), on `ubuntu:22.04` (glibc 2.35, identical to the device).
- `cmake -DSUPERNOVA=ON -DSYSTEM_BOOST=OFF -DNATIVE=OFF …`, server-only (no
  Qt/IDE/sclang).

Gotchas solved along the way: SC's `git://` submodule URLs (GitHub refuses them —
rewrite to `https://`); `libboost-test-dev` needed by the build; Ubuntu's boost
1.74 asio breaks SC 3.11.2 → use the **bundled boost** (`SYSTEM_BOOST=OFF`, statically
linked so no new runtime libs).

The binary lands at `/data/UserData/wildrider/bin/supernova`.

## 4. Phase 2 — plugin parity (130/130 UGens)

**The critical fact about supernova: it loads only `*_supernova.so` plugin
variants.** A plain scsynth `.so` is invisible to it — a synthdef that references
such a UGen fails with *"… not installed"*, and without the core plugins you even
get *"Control not installed"*. So **every** plugin Wildrider uses needs a
supernova build.

Built against the 3.11.2 source and deployed to `wildrider/plugins/`:

| Plugins | Source | Note |
|---|---|---|
| 24 core UGens (LFUGens, OscUGens, ReverbUGens, …) | SC source, `make` (all) with `SUPERNOVA=ON` | the base set, incl. `Control` |
| MiPlaits / MiRings / MiClouds | [`mi-supernova.cmake`](../move/build/mi-supernova.cmake) | mi-UGens' CMake ignores `SUPERNOVA` (raw `add_library`); the patch clones each scsynth target generically into a `_supernova` target |
| ByteBeat | `midouest/bytebeat` | already uses SC's plugin macro; `-DSUPERNOVA=ON -DTEST=OFF -DCLI=OFF` |
| JPverb / Greyhole | sc3-plugins `Version-3.9.1` | no 3.11.x tag exists; master needs a newer API |
| Stk / DWG / Membrane / … | the device's apt `sc3-plugins` `*_supernova.so` | already present |

### The reverb exception (VERB → GVerb under supernova)

`JPverbRaw` (the UGen behind **VERB**) compiles and its `.so` contains it, but
supernova **will not register it** — the stock apt build fails identically. The
DEIND reverb UGens (JPverb/Greyhole, by Julian Parker) are simply not
supernova-safe in this SC 3.11.2 combination. It is the *only* UGen out of 130
that doesn't survive.

Rather than lose the reverb, **VERB is a server-conditional synthdef**
([`supercollider/synthdefs.scd`](../supercollider/synthdefs.scd), the `verb`
def). `wr-boot.scd` sets `~wrSupernova` *before* the synthdefs compile, and VERB
branches on it:

- **scsynth** → the original `JPverb` path, byte-for-byte unchanged.
- **supernova** → `GVerb` (a supernova-safe core algorithmic reverb), mapped to the
  same knobs: `roomsize ← size`, `revtime ← t60`, `damping ← damp`,
  `inputbw ← earlyDiff`, band-limited by `lowCut`/`highCut`.

The reverb *character* differs slightly under supernova; every other module is
identical on both servers.

## 5. Phase 3 — ParGroups (spreading across cores)

This is the phase that actually uses the extra cores. Before it, supernova ran
Wildrider correctly but on a **single** core — because the whole patch lived in
one serially-ordered group.

### The problem

supernova parallelizes only siblings of a `ParGroup`, **and only when they have no
ordering dependency on each other** (execution order inside a `ParGroup` is
undefined). Wildrider's graph is a free-form DAG of module chains
(`gen → FX → FX → master`), realized in one ordered `~gModules` group with
patch-cord synths summing one module's private output bus into the next's input
bus. Just making `~gModules` a `ParGroup` would run dependent modules in parallel
and race the buses.

### The solution: topological layers

Every module is assigned a **layer**:

```
layer(m) = 0                          if m has no incoming edge (a source)
         = 1 + max(layer(src) …)      over m's incoming edges
```

so every edge runs strictly *low layer → high layer*, and **modules that share a
layer are mutually independent** (an edge between them would force the destination
to a higher layer). Each layer becomes a **`ParGroup`**; the layer ParGroups sit
in creation order inside the still-serial `~gModules`, with the bus-summing cords
placed in that serial parent **right before each destination layer**. Because all
lower layers have finished by the time a layer runs, cross-layer routing stays
**same-block correct** — no added latency.

```
~gModules (serial group)
  busClear × N               ← clear every module input bus (once, at head)
  ParGroup(layer 0)          ← all SOURCES run in parallel  ← the big DSP win
  cord, cord, …              ← edges whose destination is in layer 1
  ParGroup(layer 1)          ← runs in parallel across cores
  cord …
  ParGroup(layer 2)
  …
~gMix → ~gMaster → ~gRec     ← unchanged
```

Only **module groups** run in parallel, and each touches only its **own private
in/out bus** — the bus-summing cords live in the serial parent, never in a
`ParGroup`. So there are **no cross-thread bus races** by construction.

**On scsynth a `ParGroup` is just a plain serial group**, so this restructuring is
behaviourally identical there — the scsynth path is unchanged, verified by node-tree
dump and audio.

Implementation ([`supercollider/engine.scd`](../supercollider/engine.scd)):
`~graphLayers` computes the layers, `~applyLayers` (re)creates the layer ParGroups
and reparents live module groups into them (glitch-free, no synth restart, and it
remembers `~layerOf` for the incremental path). The three graph operations use it:

- **rebuild** (hard commit) and **morph** (gapless crossfade) re-layer wholesale;
- **grow** (fade in one added module) extends the layer stack and drops only the
  new terminal module into its layer, leaving existing cords untouched;
- the sampler-into-patch cord targets the destination module's *layer* ParGroup.

Node-tree proof (a generated patch): the two generators share **layer 0** (one
`ParGroup`), the FX chain forms serial layers 1–4, the terminal sums to master.

## 6. Phase 4 — realtime scheduling (the payoff)

Phase 3 spread the load across cores, but supernova still **XRun'd heavily**
(~80 per 5 s) under load — even at only ~50% per core. It was a **latency**
problem, not throughput: the DSP threads weren't realtime-scheduled, so the OS
scheduler didn't wake them promptly at each 2.9 ms block boundary, and the
parallel join missed the deadline.

### Why the threads weren't realtime

**This device runs everything at `SCHED_OTHER` — the realtime `rtprio` ulimit is 0
and locked** (even for root). The audio chain gets realtime anyway through **file
capabilities**: `jackd` and `scsynth` binaries carry `cap_sys_nice`, which lets
them self-elevate to `SCHED_FIFO` despite the 0 ulimit. When a SuperCollider
server connects to JACK, `libjack` promotes the server's **main callback thread**
to realtime.

But supernova's *parallel DSP helper threads* are spawned by **supernova itself**,
not by libjack. They self-elevate via `AcquireSelfRealTime` — which needs
`cap_sys_nice` **on the supernova binary**. jackd and scsynth had the caps;
**the freshly-built supernova binary had none**, so its DSP threads stayed
`SCHED_OTHER` → XRuns.

### The fix — two coupled changes on the binary

Both mirror exactly what the scsynth bundle already does:

1. **`setcap cap_ipc_lock,cap_sys_nice,cap_sys_resource=eip`** on the supernova
   binary — applied in
   [`move/deploy-controller.sh`](../move/deploy-controller.sh), re-applied *after*
   `chown` (chown clears caps).
2. A capability binary runs in **secure-exec** mode, where the dynamic loader
   **ignores `LD_LIBRARY_PATH`**. Without it, supernova couldn't find
   `libjack.so.0` (in the RNBO lib dir) and exited 127. Fix: bake an **absolute
   `DT_RPATH`** (`wildrider/lib:rnbo/lib`) into the binary at build time via
   `patchelf`, exactly as `assemble-bundle.sh` does for scsynth. Done in
   [`Dockerfile.supernova`](../move/build/Dockerfile.supernova).

### Result

The 3 DSP threads now run **`SCHED_FIFO` priority 65** — below jackd's 70, so the
I/O driver always preempts DSP, and below the SPI/IRQ kernel threads (90/91) so
the display/DAC path is never starved.

| Load | scsynth | supernova (3 threads) |
|---|---|---|
| XRuns under a heavy patch | onset ~13 modules | **0 at 19 modules** |
| Core utilisation | one core ~85–90%, three idle | ~60% across **all four** |

---

## 7. Operating supernova

### The switch

The engine is chosen at boot from the `ATELIER_THREADS` environment variable
([`move/run-engine.sh`](../move/run-engine.sh) exports it,
[`supercollider/wr-boot.scd`](../supercollider/wr-boot.scd) reads it):

- **Default:** `run-engine.sh` sets `ATELIER_THREADS="${ATELIER_THREADS:-3}"`, so
  every launch boots **supernova with 3 DSP threads**. Nothing to toggle.
- **Fall back to scsynth:** a value `< 1` selects single-core scsynth. Change the
  default in `run-engine.sh` to `0`:

  ```sh
  export ATELIER_THREADS="${ATELIER_THREADS:-0}"   # 0 = scsynth, 3 = supernova
  ```

  or, for a one-off test over SSH, launch the engine with `ATELIER_THREADS=0` in
  the environment. Either way, the change takes effect on the next engine launch
  (relaunch Wildrider from the Move menu, or restart the Move).

### Tuning

- **Thread count.** 3 is the sweet spot on a 4-core Move — one core is left for
  the system, jackd, and Ableton's own audio. Try `2` if you want to be gentler on
  the host, `4` only experimentally.
- **Priority.** DSP threads land at `SCHED_FIFO 65`, jackd at 70. If Ableton's own
  audio ever glitches while supernova runs (see below), lowering the DSP priority
  or dropping to 2 threads is the lever.

### Verifying it's live

```bash
ssh root@move.local '
  pgrep -x supernova >/dev/null && echo "supernova running" || echo "scsynth"
  grep "\[wr-boot\]" /data/UserData/wildrider/logs/engine.log | tail -1'
```

`[wr-boot] SUPERNOVA — 3 DSP threads` confirms the multicore path.

---

## 8. Known items & troubleshooting

- **⚠ Coexistence with Ableton's own audio is the one unproven item.** All
  headless tests are clean, but supernova's realtime threads share the machine
  with the Move's built-in audio engine. Stress-test by *playing the physical
  Move* under supernova — pads, recording, heavy patches, built-in instruments. If
  the *device's* output stutters, back off (lower DSP priority or threads 3→2).
- **`… not installed` after a plugin change.** A UGen has no `*_supernova.so`
  variant. Build it against the 3.11.2 source (see §4) and redeploy.
- **supernova exits 127 / `libjack.so.0 not found`.** The binary lost its
  `DT_RPATH` (e.g. rebuilt without the patchelf step) — a capability binary
  ignores `LD_LIBRARY_PATH`, so the RPATH is mandatory. Re-bake it (§6).
- **XRuns return under supernova.** Check the DSP threads are actually
  `SCHED_FIFO`: `for t in $(ls /proc/$(pgrep -x supernova)/task); do chrt -p $t;
  done`. If they're `SCHED_OTHER`, the caps were lost — **any `scp`/`chown` clears
  file capabilities**, so re-run the `setcap` from `deploy-controller.sh`.
- **After a Move OS auto-update.** Updates can wipe file capabilities and the
  Schwung shim; re-run the deploy/post-update step as root.

---

## 9. Files touched by the migration

| File | Role |
|---|---|
| `move/build/Dockerfile.supernova` | cross-build the server + plugin `_supernova.so` variants; bake `DT_RPATH` |
| `move/build/mi-supernova.cmake` | append a `_supernova` target to each mi-UGens project |
| `supercollider/wr-boot.scd` | boot switch: `ATELIER_THREADS` → supernova; sets `~wrSupernova` |
| `supercollider/synthdefs.scd` | server-conditional VERB (GVerb ⇄ JPverb) |
| `supercollider/engine.scd` | topological-layer ParGroups (`~graphLayers`/`~applyLayers`) |
| `move/run-engine.sh` | exports `ATELIER_THREADS` (default 3); boot-wait + core-pinning know supernova |
| `move/deploy-controller.sh` | `setcap` the RT capabilities onto the supernova binary |
