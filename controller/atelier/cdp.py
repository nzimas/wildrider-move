"""Composers Desktop Project (CDP) job runner — the "record a snippet, spawn N
wildly different variations" engine behind the CDP view.

The engine captures a short slice of the live master bus to a WAV; this module then
runs randomized CDP program chains over it to produce N diverse variations, which
the controller loads into sampler slots.

Diversity architecture: recipes are grouped into seven FAMILIES, each a different
class of transformation —

  PITCHTIME  varispeed / brassage pitch+stretch / pvoc time / transposed stacks
  GRANULAR   granulation, chunk scrambles, drunken walks, loops, iterations
  WAVESET    CDP's signature waveset mangles (distort *, waveset scramble)
  SPECSMEAR  spectral smears (blur/avrg/noise/scatter/spread/chorus)
  SPECWARP   spectral geometry (freq shift, gliss, waver, invert, stretch, trace)
  RHYTHM     freezes, bounces, echoes, tremolo, tap delays, stutters
  RADICAL    radical mangles, zigzags, shrink-repeats

A batch is dealt ROUND-ROBIN across shuffled families, so the N slots span the
transform space instead of clustering on lookalike smears. Each recipe randomizes
wide parameter ranges, and ~half the variations chain a second stage drawn from a
DIFFERENT family for compound results.

Robustness: CDP programs are finicky; every recipe is guarded. A failed recipe is
retried with a different REAL recipe (first within the family, then globally). No
copies, no filler — fewer than N variations are returned rather than padding.

All CLI signatures validated against the real aarch64 binaries on the device.
"""
from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

CDP_BIN = Path(os.environ.get("WR_CDP_BIN", "/data/UserData/wildrider/cdp/bin"))
_PROG_TIMEOUT = 25.0          # per-program wall clock; jobs on a 4s slice are quick
_MIN_OUT_BYTES = 2000         # a valid WAV header + a little audio


class _Ctx:
    """Per-recipe scratch: a run() that shells CDP programs, plus a temp-file mint."""

    def __init__(self, work: Path, rng: random.Random, tag: str):
        self.work = work
        self.rng = rng
        self.tag = tag
        self._n = 0
        # CDP programs write scratch to $TMPDIR, which defaults to /tmp — on the Move
        # that is the ROOT partition (~470MB, chronically ~96% full), so pvoc/spectral
        # jobs fail (or silently produce nothing) when it fills. `work` lives on the big
        # /data partition (tens of GB), so pin every CDP subprocess's TMPDIR there.
        self._env = dict(os.environ, TMPDIR=str(work))

    def tmp(self, ext: str) -> Path:
        self._n += 1
        return self.work / f"_{self.tag}_{self._n}.{ext}"

    def text(self, content: str) -> Path:
        """Mint a datafile (breakpoint / times / taps) for programs that need one."""
        p = self.tmp("txt")
        p.write_text(content)
        return p

    def run(self, prog: str, *args: str) -> bool:
        exe = CDP_BIN / prog
        if not exe.exists():
            return False
        try:
            r = subprocess.run([str(exe), *[str(a) for a in args]],
                               cwd=str(self.work), capture_output=True,
                               env=self._env, timeout=_PROG_TIMEOUT)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def sweep(self) -> None:
        """Delete this recipe's intermediate files (pvoc analyses can be several MB
        each). Called after every variation so peak disk stays tiny — the Move root
        has only a few MB free, and a full batch would otherwise overflow it."""
        for f in self.work.glob(f"_{self.tag}_*"):
            try:
                f.unlink()
            except OSError:
                pass


