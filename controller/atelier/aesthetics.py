"""Guided generative aesthetics.

When a randomizer runs in "Guided" mode the user picks an artist whose well-
established aesthetic shapes the generated values (vs. "Free" full randomization).

Three layers, in priority order, make it module-aware and close to each artist's
stated scope:

1. OVERRIDES[artist][param_id] — explicit targets for the *defining* parameters of
   an aesthetic (e.g. Lustmord verb.decay near max, Ben Frost distort.drive extreme).
2. classify(meta) -> ROLE — every other parameter is mapped by id/label into a
   semantic role (time / space / rate / bright / dirt / pitch / motion / level /
   density / generic); PROFILES[artist][role] gives a (centre, spread) tendency.
3. WET[artist][module] — the wet/dry mix of character/ambience effects, the single
   biggest perceptual lever (so "Lustmord" actually drowns things in reverb).

Targets are drawn as gauss(centre, spread) in the parameter's *musical* normalised
band (safe) unless flagged `full` (e.g. Ben Frost / Autechre extremity), then mapped
through the param's own curve/enum and clamped.

Profiles derive from the aesthetic comparison brief:
  Vidna Obmana  — immersive ambient, dense slow drones, spacious, warm
  Lustmord      — dark ambient, cavernous, static, sub-bass, vast reverberation
  Bernard Parmegiani — musique concrète, sculpted/transformational, spatial, exploratory
  Ben Frost     — abrasive/high-impact, distortion + noise, dramatic contrasts
  Autechre      — algorithmic, digital, fragmented, complex, generative DSP
"""
from __future__ import annotations

from .params import Curve, ParamMetadata, Rate

ARTISTS: list[str] = ["vidna_obmana", "lustmord", "bernard_parmegiani", "ben_frost", "autechre"]
ARTIST_LABELS: dict[str, str] = {
    "vidna_obmana": "Vidna Obmana",
    "lustmord": "Lustmord",
    "bernard_parmegiani": "Bernard Parmegiani",
    "ben_frost": "Ben Frost",
    "autechre": "Autechre",
}

TIME, SPACE, RATE, BRIGHT, DIRT, PITCH, MOTION, LEVEL, DENSITY, GENERIC = (
    "time", "space", "rate", "bright", "dirt", "pitch", "motion", "level", "density", "generic")

# Full param-id → (role, invert) for params the keyword cascade gets wrong or where
# a specific reading matters. invert flips the artist target (high value = "less").
SPECIAL: dict[str, tuple[str, bool]] = {
    "distort.type": (DIRT, False),      # enum ordered ~tube→cheby = increasing harshness
    "distort.tone": (BRIGHT, False),
    "distort.bias": (DIRT, False),
    "dx7.algorithm": (DIRT, False),     # FM algorithm ≈ spectral complexity
    "comb.brightness": (BRIGHT, False),
    "comb.damping": (BRIGHT, True),
    "comb.drive": (DIRT, False),
    "rings.bright": (BRIGHT, False),
    "rings.damp": (BRIGHT, True),
    "rings.struct": (MOTION, False),
    "gate.duty": (MOTION, False),
    "buchloid.timbre": (BRIGHT, False),
    "buchloid.waveFolds": (DIRT, False),
    "clouds.tex": (BRIGHT, False),
    "clouds.dens": (DENSITY, False),
    "grains.size": (TIME, False), "grains.density": (DENSITY, False),
    "grains.spray": (MOTION, False),
    "grains.jitter": (MOTION, False), "grains.spread": (SPACE, False),
    "grains.reverse": (MOTION, False), "grains.shimmer": (BRIGHT, False),
    "grains.scan": (RATE, False), "grains.texture": (BRIGHT, False),
    "grains.chaos": (MOTION, False), "grains.feedback": (DIRT, False),
    "pitch.feedback": (DIRT, False),
    "time.feedbackSend": (DIRT, False),
}

_DIRT = ("drive", "fuzz", "fold", "crush", "downsample", "feedback", "wrap", "bias",
         "chaos", "dispersion", "rungler", "distort", "squiz", "disint", "overdrive", "noisecolor")
_SPACE = ("size", "reverb", "rvb", "room", "width", "spatial", "diffus", "earlydiff")
_TIME = ("attack", "decay", "release", "delay", "grain", "tap", "buffer", "glide",
         "xfade", "erase", "sustain", "hold", "time")
