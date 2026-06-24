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

// Kill NaN/Inf AND bound finite blow-ups. NaN/Inf fail sig.abs < inf, so Select
// routes bad samples to 0 and finite samples through (a plain sig*mask does NOT
// work: 0 * NaN == NaN under IEEE-754). Then clip2(16) bounds huge FINITE values
// — a feedback loop can run away to ~1e27 (finite, so it passes the inf test) and
// leak a transient past the master limiter as a digital "noise burst". Because
// ~killbad wraps every module output AND the feedback LocalOuts, clipping here
// caps the loops at the source so runaway can't explode.
// + 1e-15 = an ANTI-DENORMAL floor. ~killbad wraps every module output AND every
// feedback LocalOut; on aarch64 (no flush-to-zero) a feedback loop decaying toward
// silence drifts into denormal floats, whose arithmetic is ~100x slower -> a
// one-block CPU spike -> a JACK XRun (the "occasional click"). A tiny DC keeps the
// loop floor normal; 1e-15 is ~10 orders below the 16-bit LSB, i.e. inaudible.
~killbad = { |sig|
	var wasArray = sig.isArray;
	var arr = sig.asArray;
	arr = arr.collect { |ch| Select.ar(ch.abs < inf, [DC.ar(0), ch]).clip2(16) + 1e-15 };
	wasArray.if({ arr }, { arr[0] });
};