def _ok(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size >= _MIN_OUT_BYTES
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Recipes: each takes (ctx, src, out) and returns True on a valid `out`. A recipe
# owns its intermediate files (pvoc analysis, chained stages, datafiles) via ctx.
# The source is a ~4s mono normalised WAV.
# --------------------------------------------------------------------------- #

def _anal(ctx: _Ctx, src: Path) -> Path | None:
    """pvoc analysis file for the spectral recipes."""
    ana = ctx.tmp("ana")
    return ana if ctx.run("pvoc", "anal", "1", src, ana) and _ok(ana) else None


def _synth(ctx: _Ctx, ana: Path, out: Path) -> bool:
    return ctx.run("pvoc", "synth", ana, out) and _ok(out)


# ---- PITCHTIME -------------------------------------------------------------- #

def rc_speed(ctx, src, out):                       # varispeed: pitch + time together
    ratio = ctx.rng.choice([0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0])
    return ctx.run("modify", "speed", "1", src, out, ratio) and _ok(out)

def rc_brassage_pitch(ctx, src, out):              # granular pitchshift, duration kept
    semis = round(ctx.rng.choice([-1, 1]) * ctx.rng.uniform(2.0, 14.0), 2)
    return ctx.run("modify", "brassage", "1", src, out, semis) and _ok(out)

def rc_brassage_stretch(ctx, src, out):            # granular timestretch, pitch kept
    vel = round(ctx.rng.choice([0.15, 0.25, 0.4, 0.6, 1.6, 2.5]), 2)
    return ctx.run("modify", "brassage", "2", src, out, vel) and _ok(out)

def rc_stretch_time(ctx, src, out):                # phase-vocoder time stretch/compress
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    ratio = ctx.rng.choice([0.35, 0.5, 2.0, 3.0, 4.0, 6.0, 8.0])
    return ctx.run("stretch", "time", "1", ana, b, ratio) and _synth(ctx, b, out)

def rc_stack(ctx, src, out):                       # stack transposed copies -> chordal mass
    semis = round(ctx.rng.choice([3, 4, 5, 7, -5, -7, -12, 12]) + ctx.rng.uniform(-0.3, 0.3), 2)
    count = ctx.rng.randint(2, 4)
    lean = round(ctx.rng.uniform(0.3, 1.0), 2)
    return ctx.run("modify", "stack", src, out, semis, count, lean, "0", "1", "1", "-n") and _ok(out)


# ---- GRANULAR --------------------------------------------------------------- #

def rc_brassage_granulate(ctx, src, out):          # granulate: density <1 = gapped
    dens = round(ctx.rng.uniform(0.2, 2.5), 2)
    return ctx.run("modify", "brassage", "5", src, out, dens) and _ok(out)

def rc_brassage_scramble(ctx, src, out):           # granular scramble (grainsize ms)
    gsize = ctx.rng.randint(20, 220)
    rng_ms = ctx.rng.randint(100, 800)
    return ctx.run("modify", "brassage", "4", src, out, gsize, f"-r{rng_ms}") and _ok(out)

def rc_ext_scramble(ctx, src, out):                # chunk scramble, end to end
    mn = round(ctx.rng.uniform(0.05, 0.25), 2)
    mx = round(mn + ctx.rng.uniform(0.1, 0.6), 2)
    dur = round(ctx.rng.uniform(4.0, 7.0), 1)
    return ctx.run("extend", "scramble", "1", src, out, mn, mx, dur) and _ok(out)

def rc_drunk(ctx, src, out):                       # drunken walk through the source
    outdur = round(ctx.rng.uniform(4.0, 7.0), 1)
    locus = round(ctx.rng.uniform(0.5, 3.0), 2)
    ambitus = round(ctx.rng.uniform(0.5, 1.8), 2)
    step = round(ctx.rng.uniform(0.05, 0.4), 2)
    clock = round(ctx.rng.uniform(0.08, 0.4), 2)
    return ctx.run("extend", "drunk", "1", src, out, outdur, locus, ambitus, step, clock) and _ok(out)

def rc_loop(ctx, src, out):                        # advancing micro-loop
    dur = round(ctx.rng.uniform(4.0, 7.0), 1)
    start = round(ctx.rng.uniform(0.0, 1.5), 2)
    seglen = ctx.rng.randint(40, 500)              # ms
    step = ctx.rng.randint(15, 250)                # ms
    return ctx.run("extend", "loop", "2", src, out, dur, start, seglen,
                   f"-l{step}", "-s%.2f" % ctx.rng.uniform(0.0, 0.3)) and _ok(out)

def rc_iterate(ctx, src, out):                     # fluid iteration w/ pitch scatter
    outdur = round(ctx.rng.uniform(5.0, 9.0), 1)
    delay = round(ctx.rng.uniform(0.15, 1.2), 2)
    return ctx.run("extend", "iterate", "1", src, out, outdur,
                   f"-d{delay}", "-r%.2f" % ctx.rng.uniform(0.3, 1.0),
                   "-p%.1f" % ctx.rng.uniform(0.0, 7.0),
                   "-a%.2f" % ctx.rng.uniform(0.0, 0.5)) and _ok(out)


# ---- WAVESET ---------------------------------------------------------------- #

def rc_dist_multiply(ctx, src, out):               # waveset multiply -> added harmonics
    return ctx.run("distort", "multiply", src, out, ctx.rng.randint(2, 8)) and _ok(out)

def rc_dist_divide(ctx, src, out):                 # waveset divide -> subharmonics
    return ctx.run("distort", "divide", src, out, ctx.rng.randint(2, 6)) and _ok(out)

def rc_dist_telescope(ctx, src, out):              # telescope wavesets -> pitch/texture warp
    return ctx.run("distort", "telescope", src, out, ctx.rng.randint(2, 8)) and _ok(out)

def rc_dist_reform(ctx, src, out):                 # reshape wavesets to square/triangle
    return ctx.run("distort", "reform", ctx.rng.randint(1, 4), src, out) and _ok(out)

def rc_dist_repeat(ctx, src, out):                 # repeat wavesets -> granular stutter
    return ctx.run("distort", "repeat2", src, out, ctx.rng.randint(2, 6)) and _ok(out)

def rc_dist_interpolate(ctx, src, out):            # interpolate wavesets -> smeared glide
    return ctx.run("distort", "interpolate", src, out, ctx.rng.randint(2, 10)) and _ok(out)

def rc_dist_pitch(ctx, src, out):                  # per-waveset octave jitter -> warble
    return ctx.run("distort", "pitch", src, out, round(ctx.rng.uniform(0.1, 0.9), 2)) and _ok(out)

def rc_waveset_scramble(ctx, src, out):            # scramble waveset ORDER (+trans/atten)
    mode = ctx.rng.choice([1, 2, 3, 4, 7, 8])
    seed = ctx.rng.randint(1, 256)
    args = ["scramble", str(mode), src, out]
    if mode in (1, 2):                              # modes 1-2 take an output duration
        args.append(round(ctx.rng.uniform(4.0, 6.0), 1))
    args += [seed,
             f"-c{ctx.rng.randint(2, 48)}",
             "-t%.1f" % ctx.rng.uniform(0.0, 6.0),
             "-a%.2f" % ctx.rng.uniform(0.0, 0.5)]
    return ctx.run("scramble", *args) and _ok(out)


# ---- SPECSMEAR (pvoc: anal -> transform -> synth) --------------------------- #

def rc_blur(ctx, src, out):                        # smear time frames -> wash
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "blur", ana, b, ctx.rng.randint(4, 80)) and _synth(ctx, b, out)

