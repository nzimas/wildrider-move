"""Csound offline (NRT) sound-file transformer — a second, tonally-distinct engine
for the CDP view, run BACK TO BACK with cdp.py (never in parallel).

Where CDP is a phase-vocoder / waveset specialist, this module leans on the
transformation classes Csound does that CDP (and largely SuperCollider) do NOT:

  * ATS   — analysis/transformation/resynthesis that models a sound as tonal
            partials PLUS a noise residue, so the two can be re-balanced,
            time-warped and transposed INDEPENDENTLY. Recasts dense patch output
            into pure drones, hissing shadows, or anything between.
  * LPC   — source-filter / formant recasting: extract the spectral envelope and
            re-excite it (buzz / noise), or freeze/shift the formants. Vocal,
            talking, throat-singing colours.
  * XSPEC — spectral cross-synthesis (pvscross / pvsmorph): impose the amplitudes
            of the source on the frequencies of a pitched copy of itself, or morph
            between the two — evolving, not a static smear.
  * MODAL — drive a bank of tuned modal resonators (mode) with the source, so the
            sample becomes the *excitation* of a struck/bowed metallic body.
  * GRAIN — phase-vocoder granular (mincer / sndwarp): extreme, artefact-light
            time-stretch into clouds and drones with pitch fully independent.

Each archetype renders the source through a self-contained .csd to a WAV. The
public entry point `generate()` mirrors cdp.generate() exactly (same signature +
progressive on_ready callback) so the worker treats the two engines identically.
No fallbacks/filler: a failed render is retried with a DIFFERENT real archetype.
"""
from __future__ import annotations

import os
import random
import subprocess
import wave
from pathlib import Path

# --------------------------------------------------------------------------- #
# Binary + runtime env. The bundle lives on the big /data partition next to the
# CDP one. csound loads its opcode plugins from OPCODE6DIR64 and its shared libs
# from LD_LIBRARY_PATH; TMPDIR is pinned to the work dir (the Move root is full).
# --------------------------------------------------------------------------- #
CS_DIR = Path(os.environ.get("WR_CSOUND_DIR", "/data/UserData/wildrider/csound"))
CS_BIN = CS_DIR / "bin" / "csound"
_CS_LIB = CS_DIR / "lib"
_CS_PLUG = CS_DIR / "plugins"

_RENDER_TIMEOUT = 30.0     # per csound invocation; a 4 s source renders in a second or two
_MIN_OUT_BYTES = 3000      # a valid WAV header + a little audio


# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self, work: Path, rng: random.Random, tag: str):
        self.work = work
        self.rng = rng
        self.tag = tag
        self._n = 0
        self._env = dict(
            os.environ,
            OPCODE6DIR64=str(_CS_PLUG),
            LD_LIBRARY_PATH=f"{_CS_LIB}:" + os.environ.get("LD_LIBRARY_PATH", ""),
            TMPDIR=str(work),
        )

    def tmp(self, ext: str) -> Path:
        self._n += 1
        return self.work / f"_{self.tag}_{self._n}.{ext}"

    def render(self, csd: str, out: Path) -> bool:
        """Write a .csd and render it offline to `out`. `-o out` in <CsOptions>
        already targets the file, so csound runs non-realtime."""
        cs = self.tmp("csd")
        cs.write_text(csd)
        return self._run(str(cs)) and _ok(out)

    def util(self, util: str, *args: str) -> bool:
        """Run a built-in csound utility (atsa / lpanal) via `csound -U <util>`."""
        return self._run("-U", util, *[str(a) for a in args])

    def _run(self, *args: str) -> bool:
        if not CS_BIN.exists():
            return False
        try:
            r = subprocess.run([str(CS_BIN), *args], cwd=str(self.work),
                               capture_output=True, env=self._env, timeout=_RENDER_TIMEOUT)
            if os.environ.get("CSFX_DEBUG"):
                import sys
                txt = r.stderr.decode("utf-8", "replace")
                if r.returncode != 0:
                    sys.stderr.write(f"[csfx] FAIL {' '.join(args)}\n" + txt[-1500:] + "\n")
                else:
                    amps = [l for l in txt.splitlines() if "overall amps" in l]
                    if amps:
                        sys.stderr.write(f"[csfx] ok {amps[-1].strip()}\n")
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def sweep(self) -> None:
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