// Stereo balance for a per-module pan. Applied at a module's output so every
// module is pannable in the field. Mono passes through; >2ch is left as-is.
~panOut = { |sig, pos = 0, nchan = 2|
	(nchan == 2 and: { sig.isArray }).if({ Balance2.ar(sig[0], sig[1], pos.clip(-1, 1)) }, { sig });
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

	// ---- DX7: exact 6-operator FM voice (port of DX7-Supercollider) -------
	// Per-module pitch/amp modulation LFO. Writes a frequency *multiplier*
	// (~1.0 at rest) to pmodbus and an amp-mod offset to amodbus, both read by
	// the voices. ReplaceOut (not Out) so the private bus holds exactly the
	// current value — only this synth writes it. Persistent (no FreeSelf).
	SynthDef(\InfEfx, { |pmodbus = 0, amodbus = 0,
		lfoGet1 = 0, lfoGet2 = 1, lfo_speed = 1, lfo_wave = 0, lfo_phase = 0,
		lfo_delay = 0.01, lfo_amd = 0, pitchCons = 0, envTrig = 1, tameC = 0, tameM = 1|
		var lfo, multiPitch, lfoAmp;
		lfo = Osc.ar(lfo_wave, lfo_speed, lfo_phase, 0.5 * lfoGet2, 0.5 * lfoGet2)
			+ LFNoise0.ar(lfo_speed, 0.5 * lfoGet1, 0.5 * lfoGet1);
		lfo = lfo * EnvGen.ar(Env.asr(lfo_delay, releaseTime: 0.01, curve: -5), envTrig);
		multiPitch = LinLin.ar(lfo, 0, 1, (pitchCons.neg.midiratio * tameM) + tameC, pitchCons.midiratio);
		lfoAmp = LinLin.ar(lfo, 0, 1, lfo_amd * (-3/4), 0);
		ReplaceOut.ar(pmodbus, multiPitch);
		ReplaceOut.ar(amodbus, lfoAmp);
	}).add;

	// One persistent voice = one node. The ~150 envelope/ratio/matrix args are
	// computed in sclang (dx7.scd ~dx7_voiceArgs) and pushed via .set; the gate
	// is driven internally: drone (sustained) or internal retrigger (arp).
	SynthDef(("dx7" ++ suf).asSymbol, { |out = 0, pmodbus = 0, amodbus = 0,
		pitch = 261.6, gate = 1,
		envPL0 = 0, envPL1 = 0, envPL2 = 0, envPL3 = 0, envPL4 = 0,
		envPR0 = 0, envPR1 = 0, envPR2 = 0, envPR3 = 0,
		coars1 = 1, fine_1 = 1, coars2 = 1, fine_2 = 1, coars3 = 1, fine_3 = 1,
		coars4 = 1, fine_4 = 1, coars5 = 1, fine_5 = 1, coars6 = 1, fine_6 = 1,
		env1L0 = 0, env1L1 = 0, env1L2 = 0, env1L3 = 0, env1L4 = 0, env1R0 = 1, env1R1 = 1, env1R2 = 1, env1R3 = 1, env1C0 = 3, env1C1 = 3, env1C2 = 3, env1C3 = 3,
		env2L0 = 0, env2L1 = 0, env2L2 = 0, env2L3 = 0, env2L4 = 0, env2R0 = 1, env2R1 = 1, env2R2 = 1, env2R3 = 1, env2C0 = 3, env2C1 = 3, env2C2 = 3, env2C3 = 3,
		env3L0 = 0, env3L1 = 0, env3L2 = 0, env3L3 = 0, env3L4 = 0, env3R0 = 1, env3R1 = 1, env3R2 = 1, env3R3 = 1, env3C0 = 3, env3C1 = 3, env3C2 = 3, env3C3 = 3,
		env4L0 = 0, env4L1 = 0, env4L2 = 0, env4L3 = 0, env4L4 = 0, env4R0 = 1, env4R1 = 1, env4R2 = 1, env4R3 = 1, env4C0 = 3, env4C1 = 3, env4C2 = 3, env4C3 = 3,
		env5L0 = 0, env5L1 = 0, env5L2 = 0, env5L3 = 0, env5L4 = 0, env5R0 = 1, env5R1 = 1, env5R2 = 1, env5R3 = 1, env5C0 = 3, env5C1 = 3, env5C2 = 3, env5C3 = 3,
		env6L0 = 0, env6L1 = 0, env6L2 = 0, env6L3 = 0, env6L4 = 0, env6R0 = 1, env6R1 = 1, env6R2 = 1, env6R3 = 1, env6C0 = 3, env6C1 = 3, env6C2 = 3, env6C3 = 3,
		noteBlok1 = 1, noteBlok2 = 1, noteBlok3 = 1, noteBlok4 = 1, noteBlok5 = 1, noteBlok6 = 1,
		dn0 = 0, dn1 = 0, dn2 = 0, dn3 = 0, dn4 = 0, dn5 = 0, dn6 = 0, dn7 = 0, dn8 = 0, dn9 = 0, dn10 = 0,
		dn11 = 0, dn12 = 0, dn13 = 0, dn14 = 0, dn15 = 0, dn16 = 0, dn17 = 0, dn18 = 0, dn19 = 0, dn20 = 0,
		dn21 = 0, dn22 = 0, dn23 = 0, dn24 = 0, dn25 = 0, dn26 = 0, dn27 = 0, dn28 = 0, dn29 = 0, dn30 = 0,
		dn31 = 0, dn32 = 0, dn33 = 0, dn34 = 0, dn35 = 0, dn36 = 1, dn37 = 0, dn38 = 1, dn39 = 0, dn40 = 0, dn41 = 0,
		detun1 = 7, detun2 = 7, detun3 = 7, detun4 = 7, detun5 = 7, detun6 = 7,
		modSens1 = 0, modSens2 = 0, modSens3 = 0, modSens4 = 0, modSens5 = 0, modSens6 = 0,
		outMult = 2, osc_sync = 0, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var ctls, mods, chans, sig, gOn, pmod, amod,
		envAmp1, envEnv1, envAmp2, envEnv2, envAmp3, envEnv3, envAmp4, envEnv4,
		envAmp5, envEnv5, envAmp6, envEnv6, envAmpP, envEnvP;
		// Pure drone source: the gate is held (default 1). Rhythm/dynamics are added
		// downstream by ENV/GATE modules (and, in future, MIDI generators), so there
		// is no internal clock or envelope here.
		gOn = gate;
		pmod = Lag2.ar(In.ar(pmodbus, 1), 0.01);
		amod = Lag2.ar(In.ar(amodbus, 1), 0.01);
		envEnvP = Env.new([envPL0, envPL1, envPL2, envPL3, envPL4], [envPR0, envPR1, envPR2, envPR3], 0, 3);
		envAmpP = EnvGen.kr(envEnvP, gOn, doneAction: 0);
		envEnv1 = Env.new([(-1 * env1L0).dbamp, (-1 * env1L1).dbamp, (-1 * env1L2).dbamp, (-1 * env1L3).dbamp, (-1 * env1L4).dbamp], [env1R0, env1R1, env1R2, env1R3], [env1C0, env1C1, env1C2, env1C3], 3);
		envAmp1 = EnvGen.kr(envEnv1, gOn, doneAction: 0);
		envEnv2 = Env.new([(-1 * env2L0).dbamp, (-1 * env2L1).dbamp, (-1 * env2L2).dbamp, (-1 * env2L3).dbamp, (-1 * env2L4).dbamp], [env2R0, env2R1, env2R2, env2R3], [env2C0, env2C1, env2C2, env2C3], 3);
		envAmp2 = EnvGen.kr(envEnv2, gOn, doneAction: 0);
		envEnv3 = Env.new([(-1 * env3L0).dbamp, (-1 * env3L1).dbamp, (-1 * env3L2).dbamp, (-1 * env3L3).dbamp, (-1 * env3L4).dbamp], [env3R0, env3R1, env3R2, env3R3], [env3C0, env3C1, env3C2, env3C3], 3);
		envAmp3 = EnvGen.kr(envEnv3, gOn, doneAction: 0);
		envEnv4 = Env.new([(-1 * env4L0).dbamp, (-1 * env4L1).dbamp, (-1 * env4L2).dbamp, (-1 * env4L3).dbamp, (-1 * env4L4).dbamp], [env4R0, env4R1, env4R2, env4R3], [env4C0, env4C1, env4C2, env4C3], 3);
		envAmp4 = EnvGen.kr(envEnv4, gOn, doneAction: 0);
		envEnv5 = Env.new([(-1 * env5L0).dbamp, (-1 * env5L1).dbamp, (-1 * env5L2).dbamp, (-1 * env5L3).dbamp, (-1 * env5L4).dbamp], [env5R0, env5R1, env5R2, env5R3], [env5C0, env5C1, env5C2, env5C3], 3);
		envAmp5 = EnvGen.kr(envEnv5, gOn, doneAction: 0);
		envEnv6 = Env.new([(-1 * env6L0).dbamp, (-1 * env6L1).dbamp, (-1 * env6L2).dbamp, (-1 * env6L3).dbamp, (-1 * env6L4).dbamp], [env6R0, env6R1, env6R2, env6R3], [env6C0, env6C1, env6C2, env6C3], 3);
		envAmp6 = EnvGen.kr(envEnv6, gOn, doneAction: 0);
		ctls = [
			[coars1 * fine_1 * ((pitch * noteBlok1) + ((detun1 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp1 * (amod * (modSens1 / 3)).dbamp],
			[coars2 * fine_2 * ((pitch * noteBlok2) + ((detun2 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp2 * (amod * (modSens2 / 3)).dbamp],
			[coars3 * fine_3 * ((pitch * noteBlok3) + ((detun3 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp3 * (amod * (modSens3 / 3)).dbamp],
			[coars4 * fine_4 * ((pitch * noteBlok4) + ((detun4 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp4 * (amod * (modSens4 / 3)).dbamp],
			[coars5 * fine_5 * ((pitch * noteBlok5) + ((detun5 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp5 * (amod * (modSens5 / 3)).dbamp],
			[coars6 * fine_6 * ((pitch * noteBlok6) + ((detun6 - 7) / 32)) * (envAmpP.midiratio) * pmod, Rand(0, 2pi) * osc_sync, envAmp6 * (amod * (modSens6 / 3)).dbamp]
		];
		mods = [
			[dn0, dn1, dn2, dn3, dn4, dn5], [dn6, dn7, dn8, dn9, dn10, dn11],
			[dn12, dn13, dn14, dn15, dn16, dn17], [dn18, dn19, dn20, dn21, dn22, dn23],
			[dn24, dn25, dn26, dn27, dn28, dn29], [dn30, dn31, dn32, dn33, dn34, dn35]
		];
		chans = [0, 1, 2, 3, 4, 5];
		sig = FM7.ar(ctls, mods).slice(chans) * -12.dbamp;
		sig = Mix.new([sig[0] * dn36, sig[1] * dn37, sig[2] * dn38, sig[3] * dn39, sig[4] * dn40, sig[5] * dn41]);
		sig = sig / outMult * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- DISTORT: versatile multi-stage distortion / saturation ----------
	// A full waveshaping chain (best-of SC, core UGens + a Chebyshev Shaper):
	// pre-emphasis tone -> drive + asymmetry bias -> 8-way waveshaper bank ->
	// secondary wavefolder -> bitcrush + downsample grit -> DC/tone shaping ->
	// distorted feedback path. Stack several nodes for parallel multi-character
	// textures. Goes from gentle tube warmth to screaming digital nastiness.
	SynthDef(("distort" ++ suf).asSymbol, { |out = 0, in = 0,
		drive = 2, type = 0, bias = 0, fold = 0, tone = 0,
		crush = 24, downsample = 1, feedback = 0, lowCut = 20, highCut = 18000,
		amp = 0.7, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan);
		var x = Mix(dry) / nchan;
		var fb = LocalIn.ar(1);
		var d, shaped, folded, levels, y;
		// distorted feedback into the input -> sustain / self-oscillating screams
		x = x + (fb * feedback.clip(0, 0.98));
		// pre-emphasis tilt: tone>0 brightens (more bite into the shaper), <0 darkens
		x = BHiShelf.ar(x, 1800, 1.0, tone * 12);
		x = BLowShelf.ar(x, 250, 1.0, tone.neg * 6);
		// drive into the shaper, with bias for asymmetric (even-harmonic) clipping
		d = (x * drive.max(0.001)) + bias;
		shaped = Select.ar(type.clip(0, 7).round, [
			d.tanh,                                          // 0 tube / smooth
			d.softclip,                                      // 1 soft clip
			d.clip2(1),                                      // 2 fuzz / hard clip
			d.fold2(1),                                      // 3 wavefold
			d.wrap2(1),                                      // 4 wrap (harsh)
			d.max(0).tanh + (d.min(0) * 0.3),                // 5 diode (asymmetric)
			sin(d * (pi / 2)),                               // 6 sine fold (west coast)
			Shaper.ar(~distChebyBuf.bufnum, d.clip2(1))      // 7 chebyshev (rich harmonics)
		]);
		// secondary wavefolder for extra harmonic complexity (blended in by `fold`)
		folded = (shaped * (1 + (fold * 5))).fold2(1);
		y = SelectX.ar(fold.clip(0, 1), [shaped, folded]);
		// digital grit: bit-depth reduction (core) + sample-rate reduction (Latch)
		levels = (2 ** crush.clip(1, 24)).max(2);
		y = (y * levels).round / levels;
		y = Select.ar(downsample > 1.001,
			[y, Latch.ar(y, Impulse.ar(SampleRate.ir / downsample.max(1)))]);
		// shape / tame the output
		y = LeakDC.ar(y);
		y = RHPF.ar(y, lowCut.clip(20, 2000), 1.0);
		y = LPF.ar(y, highCut.clip(200, 19000));
		// feedback send (soft-limited + NaN-killed so it can never lock up)
		LocalOut.ar(~killbad.(y.tanh) * feedback.clip(0, 0.98));
		y = y * amp * enable;
		y = ~spread.(y, nchan, pan);
		Out.ar(out, ~killbad.(XFade2.ar(dry, y, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass)));
	}).add;

	// ---- Pedal FX fleet (ported from 21echoes/pedalboard) -----------------
	// Shared insert wrapper: sum input to mono, process, spread+pan, wet/bypass.
	~fxOut = { |dry, y, pan, amp, enable, wet, bypass|
		var w = ~spread.(LeakDC.ar(y) * amp * enable, nchan, pan);
		~killbad.(XFade2.ar(dry, w, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass));
	};

	SynthDef(("overdrive" ++ suf).asSymbol, { |out = 0, in = 0, drive = 4, tone = 0,
		amp = 0.8, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, y;
		x = BHiShelf.ar(x, 1500, 1.0, tone * 9);
		x = BLowShelf.ar(x, 220, 1.0, tone.neg * 6);
		y = (x * drive.max(0.01)).tanh;
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("ampsim" ++ suf).asSymbol, { |out = 0, in = 0, gain = 6, bass = 0, mid = 0,
		treble = 0, amp = 0.8, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, y;
		y = (x * gain.max(0.01)).tanh;                 // preamp distortion
		y = BLowShelf.ar(y, 120, 1.0, bass);           // tone stack
		y = BPeakEQ.ar(y, 750, 0.8, mid);
		y = BHiShelf.ar(y, 3000, 1.0, treble);
		y = LPF.ar(y, 5500);                           // speaker cabinet roll-off
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("equalizer" ++ suf).asSymbol, { |out = 0, in = 0, low = 0, midGain = 0,
		midFreq = 900, high = 0, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, y;
		y = BLowShelf.ar(x, 120, 1.0, low);
		y = BPeakEQ.ar(y, midFreq.clip(150, 6000), 1.0, midGain);
		y = BHiShelf.ar(y, 4000, 1.0, high);
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("flanger" ++ suf).asSymbol, { |out = 0, in = 0, rate = 0.3, depth = 0.6,
		feedback = 0.4, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, lfo, dt, fb, y;
		lfo = SinOsc.kr(rate, 0, 0.5, 0.5);            // 0..1
		dt = (0.0006 + (depth * 0.006 * lfo)).clip(0.0001, 0.02);
		fb = LocalIn.ar(1);
		y = DelayC.ar(x + (fb * feedback.clip(0, 0.95)), 0.02, dt);
		LocalOut.ar(~killbad.(y));
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("phaser" ++ suf).asSymbol, { |out = 0, in = 0, rate = 0.4, depth = 0.7,
		feedback = 0.3, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, c, fb, sig, y;
		c = SinOsc.kr(rate) * depth * 0.9;             // swept all-pass coefficient
		fb = LocalIn.ar(1);
		sig = x + (fb * feedback.clip(0, 0.9));
		6.do { sig = FOS.ar(sig, c, 1.0, c) };          // 6 first-order all-pass stages
		LocalOut.ar(~killbad.(sig));
		y = (x + sig) * 0.5;                            // notches from dry+phased sum
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("ringmod" ++ suf).asSymbol, { |out = 0, in = 0, freq = 200,
		amp = 0.9, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan;
		var y = x * SinOsc.ar(freq.clip(0.1, 18000));
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("bitcrusher" ++ suf).asSymbol, { |out = 0, in = 0, bits = 8, downsample = 4,
		amp = 0.9, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, levels, y;
		levels = (2 ** bits.clip(1, 24)).max(2);
		y = (x * levels).round / levels;
		y = Latch.ar(y, Impulse.ar((SampleRate.ir / downsample.max(1)).clip(200, SampleRate.ir)));
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("lofi" ++ suf).asSymbol, { |out = 0, in = 0, bits = 10, downsample = 6,
		cutoff = 4000, noise = 0.1, amp = 0.9, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, levels, y;
		levels = (2 ** bits.clip(1, 24)).max(2);
		y = (x * levels).round / levels;
		y = Latch.ar(y, Impulse.ar((SampleRate.ir / downsample.max(1)).clip(200, SampleRate.ir)));
		y = LPF.ar(y, cutoff.clip(200, 12000));
		y = HPF.ar(y, 180);                            // thin, radio-ish low end
		y = y + PinkNoise.ar(noise * 0.06);            // hiss
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("tremolo" ++ suf).asSymbol, { |out = 0, in = 0, rate = 4, depth = 0.6,
		shape = 0, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, lfo, modu, y;
		lfo = Select.kr(shape.clip(0, 2), [SinOsc.kr(rate), LFTri.kr(rate), LFPulse.kr(rate, 0, 0.5) * 2 - 1]);
		modu = (1 - depth) + (depth * (lfo * 0.5 + 0.5));
		y = x * modu;
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
	}).add;

	SynthDef(("wavefolder" ++ suf).asSymbol, { |out = 0, in = 0, fold = 2, bias = 0,
		amp = 0.8, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan, d, y;
		d = (x + bias) * fold.max(0.01);
		y = sin(d.fold2(1) * (pi / 2));                // fold back + smooth the creases
		Out.ar(out, ~fxOut.(dry, y, pan, amp, enable, wet, bypass));
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

	// ---- GATE: signal-based rhythmic gate / chopper ----
	// Clock-driven 8-step gate with swing, jitter, probability, attack/decay
	// shaping, invert (ducking), and smoothing. Step values 0..1 scale each
	// step's gate depth; probability globally decides whether a step fires.
	SynthDef(("gate" ++ suf).asSymbol, { |out = 0, in = 0,
		freq = 4, phase = 0, swing = 0, duty = 0.5,
		attack = 5, decay = 20, shape = 0,
		step0 = 1, step1 = 0, step2 = 1, step3 = 0,
		step4 = 1, step5 = 0, step6 = 1, step7 = 0,
		probability = 1, jitter = 0,
		wet = 1, invert = 0, smoothing = 0,
		amp = 1, pan = 0,
		bypass = 0, enable = 1|
		var dry = In.ar(in, nchan);
		var period = 1.0 / freq.max(0.001);
		var curve = Select.kr(shape.clip(0, 2), [0, -4, 4]);
		var rawTrig = Impulse.ar(freq, phase);
		var jitterAmt = jitter * period * 0.5;
		var jitteredTrig = DelayC.ar(rawTrig, period, TRand.ar(0, jitterAmt, rawTrig));
		var stepIdx = PulseCount.ar(jitteredTrig) % 8;
		var isOdd = stepIdx % 2;
		var swingDelay = K2A.ar(swing * period * 0.5);
		var swungTrig = DelayC.ar(jitteredTrig, period * 0.5, Select.ar(isOdd, [DC.ar(0), swingDelay]));
		var finalStep = PulseCount.ar(swungTrig) % 8;
		var gateLevel = Select.ar(finalStep, [K2A.ar(step0), K2A.ar(step1), K2A.ar(step2), K2A.ar(step3), K2A.ar(step4), K2A.ar(step5), K2A.ar(step6), K2A.ar(step7)]);
		var probGate = Latch.ar((TRand.ar(0, 1, swungTrig) < probability), swungTrig);
		var gateTime = (duty * period).max(0.001);
		var env = EnvGen.ar(
			Env.new(
				[0, 1, 1, 0],
				[attack * 0.001, gateTime, decay * 0.001],
				curve
			),
			swungTrig
		) * gateLevel * probGate;
		var invGate = Select.ar(invert, [env, 1 - env]);
		var cutoff = (1 - smoothing).linexp(0, 1, 20, 20000).clip(20, 20000);
		var smoothGate = LPF.ar(invGate, cutoff);
		var sig = dry * smoothGate * amp * enable;
		sig = ~spread.(sig, nchan, pan);
		Out.ar(out, ~killbad.(XFade2.ar(dry, sig, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass)));
	}).add;

	// ---- PLAITS: Mutable Instruments Plaits macro-oscillator ----
	// Port of the v7b1 MiPlaits UGen. Trigger fires on enable rising edge;
	// an external ASR envelope gated by enable guarantees sustain while on.
	SynthDef(("plaits" ++ suf).asSymbol, { |out = 0, in = 0,
		pitch = 60, engine = 0, harm = 0.1, timbre = 0.5, morph = 0.5,
		trigger = 0, level = 1, fm_mod = 0, timb_mod = 0, morph_mod = 0,
		decay = 0.5, lpg_colour = 0.5,
		amp = 0.5, pan = 0,
		bypass = 0, wet = 1, enable = 1|
		var trig = Changed.kr(enable) + Changed.kr(trigger);
		var pl = MiPlaits.ar(
			pitch: pitch,
			engine: engine.round(1),
			harm: harm,
			timbre: timbre,
			morph: morph,
			trigger: trig,
			level: level,
			fm_mod: fm_mod,
			timb_mod: timb_mod,
			morph_mod: morph_mod,
			decay: decay,
			lpg_colour: lpg_colour,
			mul: 1
		);
		var env = EnvGen.kr(Env.asr(0.005, 1, 0.05), enable);
		var sig = Balance2.ar(pl[0], pl[1], pan, amp * env);
		Out.ar(out, ~killbad.(sig * (1 - bypass) * wet));
	}).add;
	// ---- FBANK: 10-band resonant stereo filterbank ------------------------
	// Mimics the Erica Synths Resonant Filterbank: ten fixed-frequency resonant
	// bandpass filters (29 Hz … 11 kHz), each with a boost/cut gain (1 = flat).
	// `resonance` is a bipolar attenuverter on a shared feedback loop: magnitude
	// narrows the bands' Q and raises feedback intensity (past ~half -> the
	// feedback-enabled bands self-oscillate at their centre frequencies, mimicking
	// no-input mixing); sign sets feedback polarity (CW positive / CCW inverted).
	// Per-band feedback enables (fb1..fb10) choose which bands ring. Stereo SPREAD
	// pans the bands across the field for a spectral stereo image.
	SynthDef(("fbank" ++ suf).asSymbol, { |out = 0, in = 0,
		gain1 = 1, gain2 = 1, gain3 = 1, gain4 = 1, gain5 = 1, gain6 = 1, gain7 = 1, gain8 = 1, gain9 = 1, gain10 = 1,
		fb1 = 1, fb2 = 1, fb3 = 1, fb4 = 1, fb5 = 1, fb6 = 1, fb7 = 1, fb8 = 1, fb9 = 1, fb10 = 1,
		resonance = 0, inputGain = 0, spread = 0, amp = 1, bypass = 0, wet = 1, enable = 1, pan = 0|
		var freqs = #[29, 61, 115, 218, 411, 777, 1500, 2800, 5200, 11000];
		var gains = [gain1, gain2, gain3, gain4, gain5, gain6, gain7, gain8, gain9, gain10];
		var fbs = [fb1, fb2, fb3, fb4, fb5, fb6, fb7, fb8, fb9, fb10];
		var dry = In.ar(in, nchan);
		var x0 = Mix(dry) / nchan * inputGain.dbamp;
		var fbk = LocalIn.ar(1);
		// magnitude -> Q (rq) + feedback intensity; sign -> feedback polarity.
		// WIDE (≈overlapping octave) bands at rest so the summed bank covers the
		// whole spectrum at full level (heavy filterbank colour); resonance narrows
		// them toward sharp resonant peaks / self-oscillation.
		var rq = abs(resonance).linexp(0, 1, 1.3, 0.006);
		// tiny resonance-scaled noise floor so the feedback can bootstrap into self-
		// oscillation with no input (mimics analog noise), inaudible otherwise.
		var seed = PinkNoise.ar(abs(resonance) * 0.003);
		var x = x0 + seed + (fbk * resonance);
		var makeup = 1.8;   // compensates the bandpass-sum level so flat ≈ unity
		var bands = Array.fill(10, { |i| Resonz.ar(x, freqs[i], rq) * makeup });
		var fbSum = Mix(Array.fill(10, { |i| bands[i] * fbs[i] }));
		var sig;
		LocalOut.ar(~killbad.((fbSum * 1.5).tanh));   // bounded, NaN-safe feedback
		(nchan == 2).if({
			sig = Mix(Array.fill(10, { |i| Pan2.ar(bands[i] * gains[i], ((i / 4.5) - 1) * spread) }));
		}, {
			sig = ~spread.(Mix(Array.fill(10, { |i| bands[i] * gains[i] })), nchan, 0);
		});
		sig = LeakDC.ar(sig) * amp * enable;
		Out.ar(out, ~killbad.(~panOut.(XFade2.ar(dry, sig, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass), pan, nchan)));
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
		smear = 0, mute = 0, pan = 0, bypass = 0, wet = 1, enable = 1, amp = 1|
		var dry = Mix(In.ar(in, nchan)) / nchan, sig;
		var fb = LocalIn.ar(1);
		var t = (tapTime * 0.001) + (LFNoise1.kr(3) * jitter * 0.001);
		var d = DelayC.ar(dry + (fb * feedbackSend.clip(0, 0.95)), 8, t.clip(0, 8));
		d = RLPF.ar(d, filterFreq, (1/filterQ).clip(0.05, 5));
		LocalOut.ar(~killbad.(LeakDC.ar(d).clip2(0.98)));
		sig = ~spread.(d * tapGain * (1 - mute) * enable * amp, nchan, pan);
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
		tremoloRate = 4, tremoloDepth = 0, pan = 0, bypass = 0, wet = 1, enable = 1, amp = 1|
		var dry = In.ar(in, nchan);
		var atten = (1 / distance.max(0.1));
		var sig = dry * atten;
		sig = LPF.ar(sig, airAbsorb.linexp(0, 1, 18000, 3000).clip(2000, 18000));
		sig = sig * (1 - (tremoloDepth * SinOsc.kr(tremoloRate).range(0, 1)));
		sig = BPeakEQ.ar(sig, 4000, 1, presence * 6);
		sig = sig * gain * (1 - mute) * enable * amp;
		Out.ar(out, ~killbad.((XFade2.ar(dry, sig, (wet*2-1)) * (1-bypass) + (dry*bypass))));
	}).add;

	// ---- SDLY: stereo / ping-pong delay -----------------------------------
	SynthDef(("sdly" ++ suf).asSymbol, { |out = 0, in = 0, timeL = 280, timeR = 420,
		feedback = 0.35, crossFeed = 0.4, damp = 0.4, width = 1.1, modRate = 0.3,
		modDepth = 0.12, amp = 1, bypass = 0, wet = 0.35, enable = 1, pan = 0|
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
		Out.ar(out, ~killbad.(~panOut.((outSig * (1 - bypass)) + (dry * bypass), pan, nchan)));
	}).add;

	// ---- VERB: spacious deep reverb (JPverb) ------------------------------
	SynthDef(("verb" ++ suf).asSymbol, { |out = 0, in = 0, size = 2.2, decay = 5,
		damp = 0.3, modDepth = 0.2, modFreq = 2, earlyDiff = 0.7, lowCut = 180,
		highCut = 9000, width = 1, amp = 1, bypass = 0, wet = 0.35, enable = 1, freeze = 0, pan = 0|
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
		Out.ar(out, ~killbad.(~panOut.((outSig * (1 - bypass)) + (dry * bypass), pan, nchan)));
	}).add;

	// ---- CLOUDS: Mutable Instruments granular processor (MiClouds) --------
	// Guarded: define only if the mi-UGens plugin is present, so a missing
	// plugin can never break the rest of the engine.
	(\MiClouds.asClass.notNil).if {
	SynthDef(("clouds" ++ suf).asSymbol, { |out = 0, in = 0, pit = 0, pos = 0.5,
		size = 0.4, dens = 0.4, tex = 0.5, inGain = 1, spread = 0.5, rvb = 0.2,
		fb = 0, freeze = 0, mode = 0, lofi = 0, amp = 0.9, bypass = 0, wet = 1, enable = 1, pan = 0|
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
		Out.ar(out, ~killbad.(~panOut.((outSig * (1 - bypass)) + (dry * bypass), pan, nchan)));
	}).add;
	};

	// ---- GRAINS: maximalist live granulator (IRCAM/GRM-leaning) -----------
	// Each node records its mono input into a private 4 s circular buffer and
	// sprays FOUR parallel grain streams from a moving read head. Per grain:
	// position spray, size jitter, pitch jitter, probabilistic reverse + octave
	// shimmer, stereo scatter. A granular feedback loop (LocalIn/Out), freeze +
	// buffer scan, and a chaos random-walk give it a far wilder reach than Clouds.
	// Window shape is selectable from a pre-built bank (~grainWins, built once).
	~grainWins ?? {
		~grainWins = [
			Env([0, 1, 0], [0.5, 0.5], \sine),                         // hann
			Env([0, 1, 0], [0.5, 0.5], [6, -6]),                       // bell
			Env([0, 1, 0.35, 0], [0.02, 0.3, 0.68], [4, 0, -3]),       // expodec
			Env([0, 0.35, 1, 0], [0.68, 0.3, 0.02], [3, 0, -4]),       // rev-expodec
			Env([0, 1, 1, 0], [0.15, 0.7, 0.15], \lin),                // plateau
			Env([0, 1, 0], [0.5, 0.5], \lin),                          // triangle
			Env([0, 1, 1, 0, 0, 1, 1, 0], [0.1, 0.2, 0.05, 0.3, 0.05, 0.2, 0.1, 0], \lin) // gapped
		].collect { |e| Buffer.sendCollection(s, e.discretize(2048), 1) };
	};

	SynthDef(("grains" ++ suf).asSymbol, { |out = 0, in = 0,
		size = 0.12, density = 40, pos = 0.0, spray = 0.08,
		jitter = 0.2, spread = 0.7,
		reverse = 0, shimmer = 0, scan = 0, texture = 0.4, chaos = 0,
		feedback = 0, freeze = 0, shape = 0, inGain = 1, amp = 0.9,
		bypass = 0, wet = 1, nodeEnable = 1, pan = 0|
		var bufFrames = (s.sampleRate * 4).asInteger;
		var buf = LocalBuf(bufFrames, 1).clear;
		var nstream = 4;
		var dry = In.ar(in, nchan);
		var mono = (nchan == 1).if({ dry }, { Mix(dry) * (1 / nchan) });
		var fb = LocalIn.ar(1);
		var rec = LeakDC.ar((mono * inGain) + (fb * feedback).tanh);
		var writePhase = Phasor.ar(0, BufRateScale.ir(buf) * (1 - freeze), 0, BufFrames.ir(buf)) / BufFrames.ir(buf);
		var envSel = Select.kr(shape.clip(0, ~grainWins.size - 1), ~grainWins.collect { |b| b.bufnum });
		var streams, wetSig, wetL, wetR, gmono, outSig;
		RecordBuf.ar(rec, buf, loop: 1, run: 1 - freeze);
		streams = Array.fill(nstream, { |i|
			var trig = Dust.ar(density / nstream);
			var rnd = TRand.ar(0, 1, trig);
			var walk = LFNoise1.ar(1.5 + (i * 0.6)) * chaos;
			var rpos = (writePhase - (pos * 0.5) - TRand.ar(0, spray, trig)
				+ (LFSaw.ar(scan * (1 + (i * 0.13)), 2.0.rand) * 0.5 * (scan > 0))
				+ (walk * 0.25)).wrap(0, 1);
			var rate = (walk * 12).midiratio;   // natural pitch; only chaos detunes grains
			var dir = Select.ar(rnd < reverse, [DC.ar(1), DC.ar(-1)]);
			var oct = Select.ar(TRand.ar(0, 1, trig) < shimmer, [DC.ar(1), DC.ar(2)]);
			var gdur = (size * (1 + texture) * (1 + TRand.ar(jitter.neg, jitter, trig))).clip(0.003, 2.0);
			var gpan = TRand.ar(spread.neg, spread, trig);
			GrainBuf.ar(2, trig, gdur, buf, rate * dir * oct, rpos, 2, gpan, envSel, 512);
		});
		wetSig = Mix(streams);                       // -> [L, R]
		wetL = wetSig[0] * amp * nodeEnable;
		wetR = wetSig[1] * amp * nodeEnable;
		gmono = (wetL + wetR) * 0.5;
		LocalOut.ar(gmono);                          // granular feedback tap
		outSig = (nchan == 1).if({
			XFade2.ar(dry, gmono, wet * 2 - 1)
		}, {
			var oL = XFade2.ar(dry[0], wetL, wet * 2 - 1);
			var oR = XFade2.ar(dry[1], wetR, wet * 2 - 1);
			(nchan == 2).if({ [oL, oR] }, { [oL, oR] ++ dry.drop(2) });
		});
		Out.ar(out, ~killbad.(~panOut.((outSig * (1 - bypass)) + (dry * bypass), pan, nchan)));
	}).add;

	// ---- MOLLY: port of "Molly the Poly" (Mark Wheeler) -------------------
	// One analogue voice = one node. Two morphing oscillators (tri->saw->pulse)
	// + sub + noise -> optional ring-mod -> resonant LP (12/24 dB) with its own
	// ADSR -> overdrive -> amp ADSR -> chorus. note + gate drive it (gate held =
	// drone; SEQ/GATE articulate the envelopes). Source: ignores `in`.
	SynthDef(("molly" ++ suf).asSymbol, { |out = 0, in = 0,
		note = 48, gate = 1, detune = 7, oscShape = 0.5, pulseWidth = 0.5,
		subLevel = 0, noiseLevel = 0,
		cutoff = 1200, resonance = 0.2, lpType = 1, filterEnvAmt = 0.3,
		fAtk = 0.05, fDec = 0.3, fSus = 0.6, fRel = 0.6,
		aAtk = 0.01, aDec = 0.3, aSus = 0.8, aRel = 0.5,
		lfoRate = 4, lfoToCutoff = 0, lfoToPitch = 0, lfoToPW = 0, lfoToAmp = 0,
		ringMod = 0, drive = 0, chorus = 0, amp = 0.3, pan = 0,
		bypass = 0, wet = 1, nodeEnable = 1|
		var freq = Lag.kr(note.midicps, 0.03);
		var lfo = SinOsc.kr(lfoRate);
		var fA = freq * (1 + (lfo * lfoToPitch * 0.06));
		var fB = fA * (2 ** (detune / 1200));
		var pw = (pulseWidth + (lfo * lfoToPW * 0.4)).clip(0.05, 0.95);
		var wave = { |f| SelectX.ar(oscShape * 2, [VarSaw.ar(f, 0, 0.5), Saw.ar(f), Pulse.ar(f, pw)]) };
		var oscA = wave.(fA);
		var oscB = wave.(fB);
		var sub = Pulse.ar(fA * 0.5, 0.5) * subLevel;
		var noise = WhiteNoise.ar(noiseLevel);
		var mix = ((oscA + oscB) * 0.5) + sub + noise;
		var fenv = EnvGen.ar(Env.adsr(fAtk, fDec, fSus, fRel), gate);
		var aenv = EnvGen.ar(Env.adsr(aAtk, aDec, aSus, aRel), gate);
		var fc = (cutoff * (2 ** ((filterEnvAmt * fenv * 4) + (lfo * lfoToCutoff * 2)))).clip(20, 18000);
		var filt, sig, cho;
		mix = XFade2.ar(mix, oscA * oscB * 2, (ringMod * 2 - 1));
		filt = Select.ar(lpType.clip(0, 1), [
			RLPF.ar(mix, fc, (1 - (resonance * 0.95)).max(0.05)),     // 12 dB
			MoogFF.ar(mix, fc, (resonance * 4).clip(0, 3.95))         // 24 dB
		]);
		sig = (filt * (1 + (drive * 6))).tanh / (1 + (drive * 2.5));  // overdrive + makeup
		sig = sig * aenv * (1 - (lfoToAmp * (lfo * 0.5 + 0.5)));      // tremolo
		cho = Mix(DelayC.ar(sig, 0.06, LFNoise2.kr([0.5, 0.7]).range(0.004, 0.02))) * 0.7;
		sig = XFade2.ar(sig, cho, (chorus * 2 - 1)) * amp * nodeEnable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- RINGS: Mutable Instruments modal/string resonator (MiRings) ------
	(\MiRings.asClass.notNil).if {
	SynthDef(("rings" ++ suf).asSymbol, { |out = 0, in = 0, pit = 48, struct = 0.25,
		bright = 0.5, damp = 0.7, pos = 0.25, model = 0, poly = 0, internalExc = 1,
		easteregg = 0, trigRate = 2, trigMode = 0, amp = 0.9, bypass = 0, wet = 1, enable = 1, pan = 0|
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
		Out.ar(out, ~killbad.(~panOut.((outSig * (1 - bypass)) + (dry * bypass), pan, nchan)));
	}).add;
	};

	// ---- master analysis + meters (drives VIZ + analysis-follower mod) ----
	// Proper spectral analysis via sc3-plugins (SpecCentroid / SpecFlatness).
	// The engine (engine.scd) attaches the source/feature labels on forward,
	// since SendReply carries only numeric values.
	SynthDef(("analysis" ++ suf).asSymbol, { |in = 0|
		var sig = In.ar(in, nchan);
		var mono = Mix(sig) / nchan;
		// Move takeover: dropped the continuous FFT(2048)+SpecCentroid+SpecFlatness
		// (big scsynth CPU on the CM4 -> JACK XRuns; unused by the Move UI).
		var rms = Amplitude.kr(mono);
		SendReply.kr(Impulse.kr(12), '/atelier/meters', Amplitude.kr(sig));
		SendReply.kr(Impulse.kr(8), '/atelier/analysisRaw', [rms, 0, 0]);
	}).add;
};