def rc_blur_chorus(ctx, src, out):                 # spectral chorus -> shimmer
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    mode = ctx.rng.randint(2, 4)
    return ctx.run("blur", "chorus", mode, ana, b, round(ctx.rng.uniform(0.2, 1.2), 2)) and _synth(ctx, b, out)

def rc_blur_spread(ctx, src, out):                 # spread spectral peaks -> noisiness
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "spread", ana, b, "-f16", "-s%.2f" % ctx.rng.uniform(0.4, 1.0)) and _synth(ctx, b, out)

def rc_blur_avrg(ctx, src, out):                   # average spectral energy -> smeared
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "avrg", ana, b, ctx.rng.randint(2, 32)) and _synth(ctx, b, out)

def rc_blur_noise(ctx, src, out):                  # push partials toward noise -> airy
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "noise", ana, b, round(ctx.rng.uniform(0.2, 0.9), 2)) and _synth(ctx, b, out)

def rc_blur_scatter(ctx, src, out):                # drop/scatter channels -> sparse glitter
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "scatter", ana, b, ctx.rng.randint(2, 12)) and _synth(ctx, b, out)


# ---- SPECWARP (spectral geometry) ------------------------------------------- #

def rc_focus_exag(ctx, src, out):                  # exaggerate spectral contour -> resonant
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("focus", "exag", ana, b, round(ctx.rng.uniform(1.5, 6.0), 2)) and _synth(ctx, b, out)

