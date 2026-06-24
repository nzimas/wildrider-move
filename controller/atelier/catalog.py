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
def _dx7_operator_params() -> list[ParamMetadata]:
    """The per-operator timbre controls (manual mode). Six operators, each with
    frequency ratio (coarse/fine/detune) and output level — these, with the
    algorithm + feedback, are what shape the FM spectrum. All randomizable +
    modulatable. Op1 is the default carrier; op2 modulates it on algorithm 1."""
    out: list[ParamMetadata] = []
    for n in range(1, 7):
        lvl = 99.0 if n == 1 else (75.0 if n == 2 else 0.0)
        out += [
            P(f"dx7.op{n}Coarse", f"Op{n} Ratio", rmin=0.0, rmax=31.0, default=1.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.SAFE, musical=(0.0, 14.0)),
            P(f"dx7.op{n}Fine", f"Op{n} Fine", rmin=0.0, rmax=99.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE),
            P(f"dx7.op{n}Detune", f"Op{n} Detune", rmin=0.0, rmax=14.0, default=7.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.SAFE, musical=(3.0, 11.0)),
            P(f"dx7.op{n}Level", f"Op{n} Level", rmin=0.0, rmax=99.0, default=lvl, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE, musical=(0.0, 99.0)),
        ]
    return out