_RATE_HINT = ("lfo", "mod", "trig", "trem", "drift", "clock", "swing", "step")
_RATE = ("rate", "clock", "speed", "density", "tempo")
_MOTION = ("jitter", "swing", "probability", "drift", "scatter", "smear",
           "moddepth", "randompitch", "randomtime", "warp", "ratchet", "depth", "pmd", "amd")
_BRIGHT = ("cut", "center", "centre", "bright", "tone", "color", "colour", "filter",
           "formant", "timbre", "slope", "bandwidth", "peak", "morph", "harm", "damp")
_PITCH = ("pitch", "note", "semitone", "cent", "tuning", "ratio", "coarse", "fine",
          "detune", "transpose", "freq", "struct")
_LEVEL = ("amp", "gain", "level", "velocity", "duck", "presence", "outmult")
_DENSITY = ("node", "voice", "poly", "stack", "maxvoice", "density")
_INVERT = ("damp",)


def classify(meta: ParamMetadata) -> tuple[str, bool]:
    fid = meta.id.lower()
    if fid in SPECIAL:
        return SPECIAL[fid]
    pid = fid.split(".", 1)[-1]
    text = pid + " " + meta.label.lower()
    invert = any(k in pid for k in _INVERT)

    def has(words):
        return any(w in text for w in words)

    if has(_DIRT):
        return DIRT, invert
    if has(_SPACE):
        return SPACE, invert
    if has(_BRIGHT):
        return BRIGHT, invert
    if "freq" in text or "rate" in text:
        if has(_RATE) or has(_RATE_HINT):
            return RATE, invert
    if has(_RATE):
        return RATE, invert
    if has(_MOTION):
        return MOTION, invert
    if has(_TIME):
        return TIME, invert
    if has(_PITCH):
        return PITCH, invert
    if has(_DENSITY):
        return DENSITY, invert
    if has(_LEVEL):
        return LEVEL, invert
    return GENERIC, invert


# role -> (centre, spread) in normalised musical space.
PROFILES: dict[str, dict[str, tuple[float, float]]] = {
    "vidna_obmana": {
        TIME: (0.80, 0.14), SPACE: (0.70, 0.15), RATE: (0.12, 0.08), BRIGHT: (0.45, 0.14),
        DIRT: (0.08, 0.06), PITCH: (0.45, 0.14), MOTION: (0.22, 0.10), LEVEL: (0.50, 0.09),
        DENSITY: (0.70, 0.16), GENERIC: (0.48, 0.14),
    },
    "lustmord": {
        TIME: (0.95, 0.06), SPACE: (0.95, 0.05), RATE: (0.04, 0.04), BRIGHT: (0.12, 0.08),
        DIRT: (0.18, 0.10), PITCH: (0.08, 0.06), MOTION: (0.05, 0.04), LEVEL: (0.45, 0.09),
        DENSITY: (0.32, 0.16), GENERIC: (0.36, 0.14),
    },
    "bernard_parmegiani": {
        TIME: (0.50, 0.26), SPACE: (0.62, 0.20), RATE: (0.42, 0.24), BRIGHT: (0.55, 0.24),
        DIRT: (0.34, 0.20), PITCH: (0.50, 0.24), MOTION: (0.62, 0.20), LEVEL: (0.52, 0.14),
        DENSITY: (0.48, 0.24), GENERIC: (0.50, 0.26),
    },
    "ben_frost": {
        TIME: (0.38, 0.26), SPACE: (0.40, 0.18), RATE: (0.46, 0.24), BRIGHT: (0.70, 0.16),
        DIRT: (0.80, 0.14), PITCH: (0.38, 0.18), MOTION: (0.52, 0.20), LEVEL: (0.62, 0.13),
        DENSITY: (0.48, 0.22), GENERIC: (0.50, 0.24),
    },
    "autechre": {
        TIME: (0.30, 0.24), SPACE: (0.36, 0.22), RATE: (0.66, 0.20), BRIGHT: (0.64, 0.22),
        DIRT: (0.62, 0.20), PITCH: (0.50, 0.22), MOTION: (0.72, 0.18), LEVEL: (0.54, 0.14),
        DENSITY: (0.66, 0.20), GENERIC: (0.52, 0.28),
    },
}
FULL_ROLES: dict[str, set[str]] = {
    "vidna_obmana": set(),
    "lustmord": {SPACE, TIME},   # extreme reverb is calm, not chaotic
    "bernard_parmegiani": set(),
    "ben_frost": {DIRT},         # the one distortion is meant to be extreme
    "autechre": {DIRT},          # was {DIRT, MOTION} — full-range motion = chaos
}

