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
# 5.1 PLAY — sampler / live recorder / virtual tape / region looper / resampler
# --------------------------------------------------------------------------- #
PLAY = ModuleSpec(
    type="PLAY",
    role="Sampler, live recorder, virtual tape, region looper, resampler.",
    node_meaning="Playhead / region reader.",
    synthdef="play",
    insert_capable=True,
    generative_capable=True,
    max_nodes=16,
    cpu_per_node=1.2,
    gestures=["capture", "overdub", "freeze", "scan", "splice",
              "throw-to-buffer", "granular-by-region"],
    node_params=[
        P("play.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("play.bufferID", "Buffer", curve=Curve.ENUM, enum=["A", "B", "C", "D"], default=0, randomize=RandomizePolicy.OFF),
        P("play.regionStart", "Region Start", unit="%", formatter="percent1", default=0.0, randomize=RandomizePolicy.WIDE),
        P("play.regionLength", "Region Length", unit="%", formatter="percent1", default=1.0, musical=(0.05, 1.0)),
        P("play.rate", "Rate", rmin=-4.0, rmax=4.0, default=1.0, curve=Curve.BIPOLAR, musical=(0.5, 2.0), formatter="float2"),
        P("play.rateMode", "Rate Mode", curve=Curve.ENUM, enum=["absolute", "ratio", "tempo"], default=0, randomize=RandomizePolicy.OFF),
        P("play.direction", "Direction", curve=Curve.ENUM, enum=["forward", "reverse", "alternate"], default=0),
        P("play.phaseOffset", "Phase", unit="%", formatter="percent1", default=0.0),
        P("play.loopMode", "Loop", curve=Curve.ENUM, enum=["oneshot", "loop", "pingpong", "scrub"], default=1),
        P("play.envAttack", "Attack", unit="ms", rmin=0.0, rmax=4000.0, default=5.0, curve=Curve.EXP, formatter="ms"),
        P("play.envRelease", "Release", unit="ms", rmin=0.0, rmax=8000.0, default=50.0, curve=Curve.EXP, formatter="ms"),
        P("play.envSlant", "Env Slant", rmin=0.0, rmax=1.0, default=0.5),
        P("play.xfade", "Loop Xfade", unit="ms", rmin=0.0, rmax=2000.0, default=20.0, curve=Curve.EXP, formatter="ms"),
        P("play.jitterTime", "Jitter Time", unit="ms", rmin=0.0, rmax=500.0, default=0.0, formatter="ms"),
        P("play.jitterRate", "Jitter Rate", unit="Hz", rmin=0.0, rmax=50.0, default=0.0, formatter="Hz"),
        P("play.startScatter", "Start Scatter", unit="%", formatter="percent1", default=0.0),
        P("play.grainWindow", "Grain Window", unit="ms", rmin=1.0, rmax=2000.0, default=120.0, curve=Curve.EXP, formatter="ms"),
        P("play.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.8, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.45, 0.9)),
        P("play.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("play.captureSource", "Capture Source", curve=Curve.ENUM, enum=["input", "master", "lane", "bus"], default=0, randomize=RandomizePolicy.OFF, modulatable=False),
        P("play.captureMode", "Capture Mode", curve=Curve.ENUM, enum=["replace", "append", "overdub", "ring"], default=0, randomize=RandomizePolicy.OFF, modulatable=False),
        P("play.preRoll", "Pre-roll", unit="ms", rmin=0.0, rmax=2000.0, default=0.0, formatter="ms", modulatable=False),
        P("play.normalizeCapture", "Normalize", curve=Curve.ENUM, enum=["off", "on"], default=0, modulatable=False),
        P("play.syncToTempo", "Sync To Tempo", curve=Curve.ENUM, enum=["off", "on"], default=0, modulatable=False),
        P("play.quantizeRegion", "Quantize Region", curve=Curve.ENUM, enum=["off", "1/16", "1/8", "1/4", "bar"], default=0),
        P("play.maxVoices", "Max Voices", curve=Curve.ENUM, enum=[str(n) for n in range(1, 17)], default=7, randomize=RandomizePolicy.OFF, danger=DangerClass.CPU, modulatable=False),
        P("play.interpolationMode", "Interpolation", curve=Curve.ENUM, enum=["none", "linear", "cubic"], default=2, modulatable=False),
        P("play.antiClick", "Anti-Click", unit="ms", rmin=0.0, rmax=50.0, default=3.0, formatter="ms", modulatable=False),
    ],
)

# --------------------------------------------------------------------------- #
# 5.2 GEN — continuously sounding generator bank
# --------------------------------------------------------------------------- #
GEN = ModuleSpec(
    type="GEN",
    role="Continuous generator bank: drones, impulses, noise, excitation.",
    node_meaning="Oscillator / noise / exciter voice.",
    synthdef="gen",
    insert_capable=False,
    generative_capable=True,
    max_nodes=32,
    cpu_per_node=0.8,
    gestures=["stack", "swarm", "excite-combs", "tune-cloud", "mutate-spectrum"],
    node_params=[
        P("gen.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("gen.waveform", "Waveform", curve=Curve.ENUM, enum=["sine", "tri", "saw", "pulse", "fold", "noise", "impulse", "fm"], default=0),
        P("gen.freq", "Freq", unit="Hz", rmin=20.0, rmax=12000.0, default=110.0, curve=Curve.EXP, musical=(40.0, 880.0), formatter="Hz"),
        P("gen.ratio", "Ratio", rmin=0.25, rmax=16.0, default=1.0, curve=Curve.EXP, musical=(0.5, 8.0)),
        P("gen.detune", "Detune", unit="semitone", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR, formatter="semitone"),
        P("gen.phase", "Phase", unit="%", formatter="percent1", default=0.0),
        P("gen.pulseWidth", "Pulse Width", default=0.5, musical=(0.1, 0.9)),
        P("gen.fold", "Fold", rmin=1.0, rmax=8.0, default=1.0, curve=Curve.EXP, musical=(1.0, 4.0)),
        P("gen.noiseColor", "Noise Color", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
        # DX7-style 6-operator FM voice (FM7.ar, the heart of the DX7-Supercollider
        # project). Active when waveform == "fm".
        P("gen.fmRatioA", "FM Ratio A", rmin=0.25, rmax=16.0, default=1.0, curve=Curve.EXP, musical=(1.0, 7.0)),
        P("gen.fmRatioB", "FM Ratio B", rmin=0.25, rmax=16.0, default=2.0, curve=Curve.EXP, musical=(1.0, 7.0)),
        P("gen.fmRatioC", "FM Ratio C", rmin=0.25, rmax=16.0, default=3.0, curve=Curve.EXP, musical=(1.0, 7.0)),
        P("gen.fmIndex", "FM Index", rmin=0.0, rmax=12.0, default=2.0, curve=Curve.EXP, musical=(0.8, 5.0)),
        P("gen.fmFeedback", "FM Feedback", rmin=0.0, rmax=2.0, default=0.0, musical=(0.0, 0.6)),
        P("gen.chaosAmount", "Chaos", default=0.0, musical=(0.0, 0.4)),
        P("gen.driftDepth", "Drift Depth", unit="semitone", rmin=0.0, rmax=2.0, default=0.0, formatter="semitone", musical=(0.0, 0.5)),
        P("gen.driftRate", "Drift Rate", unit="Hz", rmin=0.01, rmax=20.0, default=0.3, curve=Curve.EXP, formatter="Hz"),
        P("gen.gate", "Gate", curve=Curve.ENUM, enum=["closed", "open"], default=1, rate=Rate.TRIGGER, randomize=RandomizePolicy.OFF),
        P("gen.attack", "Attack", unit="ms", rmin=0.0, rmax=8000.0, default=200.0, curve=Curve.EXP, formatter="ms", musical=(5.0, 2000.0)),
        P("gen.release", "Release", unit="ms", rmin=0.0, rmax=16000.0, default=800.0, curve=Curve.EXP, formatter="ms", musical=(50.0, 4000.0)),
        P("gen.duckAmount", "Duck", default=0.0),
        P("gen.amp", "Amp", unit="dB", rmin=0.0, rmax=2.0, default=0.5, curve=Curve.DB, formatter="dB1", danger=DangerClass.LOUDNESS, musical=(0.35, 0.85)),
        P("gen.pan", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("gen.tuningRoot", "Tuning Root", unit="Hz", rmin=20.0, rmax=2000.0, default=110.0, curve=Curve.EXP, formatter="Hz", modulatable=False),
        P("gen.scaleMode", "Scale", curve=Curve.ENUM, enum=["free", "chromatic", "major", "minor", "wholetone", "harmonic"], default=0),
        P("gen.stackMode", "Stack", curve=Curve.ENUM, enum=["unison", "octaves", "harmonics", "spread"], default=0),
        P("gen.voiceDistribution", "Voice Dist", curve=Curve.ENUM, enum=["unison", "spread", "random"], default=1),
        P("gen.continuousGate", "Continuous Gate", curve=Curve.ENUM, enum=["off", "on"], default=1, modulatable=False),
        P("gen.duckResponse", "Duck Response", unit="ms", rmin=1.0, rmax=1000.0, default=80.0, curve=Curve.EXP, formatter="ms"),
        P("gen.randomPitchPolicy", "Random Pitch", curve=Curve.ENUM, enum=["off", "inScale", "free"], default=1),
    ],
)

# --------------------------------------------------------------------------- #
# 5.3 BAND — spectral / band sculpting, isolator, notch field, spatialized bands
# --------------------------------------------------------------------------- #
BAND = ModuleSpec(
    type="BAND",
    role="Spectral/band sculpting, isolator, notch field, spatialized bands.",
    node_meaning="Band or spectral window.",
    synthdef="band",
    insert_capable=True,
    generative_capable=False,
    max_nodes=24,
    cpu_per_node=1.5,
    gestures=["draw-spectral-mask", "isolate", "invert", "band-randomize"],
    node_params=[
        P("band.nodeEnable", "Enable", curve=Curve.ENUM, enum=["off", "on"], default=1, randomize=RandomizePolicy.OFF, modulatable=False),
        P("band.centerHz", "Center", unit="Hz", rmin=20.0, rmax=18000.0, default=800.0, curve=Curve.EXP, musical=(60.0, 8000.0), formatter="Hz"),
        P("band.bandwidth", "Bandwidth", unit="Hz", rmin=10.0, rmax=8000.0, default=400.0, curve=Curve.EXP, formatter="Hz", musical=(300.0, 4000.0)),
        P("band.gain", "Gain", unit="dB", rmin=-60.0, rmax=18.0, default=0.0, curve=Curve.LINEAR, formatter="dBValue", danger=DangerClass.LOUDNESS, musical=(-9.0, 6.0)),
        P("band.slope", "Slope", rmin=0.0, rmax=1.0, default=0.5),
        P("band.bump", "Bump", rmin=0.0, rmax=1.0, default=0.0),
        P("band.mode", "Mode", curve=Curve.ENUM, enum=["pass", "notch", "shelf", "isolate"], default=0),
        P("band.freeze", "Freeze", curve=Curve.ENUM, enum=["off", "on"], default=0, rate=Rate.TRIGGER),
        P("band.smear", "Smear", default=0.0),
        P("band.spatialPos", "Spatial Pos", rmin=-1.0, rmax=1.0, default=0.0, curve=Curve.BIPOLAR),
    ],
    global_params=[
        P("band.engineMode", "Engine", curve=Curve.ENUM, enum=["IIR", "FIR", "FFT"], default=0, randomize=RandomizePolicy.OFF, danger=DangerClass.CPU, modulatable=False),
        P("band.fftSize", "FFT Size", curve=Curve.ENUM, enum=["512", "1024", "2048", "4096"], default=1, danger=DangerClass.CPU, modulatable=False),
        P("band.overlap", "Overlap", curve=Curve.ENUM, enum=["2", "4", "8"], default=1, danger=DangerClass.CPU, modulatable=False),
        P("band.window", "Window", curve=Curve.ENUM, enum=["hann", "hamming", "blackman"], default=0, modulatable=False),
        P("band.maskInvert", "Mask Invert", curve=Curve.ENUM, enum=["off", "on"], default=0),
        P("band.preserveLoudness", "Preserve Loudness", curve=Curve.ENUM, enum=["off", "on"], default=1),
        P("band.latencyMode", "Latency", curve=Curve.ENUM, enum=["live", "balanced", "highQuality"], default=1, modulatable=False),
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
    generative_capable=True,   # self-oscillate / become struck voice when excited
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
# 5.8 VIZ — diagnostic / performative display layer (never affects audio)
# --------------------------------------------------------------------------- #
VIZ = ModuleSpec(
    type="VIZ",
    role="Metering, scopes, spectrogram, modulation map; no audio change.",
    node_meaning="View layer, not DSP voice.",
    synthdef="viz",
    insert_capable=False,
    generative_capable=False,
    max_nodes=8,
    is_audio=False,
    cpu_per_node=0.3,
    gestures=["inspect", "diagnose", "perform-visually"],
    node_params=[
        P("viz.source", "Source", curve=Curve.ENUM, enum=["master", "lane", "module", "bus", "modRoute"], default=0, modulatable=False),
        P("viz.scale", "Scale", curve=Curve.ENUM, enum=["lin", "log"], default=1, modulatable=False),
        P("viz.decay", "Decay", default=0.5, modulatable=False),
        P("viz.hold", "Hold", default=0.0, modulatable=False),
        P("viz.range", "Range", unit="dB", rmin=-120.0, rmax=0.0, default=-90.0, formatter="dBValue", modulatable=False),
    ],
    global_params=[
        P("viz.viewMode", "View Mode", curve=Curve.ENUM,
          enum=["meter", "scope", "spectrum", "spectrogram", "correlation", "modMap", "nodeMap"],
          default=0, modulatable=False),
        P("viz.updateRate", "Update Rate", unit="Hz", rmin=1.0, rmax=60.0, default=20.0, formatter="Hz", modulatable=False),
        P("viz.colorPolicy", "Color Policy", curve=Curve.ENUM, enum=["standard", "highContrast", "mono"], default=0, modulatable=False),
    ],
)


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
    ],
    global_params=[
        P("clouds.mode", "Mode", curve=Curve.ENUM, enum=["granular", "stretch", "looping", "spectral"], default=0, modulatable=False),
        P("clouds.lofi", "Lo-Fi", curve=Curve.ENUM, enum=["off", "on"], default=0, modulatable=False),
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


CATALOG: dict[str, ModuleSpec] = {
    m.type: m for m in (PLAY, GEN, BAND, PITCH, TIME, COMB, GAIN, SDLY, VERB,
                        CLOUDS, RINGS, BEN, BUCHLOID, ENV, GATE, PLAITS, VIZ)
}

# Ordered lanes as drawn in the page-2 schematic (Source Rack -> ... -> Gain),
# extended with the stereo delay + reverb at the tail.
DEFAULT_LANE_ORDER = ["PLAY", "GEN", "BAND", "PITCH", "TIME", "COMB", "GAIN",
                      "SDLY", "VERB", "CLOUDS", "RINGS", "BEN", "BUCHLOID", "ENV", "GATE", "PLAITS", "VIZ"]


def spec(module_type: str) -> ModuleSpec:
    return CATALOG[module_type]
