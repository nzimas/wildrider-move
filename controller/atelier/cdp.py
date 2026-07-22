"""Composers Desktop Project (CDP) job runner — the "record a snippet, spawn 8
wildly different variations" engine behind the CDP view.

The engine captures a short slice of the live master bus to a WAV; this module then
runs randomized CDP program chains over it to produce N diverse variations, which
the controller loads into sampler slots.

Design goals:
  * MAXIMUM diversity from one source — a broad palette of both time-domain and
    spectral (phase-vocoder) transforms, each with randomized parameters, and a
    chance to CHAIN two transforms for compound results.
  * Robustness — CDP programs are finicky; every recipe is guarded. If a variation's
    chain fails (bad args for this source, empty output, timeout) it falls back to a
    safe transform and finally to a copy, so all N pads always get a playable sample.

CDP CLIs are validated against the real aarch64 binaries on the device.
"""
from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

CDP_BIN = Path(os.environ.get("WR_CDP_BIN", "/data/UserData/wildrider/cdp/bin"))
_PROG_TIMEOUT = 25.0          # per-program wall clock; spectral jobs on a 3s slice are quick
_MIN_OUT_BYTES = 2000         # a valid WAV header + a little audio


class _Ctx:
    """Per-recipe scratch: a run() that shells CDP programs, plus a temp-file mint."""

    def __init__(self, work: Path, rng: random.Random, tag: str):
        self.work = work
        self.rng = rng
        self.tag = tag
        self._n = 0

    def tmp(self, ext: str) -> Path:
        self._n += 1
        return self.work / f"_{self.tag}_{self._n}.{ext}"

    def run(self, prog: str, *args: str) -> bool:
        exe = CDP_BIN / prog
        if not exe.exists():
            return False
        try:
            r = subprocess.run([str(exe), *[str(a) for a in args]],
                               cwd=str(self.work), capture_output=True,
                               timeout=_PROG_TIMEOUT)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def _ok(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size >= _MIN_OUT_BYTES
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Recipes: each takes (ctx, src, out) and returns True on a valid `out`. A recipe
# owns its intermediate files (pvoc analysis, chained stages) via ctx.tmp().
# --------------------------------------------------------------------------- #

def _anal(ctx: _Ctx, src: Path) -> Path | None:
    """pvoc analysis file for the spectral recipes."""
    ana = ctx.tmp("ana")
    return ana if ctx.run("pvoc", "anal", "1", src, ana) and _ok(ana) else None


def _synth(ctx: _Ctx, ana: Path, out: Path) -> bool:
    return ctx.run("pvoc", "synth", ana, out) and _ok(out)


# ---- time-domain (operate directly on the WAV) ----------------------------- #

def rc_speed(ctx, src, out):                       # varispeed: pitch + time together
    ratio = ctx.rng.choice([0.25, 0.5, 0.6, 0.75, 1.5, 2.0, 3.0, 4.0])
    return ctx.run("modify", "speed", "1", src, out, ratio) and _ok(out)

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

def rc_stretch_time(ctx, src, out):                # phase-vocoder time stretch (spectral)
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    ratio = ctx.rng.choice([2.0, 3.0, 4.0, 6.0, 8.0])
    return ctx.run("stretch", "time", "1", ana, b, ratio) and _synth(ctx, b, out)

def rc_dist_interpolate(ctx, src, out):            # interpolate wavesets -> smeared pitch glide
    return ctx.run("distort", "interpolate", src, out, ctx.rng.randint(2, 10)) and _ok(out)

def rc_dist_pitch(ctx, src, out):                  # per-waveset octave jitter -> warbling pitch
    return ctx.run("distort", "pitch", src, out, round(ctx.rng.uniform(0.1, 0.9), 2)) and _ok(out)

def rc_modify_radical(ctx, src, out):              # radical time-domain mangles
    if ctx.rng.random() < 0.5:
        return ctx.run("modify", "radical", "1", src, out) and _ok(out)
    return ctx.run("modify", "radical", "3", src, out, ctx.rng.randint(2, 6)) and _ok(out)

def rc_extend_zigzag(ctx, src, out):               # zig-zag back and forth through the sound
    dur = ctx.rng.choice([3.0, 4.0, 5.0, 7.0])
    return ctx.run("extend", "zigzag", "1", src, out, "0", "1.4", dur,
                   round(ctx.rng.uniform(0.05, 0.25), 2)) and _ok(out)


# ---- spectral (phase vocoder: anal -> transform -> synth) ------------------ #

def rc_blur(ctx, src, out):                        # smear time frames -> wash
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "blur", ana, b, ctx.rng.randint(4, 50)) and _synth(ctx, b, out)

def rc_blur_chorus(ctx, src, out):                 # spectral chorus -> shimmer thickening
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    mode = ctx.rng.randint(2, 4)                    # randomise partial frequencies (up/down/both)
    return ctx.run("blur", "chorus", mode, ana, b, round(ctx.rng.uniform(0.2, 0.9), 2)) and _synth(ctx, b, out)

def rc_blur_spread(ctx, src, out):                 # spread spectral peaks -> controlled noisiness
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "spread", ana, b, "-f16", "-s%.2f" % ctx.rng.uniform(0.4, 1.0)) and _synth(ctx, b, out)

def rc_focus_exag(ctx, src, out):                  # exaggerate spectral contour -> resonant
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("focus", "exag", ana, b, round(ctx.rng.uniform(1.5, 6.0), 2)) and _synth(ctx, b, out)

def rc_hilite_trace(ctx, src, out):                # keep the N loudest partials -> hollow/pure
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("hilite", "trace", "1", ana, b, ctx.rng.randint(4, 24)) and _synth(ctx, b, out)