def rc_hilite_trace(ctx, src, out):                # keep N loudest partials -> hollow/pure
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("hilite", "trace", "1", ana, b, ctx.rng.randint(3, 24)) and _synth(ctx, b, out)

def rc_strange_shift(ctx, src, out):               # linear freq shift -> inharmonic/metal
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    shift = round(ctx.rng.choice([-1, 1]) * ctx.rng.uniform(60.0, 500.0), 1)
    return ctx.run("strange", "shift", "1", ana, b, shift) and _synth(ctx, b, out)

def rc_strange_glis(ctx, src, out):                # self-glissando inside spectral env
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    rate = round(ctx.rng.choice([-1, 1]) * ctx.rng.uniform(2.0, 12.0), 1)
    return ctx.run("strange", "glis", "3", ana, b, "-f4", rate) and _synth(ctx, b, out)

def rc_strange_waver(ctx, src, out):               # oscillate harmonic <-> inharmonic
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    vib = round(ctx.rng.uniform(0.4, 8.0), 2)
    stretch = round(ctx.rng.uniform(1.3, 4.0), 2)
    return ctx.run("strange", "waver", "1", ana, b, vib, stretch, ctx.rng.randint(80, 400)) and _synth(ctx, b, out)

def rc_strange_invert(ctx, src, out):              # invert the spectrum
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("strange", "invert", "2", ana, b) and _synth(ctx, b, out)

def rc_spectrum_stretch(ctx, src, out):            # stretch partial spacing -> bell/inharmonic
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    fdiv = ctx.rng.randint(150, 600)
    mx = round(ctx.rng.uniform(1.5, 4.0), 2)
    return ctx.run("stretch", "spectrum", ctx.rng.randint(1, 2), ana, b,
                   fdiv, mx, round(ctx.rng.uniform(0.8, 2.0), 2)) and _synth(ctx, b, out)


# ---- RHYTHM ----------------------------------------------------------------- #

def rc_freeze(ctx, src, out):                      # freeze a segment by fluid iteration
    # NB the standalone `freeze` binary is broken on this aarch64 build
    # ("Failed to parse input file") — `extend freeze` is the working one.
    start = round(ctx.rng.uniform(0.2, 2.2), 2)
    end = round(start + ctx.rng.uniform(0.4, 1.5), 2)
    delay = round(ctx.rng.uniform(0.06, max(0.08, min(0.5, end - start))), 3)
    outdur = round(ctx.rng.uniform(4.0, 8.0), 1)
    return ctx.run("extend", "freeze", "1", src, out, outdur, delay,
                   round(ctx.rng.uniform(0.2, 1.0), 2),          # rand
                   round(ctx.rng.uniform(0.0, 5.0), 1),          # pshift semis
                   round(ctx.rng.uniform(0.0, 0.4), 2),          # ampcut
                   start, end, "1") and _ok(out)