# Defining-parameter overrides: param_id -> (centre, spread, full).
OVERRIDES: dict[str, dict[str, tuple[float, float, bool]]] = {
    "vidna_obmana": {
        "verb.decay": (0.6, 0.15, False), "verb.size": (0.62, 0.12, False),
        "distort.drive": (0.1, 0.05, False), "distort.type": (0.1, 0.08, False),
        "dx7.transpose": (0.5, 0.15, False),
        "gate.probability": (0.95, 0.06, False),
    },
    "lustmord": {
        "verb.decay": (0.98, 0.03, True), "verb.size": (0.92, 0.06, False),
        "verb.highCut": (0.15, 0.08, False), "verb.damp": (0.72, 0.1, False),
        "comb.decay": (0.9, 0.08, False), "comb.feedback": (0.7, 0.1, False),
        "dx7.transpose": (0.02, 0.04, False),
        "distort.type": (0.12, 0.08, False), "gate.probability": (0.9, 0.08, False),
    },
    "bernard_parmegiani": {
        "pitch.feedback": (0.5, 0.22, False), "time.feedbackSend": (0.5, 0.22, False),
        "distort.type": (0.5, 0.3, False), "verb.size": (0.6, 0.25, False),
    },
    "ben_frost": {
        "distort.drive": (0.88, 0.12, True), "distort.type": (0.45, 0.18, False),
        "distort.crush": (0.5, 0.2, False), "distort.fold": (0.45, 0.25, False),
        "comb.feedback": (0.6, 0.2, False),
        "verb.decay": (0.3, 0.2, False),
    },
    "autechre": {
        "distort.type": (0.85, 0.15, False), "distort.crush": (0.22, 0.15, False),
        "distort.downsample": (0.55, 0.25, False), "distort.drive": (0.6, 0.25, True),
        "time.feedbackSend": (0.6, 0.2, False), "gate.probability": (0.55, 0.25, False),
        "verb.decay": (0.25, 0.2, False),
    },
}

# Wet/dry of character / ambience effects (module type -> wet 0..1). Modules absent
# from a table keep their existing wet. Pure sources are never touched.
WET: dict[str, dict[str, float]] = {
    "vidna_obmana": {"VERB": 0.55, "SDLY": 0.3, "CLOUDS": 0.55, "GRAINS": 0.5, "COMB": 0.4, "PITCH": 0.35, "TIME": 0.35, "DISTORT": 0.15},
    "lustmord": {"VERB": 0.92, "SDLY": 0.5, "CLOUDS": 0.6, "GRAINS": 0.55, "COMB": 0.6, "PITCH": 0.3, "TIME": 0.45, "DISTORT": 0.4},
    "bernard_parmegiani": {"VERB": 0.55, "SDLY": 0.55, "CLOUDS": 0.6, "GRAINS": 0.6, "COMB": 0.5, "PITCH": 0.65, "TIME": 0.6, "DISTORT": 0.4},
    "ben_frost": {"VERB": 0.35, "SDLY": 0.4, "CLOUDS": 0.45, "GRAINS": 0.5, "COMB": 0.55, "PITCH": 0.4, "TIME": 0.45, "DISTORT": 0.8},
    "autechre": {"VERB": 0.3, "SDLY": 0.5, "CLOUDS": 0.55, "GRAINS": 0.6, "COMB": 0.55, "PITCH": 0.5, "TIME": 0.55, "DISTORT": 0.65},
}

# Default wet for the ported pedal FX (applied to every artist that doesn't set
# its own). EQ is full-wet; modulation/grit FX sit lower in the blend.
_FX_WET = {"OVERDRIVE": 0.6, "AMPSIM": 0.6, "EQUALIZER": 1.0, "FLANGER": 0.45,
           "PHASER": 0.45, "RINGMOD": 0.4, "BITCRUSHER": 0.5, "LOFI": 0.5,
           "TREMOLO": 0.6, "WAVEFOLDER": 0.5}