DX7 = ModuleSpec(
    type="DX7",
    role="Yamaha DX7 6-operator FM voice: 16,384 factory presets, or a hand-tweakable manual patch.",
    node_meaning="DX7 FM voice (one held note).",
    synthdef="dx7",
    insert_capable=False,
    generative_capable=True,
    max_nodes=8,
    cpu_per_node=2.6,
    gestures=["fm-bank", "preset-morph", "detuned-stack", "arp"],
    node_params=[
        P("dx7.enable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("dx7.note", "Note", unit="note", rmin=0.0, rmax=127.0, default=48.0, rate=Rate.DISCRETE, formatter="noteName", musical=(36.0, 72.0)),
        P("dx7.velocity", "Velocity", rmin=0.0, rmax=127.0, default=100.0, rate=Rate.DISCRETE, formatter="int", musical=(40.0, 120.0)),
        P("dx7.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.7, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.9)),
        P("dx7.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        # Voice source: "factory" picks one of 16,384 bundled presets; "manual"
        # builds the voice live from the operator/LFO controls below.
        P("dx7.mode", "Mode", curve=Curve.ENUM, enum=["factory", "manual"], default=0, randomize=RandomizePolicy.OFF, modulatable=False),
        P("dx7.preset", "Preset", rmin=0.0, rmax=16383.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE),
        P("dx7.transpose", "Transpose", unit="semitone", rmin=-24.0, rmax=24.0, default=0.0, rate=Rate.DISCRETE, formatter="semitone", randomize=RandomizePolicy.SAFE, musical=(-12.0, 12.0)),
        # DX7 is a pure (drone) sound source: timing/dynamics come from downstream
        # ENV / GATE modules and (future) MIDI generators, not an internal clock.
        # Manual-mode timbre controls (inert in factory mode).
        P("dx7.algorithm", "Algorithm", rmin=0.0, rmax=31.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE, musical=(0.0, 31.0)),
        P("dx7.feedback", "Feedback", rmin=0.0, rmax=7.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE, musical=(0.0, 7.0)),
        P("dx7.lfoRate", "LFO Rate", rmin=0.0, rmax=99.0, default=35.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.WIDE),
        P("dx7.lfoWave", "LFO Wave", curve=Curve.ENUM, enum=["triangle", "saw down", "saw up", "square", "sine", "s&h"], default=0, randomize=RandomizePolicy.WIDE),
        P("dx7.lfoPMD", "LFO Pitch Mod", rmin=0.0, rmax=99.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.SAFE, musical=(0.0, 40.0)),
        P("dx7.lfoAMD", "LFO Amp Mod", rmin=0.0, rmax=99.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.SAFE, musical=(0.0, 50.0)),
        P("dx7.lfoPMS", "LFO Pitch Sens", rmin=0.0, rmax=7.0, default=0.0, rate=Rate.DISCRETE, formatter="int", randomize=RandomizePolicy.SAFE, musical=(0.0, 5.0)),
        *_dx7_operator_params(),
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
# BEN — Benjolis-inspired chaotic oscillator (port of scazan/benjolis)
# --------------------------------------------------------------------------- #
BEN = ModuleSpec(
    type="BEN",
    role="Chaotic twin-oscillator / rungler voice inspired by Rob Hordijk's Benjolin.",
    node_meaning="Benjolis voice.",
    synthdef="ben",
    insert_capable=False,
    generative_capable=True,
    max_nodes=4,
    cpu_per_node=1.6,
    gestures=["rungler-chaos", "pwm-scream", "filter-sweep", "self-patching"],
    node_params=[
        P("ben.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("ben.freq1", "Osc 1 Freq", unit="Hz", rmin=20.0, rmax=2000.0, default=40.0, curve=Curve.EXP, musical=(30.0, 300.0), formatter="Hz"),
        P("ben.freq2", "Osc 2 Freq", unit="Hz", rmin=0.1, rmax=200.0, default=4.0, curve=Curve.EXP, musical=(0.5, 30.0), formatter="Hz"),
        P("ben.scale", "Rungler Scale", default=1.0, musical=(0.2, 1.0)),
        P("ben.rungler1", "Rungler 1", default=0.16, musical=(0.0, 0.5)),
        P("ben.rungler2", "Rungler 2", default=0.0, musical=(0.0, 0.5)),
        P("ben.runglerFilt", "Filter Rungler", default=9.0, musical=(0.0, 24.0)),
        P("ben.loop", "Loop", default=0.0, musical=(0.0, 1.0)),
        P("ben.filtFreq", "Filter Freq", unit="Hz", rmin=20.0, rmax=12000.0, default=40.0, curve=Curve.EXP, musical=(40.0, 4000.0), formatter="Hz"),
        P("ben.q", "Resonance", default=0.82, musical=(0.1, 0.98)),
        P("ben.gain", "Filter Gain", rmin=0.0, rmax=4.0, default=1.0, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS),
        P("ben.filterType", "Filter Type", curve=Curve.ENUM, enum=["lowpass", "highpass", "stateVar", "dfm1"], default=0, modulatable=False),
        P("ben.outSignal", "Output", curve=Curve.ENUM, enum=["tri1", "pulse1", "tri2", "pulse2", "pwm", "rungler", "filter"], default=6, modulatable=False),
        P("ben.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.5, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 0.9)),
        P("ben.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[],
)


# --------------------------------------------------------------------------- #
# BUCHLOID — west-coast complex oscillator / FM voice (port of markwheeler/passersby)
# --------------------------------------------------------------------------- #
BUCHLOID = ModuleSpec(
    type="BUCHLOID",
    role="West-coast complex oscillator with FM, wave folding and resonant lowpass gate.",
    node_meaning="Buchloid voice.",
    synthdef="buchloid",
    insert_capable=False,
    generative_capable=True,
    max_nodes=4,
    cpu_per_node=2.2,
    gestures=["complex-osc", "fm", "wave-fold", "lpg"],
    node_params=[
        P("buchloid.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("buchloid.freq", "Freq", unit="Hz", rmin=20.0, rmax=2000.0, default=220.0, curve=Curve.EXP, musical=(40.0, 880.0), formatter="Hz"),
        P("buchloid.glide", "Glide", rmin=0.0, rmax=1.0, default=0.0, musical=(0.0, 0.3)),
        P("buchloid.fm1Ratio", "FM1 Ratio", rmin=0.1, rmax=10.0, default=0.66, curve=Curve.EXP, musical=(0.5, 4.0)),
        P("buchloid.fm2Ratio", "FM2 Ratio", rmin=0.1, rmax=20.0, default=3.3, curve=Curve.EXP, musical=(1.0, 8.0)),
        P("buchloid.fm1Amount", "FM1 Amt", default=0.0, musical=(0.0, 0.5)),
        P("buchloid.fm2Amount", "FM2 Amt", default=0.0, musical=(0.0, 0.5)),
        P("buchloid.waveShape", "Wave Shape", default=0.0, musical=(0.0, 1.0)),
        P("buchloid.waveFolds", "Wave Folds", rmin=0.0, rmax=3.0, default=0.0, musical=(0.0, 1.5)),
        P("buchloid.timbre", "Timbre", default=0.0, musical=(0.0, 1.0)),
        P("buchloid.attack", "Attack", unit="ms", rmin=3.0, rmax=8000.0, default=40.0, curve=Curve.EXP, formatter="ms", musical=(5.0, 1000.0)),
        P("buchloid.peak", "Filter Peak", unit="Hz", rmin=100.0, rmax=10000.0, default=10000.0, curve=Curve.EXP, formatter="Hz", musical=(500.0, 8000.0)),
        P("buchloid.decay", "Decay", unit="ms", rmin=3.0, rmax=8000.0, default=1000.0, curve=Curve.EXP, formatter="ms", musical=(100.0, 3000.0)),
        P("buchloid.pressure", "Pressure", default=0.0, musical=(0.0, 1.0)),
        P("buchloid.velocity", "Velocity", default=0.7, musical=(0.2, 1.0)),
        P("buchloid.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.5, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 0.9)),
        P("buchloid.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
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
# PLAITS — Mutable Instruments Plaits macro-oscillator
# --------------------------------------------------------------------------- #
PLAITS = ModuleSpec(
    type="PLAITS",
    role="Mutable Instruments Plaits macro-oscillator: 16 synthesis models with internal LPG/VCA.",
    node_meaning="Plaits voice.",
    synthdef="plaits",
    insert_capable=False,
    generative_capable=True,
    max_nodes=4,
    cpu_per_node=2.5,
    gestures=["macro-osc", "fm", "wavetable", "physical", "percussion"],
    node_params=[
        P("plaits.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("plaits.pitch", "Pitch", unit="note", rmin=0.0, rmax=127.0, default=60.0, formatter="noteName", musical=(36.0, 84.0)),
        P("plaits.engine", "Engine", curve=Curve.ENUM, enum=[str(i) for i in range(16)], default=0, randomize=RandomizePolicy.SAFE, musical=(0.0, 15.0)),
        P("plaits.harm", "Harmonics", rmin=0.0, rmax=1.0, default=0.1, musical=(0.0, 1.0)),
        P("plaits.timbre", "Timbre", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.morph", "Morph", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.trigger", "Trigger", curve=Curve.ENUM, enum=["off", "on"], default=0, randomize=RandomizePolicy.OFF),
        P("plaits.level", "Level", rmin=0.0, rmax=1.0, default=1.0, musical=(0.0, 1.0)),
        P("plaits.fm_mod", "FM Mod", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("plaits.timb_mod", "Timbre Mod", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("plaits.morph_mod", "Morph Mod", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, musical=(-0.5, 0.5)),
        P("plaits.decay", "LPG Decay", rmin=0.0, rmax=1.0, default=0.5, musical=(0.1, 0.9)),
        P("plaits.lpg_colour", "LPG Colour", rmin=0.0, rmax=1.0, default=0.5, musical=(0.0, 1.0)),
        P("plaits.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.5, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.3, 0.9)),
        P("plaits.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
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


# MOLLY — a port of Mark Wheeler's "Molly the Poly" (Norns): a characterful
# analogue-voiced polysynth. Dual morphing oscillators (tri→saw→pulse) + sub +
# noise, optional ring mod, a resonant low-pass (12/24 dB) with its own ADSR, an
# amp ADSR, an LFO routable to pitch/PW/cutoff/amp, overdrive and chorus. Driven
# by note + gate (drone by default; SEQ / GATE articulate it). The `scope` param
# recreates Molly's three sound types — lead / pad / percussion — and steers
# GUIDED randomization toward that sound's musical use.
MOLLY = ModuleSpec(
    type="MOLLY",
    role="Molly the Poly: characterful analogue-voiced polysynth (lead/pad/perc).",
    node_meaning="One analogue voice (stack nodes for unison/poly drones).",
    synthdef="molly",
    insert_capable=False,
    generative_capable=True,    # a voice — note + gate (SEQ/GATE drive it)
    max_nodes=3,
    cpu_per_node=2.6,
    gestures=["lead", "pad", "percussion", "filter-sweep", "ring-mod", "chorus-wash"],
    node_params=[
        P("molly.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("molly.note", "Note", rmin=0.0, rmax=127.0, default=48.0, curve=Curve.LINEAR, formatter="float0", randomize=RandomizePolicy.OFF, modulatable=False),
        P("molly.gate", "Gate", curve=Curve.ENUM, enum=["off", "on"], default=1, rate=Rate.TRIGGER, randomize=RandomizePolicy.OFF, modulatable=False),
        P("molly.detune", "Detune", unit="cent", rmin=0.0, rmax=50.0, default=7.0, formatter="float1", musical=(2.0, 22.0)),
        P("molly.oscShape", "Osc Shape", default=0.5, musical=(0.2, 1.0)),
        P("molly.pulseWidth", "Pulse Width", rmin=0.05, rmax=0.95, default=0.5, musical=(0.2, 0.8)),
        P("molly.subLevel", "Sub", default=0.0, musical=(0.0, 0.5)),
        P("molly.noiseLevel", "Noise", default=0.0, musical=(0.0, 0.3)),
        P("molly.cutoff", "Cutoff", unit="Hz", rmin=20.0, rmax=18000.0, default=1200.0, curve=Curve.EXP, formatter="Hz", musical=(300.0, 6000.0)),
        P("molly.resonance", "Resonance", default=0.2, musical=(0.1, 0.7), danger=DangerClass.FEEDBACK),
        P("molly.filterEnvAmt", "Filter Env", rmin=-1.0, rmax=1.0, default=0.3, curve=Curve.BIPOLAR, musical=(0.0, 0.8)),
        P("molly.fAtk", "F.Attack", unit="s", rmin=0.001, rmax=5.0, default=0.05, curve=Curve.EXP, formatter="float3", musical=(0.002, 1.5)),
        P("molly.fDec", "F.Decay", unit="s", rmin=0.001, rmax=5.0, default=0.3, curve=Curve.EXP, formatter="float3", musical=(0.03, 1.5)),
        P("molly.fSus", "F.Sustain", default=0.6, musical=(0.0, 0.9)),
        P("molly.fRel", "F.Release", unit="s", rmin=0.001, rmax=8.0, default=0.6, curve=Curve.EXP, formatter="float3", musical=(0.05, 3.0)),
        P("molly.aAtk", "A.Attack", unit="s", rmin=0.001, rmax=5.0, default=0.01, curve=Curve.EXP, formatter="float3", musical=(0.002, 2.0)),
        P("molly.aDec", "A.Decay", unit="s", rmin=0.001, rmax=5.0, default=0.3, curve=Curve.EXP, formatter="float3", musical=(0.05, 1.5)),
        P("molly.aSus", "A.Sustain", default=0.8, musical=(0.0, 1.0)),
        P("molly.aRel", "A.Release", unit="s", rmin=0.001, rmax=8.0, default=0.5, curve=Curve.EXP, formatter="float3", musical=(0.05, 4.0)),
        P("molly.lfoRate", "LFO Rate", unit="Hz", rmin=0.01, rmax=30.0, default=4.0, curve=Curve.EXP, formatter="float2", musical=(0.1, 8.0)),
        P("molly.lfoToCutoff", "LFO→Cutoff", default=0.0, musical=(0.0, 0.4)),
        P("molly.lfoToPitch", "LFO→Pitch", default=0.0, musical=(0.0, 0.3)),
        P("molly.lfoToPW", "LFO→PW", default=0.0, musical=(0.0, 0.4)),
        P("molly.lfoToAmp", "LFO→Amp", default=0.0, musical=(0.0, 0.4)),
        P("molly.ringMod", "Ring Mod", default=0.0, musical=(0.0, 0.4)),
        P("molly.drive", "Drive", default=0.0, musical=(0.0, 0.6)),
        P("molly.chorus", "Chorus", default=0.0, musical=(0.0, 0.7)),
        P("molly.pan", "Pan", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="float2"),
        P("molly.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.3, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.2, 0.6)),
    ],
    global_params=[
        P("molly.scope", "Scope", curve=Curve.ENUM, enum=["lead", "pad", "percussion"], default=0, modulatable=False, randomize=RandomizePolicy.OFF),
        P("molly.lpType", "Filter", curve=Curve.ENUM, enum=["12 dB", "24 dB"], default=1, modulatable=False),
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
    type="PHASER", role="Six-stage all-pass phaser swept by an LFO, with feedback (pedalboard).",
    node_meaning="Phaser stage.", synthdef="phaser", insert_capable=True,
    generative_capable=False, max_nodes=2, cpu_per_node=1.2, gestures=["phase", "sweep", "swirl"],
    node_params=[
        _fx_enable("phaser"),
        P("phaser.rate", "Rate", unit="Hz", rmin=0.02, rmax=8.0, default=0.4, curve=Curve.EXP, formatter="float2", musical=(0.05, 2.0)),
        P("phaser.depth", "Depth", default=0.7, musical=(0.3, 1.0)),
        P("phaser.feedback", "Feedback", rmin=0.0, rmax=0.9, default=0.3, danger=DangerClass.FEEDBACK, musical=(0.0, 0.7)),
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
    m.type: m for m in (DX7, PITCH, TIME, COMB, GAIN, SDLY, VERB,
                        CLOUDS, GRAINS, RINGS, BEN, BUCHLOID, ENV, GATE, DISTORT,
                        OVERDRIVE, AMPSIM, EQUALIZER, FLANGER, PHASER, RINGMOD,
                        BITCRUSHER, LOFI, TREMOLO, WAVEFOLDER)
}   # FBANK + PLAITS + MOLLY retired from the stack (still defined above, just not registered)

# Ordered lanes (source -> processors -> spatial tail).
DEFAULT_LANE_ORDER = ["DX7", "PITCH", "TIME", "COMB", "GAIN",
                      "SDLY", "VERB", "CLOUDS", "GRAINS", "RINGS", "BEN", "BUCHLOID", "ENV", "GATE",
                      "DISTORT", "OVERDRIVE", "AMPSIM", "EQUALIZER", "FLANGER", "PHASER", "RINGMOD",
                      "BITCRUSHER", "LOFI", "TREMOLO", "WAVEFOLDER"]


def spec(module_type: str) -> ModuleSpec:
    return CATALOG[module_type]