def rc_bounce(ctx, src, out):                      # accelerating decaying repeats
    count = ctx.rng.randint(5, 16)
    startgap = round(ctx.rng.uniform(0.25, 0.9), 2)
    shorten = round(ctx.rng.uniform(0.55, 0.85), 2)
    return ctx.run("bounce", "bounce", src, out, count, startgap, shorten,
                   round(ctx.rng.uniform(0.05, 0.35), 2), "1", "-s0.05", "-c") and _ok(out)

def rc_sfecho(ctx, src, out):                      # whole-snippet decaying echo train
    delay = round(ctx.rng.uniform(4.1, 5.0), 2)    # must be >= src duration
    atten = round(ctx.rng.uniform(0.4, 0.7), 2)
    total = round(ctx.rng.uniform(8.0, 12.0), 1)
    return ctx.run("sfecho", "echo", src, out, delay, atten, total,
                   "-r%.2f" % ctx.rng.uniform(0.0, 0.3)) and _ok(out)

def rc_revecho(ctx, src, out):                     # short delay + feedback -> resonance
    delay = ctx.rng.randint(25, 700)               # ms
    mix = round(ctx.rng.uniform(0.4, 1.0), 2)
    fb = round(ctx.rng.uniform(0.3, 0.85), 2)
    return ctx.run("modify", "revecho", "1", src, out, delay, mix, fb,
                   round(ctx.rng.uniform(1.0, 3.0), 1)) and _ok(out)

def rc_stadium(ctx, src, out):                     # stadium P.A. echoes
    return ctx.run("modify", "revecho", "3", src, out,
                   "-s%.2f" % ctx.rng.uniform(0.3, 1.5),
                   f"-e{ctx.rng.randint(8, 60)}", "-n") and _ok(out)

def rc_tremolo(ctx, src, out):                     # tremolo up to audio-rate AM
    frq = round(ctx.rng.choice([ctx.rng.uniform(2.0, 14.0), ctx.rng.uniform(20.0, 120.0)]), 1)
    return ctx.run("tremolo", "tremolo", "1", src, out, frq,
                   round(ctx.rng.uniform(0.6, 1.0), 2), "1", ctx.rng.randint(1, 4)) and _ok(out)

def rc_tapdelay(ctx, src, out):                    # multi-tap delay w/ feedback
    n = ctx.rng.randint(3, 7)
    t = 0.0
    lines = []
    for _ in range(n):
        t += ctx.rng.uniform(0.06, 0.5)
        lines.append("%.3f %.2f" % (t, ctx.rng.uniform(0.3, 1.0)))
    taps = ctx.text("\n".join(lines) + "\n")
    return ctx.run("tapdelay", src, out, "0.4",
                   "%.2f" % ctx.rng.uniform(0.0, 0.6),
                   "%.2f" % ctx.rng.uniform(0.3, 0.7), taps, "2") and _ok(out)

def rc_stutter(ctx, src, out):                     # slice + re-order with silences
    n = ctx.rng.randint(5, 10)
    # slice times must be >= 0.016s apart (stutter's minimum timestep) — build them
    # as a jittered walk so spacing is always safe.
    times, t = [], 0.25
    for _ in range(n):
        times.append(t)
        t += ctx.rng.uniform(0.15, 0.55)
        if t > 3.6:
            break
    data = ctx.text("\n".join("%.3f" % x for x in times) + "\n")
    dur = round(ctx.rng.uniform(4.0, 7.0), 1)
    return ctx.run("stutter", "stutter", src, out, data, dur,
                   ctx.rng.randint(1, 4),                      # segjoins
                   round(ctx.rng.uniform(0.0, 0.4), 2),        # silprop
                   "0.02", "0.2", ctx.rng.randint(1, 256),     # seed range 0-256
                   "-t%.1f" % ctx.rng.uniform(0.0, 5.0)) and _ok(out)