def _dur(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return max(0.1, w.getnframes() / float(w.getframerate() or 44100))
    except Exception:
        return 4.0


# --------------------------------------------------------------------------- #
# .csd scaffolding. Every render is stereo 44.1 k float, 0dbfs=1, with a shared
# tail — DC-block, soft tanh limit, fade in/out — so no archetype can spit a click
# or an over. A mono, loudness-normalised source (prepared once in generate()) is
# GEN01-loaded as `gisrc` and is the input to every archetype.
# --------------------------------------------------------------------------- #
_HEAD = """<CsoundSynthesizer>
<CsOptions>
-o {out} -W -f -d -m0 --nodisplays
</CsOptions>
<CsInstruments>
sr = 44100
ksmps = 64
nchnls = 2
0dbfs = 1
gisrc ftgen 0, 0, 0, 1, "{src}", 0, 0, 1
giSine ftgen 0, 0, 16384, 10, 1
instr 1
{body}
  aLd dcblock2 aLo
  aRd dcblock2 aRo
  aenv linen 1, 0.02, p3, 0.06
  outs tanh(aLd * 1.3) * 0.72 * aenv, tanh(aRd * 1.3) * 0.72 * aenv
endin
</CsInstruments>
<CsScore>
{sco}
e
</CsScore>
</CsoundSynthesizer>
"""


def _csd(out: Path, src: Path, body: str, out_dur: float) -> str:
    """Assemble a full .csd. `body` is instr-1 code that must set a-rate `aLo` and
    `aRo`. `out_dur` sets the single score note length (seconds)."""
    return _HEAD.format(out=str(out), src=str(src), body=body,
                        sco=f"i1 0 {out_dur:.3f}")


# mono, loudness-lifted source prep — read the stereo capture, fold, makeup + soft
# clip, write a mono WAV that every archetype (and the ATS/LPC analyses) reads.
_PREP = """<CsoundSynthesizer>
<CsOptions>
-o {out} -W -f -d -m0 --nodisplays
</CsOptions>
<CsInstruments>
sr = 44100
ksmps = 64
nchnls = 1
0dbfs = 1
instr 1
  aL, aR diskin2 "{src}", 1, 0, 0
  am = (aL + aR) * 0.5 * {makeup}
  out tanh(am * 1.2) * 0.85
endin
</CsInstruments>
<CsScore>
i1 0 {dur}
e
</CsScore>
</CsoundSynthesizer>
"""


_MAX_DUR = 12.0     # cap: stretches make drones, but a sample slot wants < ~12 s

def _do(ctx: _Ctx, out: Path, body: str, out_dur: float) -> bool:
    return ctx.render(_csd(out, Path(ctx.src), body, min(out_dur, _MAX_DUR)), out)


# --------------------------------------------------------------------------- #
# Archetypes — each (ctx, out) renders ONE variation. They read the prepared mono
# source (ctx.src), its duration (ctx.sdur) and, for ATS/LPC, the pre-computed
# analysis files (ctx.atsfile / ctx.lpcfile). Grouped into FAMILIES so a batch is
# dealt across tonally-distinct territory.
# --------------------------------------------------------------------------- #

# ---- GRAIN: phase-vocoder time-stretch (pitch fully independent) ------------ #
def g_stretch(ctx, out):                           # extreme clean stretch -> pad/drone
    st = ctx.rng.choice([3, 5, 8, 12])
    semi = ctx.rng.choice([-12, -7, -5, 0, 0, 7, 12])
    p = 2 ** (semi / 12.0)
    det = ctx.rng.uniform(0.003, 0.02)
    body = (f'  itab = gisrc\n'
            f'  ilen = ftlen(itab)/sr\n'
            f'  atime line 0, p3, ilen\n'
            f'  kamp = 1\n  klk = 1\n'
            f'  kpL = {p:.5f}\n  kpR = {p * (1 + det):.5f}\n'
            f'  aLo mincer atime, kamp, kpL, itab, klk\n'
            f'  aRo mincer atime, kamp, kpR, itab, klk')
    return _do(ctx, out, body, ctx.sdur * st)

def g_scrub(ctx, out):                             # drunken scrub through the source
    semi = ctx.rng.choice([-12, -5, 0, 7, 12, 19])
    p = 2 ** (semi / 12.0)
    adv = ctx.rng.uniform(0.2, 0.6)
    jr = ctx.rng.uniform(0.5, 3.0)
    body = (f'  itab = gisrc\n'
            f'  ilen = ftlen(itab)/sr\n'
            f'  ab line 0, p3, ilen*{adv:.3f}\n'
            f'  aj randi ilen*0.09, {jr:.3f}, 2\n'
            f'  atime limit ab+aj, 0, ilen\n'
            f'  kamp = 1\n  klk = 1\n'
            f'  kpL = {p:.5f}\n  kpR = {p * 1.008:.5f}\n'
            f'  aLo mincer atime, kamp, kpL, itab, klk\n'
            f'  aRo mincer atime, kamp, kpR, itab, klk')
    return _do(ctx, out, body, ctx.sdur * 1.5)

# ---- MODAL: source excites a bank of tuned resonators -> struck body -------- #
def m_bank(ctx, out):
    root = ctx.rng.uniform(70, 300)
    ratios = ctx.rng.choice([
        [1, 2.76, 5.40, 8.93, 13.34],   # bell
        [1, 2.0, 3.0, 4.16, 5.43],      # bar
        [1, 1.5, 2.24, 2.99, 4.5],      # tube-ish
        [1, 3.01, 5.02, 7.0, 9.01],     # hollow odd-harmonic metal
    ])
    q = ctx.rng.choice([300, 800, 1500])
    drv = ctx.rng.uniform(0.6, 1.2)
    modes = "\n".join(f'  a{i} mode aexc, {root * r:.2f}, {q}'
                      for i, r in enumerate(ratios))
    summ = "+".join(f'a{i}' for i in range(len(ratios)))
    body = (f'  aexc diskin2 "{ctx.src}", 1, 0, 0\n'
            f'  aexc = aexc*{drv:.3f}\n{modes}\n'
            f'  amix = ({summ})*0.18\n'
            f'  aLo = amix\n'
            f'  aRo delay amix, 0.011')
    return _do(ctx, out, body, ctx.sdur + 2.5)

# ---- XSPEC: evolving spectral morph / cross ---------------------------------- #
def x_morph(ctx, out):                             # morph source <-> pitched self
    semi = ctx.rng.choice([-12, -7, 5, 7, 12])
    sh = 2 ** (semi / 12.0)
    fft = ctx.rng.choice([1024, 2048])
    body = (f'  aL diskin2 "{ctx.src}", 1, 0, 1\n'
            f'  aP diskin2 "{ctx.src}", {sh:.5f}, 0, 1\n'
            f'  f1 pvsanal aL, {fft}, {fft // 4}, {fft}, 1\n'
            f'  f2 pvsanal aP, {fft}, {fft // 4}, {fft}, 1\n'
            f'  km line 0, p3, 1\n'
            f'  fm pvsmorph f1, f2, km, km\n'
            f'  ao pvsynth fm\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.009')
    return _do(ctx, out, body, ctx.sdur)

def x_shiftblur(ctx, out):                         # sweeping spectral blur + freq shift
    fft = 2048
    blur = ctx.rng.uniform(0.05, 0.35)
    shz = ctx.rng.choice([-200, -90, 90, 150, 300])
    body = (f'  aL diskin2 "{ctx.src}", 1, 0, 1\n'
            f'  f1 pvsanal aL, {fft}, {fft // 4}, {fft}, 1\n'
            f'  kbl line 0.01, p3, {blur:.3f}\n'
            f'  fb pvsblur f1, kbl, 0.5\n'
            f'  fs pvshift fb, {shz}, 100\n'
            f'  ao pvsynth fs\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.008')
    return _do(ctx, out, body, ctx.sdur * 1.5)

# ---- ATS: tonal-partials + noise-residue resynthesis (the flagship) --------- #
def _ats_body(ctx, sinlev, nzlev, fmod):
    return (f'  idur ATSinfo "{ctx.atsfile}", 5\n'
            f'  ip ATSinfo "{ctx.atsfile}", 3\n'
            f'  ktime line 0, p3, idur\n'
            f'  ao ATSsinnoi ktime, {sinlev}, {nzlev}, {fmod}, "{ctx.atsfile}", ip\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.007')

def a_drone(ctx, out):                             # pure tonal partials, heavy stretch
    if not ctx.atsfile:
        return False
    fmod = ctx.rng.choice([0.5, 0.5, 1.0, 1.0, 2.0])
    return _do(ctx, out, _ats_body(ctx, 1.0, 0.1, fmod),
               ctx.sdur * ctx.rng.choice([6, 8, 10]))

def a_noise(ctx, out):                             # the hissing noise-shadow
    if not ctx.atsfile:
        return False
    fmod = round(ctx.rng.uniform(0.7, 1.6), 4)
    return _do(ctx, out, _ats_body(ctx, 0.15, 1.6, fmod),
               ctx.sdur * ctx.rng.choice([4, 5, 6]))

def a_shift(ctx, out):                             # transposed spectral recast
    if not ctx.atsfile:
        return False
    fmod = ctx.rng.choice([0.5, 1.5, 2.0])
    return _do(ctx, out, _ats_body(ctx, 0.9, 0.5, fmod),
               ctx.sdur * ctx.rng.choice([2, 3, 4]))

# ---- LPC: formant/source-filter recasting ----------------------------------- #
def _lpc_head(ctx):
    return (f'  ktime line 0, p3, {ctx.sdur:.3f}\n'
            f'  krmsr, krmso, kerr, kcps lpread ktime, "{ctx.lpcfile}"')

def l_buzz(ctx, out):                              # buzz excitation -> vocal/robotic
    if not ctx.lpcfile:
        return False
    f0 = round(ctx.rng.uniform(70, 180), 2)
    nh = ctx.rng.choice([20, 40, 80])
    body = (f'{_lpc_head(ctx)}\n'
            f'  aexc buzz krmso*3, {f0}, {nh}, giSine\n'
            f'  ao lpreson aexc\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.010')
    return _do(ctx, out, body, ctx.sdur * ctx.rng.choice([1, 2, 3]))

def l_noise(ctx, out):                             # noise excitation -> breathy/whispered
    if not ctx.lpcfile:
        return False
    body = (f'{_lpc_head(ctx)}\n'
            f'  aexc noise krmso*4, 0\n'
            f'  ao lpreson aexc\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.010')
    return _do(ctx, out, body, ctx.sdur * ctx.rng.choice([1, 2]))

def l_talk(ctx, out):                              # pitch-tracked buzz -> talking, transposed
    if not ctx.lpcfile:
        return False
    ratio = round(ctx.rng.choice([0.5, 0.75, 1.5, 2.0]), 3)
    nh = ctx.rng.choice([30, 60])
    body = (f'{_lpc_head(ctx)}\n'
            f'  aexc buzz krmso*3, kcps*{ratio}, {nh}, giSine\n'
            f'  ao lpreson aexc\n'
            f'  aLo = ao\n'
            f'  aRo delay ao, 0.009')
    return _do(ctx, out, body, ctx.sdur * ctx.rng.choice([1, 2]))


FAMILIES: dict[str, list] = {
    "GRAIN": [g_stretch, g_scrub],
    "MODAL": [m_bank],
    "XSPEC": [x_morph, x_shiftblur],
    "ATS":   [a_drone, a_noise, a_shift],
    "LPC":   [l_buzz, l_noise, l_talk],
}


def _ok2(p: Path, minb: int = 200) -> bool:
    try:
        return p.exists() and p.stat().st_size >= minb
    except OSError:
        return False


def _analyse_ats(ctx: _Ctx, mono: Path) -> str | None:
    ats = ctx.work / "_cs_src.ats"
    return str(ats) if ctx.util("atsa", str(mono), str(ats)) and _ok2(ats) else None


def _analyse_lpc(ctx: _Ctx, mono: Path) -> str | None:
    lpc = ctx.work / "_cs_src.lpc"
    ok = ctx.util("lpanal", "-p40", "-h200", "-P70", "-Q400", str(mono), str(lpc))
    return str(lpc) if ok and _ok2(lpc) else None


def _family_ready(ctx: _Ctx, fam: str) -> bool:
    if fam == "ATS":
        return ctx.atsfile is not None
    if fam == "LPC":
        return ctx.lpcfile is not None
    return True


def generate(src_wav: str, out_dir: str, count: int = 8,
             seed: int | None = None, on_ready=None) -> list[str]:
    """Record-driven entry point (same shape as cdp.generate): one source WAV ->
    up to `count` tonally-distinct Csound variations under out_dir (var0.wav ..),
    dealt round-robin across the FAMILIES. `on_ready(index, path)` fires the moment
    each variation is ready (progressive fill). No filler — a failed render is
    retried with a DIFFERENT real archetype; if the source is pathological fewer
    than `count` are returned."""
    if not CS_BIN.exists():
        return []
    src0 = Path(src_wav)
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(4), "big"))
    ctx = _Ctx(work, rng, "cs")

    # 1. prepare the mono, level-lifted source every archetype + analysis reads.
    mono = work / "_cs_src_mono.wav"
    sdur0 = _dur(src0)
    if not ctx.render(_PREP.format(out=str(mono), src=str(src0), makeup=2.5,
                                   dur=f"{sdur0:.3f}"), mono):
        return []
    ctx.src = mono
    ctx.sdur = _dur(mono)
    # 2. the two analysis-based families need a one-off analysis pass (cached, shared).
    ctx.atsfile = _analyse_ats(ctx, mono)
    ctx.lpcfile = _analyse_lpc(ctx, mono)

    # 3. deal variations round-robin across the ready families.
    fams = [f for f in FAMILIES if _family_ready(ctx, f)]
    rng.shuffle(fams)
    decks = {f: list(FAMILIES[f]) for f in fams}
    for f in decks:
        rng.shuffle(decks[f])
    cursor = {f: 0 for f in fams}

    def _pick(fam):
        deck = decks[fam]
        arch = deck[cursor[fam] % len(deck)]
        cursor[fam] += 1
        return arch

    outs: list[str] = []
    k = 0
    fi = 0
    attempts = 0
    max_attempts = count * 6
    while k < count and attempts < max_attempts and fams:
        fam = fams[fi % len(fams)]
        fi += 1
        attempts += 1
        out = work / f"var{k}.wav"
        try:
            if _pick(fam)(ctx, out) and _ok(out):
                outs.append(str(out))
                if on_ready:
                    on_ready(k, out)
                k += 1
        except Exception:
            pass
    ctx.sweep()      # drop the mono/ats/lpc/.csd intermediates; var*.wav survive
    return outs
