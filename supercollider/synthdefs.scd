// SynthDef families for the eight modules (blueprint section 4, 5.1-5.8, 10).
//
// Channel count is baked into a SynthDef at build time, so we (re)build the
// whole family for the configured output channel count (mono/stereo/quad/N).
// Each node is one Synth of the family living in its module's Group. Node-scoped
// params are Synth args set per node; global params are broadcast to all nodes.
//
// Routing is intentionally simple in this first realization: inserts read their
// module input bus and write their module output bus; sources generate into the
// output bus; a per-module mix applies bypass + wet/dry. Full serial/parallel
// lane busing is layered on top by the engine (engine.scd). Feedback paths use
// LocalIn/LocalOut with a hard LeakDC + Limiter as the blueprint requires.

// Kill NaN/Inf: per-sample comparison UGens. NaN/Inf fail sig.abs < inf, so
// Select routes bad samples to 0 and finite samples through. NOTE: a plain
// multiplication (sig * mask) does NOT work because 0 * NaN == NaN under
// IEEE-754; Select must be used instead.
~killbad = { |sig|
	var wasArray = sig.isArray;
	var arr = sig.asArray;
	arr = arr.collect { |ch| Select.ar(ch.abs < inf, [DC.ar(0), ch]) };
	wasArray.if({ arr }, { arr[0] });
};

~spread = { |sig, nchan, pos = 0|
	// Spread a mono (or already-multichannel) signal across nchan outputs.
	// pos is -1..1; PanAz spreads around a ring for N>2.
	var s = sig.isArray.if({ Mix(sig) }, { sig });
	nchan.switch(
		1, { s },
		2, { Pan2.ar(s, pos) },
		{ PanAz.ar(nchan, s, pos * 0.5 + 0.5, 1.0) }
	);
};