# ---- RADICAL ---------------------------------------------------------------- #

def rc_modify_radical(ctx, src, out):              # radical time-domain mangles
    if ctx.rng.random() < 0.5:
        return ctx.run("modify", "radical", "1", src, out) and _ok(out)
    return ctx.run("modify", "radical", "3", src, out, ctx.rng.randint(2, 6)) and _ok(out)

def rc_extend_zigzag(ctx, src, out):               # zig-zag back and forth
    dur = ctx.rng.choice([3.0, 4.0, 5.0, 7.0])
    return ctx.run("extend", "zigzag", "1", src, out, "0", "1.4", dur,
                   round(ctx.rng.uniform(0.05, 0.25), 2)) and _ok(out)

def rc_shrink(ctx, src, out):                      # shrinking repeats -> accelerando
    mode = ctx.rng.randint(1, 3)
    shrinkage = round(ctx.rng.uniform(0.55, 0.8), 2)
    gap = 4.2
    contract = round(ctx.rng.uniform(max(shrinkage, 0.55), 0.8), 2)
    return ctx.run("shrink", "shrink", mode, src, out, shrinkage,
                   gap, contract, "8", "15", "-n") and _ok(out)   # min output dur = 8s


# --------------------------------------------------------------------------- #
# Families + selection
# --------------------------------------------------------------------------- #

FAMILIES: dict[str, list] = {
    "PITCHTIME": [rc_speed, rc_brassage_pitch, rc_brassage_stretch, rc_stretch_time, rc_stack],
    "GRANULAR":  [rc_brassage_granulate, rc_brassage_scramble, rc_ext_scramble,
                  rc_drunk, rc_loop, rc_iterate],
    "WAVESET":   [rc_dist_multiply, rc_dist_divide, rc_dist_telescope, rc_dist_reform,
                  rc_dist_repeat, rc_dist_interpolate, rc_dist_pitch, rc_waveset_scramble],
    "SPECSMEAR": [rc_blur, rc_blur_chorus, rc_blur_spread, rc_blur_avrg,
                  rc_blur_noise, rc_blur_scatter],
    "SPECWARP":  [rc_focus_exag, rc_hilite_trace, rc_strange_shift, rc_strange_glis,
                  rc_strange_waver, rc_strange_invert, rc_spectrum_stretch],
    "RHYTHM":    [rc_freeze, rc_bounce, rc_sfecho, rc_revecho, rc_stadium,
                  rc_tremolo, rc_tapdelay, rc_stutter],
    "RADICAL":   [rc_modify_radical, rc_extend_zigzag, rc_shrink],
}
_ALL_RECIPES = [r for fam in FAMILIES.values() for r in fam]

# Chain-safe second stages: fast WAV->WAV transforms that do not balloon duration.
_CHAIN_SAFE: dict[str, list] = {
    "PITCHTIME": [rc_speed, rc_brassage_pitch],
    "WAVESET":   [rc_dist_multiply, rc_dist_divide, rc_dist_telescope, rc_dist_reform,
                  rc_dist_interpolate, rc_dist_pitch, rc_waveset_scramble],
    "RHYTHM":    [rc_revecho, rc_tremolo],
    "GRANULAR":  [rc_brassage_granulate, rc_brassage_scramble],
}
_RECIPE_FAMILY = {r: f for f, fam in FAMILIES.items() for r in fam}


def _one_variation(ctx: _Ctx, recipe, src: Path, out: Path) -> bool:
    """Produce one variation: seed with `recipe`; ~half the time chain one extra
    chain-safe stage drawn from a DIFFERENT family, for compound results. Every
    stage is a GENUINE CDP transform — any failure returns False (the caller
    retries with a different real recipe). No copies, no filler."""
    fam = _RECIPE_FAMILY.get(recipe)
    if ctx.rng.random() < 0.45:
        pools = [v for f, v in _CHAIN_SAFE.items() if f != fam]
        extra = ctx.rng.choice(ctx.rng.choice(pools))
        mid = ctx.tmp("wav")
        if recipe(ctx, src, mid) and _ok(mid) and extra(ctx, mid, out) and _ok(out):
            return True
        return False
    return recipe(ctx, src, out) and _ok(out)