for _a in WET:
    for _t, _v in _FX_WET.items():
        WET[_a].setdefault(_t, _v)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def guided_value(rng, meta: ParamMetadata, artist: str, expert: bool = False) -> float:
    """A parameter value shaped by the artist aesthetic for this parameter."""
    artist = artist if artist in PROFILES else "vidna_obmana"
    # Pan is a placement decision, not an aesthetic centre — spread modules across
    # the field (otherwise the role cascade parks every module dead-centre).
    if meta.id.split(".", 1)[-1].lower() in ("pan", "spatialpos"):
        lo = meta.musical_min if meta.musical_min is not None else meta.rmin
        hi = meta.musical_max if meta.musical_max is not None else meta.rmax
        return meta.clamp(rng.uniform(lo, hi))
    ov = OVERRIDES.get(artist, {}).get(meta.id)
    if ov is not None:
        center, spread, full = ov
    else:
        role, invert = classify(meta)
        center, spread = PROFILES[artist].get(role, PROFILES[artist][GENERIC])
        if invert:
            center = 1.0 - center
        full = role in FULL_ROLES.get(artist, set())
    p = _clamp01(rng.gauss(center, spread))
    # Enums carry a metadata quirk (musical range is [0,1] while the real index range
    # is [0, n-1]), so map them across the FULL index range, not the musical band —
    # otherwise every enum collapses to its first one or two values.
    if full or meta.curve is Curve.ENUM:
        norm = p
    else:
        lo = meta.to_norm(meta.musical_min if meta.musical_min is not None else meta.rmin)
        hi = meta.to_norm(meta.musical_max if meta.musical_max is not None else meta.rmax)
        if hi < lo:
            lo, hi = hi, lo
        norm = lo + p * (hi - lo)
    val = meta.to_value(norm)
    if meta.curve is Curve.ENUM or meta.rate is Rate.DISCRETE:
        val = round(val)
    return meta.clamp(val, expert_override=expert)


def guided_wet(rng, artist: str, module_type: str) -> float | None:
    """Target wet/dry for a character effect, or None to leave it unchanged."""
    table = WET.get(artist, {})
    if module_type not in table:
        return None
    return _clamp01(table[module_type] + rng.uniform(-0.08, 0.08))


# =========================================================================== #
# Comprehensive / structural guidance.
#
# Per-parameter shaping (above) is necessary but not sufficient: an aesthetic is
# as much about WHICH modules are present, HOW they move (modulation), and WHETHER
# there is rhythm (the sequencer) as it is about any single knob. The tables below
# drive those structural decisions so a Guided patch reads as the artist end-to-
# end, not just a freely-random patch with nudged knobs.
#
#   MODULE_PALETTE  — which modules to instantiate + how many (Patch/Chain scope)
#   LFO_PROFILE     — how the modulation bank (+ per-module LFOs) should behave
#   SEQ_PROFILE     — whether/how to drive the MIDI sequencer
# =========================================================================== #

# Sound generators (self-sounding) vs. effects (inserts). The single biggest cause
# of cacophony is too many simultaneous voices, so a patch gets a SMALL number of
# sources (`sources_count`, each type at most once) feeding a chain of effects up
# to `count` total. `require` effects are guaranteed; DISTORT is capped at one.
# Pure generators (self-sounding). RINGS is the only HYBRID source — it can sit
# silent with no input/excitation, so it must never be a patch's ONLY source, or
# the patch has no audible generator at all.
PURE_GENERATORS = {"DX7", "BUCHLOID", "BEN"}

