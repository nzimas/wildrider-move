"""Parameter metadata system.

This is the foundation of the whole instrument. The blueprint (section 9 and the
design constraints) is emphatic: *build the data model and parameter metadata
first*. Every parameter is machine-describable so that MIDI learn, modulation,
scene morphing, constrained randomization, UI generation, validation and safe
patch migration can all be derived rather than hand-wired per widget.

A ``ParamMetadata`` describes a parameter type. A ``ParamSlot`` is a concrete,
addressable instance holding a base value plus a summed modulation offset, with
smoothing and a safety clamp applied on the way out to the DSP graph.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Enumerations from the blueprint's metadata contract (section 9).
# --------------------------------------------------------------------------- #
class Curve(str, Enum):
    """Scaling curve mapping a normalised position in [0,1] to a value."""

    LINEAR = "linear"
    EXP = "exp"
    LOG = "log"
    BIPOLAR = "bipolar"   # linear but centred, normalised 0.5 == 0 value
    DB = "dB"             # decibel taper, value stored linear-amplitude
    SEMITONE = "semitone"
    ENUM = "enum"


class Rate(str, Enum):
    CONTROL = "control"
    AUDIO = "audio"
    TRIGGER = "trigger"
    DISCRETE = "discrete"


class RandomizePolicy(str, Enum):
    OFF = "off"
    SAFE = "safe"                 # stay within musicalRange
    WIDE = "wide"                 # full range
    EXPERT = "expert"             # full range, only when expert override armed
    DISCRETE_WEIGHTED = "discreteWeighted"


class DangerClass(str, Enum):
    NONE = "none"
    LOUDNESS = "loudness"
    FEEDBACK = "feedback"
    ROUTING = "routing"
    CPU = "CPU"
    DESTRUCTIVE = "destructive"


# --------------------------------------------------------------------------- #
# Display formatters. The blueprint lists percent1, dB1, Hz, noteName, etc.
# --------------------------------------------------------------------------- #
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _fmt_percent1(v: float, _u: str) -> str:
    return f"{v * 100:.1f}%"


def _fmt_percent0(v: float, _u: str) -> str:
    return f"{v * 100:.0f}%"


def _fmt_db1(v: float, _u: str) -> str:
    # value stored as linear amplitude; show as dB
    if v <= 0:
        return "-inf dB"
    return f"{20.0 * math.log10(v):.1f} dB"


def _fmt_db_value(v: float, _u: str) -> str:
    # value already in dB
    return f"{v:+.1f} dB"


def _fmt_hz(v: float, _u: str) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f} kHz"
    return f"{v:.1f} Hz"


def _fmt_ms(v: float, _u: str) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    return f"{v:.1f} ms"


def _fmt_semitone(v: float, _u: str) -> str:
    return f"{v:+.2f} st"


def _fmt_note(v: float, _u: str) -> str:
    midi = int(round(v))
    return f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def _fmt_float2(v: float, u: str) -> str:
    return f"{v:.2f}{(' ' + u) if u and u != 'none' else ''}"


def _fmt_int(v: float, u: str) -> str:
    return f"{int(round(v))}{(' ' + u) if u and u != 'none' else ''}"


FORMATTERS: dict[str, Callable[[float, str], str]] = {
    "percent1": _fmt_percent1,
    "percent0": _fmt_percent0,
    "dB1": _fmt_db1,
    "dBValue": _fmt_db_value,
    "Hz": _fmt_hz,
    "ms": _fmt_ms,
    "semitone": _fmt_semitone,
    "noteName": _fmt_note,
    "float2": _fmt_float2,
    "int": _fmt_int,
}


@dataclass
class ParamMetadata:
    """Describes a parameter type. Mirrors the save-format table in section 9."""

    id: str                                  # stable symbolic id, e.g. "comb.feedback"
    label: str                               # human readable
    unit: str = "none"                       # %, Hz, semitone, ms, dB, none
    rmin: float = 0.0                        # hard range min (clamp)
    rmax: float = 1.0                        # hard range max (clamp)
    musical_min: float | None = None         # default randomization range min
    musical_max: float | None = None         # default randomization range max
    default: float = 0.0
    curve: Curve = Curve.LINEAR
    curve_k: float = 4.0                     # steepness for exp/log curves
    rate: Rate = Rate.CONTROL
    smoothing_ms: float = 50.0
    modulatable: bool = True
    macro_eligible: bool = True
    midi_learnable: bool = True
    randomize_policy: RandomizePolicy = RandomizePolicy.SAFE
    danger_class: DangerClass = DangerClass.NONE
    formatter: str = "float2"
    enum_values: list[str] | None = None     # for Curve.ENUM / discrete
    migration: str | None = None             # note describing version transform

    def __post_init__(self) -> None:
        if self.musical_min is None:
            self.musical_min = self.rmin
        if self.musical_max is None:
            self.musical_max = self.rmax
        if self.curve is Curve.ENUM and self.enum_values:
            # enum ranges are index based
            self.rmin = 0.0
            self.rmax = float(len(self.enum_values) - 1)

    # -- scaling: normalised [0,1] <-> value ------------------------------- #
    def to_value(self, norm: float) -> float:
        """Map a normalised UI/mod position in [0,1] to an actual value."""
        norm = _clamp(norm, 0.0, 1.0)
        lo, hi = self.rmin, self.rmax
        if self.curve in (Curve.LINEAR, Curve.BIPOLAR, Curve.DB, Curve.SEMITONE):
            return lo + (hi - lo) * norm
        if self.curve is Curve.ENUM or self.rate is Rate.DISCRETE:
            return round(lo + (hi - lo) * norm)
        if self.curve is Curve.EXP:
            # exponential taper; guard against non-positive endpoints
            if lo <= 0:
                # shifted exp so it stays monotone through zero
                return lo + (hi - lo) * (math.exp(self.curve_k * norm) - 1) / (math.exp(self.curve_k) - 1)
            return lo * (hi / lo) ** norm
        if self.curve is Curve.LOG:
            return lo + (hi - lo) * (math.log1p(self.curve_k * norm) / math.log1p(self.curve_k))
        return lo + (hi - lo) * norm

    def to_norm(self, value: float) -> float:
        """Inverse of :meth:`to_value` for UI display of a concrete value."""
        lo, hi = self.rmin, self.rmax
        if hi == lo:
            return 0.0
        if self.curve in (Curve.LINEAR, Curve.BIPOLAR, Curve.DB, Curve.SEMITONE) \
                or self.curve is Curve.ENUM or self.rate is Rate.DISCRETE:
            return _clamp((value - lo) / (hi - lo), 0.0, 1.0)
        if self.curve is Curve.EXP:
            if lo <= 0:
                inner = (value - lo) / (hi - lo) * (math.exp(self.curve_k) - 1) + 1
                return _clamp(math.log(max(inner, 1e-9)) / self.curve_k, 0.0, 1.0)
            return _clamp(math.log(max(value, 1e-12) / lo) / math.log(hi / lo), 0.0, 1.0)
        if self.curve is Curve.LOG:
            t = (value - lo) / (hi - lo)
            return _clamp((math.exp(t * math.log1p(self.curve_k)) - 1) / self.curve_k, 0.0, 1.0)
        return _clamp((value - lo) / (hi - lo), 0.0, 1.0)

    # -- safety + display -------------------------------------------------- #
    def clamp(self, value: float, expert_override: bool = False) -> float:
        """Hard safety clamp. Expert override widens loudness/feedback ceilings
        only when the user has explicitly armed it (blueprint: modulation/random
        cannot push into unsafe ranges unless an expert override flag is set)."""
        lo, hi = self.rmin, self.rmax
        if not expert_override and self.danger_class in (DangerClass.FEEDBACK, DangerClass.LOUDNESS):
            # leave a margin below the hard ceiling for dangerous params
            hi = lo + (hi - lo) * 0.97
        return _clamp(value, lo, hi)

    def format(self, value: float) -> str:
        fn = FORMATTERS.get(self.formatter, _fmt_float2)
        if self.curve is Curve.ENUM and self.enum_values:
            idx = int(_clamp(round(value), 0, len(self.enum_values) - 1))
            return self.enum_values[idx]
        return fn(value, self.unit)

    def randomize(self, rng, current: float, amount: float, expert: bool) -> float:
        """Constrained randomization (GRM 'random amount': low keeps results near
        current, 1.0 is fully random within the policy range)."""
        policy = self.randomize_policy
        if policy is RandomizePolicy.OFF:
            return current
        if policy is RandomizePolicy.EXPERT and not expert:
            return current
        if policy in (RandomizePolicy.SAFE, RandomizePolicy.DISCRETE_WEIGHTED):
            lo, hi = self.musical_min, self.musical_max
        else:  # WIDE / EXPERT
            lo, hi = self.rmin, self.rmax
        target = rng.uniform(lo, hi)
        if self.curve is Curve.ENUM or self.rate is Rate.DISCRETE:
            target = round(target)
        # blend between current and a fresh target by the random amount
        out = current + (target - current) * _clamp(amount, 0.0, 1.0)
        return self.clamp(out, expert_override=expert)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["curve"] = self.curve.value
        d["rate"] = self.rate.value
        d["randomize_policy"] = self.randomize_policy.value
        d["danger_class"] = self.danger_class.value
        return d


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class ParamSlot:
    """A concrete addressable parameter on a specific node.

    Holds a *base* value (the slider position the user sets / scenes store) and a
    transient *mod_offset* summed from all modulation routes targeting it. The
    value pushed to the DSP graph is ``clamp(base + mod_offset)`` after smoothing
    (smoothing itself is applied server-side via a Lag on the control bus)."""

    meta: ParamMetadata
    base: float = field(default=0.0)
    mod_norm: float = 0.0         # summed modulation offset in normalised [0,1] space
    locked: bool = False          # excluded from randomize/scene recall (section: snapshot locks)
    learn_source: str | None = None  # MIDI/OSC learn binding id, if any

    def __post_init__(self) -> None:
        if self.base == 0.0:
            self.base = self.meta.default

    @property
    def effective(self) -> float:
        """Value pushed to DSP: base position moved by modulation in normalised
        space, then mapped through the curve and safety-clamped."""
        if self.mod_norm == 0.0:
            return self.meta.clamp(self.base)
        pos = self.meta.to_norm(self.base) + self.mod_norm
        val = self.meta.clamp(self.meta.to_value(pos))
        return val if math.isfinite(val) else self.meta.clamp(self.base)

    def display(self) -> str:
        return self.meta.format(self.effective)
