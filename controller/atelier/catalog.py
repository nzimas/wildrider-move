"""Module catalog.

Declarative metadata for every module type (section 4 + 5.1-5.8 of the blueprint)
and the polyadic modulator sources (section 6). Each ``ModuleSpec`` lists the
node-scoped and global parameters, the SynthDef family that realises it, the
channel modes it supports, and whether it is insert- and/or generative-capable.

Everything downstream (UI generation, randomization, modulation targets, scene
capture, save format) reads from here. There is no hand-wired parameter anywhere
else in the system.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .params import Curve, DangerClass, ParamMetadata, RandomizePolicy, Rate


def P(
    pid: str,
    label: str,
    *,
    unit: str = "none",
    rmin: float = 0.0,
    rmax: float = 1.0,
    default: float = 0.0,
    curve: Curve = Curve.LINEAR,
    rate: Rate = Rate.CONTROL,
    smoothing_ms: float = 50.0,
    musical: tuple[float, float] | None = None,
    modulatable: bool = True,
    macro: bool = True,
    midi: bool = True,
    randomize: RandomizePolicy = RandomizePolicy.SAFE,
    danger: DangerClass = DangerClass.NONE,
    formatter: str = "float2",
    enum: list[str] | None = None,
) -> ParamMetadata:
    """Terse constructor so the catalog stays readable."""
    return ParamMetadata(
        id=pid,
        label=label,
        unit=unit,
        rmin=rmin,
        rmax=rmax,
        musical_min=musical[0] if musical else None,
        musical_max=musical[1] if musical else None,
        default=default,
        curve=curve if enum is None else Curve.ENUM,
        rate=rate,
        smoothing_ms=smoothing_ms,
        modulatable=modulatable,
        macro_eligible=macro,
        midi_learnable=midi,
        randomize_policy=randomize,
        danger_class=danger,
        formatter=formatter,
        enum_values=enum,
    )


@dataclass
class ModuleSpec:
    type: str
    role: str
    node_meaning: str
    synthdef: str                          # base SynthDef family name (channel suffix appended)
    node_params: list[ParamMetadata]
    global_params: list[ParamMetadata]
    gestures: list[str] = field(default_factory=list)
    insert_capable: bool = True            # can process incoming audio
    generative_capable: bool = False       # can self-oscillate / produce sound
    max_nodes: int = 32
    is_audio: bool = True                  # VIZ is a view layer, not a DSP voice
    cpu_per_node: float = 1.0              # relative estimate for the CPU watchdog

    def node_param(self, pid: str) -> ParamMetadata | None:
        return next((p for p in self.node_params if p.id.endswith("." + pid) or p.id == pid), None)

    def all_params(self) -> list[ParamMetadata]:
        return self.node_params + self.global_params


# --------------------------------------------------------------------------- #
# FBANK — 10-band resonant stereo filterbank (Erica Synths Resonant Filterbank)
# --------------------------------------------------------------------------- #
_FBANK_FREQS = ["29 Hz", "61 Hz", "115 Hz", "218 Hz", "411 Hz",
                "777 Hz", "1.5 kHz", "2.8 kHz", "5.2 kHz", "11 kHz"]


def _fbank_global_params() -> list[ParamMetadata]:
    ps: list[ParamMetadata] = []
    # ten band boost/cut sliders — 1.0 = flat (centre detent), 0 = full cut, 2 = boost
    for i, lbl in enumerate(_FBANK_FREQS, start=1):
        ps.append(P(f"fbank.gain{i}", lbl, rmin=0.0, rmax=2.0, default=1.0,
                    formatter="float2", musical=(0.3, 1.8)))
    ps += [
        P("fbank.resonance", "Resonance", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR,
          formatter="percent1", danger=DangerClass.FEEDBACK, musical=(-0.7, 0.7)),
        P("fbank.inputGain", "Input Gain", unit="dB", rmin=0.0, rmax=24.0, default=0.0,
          formatter="dBValue", musical=(0.0, 12.0)),
        P("fbank.spread", "Spread", rmin=0.0, rmax=1.0, default=0.0, formatter="percent1", musical=(0.0, 0.8)),
        P("fbank.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB,
          formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.5, 1.1)),
        P("fbank.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ]
    # per-band feedback-loop enables — choose which bands ring / self-oscillate.
    # ON by default (the resonance/feedback character is the whole point); on/off is
    # fully randomizable at every level (per-param 🎲, section, module, patch, guided).
    for i, lbl in enumerate(_FBANK_FREQS, start=1):
        ps.append(P(f"fbank.fb{i}", f"FB {lbl}", curve=Curve.ENUM, enum=["off", "on"],
                    default=1, randomize=RandomizePolicy.WIDE, modulatable=False))
    return ps


FBANK = ModuleSpec(
    type="FBANK",
    role="10-band resonant stereo filterbank: per-band boost/cut, self-oscillating resonance feedback, spectral spread.",
    node_meaning="Resonant filterbank (single instance).",
    synthdef="fbank",
    insert_capable=True,
    generative_capable=True,    # self-oscillates with resonance (no-input mixing)
    max_nodes=1,
    cpu_per_node=2.2,
    gestures=["graphic-eq", "resonate", "self-oscillate", "no-input-mix", "spectral-spread"],
    node_params=[],
    global_params=_fbank_global_params(),
)

# --------------------------------------------------------------------------- #
# 5.2 DX7 — exact 6-operator FM voice (port of everythingwillbetakenaway/
# DX7-Supercollider). Each node is one DX7 voice playing a held note; the whole
# DX7 envelope/algorithm math runs in sclang from the loaded preset bank. A
# bundled 16,384-preset factory bank ships with the engine; user .syx banks load
# at runtime. Param ids match the SynthDef arg names so the engine receives them.
# --------------------------------------------------------------------------- #
FMTONE = ModuleSpec(
    type="FMTONE",
    role="Bespoke 4-operator FM voice (Elektron Digitone 'FM TONE'): 8 algorithms, X/Y "
         "mix, harmonics + detune, two operator envelopes, overdrive, base/width + multimode "
         "filter, amp env. A wonky internal clock self-articulates it for evolving textures.",
    node_meaning="FM TONE voice (one note in the stack).",
    synthdef="fmtone",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.4,
    gestures=["fm-tone", "metallic", "detuned-stack", "percussive-fm", "drone"],
    node_params=[
        P("fmtone.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("fmtone.pitch", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("fmtone.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.7, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.9)),
        P("fmtone.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        # --- FM core (SYN1) ---
        P("fmtone.algo", "Algorithm", rmin=1.0, rmax=8.0, default=1.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE, musical=(1.0, 8.0)),
        P("fmtone.ratioC", "Ratio C", rmin=0.25, rmax=16.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 4.0)),
        P("fmtone.ratioA", "Ratio A", rmin=0.25, rmax=16.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 9.0)),
        P("fmtone.ratioB", "Ratio B", rmin=0.25, rmax=16.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 9.0)),
        P("fmtone.offC", "Offset C", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.3, 0.3)),
        P("fmtone.offA", "Offset A", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("fmtone.offB1", "Offset B1", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("fmtone.offB2", "Offset B2", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("fmtone.harm", "Harmonics", rmin=-26.0, rmax=26.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-11.0, 19.0)),
        P("fmtone.dtun", "Detune", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 35.0)),
        P("fmtone.fdbk", "Feedback", rmin=0.0, rmax=120.0, default=0.0, formatter="int", musical=(0.0, 38.0)),
        P("fmtone.mix", "X/Y Mix", rmin=-64.0, rmax=63.0, default=0.0, curve=Curve.BIPOLAR, formatter="int", musical=(-45.0, 45.0)),
        # --- operator (FM index) envelopes (SYN2): A, and B macro-mapped ---
        P("fmtone.aAtk", "A Attack", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 35.0)),
        P("fmtone.aDec", "A Decay", rmin=0.0, rmax=127.0, default=50.0, formatter="int", musical=(20.0, 100.0)),
        P("fmtone.aEnd", "A End", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 60.0)),
        P("fmtone.aLev", "A Level", rmin=0.0, rmax=127.0, default=85.0, formatter="int", danger=DangerClass.LOUDNESS, musical=(32.0, 84.0)),
        P("fmtone.bAtk", "B Attack", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 35.0)),
        P("fmtone.bDec", "B Decay", rmin=0.0, rmax=127.0, default=50.0, formatter="int", musical=(20.0, 100.0)),
        P("fmtone.bEnd", "B End", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 60.0)),
        P("fmtone.bLev", "B Level", rmin=0.0, rmax=127.0, default=0.0, formatter="int", danger=DangerClass.LOUDNESS, musical=(0.0, 66.0)),
        # --- multimode + base/width filter (FLTR) ---
        P("fmtone.fType", "Filter Type", curve=Curve.ENUM, enum=["off", "lp12", "hp12", "lp24"], default=1, randomize=RandomizePolicy.WIDE),
        P("fmtone.fFreq", "Cutoff", rmin=0.0, rmax=127.0, default=90.0, formatter="int", musical=(72.0, 123.0)),
        P("fmtone.fReso", "Resonance", rmin=0.0, rmax=127.0, default=20.0, formatter="int", musical=(0.0, 50.0)),
        P("fmtone.fEnv", "Filter Env", rmin=-64.0, rmax=63.0, default=0.0, curve=Curve.BIPOLAR, formatter="int", musical=(-22.0, 38.0)),
        P("fmtone.fAtk", "F Attack", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 40.0)),
        P("fmtone.fDec", "F Decay", rmin=0.0, rmax=127.0, default=55.0, formatter="int", musical=(20.0, 100.0)),
        P("fmtone.fSus", "F Sustain", rmin=0.0, rmax=127.0, default=70.0, formatter="int", musical=(20.0, 110.0)),
        P("fmtone.fRel", "F Release", rmin=0.0, rmax=127.0, default=45.0, formatter="int", musical=(20.0, 100.0)),
        P("fmtone.base", "Base", rmin=0.0, rmax=127.0, default=0.0, formatter="int", musical=(0.0, 22.0)),
        P("fmtone.width", "Width", rmin=0.0, rmax=127.0, default=127.0, formatter="int", musical=(80.0, 127.0)),
        # --- amp (AMP) ---
        P("fmtone.ampAtk", "Amp Attack", rmin=0.0, rmax=127.0, default=1.0, formatter="int", musical=(0.0, 25.0)),
        P("fmtone.ampDec", "Amp Decay", rmin=0.0, rmax=127.0, default=60.0, formatter="int", musical=(25.0, 100.0)),
        P("fmtone.ampSus", "Amp Sustain", rmin=0.0, rmax=127.0, default=95.0, formatter="int", musical=(15.0, 112.0)),
        P("fmtone.ampRel", "Amp Release", rmin=0.0, rmax=127.0, default=50.0, formatter="int", musical=(20.0, 95.0)),
        P("fmtone.drv", "Overdrive", rmin=0.0, rmax=127.0, default=0.0, formatter="int", danger=DangerClass.LOUDNESS, musical=(0.0, 55.0)),
        # --- unstable internal clock (re-gates the voice; pitch is NOT sequenced) ---
        P("fmtone.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("fmtone.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("fmtone.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("fmtone.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("fmtone.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

# --------------------------------------------------------------------------- #
# FM7 — real 6-operator FM voice (Chowning matrix), ported from poundhard and
# self-articulated by Wildrider's unstable internal clock.
# --------------------------------------------------------------------------- #
FM7 = ModuleSpec(
    type="FM7",
    role="Real 6-operator FM (Chowning matrix): 6 algorithms, per-operator ratios, FM index "
         "+ feedback, brightness. A wonky internal clock self-articulates it.",
    node_meaning="FM7 voice (one note in the stack).",
    synthdef="fm7",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["fm", "epiano", "bell", "clang", "brass", "percussive-fm"],
    node_params=[
        P("fm7.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("fm7.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("fm7.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.55, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.9)),
        P("fm7.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("fm7.algo", "Algorithm", curve=Curve.ENUM, enum=["epiano", "clang", "organ", "fmbass", "bell", "stab"], default=0, randomize=RandomizePolicy.WIDE),
        P("fm7.r1", "Ratio 1", rmin=0.01, rmax=24.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 8.0)),
        P("fm7.r2", "Ratio 2", rmin=0.01, rmax=24.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 8.0)),
        P("fm7.r3", "Ratio 3", rmin=0.01, rmax=24.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 8.0)),
        P("fm7.r4", "Ratio 4", rmin=0.01, rmax=24.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 11.0)),
        P("fm7.r5", "Ratio 5", rmin=0.01, rmax=24.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 8.0)),
        P("fm7.r6", "Ratio 6", rmin=0.01, rmax=24.0, default=3.5, curve=Curve.EXP, formatter="float2", musical=(0.5, 11.0)),
        P("fm7.index", "FM Index", rmin=0.0, rmax=12.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.3, 6.0)),
        P("fm7.fb", "Feedback", rmin=0.0, rmax=1.0, default=0.1, musical=(0.0, 0.7), danger=DangerClass.FEEDBACK),
        P("fm7.bright", "Brightness", rmin=0.1, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 2.5)),
        P("fm7.attack", "Attack", unit="s", rmin=0.0005, rmax=2.0, default=0.004, curve=Curve.EXP, formatter="float3", musical=(0.001, 0.05)),
        P("fm7.decay", "Decay", unit="s", rmin=0.01, rmax=8.0, default=0.6, curve=Curve.EXP, formatter="float2", musical=(0.08, 2.5)),
        P("fm7.ampCurve", "Amp Curve", rmin=-8.0, rmax=-1.0, default=-4.0, formatter="float1", musical=(-6.0, -2.0)),
        P("fm7.mDecay", "Index Decay", rmin=0.05, rmax=1.5, default=0.6, formatter="float2", musical=(0.2, 1.2)),
        P("fm7.cutoff", "Cutoff", unit="Hz", rmin=60.0, rmax=19000.0, default=16000.0, curve=Curve.EXP, formatter="Hz", musical=(800.0, 18000.0)),
        P("fm7.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("fm7.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("fm7.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("fm7.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("fm7.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

BUCHLOID = ModuleSpec(
    type="BUCHLOID",
    role="Buchla-style complex oscillator: dual FM modulators into a sine/varsaw core, wavefolding "
         "+ timbre bloom, pressure/drive saturation, resonant LPF. A wonky internal clock self-articulates it.",
    node_meaning="Buchloid voice (one note in the stack).",
    synthdef="buchloid",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["west-coast", "complex-osc", "wavefold", "pluck", "metallic", "buchla"],
    node_params=[
        P("buchloid.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("buchloid.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("buchloid.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.25, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.12, 0.33)),
        P("buchloid.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("buchloid.fm1Ratio", "FM1 Ratio", rmin=0.1, rmax=12.0, default=0.66, curve=Curve.EXP, formatter="float2", musical=(0.25, 8.0)),
        P("buchloid.fm1Amount", "FM1 Amount", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("buchloid.fm2Ratio", "FM2 Ratio", rmin=0.1, rmax=24.0, default=3.3, curve=Curve.EXP, formatter="float2", musical=(0.5, 12.0)),
        P("buchloid.fm2Amount", "FM2 Amount", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("buchloid.waveShape", "Wave Shape", rmin=0.0, rmax=1.0, default=0.0, formatter="float2", musical=(0.0, 1.0)),
        P("buchloid.waveFolds", "Wave Folds", rmin=0.0, rmax=3.0, default=0.0, formatter="float2", musical=(0.0, 2.0)),
        P("buchloid.timbre", "Timbre", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.9)),
        P("buchloid.attack", "Attack", unit="s", rmin=0.001, rmax=4.0, default=0.02, curve=Curve.EXP, formatter="float3", musical=(0.005, 0.3)),
        P("buchloid.decay", "Decay", unit="s", rmin=0.01, rmax=8.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.1, 3.0)),
        P("buchloid.peak", "Filter Peak", unit="Hz", rmin=100.0, rmax=12000.0, default=8000.0, curve=Curve.EXP, formatter="Hz", musical=(500.0, 11000.0)),
        P("buchloid.res", "Resonance", rmin=0.0, rmax=1.0, default=0.2, danger=DangerClass.FEEDBACK, musical=(0.0, 0.85)),
        P("buchloid.pressure", "Pressure", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.8)),
        P("buchloid.drive", "Drive", rmin=0.1, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.3, 3.0)),
        P("buchloid.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("buchloid.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("buchloid.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("buchloid.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("buchloid.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

MOLLY = ModuleSpec(
    type="MOLLY",
    role="Molly-the-Poly analogue voice: 2 osc + sub + noise, filter/amp ADSRs, LFO routing, ring-mod, "
         "cross-FM/fold/crush grit, asymmetric drive, chorus. A wonky internal clock self-articulates it.",
    node_meaning="Molly voice (one note in the stack).",
    synthdef="molly",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["analogue", "poly", "pad", "bass", "lead", "acid", "grit"],
    node_params=[
        P("molly.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("molly.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("molly.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.15, 0.4)),
        P("molly.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("molly.detune", "Detune", unit="cent", rmin=0.0, rmax=50.0, default=7.0, formatter="float2", musical=(2.0, 30.0)),
        P("molly.oscShape", "Osc Shape", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("molly.pulseWidth", "Pulse Width", rmin=0.05, rmax=0.95, default=0.5, musical=(0.2, 0.8)),
        P("molly.subLevel", "Sub Level", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.noiseLevel", "Noise Level", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.4)),
        P("molly.cutoff", "Cutoff", unit="Hz", rmin=20.0, rmax=18000.0, default=1200.0, curve=Curve.EXP, formatter="Hz", musical=(200.0, 12000.0)),
        P("molly.resonance", "Resonance", rmin=0.0, rmax=1.0, default=0.2, danger=DangerClass.FEEDBACK, musical=(0.0, 0.85)),
        P("molly.lpType", "Filter Type", curve=Curve.ENUM, enum=["rlpf", "moog"], default=1),
        P("molly.filterEnvAmt", "Filter Env Amt", rmin=0.0, rmax=1.0, default=0.3, musical=(0.0, 0.8)),
        P("molly.fAtk", "Filter Atk", unit="s", rmin=0.001, rmax=4.0, default=0.05, curve=Curve.EXP, formatter="float3", musical=(0.005, 1.0)),
        P("molly.fDec", "Filter Dec", unit="s", rmin=0.005, rmax=6.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("molly.fSus", "Filter Sus", rmin=0.0, rmax=1.0, default=0.6, musical=(0.2, 0.9)),
        P("molly.fRel", "Filter Rel", unit="s", rmin=0.005, rmax=8.0, default=0.6, curve=Curve.EXP, formatter="float2", musical=(0.05, 3.0)),
        P("molly.aAtk", "Amp Atk", unit="s", rmin=0.001, rmax=4.0, default=0.01, curve=Curve.EXP, formatter="float3", musical=(0.002, 0.5)),
        P("molly.aDec", "Amp Dec", unit="s", rmin=0.005, rmax=6.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("molly.aSus", "Amp Sus", rmin=0.0, rmax=1.0, default=0.8, musical=(0.3, 1.0)),
        P("molly.aRel", "Amp Rel", unit="s", rmin=0.005, rmax=8.0, default=0.5, curve=Curve.EXP, formatter="float2", musical=(0.05, 3.0)),
        P("molly.lfoRate", "LFO Rate", unit="Hz", rmin=0.01, rmax=40.0, default=4.0, curve=Curve.EXP, formatter="float2", musical=(0.1, 12.0)),
        P("molly.lfoToCutoff", "LFO to Cutoff", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.lfoToPitch", "LFO to Pitch", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.5)),
        P("molly.lfoToPW", "LFO to PW", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.lfoToAmp", "LFO to Amp", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.6)),
        P("molly.ringMod", "Ring Mod", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.8)),
        P("molly.drive", "Drive", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.chorus", "Chorus", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.8)),
        P("molly.fmAmt", "Cross-FM", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.5)),
        P("molly.fold", "Wavefold", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.crush", "Bitcrush", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.downsample", "Downsample", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.7)),
        P("molly.grit", "Grit", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.6)),
        P("molly.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("molly.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("molly.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("molly.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("molly.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

BEN = ModuleSpec(
    type="BEN",
    role="Benjolin: two cross-modulating oscillators + an 8-bit shift-register rungler whose "
         "DAC feeds back into pitch and cutoff — stepped self-patterning chaos. A wonky internal clock articulates it.",
    node_meaning="Benjolin voice (one in the stack).",
    synthdef="ben",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["benjolin", "rungler", "chaos-drone", "stepped", "cross-mod", "glitch"],
    node_params=[
        P("ben.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("ben.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("ben.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.5)),
        P("ben.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("ben.freq2", "Osc2 Freq", unit="Hz", rmin=0.02, rmax=12000.0, default=4.0, curve=Curve.EXP, formatter="Hz", musical=(0.5, 500.0)),
        P("ben.scale", "Rungler Scale", rmin=0.0, rmax=1.0, default=1.0, formatter="percent1", musical=(0.2, 1.0)),
        P("ben.rungler1", "Rungler>Osc1", rmin=0.0, rmax=2.0, default=0.16, formatter="float2", musical=(0.0, 0.6)),
        P("ben.rungler2", "Rungler>Osc2", rmin=0.0, rmax=2.0, default=0.0, formatter="float2", musical=(0.0, 0.4)),
        P("ben.runglerFilt", "Rungler>Cutoff", rmin=0.0, rmax=64.0, default=9.0, curve=Curve.EXP, formatter="float2", musical=(0.0, 24.0)),
        P("ben.filtFreq", "Filter Freq", unit="Hz", rmin=20.0, rmax=16000.0, default=40.0, curve=Curve.EXP, formatter="Hz", musical=(60.0, 8000.0)),
        P("ben.q", "Resonance", rmin=0.0, rmax=1.0, default=0.82, formatter="percent1", danger=DangerClass.FEEDBACK, musical=(0.2, 0.95)),
        P("ben.filterType", "Filter Type", curve=Curve.ENUM, enum=["lowpass", "highpass", "svf", "dfm1"], default=0, randomize=RandomizePolicy.WIDE),
        P("ben.outSignal", "Output Tap", curve=Curve.ENUM, enum=["tri1", "pulse1", "tri2", "pulse2", "pwm", "shift", "filtered"], default=6, randomize=RandomizePolicy.WIDE),
        P("ben.gain", "Drive", rmin=0.01, rmax=8.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.3, 4.0)),
        P("ben.decay", "Length Mult", rmin=0.1, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 3.0)),
        P("ben.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("ben.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("ben.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("ben.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("ben.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

NOIZEOP = ModuleSpec(
    type="NOIZEOP",
    role="Four sine oscillators combined through six nonlinear algorithms (products, ratios, "
         "truncation, hypot, sum-of-squares) -> spiky glitchy noise, filter bank. A wonky internal clock articulates it.",
    node_meaning="NoizeOp voice (one in the stack).",
    synthdef="noizeop",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.5,
    gestures=["glitch", "noise", "digital-chaos", "spiky", "nonlinear", "harsh"],
    node_params=[
        P("noizeop.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("noizeop.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("noizeop.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.5)),
        P("noizeop.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("noizeop.freq01", "Ratio 1", rmin=0.125, rmax=16.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.25, 8.0)),
        P("noizeop.freq02", "Ratio 2", rmin=0.125, rmax=16.0, default=1.5, curve=Curve.EXP, formatter="float2", musical=(0.25, 8.0)),
        P("noizeop.freq03", "Ratio 3", rmin=0.125, rmax=16.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.25, 8.0)),
        P("noizeop.freq04", "Ratio 4", rmin=0.125, rmax=16.0, default=3.0, curve=Curve.EXP, formatter="float2", musical=(0.25, 8.0)),
        P("noizeop.mul01", "Osc1 Level", rmin=0.0, rmax=2.0, default=1.0, formatter="float2", musical=(0.3, 1.5)),
        P("noizeop.mul02", "Osc2 Level", rmin=0.0, rmax=2.0, default=1.0, formatter="float2", musical=(0.3, 1.5)),
        P("noizeop.mul03", "Osc3 Level", rmin=0.0, rmax=2.0, default=1.0, formatter="float2", musical=(0.3, 1.5)),
        P("noizeop.mul04", "Osc4 Level", rmin=0.0, rmax=2.0, default=1.0, formatter="float2", musical=(0.3, 1.5)),
        P("noizeop.a_mod_01", "Algo1 Mod", rmin=0.001, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float3", musical=(0.2, 2.0)),
        P("noizeop.a_mod_02", "Algo2 Mod", rmin=0.001, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float3", musical=(0.2, 2.0)),
        P("noizeop.a_mod_03", "Algo3 Trunc", rmin=0.001, rmax=1.0, default=0.02, curve=Curve.EXP, formatter="float3", musical=(0.005, 0.2)),
        P("noizeop.a_mod_04", "Algo4 Mod", rmin=0.001, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float3", musical=(0.2, 2.0)),
        P("noizeop.a_mod_05", "Algo5 Mod", rmin=0.001, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float3", musical=(0.2, 2.0)),
        P("noizeop.a_mod_06", "Algo6 Mod", rmin=0.001, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float3", musical=(0.2, 2.0)),
        P("noizeop.a_vol_01", "Algo1 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.a_vol_02", "Algo2 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.a_vol_03", "Algo3 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.a_vol_04", "Algo4 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.a_vol_05", "Algo5 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.a_vol_06", "Algo6 Mix", rmin=0.0, rmax=1.0, default=0.5, formatter="percent1", musical=(0.0, 1.0)),
        P("noizeop.ffreq01", "HiPass Freq", unit="Hz", rmin=20.0, rmax=18000.0, default=40.0, curve=Curve.EXP, formatter="Hz", musical=(30.0, 2000.0)),
        P("noizeop.ffreq02", "LoPass Freq", unit="Hz", rmin=20.0, rmax=18000.0, default=12000.0, curve=Curve.EXP, formatter="Hz", musical=(2000.0, 16000.0)),
        P("noizeop.ffreq03", "Resonz Freq", unit="Hz", rmin=20.0, rmax=18000.0, default=1200.0, curve=Curve.EXP, formatter="Hz", musical=(200.0, 6000.0)),
        P("noizeop.q01", "HiPass Q", rmin=0.05, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 2.0)),
        P("noizeop.q02", "LoPass Q", rmin=0.05, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 2.0)),
        P("noizeop.q03", "Resonz Q", rmin=0.05, rmax=4.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 2.0)),
        P("noizeop.gain", "Drive", rmin=0.01, rmax=8.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.3, 4.0)),
        P("noizeop.decay", "Length Mult", rmin=0.1, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 3.0)),
        P("noizeop.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("noizeop.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("noizeop.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("noizeop.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("noizeop.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

CHAOS = ModuleSpec(
    type="CHAOS",
    role="Chaotic-map oscillators (feedback sine + iterated strange attractors): note sets the "
         "iteration rate, chaosA/B steer the attractor from tone to noise, wavefolder + resonant filter. A wonky internal clock articulates it.",
    node_meaning="Chaos voice (one in the stack).",
    synthdef="chaos",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["chaos", "strange-attractor", "noise", "glitch", "fbsine", "henon"],
    node_params=[
        P("chaos.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("chaos.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("chaos.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.2, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.1, 0.35)),
        P("chaos.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("chaos.type", "Map", curve=Curve.ENUM, enum=["fbsine", "latoocarfian", "henon", "standard", "cusp"], default=0, randomize=RandomizePolicy.WIDE),
        P("chaos.chaosA", "Chaos A", rmin=0.0, rmax=4.0, default=1.1, formatter="float2", musical=(0.3, 3.0)),
        P("chaos.chaosB", "Chaos B", rmin=0.0, rmax=3.0, default=0.5, formatter="float2", musical=(0.2, 2.0)),
        P("chaos.fold", "Wavefold", rmin=0.0, rmax=1.0, default=0.0, formatter="percent1", musical=(0.0, 0.8)),
        P("chaos.cutoff", "Cutoff", unit="Hz", rmin=40.0, rmax=16000.0, default=6000.0, curve=Curve.EXP, formatter="Hz", musical=(400.0, 12000.0)),
        P("chaos.res", "Resonance", rmin=0.0, rmax=1.0, default=0.2, formatter="percent1", danger=DangerClass.FEEDBACK, musical=(0.0, 0.8)),
        P("chaos.attack", "Attack", unit="s", rmin=0.0005, rmax=2.0, default=0.003, curve=Curve.EXP, formatter="float3", musical=(0.001, 0.05)),
        P("chaos.decay", "Decay", unit="s", rmin=0.01, rmax=8.0, default=0.5, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("chaos.ampCurve", "Amp Curve", rmin=-8.0, rmax=-1.0, default=-4.0, formatter="float1", musical=(-6.0, -2.0)),
        P("chaos.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("chaos.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("chaos.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("chaos.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("chaos.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

ICARUS = ModuleSpec(
    type="ICARUS",
    role="'Dreamcrusher' pad/drone: detuned VarSaw + Pulse sub through a feedback delay "
         "network, MoogLadder low-pass and Dust dropouts. A wonky internal clock re-articulates it.",
    node_meaning="Icarus voice (one drone in the stack).",
    synthdef="icarus",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=4.0,
    gestures=["pad", "drone", "dreamcrush", "detune-swell", "feedback-wash", "dropout"],
    node_params=[
        P("icarus.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("icarus.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("icarus.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.55)),
        P("icarus.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("icarus.subpitch", "Sub Octave", rmin=0.0, rmax=3.0, default=1.0, formatter="float1", musical=(1.0, 2.0)),
        P("icarus.sublevel", "Sub Level", rmin=0.0, rmax=1.0, default=0.3, musical=(0.0, 0.6)),
        P("icarus.detuning", "Detune", rmin=0.0, rmax=1.0, default=0.1, musical=(0.0, 0.4)),
        P("icarus.portamento", "Portamento", unit="s", rmin=0.0, rmax=1.0, default=0.1, curve=Curve.EXP, formatter="float2", musical=(0.02, 0.4)),
        P("icarus.pwmcenter", "PWM Center", rmin=0.0, rmax=1.0, default=0.5, musical=(0.3, 0.7)),
        P("icarus.pwmwidth", "PWM Width", rmin=0.0, rmax=1.0, default=0.05, musical=(0.0, 0.3)),
        P("icarus.pwmfreq", "PWM Rate", unit="Hz", rmin=0.1, rmax=30.0, default=10.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 20.0)),
        P("icarus.lpf", "Cutoff", unit="Hz", rmin=20.0, rmax=18000.0, default=6000.0, curve=Curve.EXP, formatter="Hz", musical=(400.0, 12000.0)),
        P("icarus.resonance", "Resonance", rmin=0.0, rmax=1.0, default=0.2, musical=(0.0, 0.7)),
        P("icarus.feedback", "FDN Feedback", rmin=0.0, rmax=0.98, default=0.5, musical=(0.2, 0.85), danger=DangerClass.FEEDBACK),
        P("icarus.delaytime", "Delay Time", unit="s", rmin=0.0, rmax=0.5, default=0.25, curve=Curve.EXP, formatter="float3", musical=(0.02, 0.45)),
        P("icarus.destruction", "Destruction", unit="Hz", rmin=0.0, rmax=30.0, default=0.0, formatter="float1", musical=(0.0, 8.0)),
        P("icarus.attack", "Attack", unit="s", rmin=0.0005, rmax=2.0, default=0.3, curve=Curve.EXP, formatter="float3", musical=(0.1, 0.8)),
        P("icarus.release", "Release", unit="s", rmin=0.01, rmax=12.0, default=4.0, curve=Curve.EXP, formatter="float2", musical=(2.0, 6.0)),
        P("icarus.sustain", "Sustain", rmin=0.0, rmax=1.0, default=0.92, musical=(0.75, 1.0)),
        P("icarus.gain", "Gain", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.5, 1.2)),
        P("icarus.clkRate", "Clock Rate", unit="Hz", rmin=0.05, rmax=12.0, default=0.4, curve=Curve.EXP, formatter="float2", musical=(0.08, 0.8)),
        P("icarus.clkChaos", "Clock Chaos", default=0.2, musical=(0.05, 0.4)),
        P("icarus.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("icarus.clkLen", "Note Length", unit="s", rmin=0.02, rmax=16.0, default=5.0, curve=Curve.EXP, formatter="float2", musical=(3.0, 10.0)),
        P("icarus.clkVel", "Velocity Var", default=0.2, musical=(0.0, 0.35)),
    ],
)

PLAITS = ModuleSpec(
    type="PLAITS",
    role="Mutable Instruments Plaits: 16-model macro-oscillator (analog/wavetable/FM/chord/"
         "speech/noise/percussion). A wonky internal clock triggers its LPG and articulates it.",
    node_meaning="Plaits voice (one note in the stack).",
    synthdef="plaits",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["macro-osc", "model-morph", "pluck", "chord", "speech", "percussion"],
    node_params=[
        P("plaits.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("plaits.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("plaits.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.4, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.25, 0.7)),
        P("plaits.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("plaits.model", "Engine", curve=Curve.ENUM,
          enum=["va", "waveshape", "fm", "grain", "additive", "wavetable", "chord", "speech",
                "swarm", "noise", "particle", "string", "modal", "bass-drum", "snare", "hi-hat"],
          default=0, randomize=RandomizePolicy.WIDE),
        P("plaits.harm", "Harmonics", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.timbre", "Timbre", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.morph", "Morph", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.decay", "LPG Decay", rmin=0.0, rmax=1.0, default=0.5, musical=(0.1, 0.9)),
        P("plaits.lpgColour", "LPG Colour", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.aux", "Out/Aux Blend", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("plaits.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("plaits.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("plaits.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("plaits.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("plaits.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

SHAKER = ModuleSpec(
    type="SHAKER",
    role="STK stochastic shakers (maraca, cabasa, guiro, tambourine, sleigh bells...): "
         "energy/decay/objects shape the gesture, resonance tilts with the note. A wonky clock re-excites it.",
    node_meaning="Shaker voice (one shaker in the stack).",
    synthdef="shaker",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["maraca", "cabasa", "guiro", "tambourine", "sleighbells", "granular-shake"],
    node_params=[
        P("shaker.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("shaker.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=60.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 84.0)),
        P("shaker.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.35, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.6)),
        P("shaker.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("shaker.instr", "Model", curve=Curve.ENUM, enum=["maraca", "cabasa", "sekere", "guiro", "waterdrops", "bamboo", "tambourine", "sleighbells", "sticks", "crunch", "wrench", "sandpaper", "cokecan", "nextmug", "pennymug", "nickelmug", "dimemug", "quartermug", "francmug", "pesomug", "bigrocks", "littlerocks", "tunedbamboo"], default=0, randomize=RandomizePolicy.WIDE),
        P("shaker.energy", "Energy", rmin=0.0, rmax=128.0, default=90.0, formatter="float1", musical=(40.0, 120.0)),
        P("shaker.decay", "Decay", rmin=0.0, rmax=128.0, default=70.0, formatter="float1", musical=(20.0, 110.0)),
        P("shaker.objects", "Objects", rmin=0.0, rmax=128.0, default=40.0, formatter="float1", musical=(2.0, 80.0)),
        P("shaker.resfreq", "Res Freq", rmin=0.0, rmax=128.0, default=64.0, formatter="float1", musical=(20.0, 110.0)),
        P("shaker.atk", "Attack", unit="s", rmin=0.0002, rmax=0.2, default=0.001, curve=Curve.EXP, formatter="float3", musical=(0.0005, 0.05)),
        P("shaker.dec", "Decay Time", unit="s", rmin=0.02, rmax=3.0, default=0.35, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("shaker.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("shaker.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("shaker.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("shaker.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("shaker.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

MEMBRANE = ModuleSpec(
    type="MEMBRANE",
    role="2D waveguide struck circular membrane (toms, frame drums, gongs, warped skins): "
         "tension tunes it, loss sets ring time, a noise strike excites it. A wonky clock re-strikes it.",
    node_meaning="Membrane voice (one drum in the stack).",
    synthdef="membrane",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["tom", "framedrum", "gong", "warped-skin", "membrane-thud"],
    node_params=[
        P("membrane.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("membrane.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(30.0, 72.0)),
        P("membrane.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.35, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.6)),
        P("membrane.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("membrane.tension", "Tension", rmin=0.004, rmax=0.22, default=0.05, curve=Curve.EXP, formatter="float3", musical=(0.01, 0.15)),
        P("membrane.loss", "Ring / Loss", rmin=0.9, rmax=0.99998, default=0.9995, curve=Curve.EXP, formatter="float3", musical=(0.99, 0.9999)),
        P("membrane.tone", "Strike Tone", default=0.5, musical=(0.2, 0.9)),
        P("membrane.strike", "Strike Time", default=0.5, musical=(0.1, 0.9)),
        P("membrane.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("membrane.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("membrane.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("membrane.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("membrane.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

MALLET = ModuleSpec(
    type="MALLET",
    role="STK ModalBar struck mallets (marimba, vibraphone, agogo, wood, reso, beats): "
         "the note tunes it, stick hardness/position and decay shape the ring. A wonky clock re-strikes it.",
    node_meaning="Mallet voice (one bar in the stack).",
    synthdef="mallet",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["marimba", "vibraphone", "agogo", "woodblock", "bell", "percussive-mallet"],
    node_params=[
        P("mallet.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("mallet.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=60.0, rate=Rate.DISCRETE, formatter="noteName", musical=(48.0, 84.0)),
        P("mallet.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.55)),
        P("mallet.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("mallet.instrument", "Model", curve=Curve.ENUM, enum=["marimba", "vibraphone", "agogo", "wood1", "reso", "wood2", "beats", "twofixed", "clump"], default=0, randomize=RandomizePolicy.WIDE),
        P("mallet.stickhardness", "Stick Hardness", rmin=0.0, rmax=128.0, default=64.0, formatter="float1", musical=(20.0, 110.0)),
        P("mallet.stickposition", "Stick Position", rmin=0.0, rmax=128.0, default=28.0, formatter="float1", musical=(0.0, 90.0)),
        P("mallet.vibratogain", "Vibrato Gain", rmin=0.0, rmax=128.0, default=8.0, formatter="float1", musical=(0.0, 40.0)),
        P("mallet.vibratofreq", "Vibrato Freq", rmin=0.0, rmax=128.0, default=20.0, formatter="float1", musical=(0.0, 80.0)),
        P("mallet.directmix", "Direct Mix", rmin=0.0, rmax=128.0, default=40.0, formatter="float1", musical=(0.0, 100.0)),
        P("mallet.decay", "Decay", unit="s", rmin=0.05, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.1, 3.0)),
        P("mallet.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("mallet.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("mallet.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("mallet.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("mallet.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

BOWED = ModuleSpec(
    type="BOWED",
    role="STK banded waveguide (uniform/tuned bar, glass harmonica, Tibetan bowl): the note "
         "tunes it, the clock gate bows each hit, striking toggles struck vs bowed. A wonky clock articulates it.",
    node_meaning="Bowed voice (one bar/bowl in the stack).",
    synthdef="bowed",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["glassharmonica", "tibetanbowl", "bowed-bar", "singing-metal", "struck-glass"],
    node_params=[
        P("bowed.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("bowed.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=60.0, rate=Rate.DISCRETE, formatter="noteName", musical=(48.0, 84.0)),
        P("bowed.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.55)),
        P("bowed.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("bowed.instr", "Model", curve=Curve.ENUM, enum=["uniformbar", "tunedbar", "glassharmonica", "tibetanbowl"], default=0, randomize=RandomizePolicy.WIDE),
        P("bowed.bowpressure", "Bow Pressure", rmin=0.0, rmax=128.0, default=70.0, formatter="float1", musical=(20.0, 110.0)),
        P("bowed.bowmotion", "Bow Motion", rmin=0.0, rmax=128.0, default=30.0, formatter="float1", musical=(0.0, 90.0)),
        P("bowed.integration", "Integration", curve=Curve.ENUM, enum=["off", "on"], default=0),
        P("bowed.modalresonance", "Modal Resonance", rmin=0.0, rmax=128.0, default=90.0, formatter="float1", musical=(40.0, 120.0)),
        P("bowed.bowvelocity", "Bow Velocity", rmin=0.0, rmax=128.0, default=80.0, formatter="float1", musical=(20.0, 120.0)),
        P("bowed.striking", "Striking", curve=Curve.ENUM, enum=["bowed", "struck"], default=0),
        P("bowed.decay", "Release", unit="s", rmin=0.02, rmax=4.0, default=1.5, curve=Curve.EXP, formatter="float2", musical=(0.1, 3.0)),
        P("bowed.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("bowed.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("bowed.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("bowed.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("bowed.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

PLUCK = ModuleSpec(
    type="PLUCK",
    role="Digital-waveguide plucked stiff string (koto, clav, harp, muted plucks): stiffness adds "
         "inharmonicity, pos/decay/damp/bright shape it. A wonky clock re-plucks it.",
    node_meaning="Pluck voice (one string in the stack).",
    synthdef="pluck",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["koto", "clav", "harp", "muted-pluck", "stiff-string"],
    node_params=[
        P("pluck.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("pluck.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=60.0, rate=Rate.DISCRETE, formatter="noteName", musical=(40.0, 84.0)),
        P("pluck.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.4, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.25, 0.65)),
        P("pluck.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("pluck.pos", "Pluck Position", rmin=0.02, rmax=0.5, default=0.14, formatter="float2", musical=(0.05, 0.4)),
        P("pluck.decay", "Decay", unit="s", rmin=0.05, rmax=8.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.2, 4.0)),
        P("pluck.damp", "Damping", rmin=1.0, rmax=80.0, default=30.0, curve=Curve.EXP, formatter="float1", musical=(5.0, 60.0)),
        P("pluck.bright", "Brightness", default=0.5, musical=(0.1, 0.9)),
        P("pluck.excite", "Excite Time", unit="s", rmin=0.001, rmax=0.05, default=0.008, curve=Curve.EXP, formatter="float3", musical=(0.002, 0.03)),
        P("pluck.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("pluck.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("pluck.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("pluck.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("pluck.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

TUBE = ModuleSpec(
    type="TUBE",
    role="Two-tube (vocal-tract-ish) waveguide: hollow formant plucks and reedy tones. Tube lengths "
         "set from the note, k junction and balance shape it, the clock gate breathes it. A wonky clock articulates it.",
    node_meaning="Tube voice (one resonator in the stack).",
    synthdef="tube",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=3.0,
    gestures=["formant-pluck", "reed", "hollow-tone", "vocal-tract", "tube-resonance"],
    node_params=[
        P("tube.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("tube.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=60.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 78.0)),
        P("tube.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.4, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.25, 0.65)),
        P("tube.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("tube.k", "Junction", rmin=0.001, rmax=0.2, default=0.01, curve=Curve.EXP, formatter="float3", musical=(0.002, 0.1)),
        P("tube.loss", "Loss", rmin=0.9, rmax=1.0, default=0.99, formatter="float3", musical=(0.95, 0.999)),
        P("tube.balance", "Balance", rmin=0.1, rmax=0.9, default=0.5, formatter="float2", musical=(0.2, 0.8)),
        P("tube.excite", "Breath Attack", unit="s", rmin=0.001, rmax=0.08, default=0.01, curve=Curve.EXP, formatter="float3", musical=(0.003, 0.05)),
        P("tube.decay", "Release", unit="s", rmin=0.05, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.1, 3.0)),
        P("tube.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("tube.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("tube.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("tube.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("tube.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

WTABLE = ModuleSpec(
    type="WTABLE",
    role="Ableton-style wavetable voice: two morphing wavetable oscillators (Move's sprite bank) "
         "with position sweep + LFO, sub, noise, 3-mode filter and drive. A wonky internal clock articulates it.",
    node_meaning="Wavetable voice (one note in the stack).",
    synthdef="wtable",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["wavetable", "morph-sweep", "pwm-pad", "digital-bass", "position-lfo"],
    node_params=[
        P("wtable.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("wtable.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("wtable.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.6)),
        P("wtable.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("wtable.pos1", "Position A", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("wtable.pos2", "Position B", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("wtable.oscmix", "Osc Mix", rmin=0.0, rmax=1.0, default=0.5, musical=(0.1, 0.9)),
        P("wtable.detune", "Detune", unit="cent", rmin=-50.0, rmax=50.0, default=0.0, curve=Curve.BIPOLAR, formatter="float1", musical=(-25.0, 25.0)),
        P("wtable.transpose2", "Transpose B", unit="semitone", rmin=-24.0, rmax=24.0, default=0.0, rate=Rate.DISCRETE, curve=Curve.BIPOLAR, formatter="float1", musical=(-12.0, 12.0)),
        P("wtable.suboct", "Sub Octave", rmin=0.0, rmax=3.0, default=1.0, rate=Rate.DISCRETE, formatter="float1", musical=(1.0, 2.0)),
        P("wtable.sublevel", "Sub Level", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.6)),
        P("wtable.noiselevel", "Noise Level", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.4)),
        P("wtable.cutoff", "Cutoff", unit="Hz", rmin=40.0, rmax=18000.0, default=8000.0, curve=Curve.EXP, formatter="Hz", musical=(500.0, 16000.0)),
        P("wtable.res", "Resonance", rmin=0.0, rmax=1.0, default=0.2, musical=(0.0, 0.75)),
        P("wtable.filttype", "Filter Type", curve=Curve.ENUM, enum=["lowpass", "bandpass", "highpass"], default=0, randomize=RandomizePolicy.WIDE),
        P("wtable.drive", "Drive", rmin=0.1, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 3.0)),
        P("wtable.filtenv", "Filter Env", rmin=0.0, rmax=1.0, default=0.3, musical=(0.0, 0.8)),
        P("wtable.posenv", "Position Env", rmin=0.0, rmax=1.0, default=0.35, musical=(0.0, 0.8)),
        P("wtable.poslfoRate", "Position LFO Rate", unit="Hz", rmin=0.01, rmax=30.0, default=0.5, curve=Curve.EXP, formatter="float2", musical=(0.05, 8.0)),
        P("wtable.poslfoAmt", "Position LFO Amt", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.6)),
        P("wtable.attack", "Attack", unit="s", rmin=0.001, rmax=4.0, default=0.01, curve=Curve.EXP, formatter="float3", musical=(0.002, 0.5)),
        P("wtable.decay", "Decay", unit="s", rmin=0.005, rmax=8.0, default=0.5, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.5)),
        P("wtable.sustain", "Sustain", rmin=0.0, rmax=1.0, default=0.7, musical=(0.3, 1.0)),
        P("wtable.release", "Release", unit="s", rmin=0.01, rmax=8.0, default=0.7, curve=Curve.EXP, formatter="float2", musical=(0.1, 3.0)),
        P("wtable.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("wtable.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("wtable.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("wtable.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("wtable.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)

BYTEBEAT = ModuleSpec(
    type="BYTEBEAT",
    role="8-bit bytebeat voice: a curated expression clocked at a note-scaled rate through a "
         "resonant filter + drive. A wonky internal clock re-gates it.",
    node_meaning="Bytebeat voice (one note in the stack).",
    synthdef="bytebeat",
    insert_capable=False,
    generative_capable=True,
    max_nodes=2,
    cpu_per_node=2.0,
    gestures=["bytebeat", "8bit", "chiptune", "glitch", "digital", "lofi"],
    node_params=[
        P("bytebeat.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("bytebeat.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("bytebeat.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.4, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.25, 0.6)),
        P("bytebeat.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("bytebeat.rate", "Bit Rate", unit="Hz", rmin=200.0, rmax=44100.0, default=8000.0, curve=Curve.EXP, formatter="Hz", musical=(1000.0, 22050.0)),
        P("bytebeat.cutoff", "Cutoff", unit="Hz", rmin=40.0, rmax=18000.0, default=12000.0, curve=Curve.EXP, formatter="Hz", musical=(800.0, 16000.0)),
        P("bytebeat.res", "Resonance", rmin=0.0, rmax=0.96, default=0.1, musical=(0.0, 0.8)),
        P("bytebeat.drive", "Drive", rmin=0.1, rmax=6.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 3.0)),
        P("bytebeat.attack", "Attack", unit="s", rmin=0.001, rmax=4.0, default=0.004, curve=Curve.EXP, formatter="float3", musical=(0.002, 0.3)),
        P("bytebeat.decay", "Decay", unit="s", rmin=0.005, rmax=8.0, default=0.5, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("bytebeat.sustain", "Sustain", rmin=0.0, rmax=1.0, default=0.7, musical=(0.3, 1.0)),
        P("bytebeat.release", "Release", unit="s", rmin=0.005, rmax=8.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("bytebeat.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, formatter="float2", musical=(0.4, 6.0)),
        P("bytebeat.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("bytebeat.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("bytebeat.clkLen", "Note Length", unit="s", rmin=0.02, rmax=4.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.5)),
        P("bytebeat.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
    ],
)


# --------------------------------------------------------------------------- #
# 5.4 PITCH — pitch-shifting granular delay surface
# --------------------------------------------------------------------------- #
PITCH = ModuleSpec(
    type="PITCH",
    role="Pitch-shifting granular delay surface.",
    node_meaning="Pitch shifter / delay node.",
    synthdef="pitch",
    insert_capable=True,
    generative_capable=False,
    max_nodes=16,
    cpu_per_node=1.4,
    gestures=["pitch-constellation", "shimmer-swarm", "detuned-cluster", "octave-fan"],
    node_params=[
        P("pitch.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("pitch.semitones", "Semitones", unit="semitone", rmin=-24.0, rmax=24.0, default=0.0, curve=Curve.BIPOLAR, musical=(-12.0, 12.0), formatter="semitone"),
        P("pitch.cents", "Cents", rmin=-100.0, rmax=100.0, default=0.0, curve=Curve.BIPOLAR),
        P("pitch.delayMs", "Delay", unit="ms", rmin=0.0, rmax=4000.0, default=0.0, curve=Curve.EXP, formatter="ms"),
        P("pitch.grainSize", "Grain Size", unit="ms", rmin=10.0, rmax=1000.0, default=120.0, curve=Curve.EXP, formatter="ms"),
        P("pitch.windowShape", "Window", curve=Curve.ENUM, enum=["hann", "tukey", "gauss"], default=0),
        P("pitch.feedback", "Feedback", default=0.0, musical=(0.0, 0.6), danger=DangerClass.FEEDBACK),
        P("pitch.density", "Density", rmin=1.0, rmax=16.0, default=2.0, curve=Curve.EXP),
        P("pitch.spread", "Spread", default=0.2),
        P("pitch.randomPitch", "Random Pitch", unit="semitone", rmin=0.0, rmax=12.0, default=0.0, formatter="semitone"),
        P("pitch.randomTime", "Random Time", unit="ms", rmin=0.0, rmax=500.0, default=0.0, formatter="ms"),
        P("pitch.formantBias", "Formant", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        P("pitch.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.8, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.45, 0.9)),
        P("pitch.spatialPos", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("pitch.qualityMode", "Quality", curve=Curve.ENUM, enum=["live", "balanced", "highQuality"], default=1, danger=DangerClass.CPU, modulatable=False),
        P("pitch.maxDelay", "Max Delay", unit="ms", rmin=100.0, rmax=8000.0, default=4000.0, curve=Curve.EXP, formatter="ms", modulatable=False),
        P("pitch.feedbackTopology", "FB Topology", curve=Curve.ENUM, enum=["independent", "cross", "matrix"], default=0, modulatable=False),
        P("pitch.pitchQuantize", "Pitch Quantize", curve=Curve.ENUM, enum=["off", "semitone", "scale"], default=0),
        P("pitch.scale", "Scale", curve=Curve.ENUM, enum=["chromatic", "major", "minor", "wholetone"], default=0),
        P("pitch.antiAlias", "Anti-Alias", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
        P("pitch.safetyFeedbackLimit", "FB Limit", default=0.85, danger=DangerClass.FEEDBACK, modulatable=False, randomize=RandomizePolicy.OFF),
    ],
)

# --------------------------------------------------------------------------- #
# 5.5 TIME — delay-time laboratory, 1..32 taps
# --------------------------------------------------------------------------- #
TIME = ModuleSpec(
    type="TIME",
    role="Buffer delay, multi-tap delay, variable transport playback.",
    node_meaning="Tap / read head.",
    synthdef="time",
    insert_capable=True,
    generative_capable=False,
    max_nodes=32,
    cpu_per_node=0.9,
    gestures=["draw-taps", "time-stretch-illusion", "rhythmic-smear", "freeze-buffer"],
    node_params=[
        P("time.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("time.tapTime", "Tap Time", unit="ms", rmin=0.0, rmax=8000.0, default=250.0, curve=Curve.EXP, musical=(20.0, 2000.0), formatter="ms"),
        P("time.tapGain", "Tap Gain", unit="dB", rmin=0.0, rmax=1.5, default=0.7, curve=Curve.DB, formatter="dB1", musical=(0.4, 0.9)),
        P("time.speed", "Speed", rmin=-2.0, rmax=2.0, default=1.0, curve=Curve.BIPOLAR, musical=(0.5, 1.5)),
        P("time.reverse", "Reverse", curve=Curve.ENUM, enum=["off", "on"], default=0),
        P("time.filterFreq", "Filter Freq", unit="Hz", rmin=40.0, rmax=18000.0, default=8000.0, curve=Curve.EXP, formatter="Hz", musical=(3000.0, 16000.0)),
        P("time.filterQ", "Filter Q", rmin=0.1, rmax=10.0, default=0.7, curve=Curve.EXP),
        P("time.feedbackSend", "Feedback", default=0.2, musical=(0.0, 0.6), danger=DangerClass.FEEDBACK),
        P("time.jitter", "Jitter", unit="ms", rmin=0.0, rmax=200.0, default=0.0, formatter="ms"),
        P("time.quantize", "Quantize", curve=Curve.ENUM, enum=["off", "1/16", "1/8", "1/4"], default=0),
        P("time.smear", "Smear", default=0.0),
        P("time.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        P("time.mute", "Mute", curve=Curve.ENUM, enum=["off", "on"], default=0),
    ],
    global_params=[
        P("time.bufferLength", "Buffer Length", unit="ms", rmin=500.0, rmax=30000.0, default=8000.0, curve=Curve.EXP, formatter="ms", modulatable=False),
        P("time.syncMode", "Sync", curve=Curve.ENUM, enum=["free", "tempo"], default=0, modulatable=False),
        P("time.feedbackMode", "FB Mode", curve=Curve.ENUM, enum=["perTap", "global", "matrix"], default=0, modulatable=False),
        P("time.freeze", "Freeze", curve=Curve.ENUM, enum=["off", "on"], default=0, rate=Rate.TRIGGER),
        P("time.eraseRate", "Erase Rate", default=0.0),
        P("time.inputToBuffer", "Input To Buffer", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
        P("time.interpolation", "Interpolation", curve=Curve.ENUM, enum=["linear", "cubic"], default=1, modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #
# 5.6 COMB — resonator and waveguide field
# --------------------------------------------------------------------------- #
COMB = ModuleSpec(
    type="COMB",
    role="Comb filter bank, resonator, waveguide, physical-model colour.",
    node_meaning="Comb line / resonator.",
    synthdef="comb",
    insert_capable=True,
    generative_capable=False,  # pure processor now — RINGS is the only hybrid
    max_nodes=32,
    cpu_per_node=1.0,
    gestures=["strum", "freeze-body", "metal/wood-morph", "metallic-scatter"],
    node_params=[
        P("comb.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("comb.freqOrDelay", "Freq", unit="Hz", rmin=20.0, rmax=8000.0, default=220.0, curve=Curve.EXP, musical=(55.0, 1760.0), formatter="Hz"),
        P("comb.feedback", "Feedback", rmin=0.0, rmax=0.98, default=0.35, musical=(0.05, 0.75), danger=DangerClass.FEEDBACK, formatter="percent1"),
        P("comb.damping", "Damping", default=0.3, musical=(0.05, 0.45)),
        P("comb.polarity", "Polarity", curve=Curve.ENUM, enum=["positive", "negative"], default=0),
        P("comb.warp", "Warp", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        P("comb.dispersion", "Dispersion", default=0.0),
        P("comb.drive", "Drive", rmin=1.0, rmax=8.0, default=1.0, curve=Curve.EXP, danger=DangerClass.LOUDNESS, musical=(1.0, 3.0)),
        P("comb.exciteAmount", "Excite", default=0.5, musical=(0.3, 0.9)),
        P("comb.decay", "Decay", unit="ms", rmin=10.0, rmax=20000.0, default=1500.0, curve=Curve.EXP, formatter="ms", musical=(200.0, 6000.0)),
        P("comb.brightness", "Brightness", default=0.5, musical=(0.45, 0.95)),
        P("comb.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.6, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.8)),
        P("comb.spatialPos", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("comb.tuningMode", "Tuning", curve=Curve.ENUM, enum=["free", "scale", "harmonic"], default=0),
        P("comb.scale", "Scale", curve=Curve.ENUM, enum=["chromatic", "major", "minor", "pentatonic"], default=0),
        P("comb.maxDelay", "Max Delay", unit="ms", rmin=10.0, rmax=2000.0, default=500.0, curve=Curve.EXP, formatter="ms", modulatable=False),
        P("comb.selfOscLimit", "Self-Osc Limit", default=0.95, danger=DangerClass.FEEDBACK, modulatable=False, randomize=RandomizePolicy.OFF),
        P("comb.excitationSource", "Excitation", curve=Curve.ENUM, enum=["input", "noise", "impulse", "gen"], default=0),
        P("comb.bodyModel", "Body", curve=Curve.ENUM, enum=["string", "tube", "plate", "membrane"], default=0),
        P("comb.feedbackSafety", "FB Safety", curve=Curve.ENUM, enum=["on", "expert"], default=0, danger=DangerClass.FEEDBACK, modulatable=False),
        P("comb.dcBlock", "DC Block", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #
# 5.7 GAIN — movement and presence (not merely volume)
# --------------------------------------------------------------------------- #
GAIN = ModuleSpec(
    type="GAIN",
    role="Amplitude, depth, doppler, distance, air absorption, presence, utility.",
    node_meaning="Gain / spatial depth point.",
    synthdef="gain",
    insert_capable=True,
    generative_capable=False,
    max_nodes=16,
    cpu_per_node=0.6,
    gestures=["move-in-depth", "acoustic-perspective", "macro-fades", "spatial-ducking"],
    node_params=[
        P("gain.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("gain.gain", "Gain", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.2)),
        P("gain.mute", "Mute", curve=Curve.ENUM, enum=["off", "on"], default=0, randomize=RandomizePolicy.OFF),
        P("gain.distance", "Distance", unit="m", rmin=0.1, rmax=100.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 3.0)),
        P("gain.velocity", "Velocity", rmin=-20.0, rmax=20.0, default=0.0, curve=Curve.BIPOLAR),
        P("gain.dopplerDepth", "Doppler", default=0.0),
        P("gain.airAbsorb", "Air Absorb", default=0.0, musical=(0.0, 0.3)),
        P("gain.presence", "Presence", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        P("gain.tremoloRate", "Tremolo Rate", unit="Hz", rmin=0.01, rmax=30.0, default=4.0, curve=Curve.EXP, formatter="Hz"),
        P("gain.tremoloDepth", "Tremolo Depth", default=0.0),
        P("gain.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        P("gain.duck", "Duck", default=0.0),
    ],
    global_params=[
        P("gain.scaleMeters", "Scale (m)", rmin=1.0, rmax=200.0, default=20.0, curve=Curve.EXP, modulatable=False),
        P("gain.speedOfSound", "Speed Of Sound", unit="m/s", rmin=300.0, rmax=400.0, default=343.0, formatter="float2", modulatable=False),
        P("gain.limiter", "Limiter", curve=Curve.ENUM, enum=["off", "on"], default=1, danger=DangerClass.LOUDNESS, modulatable=False),
        P("gain.compensation", "Compensation", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
        P("gain.automationSmoothing", "Auto Smooth", unit="ms", rmin=0.0, rmax=2000.0, default=80.0, formatter="ms", modulatable=False),
        P("gain.silenceSleep", "Silence Sleep", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# SDLY — stereo / ping-pong delay (end-of-chain spatial effect)
# --------------------------------------------------------------------------- #
SDLY = ModuleSpec(
    type="SDLY",
    role="Stereo / ping-pong delay with cross-feedback, tone and width.",
    node_meaning="Stereo delay voice.",
    synthdef="sdly",
    insert_capable=True,
    generative_capable=False,
    max_nodes=4,
    cpu_per_node=1.0,
    gestures=["ping-pong", "dub-feedback", "widen", "rhythmic-echo"],
    node_params=[
        P("sdly.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("sdly.timeL", "Time L", unit="ms", rmin=1.0, rmax=2000.0, default=280.0, curve=Curve.EXP, formatter="ms", musical=(80.0, 900.0)),
        P("sdly.timeR", "Time R", unit="ms", rmin=1.0, rmax=2000.0, default=420.0, curve=Curve.EXP, formatter="ms", musical=(80.0, 900.0)),
        P("sdly.feedback", "Feedback", rmin=0.0, rmax=0.95, default=0.35, musical=(0.1, 0.7), danger=DangerClass.FEEDBACK, formatter="percent1"),
        P("sdly.crossFeed", "Cross Feed", default=0.4, musical=(0.2, 0.85)),
        P("sdly.damp", "Damping", default=0.4, musical=(0.1, 0.6)),
        P("sdly.width", "Width", rmin=0.0, rmax=1.5, default=1.1, musical=(0.85, 1.4)),
        P("sdly.modRate", "Mod Rate", unit="Hz", rmin=0.01, rmax=8.0, default=0.3, curve=Curve.EXP, formatter="Hz"),
        P("sdly.modDepth", "Mod Depth", default=0.12, musical=(0.0, 0.35)),
        P("sdly.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.1)),
        P("sdly.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ],
    global_params=[
        P("sdly.syncMode", "Sync", curve=Curve.ENUM, enum=["free", "tempo"], default=0, modulatable=False),
        P("sdly.pingpong", "Ping-Pong", curve=Curve.ENUM, enum=["off", "on"], default=1),
    ],
)

# --------------------------------------------------------------------------- #
# VERB — spacious, deep reverb (JPverb), end-of-chain
# --------------------------------------------------------------------------- #
VERB = ModuleSpec(
    type="VERB",
    role="Spacious, deep, modulated reverb (size, decay, diffusion, width).",
    node_meaning="Reverb voice.",
    synthdef="verb",
    insert_capable=True,
    generative_capable=False,
    max_nodes=2,
    cpu_per_node=2.5,
    gestures=["cathedral", "infinite-tail", "shimmer-space", "freeze"],
    node_params=[
        P("verb.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("verb.size", "Size", rmin=0.5, rmax=5.0, default=2.2, musical=(1.0, 3.8)),
        P("verb.decay", "Decay", unit="s", rmin=0.1, rmax=30.0, default=5.0, curve=Curve.EXP, formatter="float2", musical=(1.5, 12.0)),
        P("verb.damp", "Damping", default=0.3, musical=(0.1, 0.6)),
        P("verb.modDepth", "Mod Depth", default=0.2, musical=(0.0, 0.4)),
        P("verb.modFreq", "Mod Freq", unit="Hz", rmin=0.01, rmax=10.0, default=2.0, curve=Curve.EXP, formatter="Hz", musical=(0.2, 4.0)),
        P("verb.earlyDiff", "Diffusion", default=0.7, musical=(0.4, 0.95)),
        P("verb.lowCut", "Low Cut", unit="Hz", rmin=20.0, rmax=1000.0, default=180.0, curve=Curve.EXP, formatter="Hz", musical=(50.0, 400.0)),
        P("verb.highCut", "High Cut", unit="Hz", rmin=1000.0, rmax=18000.0, default=9000.0, curve=Curve.EXP, formatter="Hz", musical=(4000.0, 14000.0)),
        P("verb.width", "Width", rmin=0.0, rmax=1.5, default=1.0, musical=(0.85, 1.35)),
        P("verb.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.1)),
        P("verb.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ],
    global_params=[
        P("verb.freeze", "Freeze", curve=Curve.ENUM, enum=["off", "on"], default=0, rate=Rate.TRIGGER),
    ],
)


# --------------------------------------------------------------------------- #
# CLOUDS — Mutable Instruments Clouds granular texture processor (MiClouds)
# --------------------------------------------------------------------------- #
CLOUDS = ModuleSpec(
    type="CLOUDS",
    role="Granular texture synthesizer (Mutable Instruments Clouds).",
    node_meaning="Granular cloud.",
    synthdef="clouds",
    insert_capable=True,
    generative_capable=False,   # processes input (freeze sustains it)
    max_nodes=2,
    cpu_per_node=2.6,
    gestures=["freeze-cloud", "spectral-smear", "granular-stretch", "shimmer-feedback"],
    node_params=[
        P("clouds.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("clouds.pit", "Pitch", unit="semitone", rmin=-24.0, rmax=24.0, default=0.0, curve=Curve.BIPOLAR, musical=(-12.0, 12.0), formatter="semitone"),
        P("clouds.pos", "Position", default=0.5),
        P("clouds.size", "Grain Size", default=0.4, musical=(0.2, 0.8)),
        P("clouds.dens", "Density", default=0.4, musical=(0.2, 0.8)),
        P("clouds.tex", "Texture", default=0.5, musical=(0.3, 0.8)),
        P("clouds.inGain", "Input Gain", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", musical=(0.6, 1.4), danger=DangerClass.LOUDNESS),
        P("clouds.spread", "Spread", default=0.5, musical=(0.3, 1.0)),
        P("clouds.rvb", "Reverb", default=0.2, musical=(0.0, 0.6)),
        P("clouds.fb", "Feedback", default=0.0, musical=(0.0, 0.5), danger=DangerClass.FEEDBACK),
        P("clouds.freeze", "Freeze", curve=Curve.ENUM, enum=["off", "on"], default=0, rate=Rate.TRIGGER),
        P("clouds.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.9, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.1)),
        P("clouds.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ],
    global_params=[
        P("clouds.mode", "Mode", curve=Curve.ENUM, enum=["granular", "stretch", "looping", "spectral"], default=0, modulatable=False),
        P("clouds.lofi", "Lo-Fi", curve=Curve.ENUM, enum=["off", "on"], default=0, modulatable=False),
    ],
)

# GRAINS — a maximalist, IRCAM/GRM-leaning live granulator. A crazier alternative
# to CLOUDS: a high grain count (4 parallel grain streams per node), independent
# spray / size-jitter / pitch-jitter, per-grain reverse + octave shimmer, a
# granular feedback loop, freeze + buffer scanning, a chaos random-walk and seven
# selectable grain windows. Every continuous param is modulatable, so an LFO bank
# pointed at it yields constantly-evolving, dynamic textures.
GRAINS = ModuleSpec(
    type="GRAINS",
    role="Maximalist live granulator: dense grain clouds, spray/jitter, shimmer, "
         "reverse, granular feedback, freeze + scan, chaos. A wilder Clouds.",
    node_meaning="Independent grain cloud (decorrelated per node).",
    synthdef="grains",
    insert_capable=True,
    generative_capable=False,   # granulates incoming audio (freeze sustains it)
    max_nodes=3,
    cpu_per_node=3.2,
    gestures=["grain-storm", "freeze-scan", "shimmer-feedback", "pitch-spray", "chaos-walk"],
    node_params=[
        P("grains.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("grains.size", "Grain Size", unit="s", rmin=0.003, rmax=0.6, default=0.12, curve=Curve.EXP, formatter="float3", musical=(0.02, 0.35)),
        P("grains.density", "Density", unit="gr/s", rmin=1.0, rmax=200.0, default=40.0, curve=Curve.EXP, formatter="float0", musical=(8.0, 120.0)),
        P("grains.pos", "Position", default=0.0, musical=(0.0, 0.6)),
        P("grains.spray", "Spray", rmin=0.0, rmax=0.5, default=0.08, musical=(0.0, 0.3)),
        P("grains.jitter", "Size Jitter", rmin=0.0, rmax=1.0, default=0.2, musical=(0.0, 0.6)),
        P("grains.spread", "Stereo Spread", default=0.7, musical=(0.3, 1.0)),
        P("grains.reverse", "Reverse", default=0.0, musical=(0.0, 0.5)),
        P("grains.shimmer", "Shimmer", default=0.0, musical=(0.0, 0.6)),
        P("grains.scan", "Scan", unit="Hz", rmin=0.0, rmax=4.0, default=0.0, curve=Curve.EXP, formatter="float2", musical=(0.0, 1.5)),
        P("grains.texture", "Texture", default=0.4, musical=(0.2, 0.8)),
        P("grains.chaos", "Chaos", default=0.0, musical=(0.0, 0.5)),
        P("grains.feedback", "Feedback", rmin=0.0, rmax=0.9, default=0.0, danger=DangerClass.FEEDBACK, musical=(0.0, 0.5)),
        P("grains.freeze", "Freeze", curve=Curve.ENUM, enum=["off", "on"], default=0, rate=Rate.TRIGGER),
        P("grains.inGain", "Input Gain", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", musical=(0.6, 1.4), danger=DangerClass.LOUDNESS),
        P("grains.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.9, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.1)),
        P("grains.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ],
    global_params=[
        P("grains.shape", "Grain Shape", curve=Curve.ENUM,
          enum=["hann", "bell", "expodec", "rev-expodec", "plateau", "triangle", "gapped"],
          default=0, modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #
# RINGS — Mutable Instruments Rings modal/string resonator (MiRings)
# --------------------------------------------------------------------------- #
RINGS = ModuleSpec(
    type="RINGS",
    role="Modal / sympathetic-string resonator (Mutable Instruments Rings).",
    node_meaning="Resonator voice.",
    synthdef="rings",
    insert_capable=True,        # resonates incoming audio (external exciter)
    generative_capable=True,    # internal exciter -> self-sounding voice
    max_nodes=4,
    cpu_per_node=1.9,
    gestures=["pluck", "bow-drone", "sympathetic-strings", "inharmonic-bell", "disastrous-peace"],
    node_params=[
        P("rings.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("rings.pit", "Pitch", unit="note", rmin=24.0, rmax=96.0, default=48.0, musical=(36.0, 72.0), formatter="noteName"),
        P("rings.struct", "Structure", default=0.25, musical=(0.1, 0.8)),
        P("rings.bright", "Brightness", default=0.5, musical=(0.3, 0.9)),
        P("rings.damp", "Damping", default=0.7, musical=(0.4, 0.95)),
        P("rings.pos", "Position", default=0.25, musical=(0.1, 0.8)),
        P("rings.trigRate", "Trig Rate", unit="Hz", rmin=0.1, rmax=20.0, default=2.0, curve=Curve.EXP, formatter="Hz", musical=(0.3, 6.0)),
        P("rings.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.9, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.6, 1.1)),
        P("rings.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2", musical=(-0.85, 0.85)),
    ],
    global_params=[
        P("rings.model", "Model", curve=Curve.ENUM, enum=["modal", "sympathetic", "inharmonic", "fm", "westernChords", "stringAndReverb"], default=0),
        P("rings.poly", "Polyphony", curve=Curve.ENUM, enum=["1", "2", "4"], default=0, modulatable=False),
        P("rings.internalExciter", "Internal Exciter", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
        P("rings.trigMode", "Trig Mode", curve=Curve.ENUM, enum=["periodic", "random"], default=0, modulatable=False),
        P("rings.easteregg", "Disastrous Peace", curve=Curve.ENUM, enum=["off", "on"], default=0, modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #
# WAVIARY — sophisticated morphing-wavetable voice (abrasive & rich)
# --------------------------------------------------------------------------- #
# A detuned-unison VOsc sweeps a bank of consecutive wavetables (smooth -> rich
# -> abrasive) with freq-domain FM, a crossfaded hard-sync scream, wavefolding,
# tanh drive and an env-swept resonant MoogFF. An unstable internal clock (shared
# with DX7) re-gates the envelopes, so the voice is alive and rhythmic, not static.
WAVIARY = ModuleSpec(
    type="WAVIARY",
    role="Morphing-wavetable voice: detuned unison swept across smooth->abrasive tables, FM, hard-sync, wavefold, resonant filter.",
    node_meaning="Wavetable voice.",
    synthdef="waviary",
    insert_capable=False,
    generative_capable=True,
    max_nodes=3,
    cpu_per_node=2.2,
    gestures=["wavetable-morph", "hard-sync-scream", "fold-drive", "resonant-sweep"],
    node_params=[
        P("waviary.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("waviary.pitch", "Pitch", unit="Hz", rmin=20.0, rmax=2000.0, default=110.0, curve=Curve.EXP, musical=(45.0, 440.0), formatter="Hz"),
        P("waviary.position", "Wave Pos", default=0.3, musical=(0.0, 1.0)),
        P("waviary.posMod", "Pos Env", default=0.4, musical=(0.0, 0.8)),
        P("waviary.posDrift", "Pos Drift", default=0.15, musical=(0.0, 0.5)),
        P("waviary.detune", "Detune", default=0.12, musical=(0.0, 0.6)),
        P("waviary.sub", "Sub", default=0.25, musical=(0.0, 0.7)),
        P("waviary.noise", "Noise", default=0.0, musical=(0.0, 0.4)),
        P("waviary.fold", "Wavefold", default=0.0, musical=(0.0, 0.95), danger=DangerClass.LOUDNESS),
        P("waviary.drive", "Drive", default=1.0, musical=(0.5, 4.0), danger=DangerClass.LOUDNESS),
        P("waviary.fmRatio", "FM Ratio", rmin=0.25, rmax=12.0, default=2.0, curve=Curve.EXP, musical=(0.5, 7.0)),
        P("waviary.fmAmount", "FM Amount", default=0.0, musical=(0.0, 0.6)),
        P("waviary.sync", "Hard Sync", default=0.0, musical=(0.0, 0.9)),
        P("waviary.syncRatio", "Sync Ratio", rmin=1.0, rmax=8.0, default=1.5, curve=Curve.EXP, musical=(1.0, 5.0)),
        P("waviary.cutoff", "Cutoff", unit="Hz", rmin=40.0, rmax=14000.0, default=2200.0, curve=Curve.EXP, musical=(180.0, 8000.0), formatter="Hz"),
        P("waviary.res", "Resonance", default=0.4, musical=(0.1, 0.92), danger=DangerClass.FEEDBACK),
        P("waviary.envDepth", "Filter Env", default=0.5, musical=(0.0, 1.0)),
        P("waviary.attack", "Attack", unit="s", rmin=0.001, rmax=2.0, default=0.01, curve=Curve.EXP, musical=(0.002, 0.4), formatter="float2"),
        P("waviary.clkRate", "Clock Rate", unit="Hz", rmin=0.1, rmax=12.0, default=2.0, curve=Curve.EXP, musical=(0.4, 6.0), formatter="Hz"),
        P("waviary.clkChaos", "Clock Chaos", default=0.4, musical=(0.1, 0.9)),
        P("waviary.clkDrift", "Clock Drift", default=0.3, musical=(0.0, 0.8)),
        P("waviary.clkLen", "Note Length", default=0.35, musical=(0.05, 1.5)),
        P("waviary.clkVel", "Velocity Var", default=0.5, musical=(0.0, 0.9)),
        P("waviary.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.5, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 0.9)),
        P("waviary.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[],
)




# --------------------------------------------------------------------------- #
# ENV — ADSR envelope VCA / audio gate
# --------------------------------------------------------------------------- #
ENV = ModuleSpec(
    type="ENV",
    role="ADSR envelope VCA: shapes the amplitude of incoming audio with a classic attack-decay-sustain-release envelope.",
    node_meaning="Envelope voice.",
    synthdef="env",
    insert_capable=True,
    generative_capable=False,
    max_nodes=4,
    cpu_per_node=1.0,
    gestures=["envelope", "gate", "vca"],
    node_params=[
        P("env.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("env.attack", "Attack", unit="ms", rmin=1.0, rmax=10000.0, default=20.0, curve=Curve.EXP, formatter="ms", musical=(5.0, 500.0)),
        P("env.decay", "Decay", unit="ms", rmin=1.0, rmax=10000.0, default=200.0, curve=Curve.EXP, formatter="ms", musical=(10.0, 1000.0)),
        P("env.sustain", "Sustain", rmin=0.0, rmax=1.0, default=0.7, musical=(0.0, 1.0)),
        P("env.release", "Release", unit="ms", rmin=1.0, rmax=15000.0, default=500.0, curve=Curve.EXP, formatter="ms", musical=(50.0, 3000.0)),
        P("env.curve", "Curve", rmin=-8.0, rmax=8.0, default=-4.0, musical=(-4.0, 0.0)),
        P("env.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 1.0)),
        P("env.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[],
)


# --------------------------------------------------------------------------- #
# GATE — signal-based rhythmic gate / chopper
# --------------------------------------------------------------------------- #
GATE = ModuleSpec(
    type="GATE",
    role="Clock-driven 8-step rhythmic gate: swing, jitter, probability, A/D shaping, invert/ducking and smoothing.",
    node_meaning="Gate voice.",
    synthdef="gate",
    insert_capable=True,
    generative_capable=False,
    max_nodes=4,
    cpu_per_node=1.5,
    gestures=["gate", "chopper", "sequencer", "duck"],
    node_params=[
        P("gate.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("gate.freq", "Clock", unit="Hz", rmin=0.1, rmax=100.0, default=4.0, curve=Curve.EXP, formatter="Hz", musical=(0.5, 16.0)),
        P("gate.phase", "Phase", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("gate.swing", "Swing", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.5)),
        P("gate.duty", "Duty", rmin=0.01, rmax=0.99, default=0.5, musical=(0.2, 0.8)),
        P("gate.attack", "Attack", unit="ms", rmin=0.1, rmax=500.0, default=5.0, curve=Curve.EXP, formatter="ms", musical=(1.0, 50.0)),
        P("gate.decay", "Decay", unit="ms", rmin=0.1, rmax=1000.0, default=20.0, curve=Curve.EXP, formatter="ms", musical=(5.0, 200.0)),
        P("gate.shape", "Shape", curve=Curve.ENUM, enum=["linear", "exp", "log"], default=0, randomize=RandomizePolicy.OFF),
        P("gate.step0", "Step 1", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("gate.step1", "Step 2", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("gate.step2", "Step 3", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("gate.step3", "Step 4", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("gate.step4", "Step 5", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("gate.step5", "Step 6", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("gate.step6", "Step 7", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("gate.step7", "Step 8", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 1.0)),
        P("gate.probability", "Probability", rmin=0.0, rmax=1.0, default=1.0, musical=(0.5, 1.0)),
        P("gate.jitter", "Jitter", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.5)),
        P("gate.wet", "Wet", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("gate.invert", "Invert", curve=Curve.ENUM, enum=["off", "on"], default=0, randomize=RandomizePolicy.OFF),
        P("gate.smoothing", "Smooth", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.5)),
        P("gate.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 1.0)),
        P("gate.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[],
)




# --------------------------------------------------------------------------- #
# DISTORT — versatile multi-stage distortion / saturation / waveshaper
# --------------------------------------------------------------------------- #
DISTORT = ModuleSpec(
    type="DISTORT",
    role="Multi-algorithm distortion: tube warmth to wavefolding, bitcrush and screaming feedback.",
    node_meaning="Distortion / waveshaper stage (stack for parallel characters).",
    synthdef="distort",
    insert_capable=True,
    generative_capable=False,
    max_nodes=4,
    cpu_per_node=1.7,
    gestures=["saturate", "fuzz", "wavefold", "bitcrush", "scream", "parallel-texture"],
    node_params=[
        P("distort.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("distort.drive", "Drive", rmin=1.0, rmax=64.0, default=2.0, curve=Curve.EXP, musical=(1.5, 24.0)),
        P("distort.type", "Type", curve=Curve.ENUM, enum=["tube", "soft", "fuzz", "fold", "wrap", "diode", "sine", "cheby"], default=0),
        P("distort.bias", "Bias", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.6)),
        P("distort.fold", "Fold", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.8)),
        P("distort.tone", "Tone", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.6, 0.7)),
        P("distort.crush", "Bit Crush", rmin=1.0, rmax=24.0, default=24.0, rate=Rate.DISCRETE, formatter="int", musical=(5.0, 24.0)),
        P("distort.downsample", "Downsample", rmin=1.0, rmax=64.0, default=1.0, curve=Curve.EXP, formatter="float2", musical=(1.0, 16.0)),
        P("distort.feedback", "Feedback", rmin=0.0, rmax=0.98, default=0.0, musical=(0.0, 0.6), danger=DangerClass.FEEDBACK, formatter="percent1"),
        P("distort.lowCut", "Low Cut", unit="Hz", rmin=20.0, rmax=2000.0, default=20.0, curve=Curve.EXP, formatter="Hz", musical=(20.0, 400.0)),
        P("distort.highCut", "High Cut", unit="Hz", rmin=200.0, rmax=19000.0, default=18000.0, curve=Curve.EXP, formatter="Hz", musical=(2500.0, 16000.0)),
        P("distort.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.7, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.9)),
        P("distort.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[],
)


# --------------------------------------------------------------------------- #
# SEQ — MIDI sequencer / clock source (control-only; drives modules over the MIDI
# graph). No audio synth, no DSP params: its bespoke clock + Turing-machine lanes
# live in the SeqEngine (seq.py) and are edited via dedicated commands/UI. Lanes
# are built dynamically from whatever modules its MIDI-out is connected to.
# --------------------------------------------------------------------------- #
SEQ = ModuleSpec(
    type="SEQ",
    role="MIDI sequencer & clock: Turing-machine note + CC lanes, polymetric, driving connected modules.",
    node_meaning="Sequencer (control source).",
    synthdef="seq",
    insert_capable=False,
    generative_capable=False,
    max_nodes=1,
    is_audio=False,
    cpu_per_node=0.0,
    gestures=["turing-machine", "polymeter", "cc-lanes", "clock"],
    node_params=[],
    # Global, modulatable clock controls (the SeqEngine reads these each tick, so the
    # module-LFO bank can wobble tempo / thin out density / freeze-and-evolve mutation).
    global_params=[
        P("seq.tempo", "Tempo", unit="bpm", rmin=20.0, rmax=300.0, default=120.0, formatter="float2", musical=(70.0, 170.0)),
        P("seq.density", "Density", rmin=0.0, rmax=2.0, default=1.0, formatter="percent0", musical=(0.4, 1.4)),
        P("seq.mutation", "Mutation", rmin=0.0, rmax=2.0, default=1.0, formatter="percent0", musical=(0.0, 1.5)),
    ],
)




# =========================================================================== #
# Pedal FX — a fleet of classic effects ported from 21echoes/pedalboard. Each is
# an insert (mono-summed -> processed -> spread/panned), with the module wet/dry
# as the mix and a per-effect amp + pan. Kept compact and modulatable.
# =========================================================================== #
def _fx_tail(prefix: str, amp_default: float = 0.85):
    return [
        P(f"{prefix}.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=amp_default,
          curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.4, 1.0)),
        P(f"{prefix}.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR,
          formatter="float2", musical=(-0.85, 0.85)),
    ]


def _fx_enable(prefix: str):
    return P(f"{prefix}.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"],
             default=1, randomize=RandomizePolicy.OFF, modulatable=False)


OVERDRIVE = ModuleSpec(
    type="OVERDRIVE", role="Soft-clipping overdrive with tone tilt (pedalboard).",
    node_meaning="Overdrive stage.", synthdef="overdrive", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=1.0, gestures=["overdrive", "warm-clip"],
    node_params=[
        _fx_enable("overdrive"),
        P("overdrive.drive", "Drive", rmin=1.0, rmax=50.0, default=4.0, curve=Curve.EXP, musical=(1.5, 24.0)),
        P("overdrive.tone", "Tone", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.6, 0.7)),
        *_fx_tail("overdrive", 0.8),
    ], global_params=[],
)

AMPSIM = ModuleSpec(
    type="AMPSIM", role="Guitar amp simulator: drive + 3-band tone stack + cabinet (pedalboard).",
    node_meaning="Amp + cabinet stage.", synthdef="ampsim", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=1.4, gestures=["amp", "cabinet", "grind"],
    node_params=[
        _fx_enable("ampsim"),
        P("ampsim.gain", "Gain", rmin=1.0, rmax=40.0, default=6.0, curve=Curve.EXP, musical=(2.0, 20.0)),
        P("ampsim.bass", "Bass", unit="dB", rmin=-15.0, rmax=15.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-8.0, 8.0)),
        P("ampsim.mid", "Mid", unit="dB", rmin=-15.0, rmax=15.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-8.0, 8.0)),
        P("ampsim.treble", "Treble", unit="dB", rmin=-15.0, rmax=15.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-8.0, 8.0)),
        *_fx_tail("ampsim", 0.8),
    ], global_params=[],
)

EQUALIZER = ModuleSpec(
    type="EQUALIZER", role="Three-band parametric EQ: low/high shelves + sweepable mid (pedalboard).",
    node_meaning="EQ stage.", synthdef="equalizer", insert_capable=True,
    generative_capable=False, max_nodes=2, cpu_per_node=1.0, gestures=["eq", "tone-shape"],
    node_params=[
        _fx_enable("equalizer"),
        P("equalizer.low", "Low", unit="dB", rmin=-18.0, rmax=18.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-10.0, 10.0)),
        P("equalizer.midGain", "Mid", unit="dB", rmin=-18.0, rmax=18.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-10.0, 10.0)),
        P("equalizer.midFreq", "Mid Freq", unit="Hz", rmin=150.0, rmax=6000.0, default=900.0, curve=Curve.EXP, formatter="Hz", musical=(300.0, 3500.0)),
        P("equalizer.high", "High", unit="dB", rmin=-18.0, rmax=18.0, default=0.0, curve=Curve.BIPOLAR, formatter="dB1", musical=(-10.0, 10.0)),
        *_fx_tail("equalizer", 1.0),
    ], global_params=[],
)

FLANGER = ModuleSpec(
    type="FLANGER", role="Modulated short-delay flanger with feedback (pedalboard).",
    node_meaning="Flanger stage.", synthdef="flanger", insert_capable=True,
    generative_capable=False, max_nodes=2, cpu_per_node=1.1, gestures=["flange", "jet", "sweep"],
    node_params=[
        _fx_enable("flanger"),
        P("flanger.rate", "Rate", unit="Hz", rmin=0.02, rmax=8.0, default=0.3, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("flanger.depth", "Depth", default=0.6, musical=(0.3, 1.0)),
        P("flanger.feedback", "Feedback", rmin=0.0, rmax=0.95, default=0.4, danger=DangerClass.FEEDBACK, musical=(0.0, 0.8)),
        *_fx_tail("flanger", 1.0),
    ], global_params=[],
)

PHASER = ModuleSpec(
    type="PHASER", role="1970s string-machine phaser: 6-stage all-pass notch sweep with "
                        "resonant feedback and a wide quadrature-LFO stereo swirl (ARP/Solina + Small Stone).",
    node_meaning="Phaser voice.", synthdef="phaser", insert_capable=True,
    generative_capable=False, max_nodes=2, cpu_per_node=1.2, gestures=["phase", "sweep", "swirl"],
    node_params=[
        _fx_enable("phaser"),
        P("phaser.rate", "Rate", unit="Hz", rmin=0.02, rmax=8.0, default=0.4, curve=Curve.EXP, formatter="float2", musical=(0.05, 1.2)),
        P("phaser.depth", "Depth", default=0.7, musical=(0.4, 1.0)),
        P("phaser.feedback", "Feedback", rmin=0.0, rmax=0.9, default=0.35, danger=DangerClass.FEEDBACK, musical=(0.1, 0.7)),
        P("phaser.spread", "Stereo", default=0.8, musical=(0.5, 1.0)),
        *_fx_tail("phaser", 1.0),
    ], global_params=[],
)

RINGMOD = ModuleSpec(
    type="RINGMOD", role="Ring modulator: multiply the signal by a sine carrier (pedalboard).",
    node_meaning="Ring-mod stage.", synthdef="ringmod", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=0.8, gestures=["ringmod", "metallic", "clang"],
    node_params=[
        _fx_enable("ringmod"),
        P("ringmod.freq", "Frequency", unit="Hz", rmin=1.0, rmax=4000.0, default=200.0, curve=Curve.EXP, formatter="Hz", musical=(30.0, 1200.0)),
        *_fx_tail("ringmod", 0.9),
    ], global_params=[],
)

BITCRUSHER = ModuleSpec(
    type="BITCRUSHER", role="Bit-depth + sample-rate reduction (pedalboard).",
    node_meaning="Bitcrush stage.", synthdef="bitcrusher", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=0.8, gestures=["bitcrush", "downsample", "digital-grit"],
    node_params=[
        _fx_enable("bitcrusher"),
        P("bitcrusher.bits", "Bits", rmin=1.0, rmax=24.0, default=8.0, rate=Rate.DISCRETE, formatter="int", musical=(3.0, 12.0)),
        P("bitcrusher.downsample", "Downsample", rmin=1.0, rmax=64.0, default=4.0, curve=Curve.EXP, formatter="float2", musical=(1.0, 24.0)),
        *_fx_tail("bitcrusher", 0.9),
    ], global_params=[],
)

LOFI = ModuleSpec(
    type="LOFI", role="Lo-fi degrade: bit/sample-rate crush + band-limit + hiss (pedalboard).",
    node_meaning="Lo-fi stage.", synthdef="lofi", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=1.0, gestures=["lo-fi", "radio", "tape-grunge"],
    node_params=[
        _fx_enable("lofi"),
        P("lofi.bits", "Bits", rmin=1.0, rmax=24.0, default=10.0, rate=Rate.DISCRETE, formatter="int", musical=(4.0, 14.0)),
        P("lofi.downsample", "Downsample", rmin=1.0, rmax=48.0, default=6.0, curve=Curve.EXP, formatter="float2", musical=(1.0, 20.0)),
        P("lofi.cutoff", "Tone", unit="Hz", rmin=200.0, rmax=12000.0, default=4000.0, curve=Curve.EXP, formatter="Hz", musical=(800.0, 7000.0)),
        P("lofi.noise", "Hiss", default=0.1, musical=(0.0, 0.4)),
        *_fx_tail("lofi", 0.9),
    ], global_params=[],
)

TREMOLO = ModuleSpec(
    type="TREMOLO", role="Amplitude tremolo with selectable LFO shape (pedalboard).",
    node_meaning="Tremolo stage.", synthdef="tremolo", insert_capable=True,
    generative_capable=False, max_nodes=2, cpu_per_node=0.7, gestures=["tremolo", "chop", "pulse"],
    node_params=[
        _fx_enable("tremolo"),
        P("tremolo.rate", "Rate", unit="Hz", rmin=0.05, rmax=20.0, default=4.0, curve=Curve.EXP, formatter="float2", musical=(0.5, 10.0)),
        P("tremolo.depth", "Depth", default=0.6, musical=(0.2, 1.0)),
        P("tremolo.shape", "Shape", curve=Curve.ENUM, enum=["sine", "triangle", "square"], default=0, modulatable=False),
        *_fx_tail("tremolo", 1.0),
    ], global_params=[],
)

WAVEFOLDER = ModuleSpec(
    type="WAVEFOLDER", role="West-coast wavefolder: fold the waveform back on itself (pedalboard).",
    node_meaning="Wavefolder stage.", synthdef="wavefolder", insert_capable=True,
    generative_capable=False, max_nodes=4, cpu_per_node=1.0, gestures=["wavefold", "harmonics", "west-coast"],
    node_params=[
        _fx_enable("wavefolder"),
        P("wavefolder.fold", "Fold", rmin=1.0, rmax=20.0, default=2.0, curve=Curve.EXP, musical=(1.5, 12.0)),
        P("wavefolder.bias", "Bias", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.6, 0.6)),
        *_fx_tail("wavefolder", 0.8),
    ], global_params=[],
)


# NB (Wildrider-Move): SEQ (MIDI sequencer) is intentionally excluded — the Move
# takeover does not use it. Its ModuleSpec + SeqEngine remain in the codebase for
# desktop parity, but it is not registered, so it never appears in any patch/grid.
CATALOG: dict[str, ModuleSpec] = {
    m.type: m for m in (FMTONE, FM7, BUCHLOID, MOLLY, BEN, NOIZEOP, CHAOS,
                        ICARUS, PLAITS, SHAKER, MEMBRANE, PLUCK, TUBE, WTABLE, BYTEBEAT,   # MALLET, BOWED excluded (StkInst rawwave models silent/racy on this sc3-plugins build)
                        PITCH, TIME, COMB, GAIN, SDLY, VERB,
                        CLOUDS, GRAINS, RINGS, WAVIARY, ENV, GATE, DISTORT,
                        OVERDRIVE, AMPSIM, EQUALIZER, FLANGER, PHASER, RINGMOD,
                        BITCRUSHER, LOFI, TREMOLO, WAVEFOLDER)
}   # FBANK + PLAITS + MOLLY + BUCHLOID retired above; BEN replaced by WAVIARY

# Ordered lanes (source -> processors -> spatial tail).
DEFAULT_LANE_ORDER = ["FMTONE", "FM7", "BUCHLOID", "MOLLY", "BEN", "NOIZEOP", "CHAOS",
                      "ICARUS", "PLAITS", "SHAKER", "MEMBRANE", "PLUCK", "TUBE", "WTABLE", "BYTEBEAT",   # MALLET/BOWED: unreliable StkInst rawwave load on this build
                      "PITCH", "TIME", "COMB", "GAIN",
                      "SDLY", "VERB", "CLOUDS", "GRAINS", "RINGS", "WAVIARY", "ENV", "GATE",
                      "DISTORT", "OVERDRIVE", "AMPSIM", "EQUALIZER", "FLANGER", "PHASER", "RINGMOD",
                      "BITCRUSHER", "LOFI", "TREMOLO", "WAVEFOLDER"]


def spec(module_type: str) -> ModuleSpec:
    return CATALOG[module_type]