MODULE_PALETTE: dict[str, dict] = {
    "vidna_obmana": {  # immersive ambient — one or two warm voices bathed in space
        "sources": {"DX7": 3, "RINGS": 1},
        "effects": {"VERB": 3, "CLOUDS": 3, "GRAINS": 2, "SDLY": 2, "COMB": 1, "PITCH": 1},
        "sources_count": (1, 2), "count": (7, 12), "require": ["VERB", "CLOUDS"],
    },
    "lustmord": {  # dark ambient — a single deep drone in a cavern
        "sources": {"DX7": 3, "BUCHLOID": 1},
        "effects": {"VERB": 4, "COMB": 2, "SDLY": 1, "PITCH": 1},
        "sources_count": (1, 1), "count": (6, 10), "require": ["VERB"],
    },
    "bernard_parmegiani": {  # musique concrète — one voice, transformed in space
        "sources": {"RINGS": 2, "DX7": 2, "BUCHLOID": 1},
        "effects": {"PITCH": 3, "TIME": 3, "COMB": 2, "VERB": 2, "CLOUDS": 2, "GRAINS": 2, "SDLY": 2, "GATE": 1},
        "sources_count": (1, 2), "count": (8, 14), "require": ["TIME", "PITCH"],
    },
    "ben_frost": {  # abrasive — a voice driven hard, rhythmic gating, little reverb
        "sources": {"DX7": 3, "BUCHLOID": 1},
        "effects": {"DISTORT": 4, "GATE": 2, "COMB": 1, "VERB": 1, "SDLY": 1},
        "sources_count": (1, 2), "count": (7, 12), "require": ["DISTORT", "GATE"],
    },
    "autechre": {  # algorithmic — one or two voices, fragmented, digital artefacts
        "sources": {"RINGS": 2, "DX7": 2},
        "effects": {"DISTORT": 2, "GATE": 3, "TIME": 2, "COMB": 2, "SDLY": 2, "GRAINS": 2},
        "sources_count": (1, 2), "count": (8, 14), "require": ["GATE"],
    },
}

# Fold the ported pedal FX into each artist's effect palette so guided generation
# can reach for them where they fit the aesthetic.
_PALETTE_FX = {
    "vidna_obmana": {"EQUALIZER": 1, "FLANGER": 1, "TREMOLO": 1},
    "lustmord": {"EQUALIZER": 1, "LOFI": 1},
    "bernard_parmegiani": {"FLANGER": 2, "PHASER": 2, "RINGMOD": 1, "EQUALIZER": 1},
    "ben_frost": {"OVERDRIVE": 2, "AMPSIM": 1, "BITCRUSHER": 2, "WAVEFOLDER": 2},
    "autechre": {"BITCRUSHER": 2, "RINGMOD": 2, "LOFI": 2, "PHASER": 1},
}
for _a, _fx in _PALETTE_FX.items():
    MODULE_PALETTE[_a]["effects"].update(_fx)

# Modulation bank behaviour. rate band is in Hz (log-distributed); depth 0..1 but
# kept MODEST — depth is a full-range multiplier, so high values swing params wildly
# and destabilise the patch. `routed` is the fraction of LFOs that get a target;
# `roles` is curated to timbral/spatial motion (PITCH/LEVEL are excluded to avoid
# atonal drift and pumping).
# Canonical signal-flow order for a deliberate effect chain (low = early). The
# guided wirer sorts a patch's effects by this so the processing reads like a
# real chain (tone -> dirt -> modulation -> pitch/texture -> time -> space)
# instead of random parallel summing, which is the main cause of cacophony.
CHAIN_ORDER: dict[str, int] = {
    "EQUALIZER": 12, "COMB": 14,
    "GATE": 20,
    "DISTORT": 30, "OVERDRIVE": 31, "AMPSIM": 32,
    "WAVEFOLDER": 33, "BITCRUSHER": 34, "LOFI": 35, "RINGMOD": 36,
    "FLANGER": 40, "PHASER": 41, "TREMOLO": 42,
    "PITCH": 50, "RINGS": 51, "CLOUDS": 52, "GRAINS": 53,
    "SDLY": 60, "TIME": 62,
    "VERB": 70,
}
def chain_rank(module_type: str) -> int:
    return CHAIN_ORDER.get(module_type, 45)


LFO_PROFILE: dict[str, dict] = {
    "vidna_obmana": {"count": 5, "rate": (0.01, 0.07), "depth": (0.08, 0.26),
                     "shapes": ["sine", "sine", "triangle"], "routed": 0.6,
                     "roles": {SPACE, TIME, BRIGHT}},
    "lustmord": {"count": 4, "rate": (0.004, 0.035), "depth": (0.06, 0.20),
                 "shapes": ["sine"], "routed": 0.5,
                 "roles": {SPACE, TIME, BRIGHT}},
    "bernard_parmegiani": {"count": 6, "rate": (0.03, 0.5), "depth": (0.12, 0.40),
                           "shapes": ["sine", "triangle", "sh"], "routed": 0.7,
                           "roles": {SPACE, MOTION, TIME, BRIGHT}},
    "ben_frost": {"count": 5, "rate": (0.05, 0.9), "depth": (0.14, 0.42),
                  "shapes": ["triangle", "sine", "sh"], "routed": 0.65,
                  "roles": {DIRT, BRIGHT, MOTION}},
    "autechre": {"count": 7, "rate": (0.1, 3.5), "depth": (0.16, 0.46),
                 "shapes": ["sh", "triangle", "sine"], "routed": 0.75,
                 "roles": {MOTION, BRIGHT, RATE}},
}