def generate(src_wav: str, out_dir: str, count: int = 8,
             seed: int | None = None, on_ready=None) -> list[str]:
    """Record-driven entry point: turn one source WAV into up to `count` DIVERSE CDP
    variations under out_dir (var0.wav ..). Variations are dealt round-robin across
    the transform FAMILIES so the batch spans the space. Each is a real CDP
    transform; on failure a DIFFERENT real recipe is tried (same family first, then
    global). No fallbacks/filler — if the source is pathological, fewer than
    `count` are returned rather than padding with copies.

    `on_ready(index, path)` (optional) is called the moment each variation is ready,
    so the caller can load it into its slot immediately (progressive fill)."""
    src = Path(src_wav)
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(4), "big"))
    # Source prep. CDP's spectral (pvoc) and waveset (distort) programs are MONO-only,
    # but the captured master bus is stereo -> fold to mono (housekeep chans 4), then
    # NORMALISE (modify loudness 3) so quiet captures still produce audible output.
    c0 = _Ctx(work, rng, "src")
    mono = work / "_source_mono.wav"
    src_n = work / "_source_norm.wav"
    if not (c0.run("housekeep", "chans", "4", src, mono) and _ok(mono)):
        return []
    if not (c0.run("modify", "loudness", "3", mono, src_n, "-l0.7") and _ok(src_n)):
        return []
    src = src_n
    # Family round-robin: shuffle the family order once, then deal one variation per
    # family in turn. Within a family, recipes are drawn from a shuffled deck that
    # reshuffles when exhausted, so long batches (24) still avoid recipe repeats
    # until every recipe in the family has been used.
    fam_names = list(FAMILIES.keys())
    rng.shuffle(fam_names)
    decks = {f: [] for f in fam_names}

    def _draw(fam: str):
        if not decks[fam]:
            decks[fam] = list(FAMILIES[fam])
            rng.shuffle(decks[fam])
        return decks[fam].pop()

    outs: list[str] = []
    attempt = 0
    k = 0
    failures = 0
    while len(outs) < count and failures < count * 6:
        fam = fam_names[k % len(fam_names)]
        k += 1
        out = work / f"var{len(outs)}.wav"
        try:
            out.unlink()
        except OSError:
            pass
        done = False
        # try up to 3 recipes from this family, then 2 from the global pool
        tries = [ _draw(fam) for _ in range(3) ] + [rng.choice(_ALL_RECIPES) for _ in range(2)]
        for recipe in tries:
            ctx = _Ctx(work, rng, f"v{len(outs)}_{attempt}")
            attempt += 1
            raw = ctx.tmp("wav")     # mono recipe output
            nrm = ctx.tmp("wav")     # normalised mono
            # real (mono) variation -> normalise to a consistent audible level ->
            # interleave mono->stereo so it loads like a normal sampler take.
            ok = _one_variation(ctx, recipe, src, raw) and _ok(raw) \
                    and ctx.run("modify", "loudness", "3", raw, nrm, "-l0.5") and _ok(nrm) \
                    and ctx.run("submix", "interleave", nrm, nrm, out) and _ok(out)
            ctx.sweep()             # drop THIS recipe's intermediates before the next
            if ok:
                idx = len(outs)
                outs.append(str(out))
                if on_ready is not None:
                    try:
                        on_ready(idx, str(out))     # progressive: load this slot now
                    except Exception:
                        pass
                done = True
                break
        if not done:
            failures += 1
    # tidy intermediates (keep only the var*.wav results)
    for f in work.glob("_*"):
        try:
            f.unlink()
        except OSError:
            pass
    return outs