~buildDefs = { |nchan = 2|
	var suf = "_" ++ nchan;

	// ---- PLAY: playhead / region reader / looper / resampler -------------
	SynthDef(("play" ++ suf).asSymbol, { |out = 0, in = 0, buf = 0, gate = 1,
		regionStart = 0, regionLength = 1, rate = 1, direction = 0, loopMode = 1,
		envAttack = 0.005, envRelease = 0.05, xfade = 0.02, jitterTime = 0, jitterRate = 0,
		startScatter = 0, amp = 0.8, pan = 0, bypass = 0, wet = 1, enable = 1|
		var frames = BufFrames.kr(buf).max(1);
		var dir = Select.kr(direction, [1, -1, LFPulse.kr(0.5).linlin(0,1,-1,1)]);
		var jit = LFNoise1.kr(jitterRate.max(0.001)) * jitterTime * 0.001;
		var phStart = (regionStart + (TRand.kr(0, startScatter, Impulse.kr(0)) )) * frames;
		var phEnd = ((regionStart + regionLength).clip(0,1)) * frames;
		var phasor = Phasor.ar(0, BufRateScale.kr(buf) * rate * dir,
			phStart, phEnd) + (jit * BufSampleRate.kr(buf));
		var sig = BufRd.ar(1, buf, phasor.clip(0, frames - 1), 1, 4);
		var env = EnvGen.kr(Env.asr(envAttack * 0.001, 1, envRelease * 0.001), gate);
		sig = (sig * env * amp * enable).tanh;
		sig = ~spread.(sig, nchan, pan);
		Out.ar(out, ~killbad.(sig * (1 - bypass) * wet));
	}).add;

	// ---- GEN: oscillator / noise / exciter voice -------------------------
	SynthDef(("gen" ++ suf).asSymbol, { |out = 0, waveform = 0, freq = 110, ratio = 1,
		detune = 0, pulseWidth = 0.5, fold = 1, noiseColor = 0, chaosAmount = 0,
		driftDepth = 0, driftRate = 0.3, gate = 1, attack = 200, release = 800,
		amp = 0.5, pan = 0, bypass = 0, enable = 1,
		fmRatioA = 1, fmRatioB = 2, fmRatioC = 3, fmIndex = 2, fmFeedback = 0|
		var f = freq * ratio * (detune * (1/12)).midiratio
			* (LFNoise1.kr(driftRate) * driftDepth * (1/12)).midiratio;
		var saw = Saw.ar(f), pul = Pulse.ar(f, pulseWidth);
		var fld = (SinOsc.ar(f) * fold).fold(-1, 1);
		var nz = LPF.ar(WhiteNoise.ar, noiseColor.linexp(-1, 1, 200, 12000));
		var imp = Impulse.ar(f.clip(0.1, 80));
		var chaos = LinCongC.ar(SampleRate.ir/4 * (chaosAmount + 0.001), 1.1, 0.13, 1.0, 0) * chaosAmount;
		// DX7-style 6-operator FM (FM7.ar from sc3-plugins, the heart of the
		// DX7-Supercollider project). Three carrier/modulator pairs feeding
		// op0<-op1, op2<-op3, op4<-op5, with op5 self-feedback; carriers summed.
		var ctls = [[f, 0, 1], [f * fmRatioA, 0, 1], [f, 0, 1],
		            [f * fmRatioB, 0, 1], [f, 0, 1], [f * fmRatioC, 0, 1]];
		var mods = [[0, fmIndex, 0, 0, 0, 0],
		            [0, 0, 0, 0, 0, 0],
		            [0, 0, 0, fmIndex, 0, 0],
		            [0, 0, 0, 0, 0, 0],
		            [0, 0, 0, 0, 0, fmIndex],
		            [0, 0, 0, 0, 0, fmFeedback]];
		var fm = FM7.ar(ctls, mods);
		var fmVoice = (fm[0] + fm[2] + fm[4]) * 0.4;
		var sig = Select.ar(waveform.clip(0, 7),
			[SinOsc.ar(f), VarSaw.ar(f, 0, 0.5), saw, pul, fld, nz, imp, fmVoice]);
		var env = EnvGen.kr(Env.asr(attack * 0.001, 1, release * 0.001), gate);
		sig = (sig + chaos) * env * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass)));
	}).add;

	// ---- BEN: Benjolis-inspired chaotic twin-oscillator / rungler voice ----
	// Port of scazan/benjolis' Engine_Benjolis.sc. Requires sc3-plugins
	// (PulseDPW, SVF, DFM1). The rungler is an 8-bit shift register fed by
	// osc1 and clocked by osc2; its output modulates both oscillators and the
	// filter cutoff, producing chaotic, self-modulating timbres.
	SynthDef(("ben" ++ suf).asSymbol, { |out = 0, in = 0,
		freq1 = 40, freq2 = 4, scale = 1, rungler1 = 0.16, rungler2 = 0.0,
		runglerFilt = 9, loop = 0, filtFreq = 40, q = 0.82, gain = 1,
		filterType = 0, outSignal = 6, amp = 0.5, pan = 0,
		bypass = 0, wet = 1, enable = 1|
		var bufR = LocalIn.ar(2, 0);
		var rungler = bufR[0], buf = bufR[1];
		var sr = SampleDur.ir;
		var tri1 = LFTri.ar((rungler * rungler1) + freq1);
		var tri2 = LFTri.ar((rungler * rungler2) + freq2);
		var osc1 = PulseDPW.ar((rungler * rungler1) + freq1);
		var osc2 = PulseDPW.ar((rungler * rungler2) + freq2);
		var pwm = BinaryOpUGen('>', (tri1 + tri2), 0);
		var sh0, sh1, sh2, sh3, sh4, sh5, sh6, sh7, sh8 = 1;
		var filt, output, sig;
		osc1 = ((buf * loop) + (osc1 * (loop * -1 + 1)));
		sh0 = BinaryOpUGen('>', osc1, 0.5);
		sh0 = BinaryOpUGen('==', (sh8 > sh0), (sh8 < sh0));
		sh0 = (sh0 * -1) + 1;
		sh1 = DelayN.ar(Latch.ar(sh0, osc2), 0.01, sr);
		sh2 = DelayN.ar(Latch.ar(sh1, osc2), 0.01, sr * 2);
		sh3 = DelayN.ar(Latch.ar(sh2, osc2), 0.01, sr * 3);
		sh4 = DelayN.ar(Latch.ar(sh3, osc2), 0.01, sr * 4);
		sh5 = DelayN.ar(Latch.ar(sh4, osc2), 0.01, sr * 5);
		sh6 = DelayN.ar(Latch.ar(sh5, osc2), 0.01, sr * 6);
		sh7 = DelayN.ar(Latch.ar(sh6, osc2), 0.01, sr * 7);
		sh8 = DelayN.ar(Latch.ar(sh7, osc2), 0.01, sr * 8);
		rungler = ((sh1 / 2.pow(8)) + (sh2 / 2.pow(7)) + (sh3 / 2.pow(6)) +
			(sh4 / 2.pow(5)) + (sh5 / 2.pow(4)) + (sh6 / 2.pow(3)) +
			(sh7 / 2.pow(2)) + (sh8 / 2.pow(1)));
		buf = rungler;
		rungler = (rungler * scale.linlin(0, 1, 0, 127)).midicps;
		LocalOut.ar([rungler, buf]);
		filt = Select.ar(filterType.clip(0, 3), [
			RLPF.ar(pwm, (rungler * runglerFilt) + filtFreq, q * -1 + 1, gain),
			RHPF.ar(pwm, (rungler * runglerFilt) + filtFreq, q * -1 + 1, gain),
			SVF.ar(pwm, (rungler * runglerFilt) + filtFreq, q, 1, 0, 0, 0, 0, gain),
			DFM1.ar(pwm, (rungler * runglerFilt) + filtFreq, q, gain, 1)
		]);
		output = Select.ar(outSignal.clip(0, 6), [tri1, osc1, tri2, osc2, pwm, sh0, filt]);
		sig = ~spread.(LeakDC.ar((output * amp * enable).tanh), nchan, pan);
		Out.ar(out, ~killbad.(sig * (1 - bypass) * wet));
	}).add;


	// ---- BUCHLOID: west-coast complex oscillator / FM / wave-fold voice ----
	// Port of markwheeler/passersby's Engine_Passersby.sc synth voice.
	// Simplified for Wildrider: no internal LFOs/reverb; ASR envelope gated by
	// enable so each node is a sustained drone voice.
	SynthDef(("buchloid" ++ suf).asSymbol, { |out = 0, in = 0,
		freq = 220, glide = 0, fm1Ratio = 0.66, fm2Ratio = 3.3,
		fm1Amount = 0, fm2Amount = 0, waveShape = 0, waveFolds = 0,
		timbre = 0, attack = 0.04, peak = 10000, decay = 1,
		pressure = 0, velocity = 0.7, amp = 0.5, pan = 0,
		bypass = 0, wet = 1, enable = 1|
		var i_nyquist = SampleRate.ir * 0.5, signal;
		var controlLag = 0.005, i_numHarmonics = 44;
		var modFreq, mod1, mod2, mod1Index, mod2Index, mod1Freq, mod2Freq;
		var sinOsc, triOsc, additiveOsc, additivePhase;
		var filterEnvVel, filterEnvLow, asrEnvelope, asrFilterFreq, asrSignal;
		var gate = enable;
		var atk = attack * 0.001;
		var dec = decay * 0.001;
		freq = Lag.kr(freq, 0.007 + glide).clip(20, i_nyquist);
		fm1Ratio = Lag.kr(fm1Ratio, controlLag);
		fm2Ratio = Lag.kr(fm2Ratio, controlLag);
		fm1Amount = Lag.kr(fm1Amount.squared, controlLag);
		fm2Amount = Lag.kr(fm2Amount.squared, controlLag);
		waveShape = Lag.kr(waveShape, controlLag);
		waveFolds = Lag.kr(waveFolds, controlLag);
		atk = Lag.kr(atk, controlLag);
		peak = Lag.kr(peak, controlLag);
		dec = Lag.kr(dec, controlLag);
		mod1Index = fm1Amount * 22;
		mod1Freq = freq * fm1Ratio * LFNoise2.kr(freq: 0.1, mul: 0.001, add: 1);
		mod1 = SinOsc.ar(freq: mod1Freq, phase: 0, mul: mod1Index * mod1Freq, add: 0);
		mod2Index = fm2Amount * 12;
		mod2Freq = freq * fm2Ratio * LFNoise2.kr(freq: 0.1, mul: 0.005, add: 1);
		mod2 = SinOsc.ar(freq: mod2Freq, phase: 0, mul: mod2Index * mod2Freq, add: 0);
		modFreq = freq + mod1 + mod2;
		sinOsc = SinOsc.ar(freq: modFreq, phase: 0, mul: 0.5);
		triOsc = VarSaw.ar(freq: modFreq, iphase: 0, width: 0.5, mul: 0.5);
		additivePhase = LFSaw.ar(freq: modFreq, iphase: 1, mul: pi, add: pi);
		additiveOsc = Mix.fill(i_numHarmonics, { |index|
			var harmonic, harmonicFreq, harmonicCutoff, attenuation;
			harmonic = index + 1;
			harmonicFreq = freq * harmonic;
			harmonicCutoff = i_nyquist - harmonicFreq;
			attenuation = Select.kr(index, [1,
				(harmonicCutoff - (harmonicFreq * 0.25) - harmonicFreq).expexp(0.000001, harmonicFreq * 0.5, 0.000001, 1)]);
			(sin(additivePhase * harmonic % 2pi) / harmonic) * attenuation * (harmonic % 2 + waveShape.linlin(0.666666, 1, 0, 1)).min(1);
		});
		signal = LinSelectX.ar(waveShape * 3, [sinOsc, triOsc, additiveOsc]);
		signal = Fold.ar(in: signal * (1 + (timbre * 0.5) + (waveFolds * 2)), lo: -0.5, hi: 0.5);
		signal = LPF.ar(in: signal, freq: 12000);
		signal = signal + PinkNoise.ar(mul: 0.003);
		filterEnvVel = velocity.linlin(0, 1, 0.5, 1);
		filterEnvLow = (peak * filterEnvVel).min(300);
		asrEnvelope = EnvGen.ar(envelope: Env.new(levels: [0, 1, 0], times: [atk, dec], curve: -4, releaseNode: 1), gate: gate);
		asrFilterFreq = asrEnvelope.linlin(0, 1, filterEnvLow, peak * filterEnvVel);
		asrSignal = RLPF.ar(in: signal, freq: asrFilterFreq, rq: 0.95);
		asrSignal = RLPF.ar(in: asrSignal, freq: asrFilterFreq, rq: 0.95);
		asrSignal = asrSignal * EnvGen.ar(envelope: Env.asr(attackTime: atk, sustainLevel: 1, releaseTime: dec, curve: -4), gate: gate);
		signal = asrSignal * velocity.linlin(0, 1, 0.2, 1);
		signal = tanh(signal * pressure.linlin(0, 1, 1.5, 3) * amp).softclip;
		signal = ~spread.(signal * enable, nchan, pan);
		Out.ar(out, ~killbad.(signal * (1 - bypass) * wet));
	}).add;


	// ---- ENV: ADSR envelope VCA / audio gate ----
	// EnvGen-driven amplitude envelope applied to the incoming audio signal.
	// `enable` acts as the gate: 0 releases, 1 attacks/sustains; returning to 1
	// re-triggers the envelope.
	SynthDef(("env" ++ suf).asSymbol, { |out = 0, in = 0,
		attack = 20, decay = 200, sustain = 0.7, release = 500,
		curve = -4, amp = 1, pan = 0,
		bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan);
		var gate = enable;
		var env = EnvGen.kr(
			Env.adsr(
				attackTime: attack * 0.001,
				decayTime: decay * 0.001,
				sustainLevel: sustain,
				releaseTime: release * 0.001,
				curve: curve
			),
			gate
		);
		var sig = dry * env * amp;
		sig = ~spread.(sig * enable, nchan, pan);
		Out.ar(out, ~killbad.(XFade2.ar(dry, sig, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass)));
	}).add;
	// ---- BAND: band / spectral window (IIR realization first version) ----
	SynthDef(("band" ++ suf).asSymbol, { |out = 0, in = 0, centerHz = 800,
		bandwidth = 400, gain = 0, mode = 0, freeze = 0, smear = 0,
		spatialPos = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), sig;
		var rq = (bandwidth / centerHz).clip(0.001, 4);
		var band = Mix(dry) / nchan;
		var filt = Select.ar(mode.clip(0,3), [
			BPF.ar(band, centerHz, rq),
			band - BPF.ar(band, centerHz, rq),    // notch
			band + (BPeakEQ.ar(band, centerHz, rq, gain) - band),
			BPF.ar(band, centerHz, rq)             // isolate
		]) * gain.dbamp;
		filt = LPF.ar(filt, (1 - smear).linexp(0, 1, 18000, 600).clip(200, 18000));
		sig = ~spread.(filt * enable, nchan, spatialPos);
		Out.ar(out, ~killbad.(XFade2.ar(dry, sig, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass)));
	}).add;

	// ---- PITCH: pitch-shifting granular delay surface --------------------
	SynthDef(("pitch" ++ suf).asSymbol, { |out = 0, in = 0, semitones = 0, cents = 0,
		delayMs = 0, grainSize = 0.12, feedback = 0, density = 2, spread = 0.2,
		randomPitch = 0, formantBias = 0, amp = 0.8, spatialPos = 0,
		safetyFeedbackLimit = 0.85, bypass = 0, wet = 1, enable = 1|
		var dry = Mix(In.ar(in, nchan)) / nchan, sig;
		var fb = LocalIn.ar(1);
		var inSig = dry + (fb * feedback.min(safetyFeedbackLimit));
		var ratio = (semitones + (cents/100) + TRand.kr(randomPitch.neg, randomPitch, Impulse.kr(density))).midiratio;
		var shifted = PitchShift.ar(inSig, (grainSize * 0.001).clip(0.001, 1.0), ratio, 0.01, spread);
		shifted = DelayC.ar(shifted, 4, (delayMs * 0.001).clip(0, 4));
		LocalOut.ar(~killbad.(LeakDC.ar(shifted).clip2(0.95)));
		sig = ~spread.(shifted * amp * enable, nchan, spatialPos);
		Out.ar(out, ~killbad.(XFade2.ar(In.ar(in, nchan), sig, (wet*2-1)) * (1-bypass) + (In.ar(in, nchan)*bypass)));
	}).add;

	// ---- TIME: delay-time laboratory (tap / read head) -------------------
	SynthDef(("time" ++ suf).asSymbol, { |out = 0, in = 0, tapTime = 0.25, tapGain = 0.7,
		speed = 1, filterFreq = 8000, filterQ = 0.7, feedbackSend = 0.2, jitter = 0,
		smear = 0, mute = 0, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = Mix(In.ar(in, nchan)) / nchan, sig;
		var fb = LocalIn.ar(1);
		var t = (tapTime * 0.001) + (LFNoise1.kr(3) * jitter * 0.001);
		var d = DelayC.ar(dry + (fb * feedbackSend.clip(0, 0.95)), 8, t.clip(0, 8));
		d = RLPF.ar(d, filterFreq, (1/filterQ).clip(0.05, 5));
		LocalOut.ar(~killbad.(LeakDC.ar(d).clip2(0.98)));
		sig = ~spread.(d * tapGain * (1 - mute) * enable, nchan, pan);
		Out.ar(out, ~killbad.(XFade2.ar(In.ar(in, nchan), sig, (wet*2-1)) * (1-bypass) + (In.ar(in,nchan)*bypass)));
	}).add;

	// ---- COMB: resonator / waveguide field -------------------------------
	SynthDef(("comb" ++ suf).asSymbol, { |out = 0, in = 0, freqOrDelay = 220,
		feedback = 0.35, damping = 0.3, polarity = 0, dispersion = 0, drive = 1,
		exciteAmount = 0.5, decay = 1.5, brightness = 0.5, excitationSource = 0,
		amp = 0.6, spatialPos = 0, selfOscLimit = 0.95, bypass = 0, wet = 1, enable = 1|
		var dt = (1 / freqOrDelay).clip(0.0002, 0.5);
		var dry = Mix(In.ar(in, nchan)) / nchan, sig;
		var exc = Select.ar(excitationSource.clip(0,3), [
			dry, PinkNoise.ar(exciteAmount), Dust.ar(8) * exciteAmount, dry]) * exciteAmount;
		var rt = (decay * 0.001).clip(0.01, 20);   // ms -> s
		var line = Select.ar(polarity.clip(0,1), [
			CombC.ar(exc * drive, 0.5, dt, rt),
			CombC.ar(exc * drive, 0.5, dt, rt)
		]);
		line = (LPF.ar(line, brightness.linexp(0, 1, 400, 16000)) * (1 - damping))
			+ (line * damping);
		line = (line * (feedback.clip(0, selfOscLimit) + 1)).tanh * amp * enable;
		sig = ~spread.(line, nchan, spatialPos);
		Out.ar(out, ~killbad.(XFade2.ar(In.ar(in, nchan), sig, (wet*2-1)) * (1-bypass) + (In.ar(in,nchan)*bypass)));
	}).add;

	// ---- GAIN: movement & presence ---------------------------------------
	SynthDef(("gain" ++ suf).asSymbol, { |out = 0, in = 0, gain = 1, mute = 0,
		distance = 1, dopplerDepth = 0, airAbsorb = 0, presence = 0,
		tremoloRate = 4, tremoloDepth = 0, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan);
		var atten = (1 / distance.max(0.1));
		var sig = dry * atten;
		sig = LPF.ar(sig, airAbsorb.linexp(0, 1, 18000, 3000).clip(2000, 18000));
		sig = sig * (1 - (tremoloDepth * SinOsc.kr(tremoloRate).range(0, 1)));
		sig = BPeakEQ.ar(sig, 4000, 1, presence * 6);
		sig = sig * gain * (1 - mute) * enable;
		Out.ar(out, ~killbad.((XFade2.ar(dry, sig, (wet*2-1)) * (1-bypass) + (dry*bypass))));
	}).add;

	// ---- SDLY: stereo / ping-pong delay -----------------------------------
	SynthDef(("sdly" ++ suf).asSymbol, { |out = 0, in = 0, timeL = 280, timeR = 420,
		feedback = 0.35, crossFeed = 0.4, damp = 0.4, width = 1.1, modRate = 0.3,
		modDepth = 0.12, amp = 1, bypass = 0, wet = 0.35, enable = 1|
		var dry = In.ar(in, nchan);
		var inL = (nchan == 1).if({ dry }, { dry[0] });
		var inR = (nchan == 1).if({ dry }, { dry[1] });
		var fb = LocalIn.ar(2);
		var modL = SinOsc.kr(modRate, 0) * modDepth * 0.004;
		var modR = SinOsc.kr(modRate, 0.5pi) * modDepth * 0.004;
		var dL = DelayC.ar(inL + (fb[0] * feedback) + (fb[1] * crossFeed), 4,
			((timeL * 0.001) + modL).clip(0.001, 4));
		var dR = DelayC.ar(inR + (fb[1] * feedback) + (fb[0] * crossFeed), 4,
			((timeR * 0.001) + modR).clip(0.001, 4));
		var mid, side, wetL, wetR, outSig;
		dL = LPF.ar(dL, damp.linexp(0, 1, 16000, 1200));
		dR = LPF.ar(dR, damp.linexp(0, 1, 16000, 1200));
		LocalOut.ar(~killbad.([LeakDC.ar(dL), LeakDC.ar(dR)].clip2(0.98) * enable));
		mid = (dL + dR) * 0.5; side = (dL - dR) * 0.5 * width;
		wetL = (mid + side) * amp; wetR = (mid - side) * amp;
		outSig = (nchan == 1).if({ XFade2.ar(dry, (wetL + wetR) * 0.5, wet * 2 - 1) }, {
			var oL = XFade2.ar(inL, wetL, wet * 2 - 1);
			var oR = XFade2.ar(inR, wetR, wet * 2 - 1);
			(nchan == 2).if({ [oL, oR] }, { [oL, oR] ++ dry.drop(2) });
		});
		Out.ar(out, ~killbad.((outSig * (1 - bypass)) + (dry * bypass)));
	}).add;

	// ---- VERB: spacious deep reverb (JPverb) ------------------------------
	SynthDef(("verb" ++ suf).asSymbol, { |out = 0, in = 0, size = 2.2, decay = 5,
		damp = 0.3, modDepth = 0.2, modFreq = 2, earlyDiff = 0.7, lowCut = 180,
		highCut = 9000, width = 1, amp = 1, bypass = 0, wet = 0.35, enable = 1, freeze = 0|
		var dry = In.ar(in, nchan);
		var inL = (nchan == 1).if({ dry }, { dry[0] });
		var inR = (nchan == 1).if({ dry }, { dry[1] });
		var t60 = Select.kr(freeze, [decay, 240]);   // freeze -> very long tail
		var rev = JPverb.ar([inL, inR], t60, damp, size, earlyDiff, modDepth, modFreq,
			1.0, 1.0, 1.0, lowCut, highCut) * enable;
		var wL = rev[0], wR = rev[1];
		var mid = (wL + wR) * 0.5, side = (wL - wR) * 0.5 * width;
		var wetL = (mid + side) * amp, wetR = (mid - side) * amp, outSig;
		outSig = (nchan == 1).if({ XFade2.ar(dry, (wetL + wetR) * 0.5, wet * 2 - 1) }, {
			var oL = XFade2.ar(inL, wetL, wet * 2 - 1);
			var oR = XFade2.ar(inR, wetR, wet * 2 - 1);
			(nchan == 2).if({ [oL, oR] }, { [oL, oR] ++ dry.drop(2) });
		});
		Out.ar(out, ~killbad.((outSig * (1 - bypass)) + (dry * bypass)));
	}).add;

	// ---- CLOUDS: Mutable Instruments granular processor (MiClouds) --------
	// Guarded: define only if the mi-UGens plugin is present, so a missing
	// plugin can never break the rest of the engine.
	(\MiClouds.asClass.notNil).if {
	SynthDef(("clouds" ++ suf).asSymbol, { |out = 0, in = 0, pit = 0, pos = 0.5,
		size = 0.4, dens = 0.4, tex = 0.5, inGain = 1, spread = 0.5, rvb = 0.2,
		fb = 0, freeze = 0, mode = 0, lofi = 0, amp = 0.9, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan);
		var inL = (nchan == 1).if({ dry }, { dry[0] });
		var inR = (nchan == 1).if({ dry }, { dry[1] });
		var mono = (inL + inR) * 0.5;
		// MiClouds: in, pit, pos, size, dens, tex, drywet, in_gain, spread, rvb, fb, freeze, mode, lofi, trig
		var sig = MiClouds.ar(mono, pit, pos, size, dens, tex, 1.0, inGain, spread, rvb, fb, freeze, mode, lofi, 0);
		var wetL = sig[0] * amp * enable, wetR = sig[1] * amp * enable, outSig;
		outSig = (nchan == 1).if({ XFade2.ar(dry, (wetL + wetR) * 0.5, wet * 2 - 1) }, {
			var oL = XFade2.ar(inL, wetL, wet * 2 - 1);
			var oR = XFade2.ar(inR, wetR, wet * 2 - 1);
			(nchan == 2).if({ [oL, oR] }, { [oL, oR] ++ dry.drop(2) });
		});
		Out.ar(out, ~killbad.((outSig * (1 - bypass)) + (dry * bypass)));
	}).add;
	};

	// ---- RINGS: Mutable Instruments modal/string resonator (MiRings) ------
	(\MiRings.asClass.notNil).if {
	SynthDef(("rings" ++ suf).asSymbol, { |out = 0, in = 0, pit = 48, struct = 0.25,
		bright = 0.5, damp = 0.7, pos = 0.25, model = 0, poly = 0, internalExc = 1,
		easteregg = 0, trigRate = 2, trigMode = 0, amp = 0.9, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan);
		var inL = (nchan == 1).if({ dry }, { dry[0] });
		var inR = (nchan == 1).if({ dry }, { dry[1] });
		var mono = (inL + inR) * 0.5;
		var trig = Select.kr(trigMode.clip(0, 1), [Impulse.kr(trigRate), Dust.kr(trigRate)]);
		var polyN = Select.kr(poly.clip(0, 2), [1, 2, 4]);
		// MiRings: in, trig, pit, struct, bright, damp, pos, model, poly, intern_exciter, easteregg, bypass
		var sig = MiRings.ar(mono, trig, pit, struct, bright, damp, pos, model, polyN, internalExc, easteregg, 0);
		var wetL = sig[0] * amp * enable, wetR = sig[1] * amp * enable, outSig;
		outSig = (nchan == 1).if({ XFade2.ar(dry, (wetL + wetR) * 0.5, wet * 2 - 1) }, {
			var oL = XFade2.ar(inL, wetL, wet * 2 - 1);
			var oR = XFade2.ar(inR, wetR, wet * 2 - 1);
			(nchan == 2).if({ [oL, oR] }, { [oL, oR] ++ dry.drop(2) });
		});
		Out.ar(out, ~killbad.((outSig * (1 - bypass)) + (dry * bypass)));
	}).add;
	};

	// ---- master analysis + meters (drives VIZ + analysis-follower mod) ----
	// Proper spectral analysis via sc3-plugins (SpecCentroid / SpecFlatness).
	// The engine (engine.scd) attaches the source/feature labels on forward,
	// since SendReply carries only numeric values.
	SynthDef(("analysis" ++ suf).asSymbol, { |in = 0|
		var sig = In.ar(in, nchan);
		var mono = Mix(sig) / nchan;
		var chain = FFT(LocalBuf(2048), mono);
		var rms = Amplitude.kr(mono);
		var centroid = SpecCentroid.kr(chain).explin(20, 20000, 0, 1);
		var flatness = SpecFlatness.kr(chain);
		SendReply.kr(Impulse.kr(20), '/atelier/meters', Amplitude.kr(sig));
		SendReply.kr(Impulse.kr(15), '/atelier/analysisRaw', [rms, centroid, flatness]);
	}).add;
};