# Rhythm is NOT driven by the MIDI sequencer in generative patches — it is left to
# the GATE module (clock-driven rhythmic gate) wherever an aesthetic calls for it:
# rhythmic artists carry GATE in their palette (Ben Frost / Autechre require it,
# Parmegiani may grow one), so a guided patch gets its pulse from a GATE insert in
# the signal path rather than from an auto-added SEQ.


# MOLLY ("Molly the Poly") sound types — its Lua control layer scopes three uses:
# lead, pad, percussion. These real-unit ranges port that randomizer so GUIDED
# randomization of a MOLLY draws values appropriate to the active `scope` param.
MOLLY_SCOPES = ["lead", "pad", "percussion"]
MOLLY_SCOPE: dict[str, dict[str, tuple[float, float]]] = {
    "lead": {   # bright, cutting, sustained
        "detune": (2, 12), "oscShape": (0.45, 1.0), "pulseWidth": (0.2, 0.6),
        "subLevel": (0.0, 0.4), "noiseLevel": (0.0, 0.1),
        "cutoff": (1500, 8000), "resonance": (0.2, 0.6), "filterEnvAmt": (0.1, 0.5),
        "fAtk": (0.001, 0.05), "fDec": (0.05, 0.4), "fSus": (0.3, 0.8), "fRel": (0.1, 0.5),
        "aAtk": (0.001, 0.03), "aDec": (0.1, 0.5), "aSus": (0.6, 1.0), "aRel": (0.1, 0.5),
        "lfoRate": (3, 7), "lfoToCutoff": (0.0, 0.2), "lfoToPitch": (0.0, 0.3),
        "lfoToPW": (0.0, 0.2), "lfoToAmp": (0.0, 0.1),
        "ringMod": (0.0, 0.2), "drive": (0.1, 0.5), "chorus": (0.0, 0.3),
    },
    "pad": {    # lush, slow, wide, evolving
        "detune": (5, 25), "oscShape": (0.2, 0.8), "pulseWidth": (0.3, 0.7),
        "subLevel": (0.1, 0.5), "noiseLevel": (0.0, 0.1),
        "cutoff": (400, 2500), "resonance": (0.1, 0.4), "filterEnvAmt": (0.2, 0.7),
        "fAtk": (0.3, 2.0), "fDec": (0.5, 2.0), "fSus": (0.4, 0.9), "fRel": (1.0, 5.0),
        "aAtk": (0.4, 2.5), "aDec": (0.5, 2.0), "aSus": (0.6, 1.0), "aRel": (1.5, 6.0),
        "lfoRate": (0.1, 1.5), "lfoToCutoff": (0.0, 0.4), "lfoToPitch": (0.0, 0.1),
        "lfoToPW": (0.0, 0.4), "lfoToAmp": (0.0, 0.2),
        "ringMod": (0.0, 0.1), "drive": (0.0, 0.2), "chorus": (0.4, 0.9),
    },
    "percussion": {  # plucky, transient, short
        "detune": (0, 10), "oscShape": (0.3, 1.0), "pulseWidth": (0.1, 0.6),
        "subLevel": (0.0, 0.4), "noiseLevel": (0.1, 0.6),
        "cutoff": (800, 6000), "resonance": (0.3, 0.8), "filterEnvAmt": (0.4, 0.9),
        "fAtk": (0.001, 0.01), "fDec": (0.03, 0.25), "fSus": (0.0, 0.2), "fRel": (0.03, 0.2),
        "aAtk": (0.001, 0.01), "aDec": (0.05, 0.35), "aSus": (0.0, 0.15), "aRel": (0.03, 0.25),
        "lfoRate": (2, 10), "lfoToCutoff": (0.0, 0.2), "lfoToPitch": (0.0, 0.1),
        "lfoToPW": (0.0, 0.2), "lfoToAmp": (0.0, 0.1),
        "ringMod": (0.0, 0.4), "drive": (0.2, 0.7), "chorus": (0.0, 0.2),
    },
}
# Which Molly scope each artist leans on when guided generation spawns a MOLLY.
ARTIST_MOLLY_SCOPE: dict[str, str] = {
    "vidna_obmana": "pad", "lustmord": "pad", "bernard_parmegiani": "pad",
    "ben_frost": "lead", "autechre": "percussion",
}