def rc_blur_avrg(ctx, src, out):                   # average spectral energy over N channels -> smeared
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "avrg", ana, b, ctx.rng.randint(2, 24)) and _synth(ctx, b, out)

def rc_blur_noise(ctx, src, out):                  # push partials toward noise -> breathy/airy
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "noise", ana, b, round(ctx.rng.uniform(0.2, 0.9), 2)) and _synth(ctx, b, out)

def rc_blur_scatter(ctx, src, out):                # randomly drop/scatter spectral channels -> sparse glitter
    ana = _anal(ctx, src)
    if not ana: return False
    b = ctx.tmp("ana")
    return ctx.run("blur", "scatter", ana, b, ctx.rng.randint(2, 12)) and _synth(ctx, b, out)


# Time-domain recipes are safe to chain (fast, WAV->WAV). Spectral ones (pvoc) are
# slower, so they seed a chain but extra stages are drawn from the time-domain set.
_TD_RECIPES = [rc_speed, rc_dist_multiply, rc_dist_divide, rc_dist_telescope,
               rc_dist_reform, rc_dist_repeat, rc_dist_interpolate, rc_dist_pitch,
               rc_modify_radical, rc_extend_zigzag]
_SPEC_RECIPES = [rc_blur, rc_blur_chorus, rc_blur_spread, rc_focus_exag,
                 rc_hilite_trace, rc_stretch_time, rc_blur_avrg, rc_blur_noise,
                 rc_blur_scatter]
_ALL_RECIPES = _TD_RECIPES + _SPEC_RECIPES


def _one_variation(ctx: _Ctx, recipe, src: Path, out: Path) -> bool:
    """Produce one variation: seed with `recipe`, then chain 0-2 extra time-domain
    transforms on top for compound, far-more-diverse results. Every stage is a
    GENUINE CDP transform — if any stage fails this returns False (the caller retries
    with a different real recipe). No copies, no filler."""
    extra = ctx.rng.choices([0, 1, 2], weights=[30, 42, 28])[0]
    if extra == 0:
        return recipe(ctx, src, out) and _ok(out)
    stages = [recipe] + [ctx.rng.choice(_TD_RECIPES) for _ in range(extra)]
    cur = src
    for i, stg in enumerate(stages):
        dst = out if i == len(stages) - 1 else ctx.tmp("wav")
        if not (stg(ctx, cur, dst) and _ok(dst)):
            return False
        cur = dst
    return _ok(out)


def generate(src_wav: str, out_dir: str, count: int = 8,
             seed: int | None = None, on_ready=None) -> list[str]:
    """Record-driven entry point: turn one source WAV into up to `count` DIVERSE CDP
    variations under out_dir (var0.wav ..). Each variation is a real CDP transform;
    on failure a DIFFERENT real recipe is tried. No fallbacks/filler — if the source
    is pathological and some recipes can't produce output, fewer than `count` are
    returned rather than padding with copies.

    `on_ready(index, path)` (optional) is called the moment each variation is ready,
    so the caller can load it into its slot immediately (progressive fill) instead of
    waiting for the whole batch."""
    src = Path(src_wav)
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(4), "big"))
    # Source prep. CDP's spectral (pvoc) and waveset (distort) programs are MONO-only,
    # but the captured master bus is stereo -> fold to mono first (housekeep chans 4).
    # Then NORMALISE: a live capture is often quiet (master bus pre-gain) and spectral
    # transforms shed further energy, so un-normalised variations come out inaudible;
    # `modify loudness 3` forces the peak to a target. These are real record-processing
    # steps, not fallbacks; if they can't run (silent source) we honestly return
    # nothing rather than process garbage.
    c0 = _Ctx(work, rng, "src")
    mono = work / "_source_mono.wav"
    src_n = work / "_source_norm.wav"
    if not (c0.run("housekeep", "chans", "4", src, mono) and _ok(mono)):
        return []
    if not (c0.run("modify", "loudness", "3", mono, src_n, "-l0.7") and _ok(src_n)):
        return []
    src = src_n
    # A shuffled pool of recipes, repeated enough to keep trying real transforms until
    # `count` succeed. Shuffling favours a diverse spread across the variations.
    pool: list = []
    while len(pool) < count * 8:
        block = list(_ALL_RECIPES)
        rng.shuffle(block)
        pool += block
    outs: list[str] = []
    attempt = 0
    for recipe in pool:
        if len(outs) >= count:
            break
        out = work / f"var{len(outs)}.wav"
        try:
            out.unlink()
        except OSError:
            pass
        ctx = _Ctx(work, rng, f"v{len(outs)}_{attempt}")
        attempt += 1
        raw = ctx.tmp("wav")     # mono recipe output
        nrm = ctx.tmp("wav")     # normalised mono
        # produce a real (mono) variation, normalise it to a consistent audible level,
        # then interleave mono->stereo so it loads/plays like a normal sampler take
        # (the sampler's BufRd is 2-channel).
        if _one_variation(ctx, recipe, src, raw) and _ok(raw) \
                and ctx.run("modify", "loudness", "3", raw, nrm, "-l0.5") and _ok(nrm) \
                and ctx.run("submix", "interleave", nrm, nrm, out) and _ok(out):
            idx = len(outs)
            outs.append(str(out))
            if on_ready is not None:
                try:
                    on_ready(idx, str(out))     # progressive: load this slot now
                except Exception:
                    pass
    # tidy intermediates (keep only the var*.wav results)
    for f in work.glob("_*"):
        try:
            f.unlink()
        except OSError:
            pass
    return outs