def pick_molly_scope(rng, artist: str) -> str:
    """A MOLLY voice type. Was pinned to one scope per artist, so MOLLY always came
    out the same (a detuned 'pad' drone for the ambient artists). Bias toward the
    artist's preferred scope but roll the others in too, for real variety."""
    pref = ARTIST_MOLLY_SCOPE.get(artist, "lead")
    if rng.random() < 0.5:
        return pref
    return rng.choice(MOLLY_SCOPES)


def molly_value(rng, meta: ParamMetadata, scope: str) -> float | None:
    """A scope-appropriate value for a MOLLY param (or None to defer to the generic
    guided shaping for params the scope recipe doesn't cover)."""
    table = MOLLY_SCOPE.get(scope) or MOLLY_SCOPE["lead"]
    short = meta.id.split(".", 1)[-1]
    if short in table:
        lo, hi = table[short]
        return meta.clamp(rng.uniform(lo, hi))
    return None


def _logrand(rng, lo: float, hi: float) -> float:
    lo = max(1e-6, lo)
    return lo * ((hi / lo) ** rng.random())


def _weighted_bag(weights: dict[str, int]) -> list[str]:
    bag: list[str] = []
    for k, w in weights.items():
        bag.extend([k] * max(1, int(w)))
    return bag


def pick_modules(rng, artist: str) -> list[str] | None:
    """An artist-congruent module set: a small number of voices + a chain of
    effects (no graph wiring — the caller wires). Few sources is the primary guard
    against cacophony, so sources are capped hard and each source type used once."""
    pal = MODULE_PALETTE.get(artist)
    if not pal:
        return None
    total = rng.randint(*pal["count"])
    ns_lo, ns_hi = pal.get("sources_count", (1, 2))
    n_sources = max(1, rng.randint(ns_lo, ns_hi))
    chosen: list[str] = []
    used: dict[str, int] = {}

    def take(t: str, cap: int) -> bool:
        if used.get(t, 0) < cap and len(chosen) < total:
            chosen.append(t)
            used[t] = used.get(t, 0) + 1
            return True
        return False

    src_bag = _weighted_bag(pal["sources"])
    # Guarantee the FIRST source is a PURE generator (self-sounding), so no patch
    # ends up with RINGS as its only — and possibly silent — source.
    pure = {s: w for s, w in pal["sources"].items() if s in PURE_GENERATORS}
    if pure:
        take(rng.choice(_weighted_bag(pure)), cap=1)
    guard = 0
    while sum(used.get(s, 0) for s in pal["sources"]) < n_sources and guard < 100:
        guard += 1
        take(rng.choice(src_bag), cap=1)        # each source type at most once
    for t in pal.get("require", []):            # defining effects guaranteed
        take(t, cap=1)
    fx_bag = _weighted_bag(pal["effects"])
    guard = 0
    while len(chosen) < total and guard < 300:
        guard += 1
        t = rng.choice(fx_bag)
        take(t, cap=1 if t == "DISTORT" else 2)  # never stack distortions
    return chosen


def lfo_plan(rng, artist: str, targets: list[tuple[str, str, str]]) -> list[dict]:
    """Per-LFO {shape, rate, depth, target} list. `targets` is (mid, pid, role);
    routes prefer the artist's motion roles. target "" leaves an LFO free-running."""
    prof = LFO_PROFILE.get(artist)
    if not prof:
        return []
    roles = prof["roles"]
    preferred = [t for t in targets if t[2] in roles]
    pool = list(preferred or targets)
    rng.shuffle(pool)
    plans: list[dict] = []
    used: set[str] = set()      # avoid stacking several LFOs on one param (fighting)
    for _ in range(int(prof["count"])):
        tgt = ""
        if pool and rng.random() < prof["routed"]:
            fresh = [t for t in pool if f"{t[0]}|{t[1]}" not in used]
            mid, pid, _role = rng.choice(fresh) if fresh else rng.choice(pool)
            tgt = f"{mid}|{pid}"
            used.add(tgt)
        plans.append({"shape": rng.choice(prof["shapes"]),
                      "rate": _logrand(rng, *prof["rate"]),
                      "depth": rng.uniform(*prof["depth"]),
                      "target": tgt})
    return plans
