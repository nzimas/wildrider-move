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

	// ---- STK init: point the STK plugin at the rawwave bank before any physical-
	// model voice (MALLET/BOWED — StkInst) spawns. SHAKER is stochastic (no rawwaves)
	// but this is harmless for it. Fired once at boot (see engine.scd). No suffix.
	SynthDef(\stkInit, { StkGlobals.ar(1, 1, "/data/UserData/wildrider/rawwaves"); Line.kr(0, 1, 0.3, doneAction: 2) }).add;

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
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5,
		outMult = 2, osc_sync = 0, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var ctls, mods, chans, sig, gOn, pmod, amod,
		envAmp1, envEnv1, envAmp2, envEnv2, envAmp3, envEnv3, envAmp4, envEnv4,
		envAmp5, envEnv5, envAmp6, envEnv6, envAmpP, envEnvP,
		clkR, clkBase, clkTrig, clkLenV, clkVelV;
		// UNSTABLE INTERNAL CLOCK: instead of a held drone (which leaves the many
		// percussive/decaying presets silent), an irregular clock RE-GATES the FM
		// envelope — drifting rate, dropped/extra hits, and per-hit note length +
		// velocity. Pitch is NOT sequenced (the carrier freqs are untouched). This
		// keeps every preset audible and alive, like a wonky internal sequencer.
		clkR = (clkRate * (2 ** (LFNoise2.kr(LFNoise1.kr(0.2).exprange(0.05, 0.6)) * clkDrift * 1.5))).clip(0.05, 30);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));  // syncopated drops
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7) + Impulse.kr(0);              // + chaos hits + 1 at start
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		gOn = Trig.kr(clkTrig, clkLenV);
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
		sig = sig / outMult * amp * enable * clkVelV;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- FMTONE: bespoke 4-operator FM voice (Elektron Digitone "FM TONE") -
	// Signal path: FM core (ops C, A, B1, B2; 8 algorithms with X/Y carrier taps +
	// MIX crossfade; HARM additive-ish harmonics; DTUN; feedback on A) -> Overdrive
	// -> Base/Width filter -> Multimode filter (+ env) -> Amp env. Two AD-with-end
	// operator envelopes (A, B macro-mapped) set the FM index over time. A wonky
	// internal clock re-gates the voice (shared with DX7) so it self-articulates.
	SynthDef(("fmtone" ++ suf).asSymbol, { |out = 0, pmodbus = 0, amodbus = 0,
		pitch = 48, gate = 1,
		algo = 1, ratioC = 1, ratioA = 1, ratioB = 1, offC = 0, offA = 0, offB1 = 0, offB2 = 0,
		harm = 0, dtun = 0, fdbk = 0, mix = 0,
		aAtk = 0, aDec = 50, aEnd = 0, aLev = 85, bAtk = 0, bDec = 50, bEnd = 0, bLev = 0,
		fType = 0, fFreq = 90, fReso = 20, fEnv = 0, fAtk = 0, fDec = 55, fSus = 70, fRel = 45,
		base = 0, width = 127,
		ampAtk = 1, ampDec = 60, ampSus = 95, ampRel = 50, drv = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, gOn, pmod, f0, fC, fA, fB1, fB2,
			envA, envB, fbA, opB2, opB1, opA, opC, hP, hN, idx, fdA,
			gB2B1, gB2A, gB1A, gAC, gB1C, gB2C, ySel, voice, baseHz, topHz, bw, od, lpf,
			fEnvS, cut, rq, mm, ampE, sig;
		// harmonic morph: saturate to add upper partials (cheap — a few UGens, vs ~10
		// for a Chebyshev blend; the per-op shaper is created 3x, so this matters a lot
		// for synth INSTANTIATION cost -> patch-rebuild XRuns).
		var hsh = { |x, a| (x * (1 + (a * 3.5))).tanh * (1 / (1 + (a * 1.6))) };
		// --- unstable internal clock (re-gates the voice) ---
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);   // densityMul = density knob (wide: sparse .. saturated)
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		gOn = Trig.kr(clkTrig, clkLenV);
		pmod = Lag2.ar(In.ar(pmodbus, 1), 0.01);          // per-module vibrato LFO
		f0 = (pitch.clip(0, 127).midicps * (2 ** (pitchShift / 12)) * pmod).clip(38, 12000);   // pitchShift semis; floor 38Hz avoids sub gargle
		// operator freqs: ratio + fine offset + detune drift on A / B2
		// Snap the CARRIER to integers (keeps the voice in tune), but snap the
		// MODULATORS only to a fine 0.25 grid — that lets inharmonic ratios (1.25,
		// 1.75, 2.25, 3.5 ...) through for metallic / glassy / bell timbres, while the
		// moderate index keeps them tonal rather than noise. Offsets/detune drift on top.
		fC  = f0 * (ratioC.round(1).clip(1, 16) + (offC * 0.5)).max(0.01);
		fA  = f0 * (ratioA.round(0.25).clip(0.25, 16) + (offA * 0.5)).max(0.01) * (1 + ((dtun / 127) * 0.035 * LFNoise2.kr(0.15)));
		fB1 = f0 * (ratioB.round(0.25).clip(0.25, 16) + (offB1 * 0.5)).max(0.01);
		fB2 = f0 * (ratioB.round(0.25).clip(0.25, 16) + (offB2 * 0.5)).max(0.01) * (1 - ((dtun / 127) * 0.035));
		// operator (FM index) envelopes: AD + end level, gated by the clock
		envA = EnvGen.kr(Env.new([0, 1, (aEnd / 127)], [(aAtk / 127 * 2) + 0.001, (aDec / 127 * 4) + 0.004], [-4, -4]), gOn) * (aLev / 127);
		envB = EnvGen.kr(Env.new([0, 1, (bEnd / 127)], [(bAtk / 127 * 2) + 0.001, (bDec / 127 * 4) + 0.004], [-4, -4]), gOn) * (bLev / 127);
		idx = 3.0;                                          // FM index scale (radians) — kept moderate so the spectrum stays tonal, not noise
		hP = (harm.max(0)) / 26; hN = (harm.neg.max(0)) / 26;
		// algorithm matrix gains: [B2->B1, B2->A, B1->A, A->C, B1->C, B2->C]; Y carrier tap
		gB2B1 = Select.kr(algo - 1, [0, 1, 0, 1, 0, 1, 0, 1]);
		gB2A  = Select.kr(algo - 1, [1, 0, 0, 0, 1, 0, 1, 0]);
		gB1A  = Select.kr(algo - 1, [0, 0, 0, 1, 1, 0, 1, 0]);
		gAC   = Select.kr(algo - 1, [1, 1, 1, 1, 1, 1, 1, 0]);
		gB1C  = Select.kr(algo - 1, [1, 0, 1, 0, 0, 1, 1, 1]);
		gB2C  = Select.kr(algo - 1, [0, 0, 1, 0, 0, 0, 0, 1]);
		ySel  = Select.kr(algo - 1, [2, 2, 1, 1, 1, 2, 2, 3]);   // 0=C 1=A 2=B1 3=B2
		// operators in modulation order B2 -> B1 -> A -> C; feedback on A
		fbA = LocalIn.ar(1);
		opB2 = SinOsc.ar(fB2, 0);
		opB1 = hsh.(SinOsc.ar(fB1, opB2 * envB * idx * gB2B1), hP);
		opA  = hsh.(SinOsc.ar(fA, fbA + (opB2 * envB * idx * gB2A) + (opB1 * envB * idx * gB1A)), hP);
		fdA  = (fdbk / 120) * 3.2;
		LocalOut.ar(opA * fdA);
		opC  = hsh.(SinOsc.ar(fC, (opA * envA * idx * gAC) + (opB1 * envB * idx * gB1C) + (opB2 * envB * idx * gB2C)), hN);
		// two carrier taps (X = C; Y = algo-selected) crossfaded by MIX
		voice = XFade2.ar(opC, Select.ar(ySel, [opC, opA, opB1, opB2]), (mix / 64).clip(-1, 1));
		// Overdrive -> Base/Width filter -> Multimode filter -> Amp
		od = ((voice * (1 + ((drv / 127) * 9))).tanh) * (1 - (drv / 254));
		baseHz = (base / 127).linexp(0, 1, 16, 13000); topHz = (width / 127).linexp(0, 1, 180, 18500);
		bw = LPF.ar(HPF.ar(od, baseHz.clip(16, 16000)), topHz.max(baseHz * 1.1).clip(40, 18500));
		fEnvS = EnvGen.kr(Env.adsr((fAtk / 127 * 2) + 0.001, (fDec / 127 * 3) + 0.004, (fSus / 127), (fRel / 127 * 4) + 0.02), gOn);
		cut = ((fFreq / 127).linexp(0, 1, 28, 16000) * (2 ** (fEnvS * (fEnv / 64) * 4))).clip(20, 18000);
		rq = (1 - ((fReso / 127) * 0.88)).clip(0.12, 1);    // floor keeps high res from blowing up on FM transients (-> XRuns)
		lpf = RLPF.ar(bw, cut, rq);                          // computed once, reused for lp12 + lp24
		mm = Select.ar(fType.clip(0, 3), [bw, lpf, RHPF.ar(bw, cut, rq), RLPF.ar(lpf, cut, rq)]);
		ampE = EnvGen.kr(Env.adsr((ampAtk / 127 * 2) + 0.001, (ampDec / 127 * 3) + 0.004, (ampSus / 127), (ampRel / 127 * 4) + 0.02), gOn);
		sig = mm * ampE * clkVelV * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- FM7: real 6-operator FM (ported from poundhard), wonky-clock articulated ----
	// A full FM7 (Chowning) matrix voice: 6 operators, 6 algorithms, per-op ratios, FM
	// index + feedback. Poundhard triggered it per drum step; here Wildrider's unstable
	// internal clock re-gates the amp + index envelopes so it self-articulates.
	SynthDef(("fm7" ++ suf).asSymbol, { |out = 0, note = 48,
		algo = 0, r1 = 1, r2 = 1, r3 = 1, r4 = 2, r5 = 1, r6 = 3.5,
		index = 1.0, fb = 0.1, bright = 1.0,
		attack = 0.004, decay = 0.6, ampCurve = -4, mDecay = 0.6, cutoff = 16000,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.55, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkVelV, v, f0, aEnv, mEnv, ratios, ctlMatrix, modMatrix, ops, sig, fcut;
		var algos = [
			( mods: [[0,1,0,0,0,0],[0,1,0,0,0,0],[0,0,0,1,0,0],[0,0,0,1,0,0],[0,0,0,0,0,1],[0,0,0,0,0,1]], carr: [1,0,1,0,1,0] ),
			( mods: [[0,1,0,0,0,0],[0,0,1,0,0,0],[0,0,0,1,0,0],[0,0,0,0,1,0],[0,0,0,0,0,1],[0,0,0,0,0,1]], carr: [1,0,0,0,0,0] ),
			( mods: [[0,0,0,0,1,0],[0,0,0,0,1,0],[0,0,0,0,0,1],[0,0,0,0,0,1],[0,0,0,0,1,0],[0,0,0,0,0,1]], carr: [1,1,1,1,0,0] ),
			( mods: [[0,1,0,0,0,0],[0,1,0,0,0,0],[0,0,0,1,0,0],[0,0,0,0,0,0],[0,0,0,0,0,0],[0,0,0,0,0,0]], carr: [1,0,1,0,0,0] ),
			( mods: [[0,1,1,1,0,0],[0,0,0,0,1,0],[0,0,0,0,0,0],[0,0,0,0,0,0],[0,0,0,0,1,0],[0,0,0,0,0,0]], carr: [1,0,0,0,0,1] ),
			( mods: [[0,1,0,0,0,0],[0,0,1,0,0,0],[0,0,1,0,0,0],[0,0,0,0,1,0],[0,0,0,0,0,1],[0,0,0,0,0,1]], carr: [1,0,0,1,0,0] )
		];
		// unstable internal clock (Wildrider signature): drifting rate + dropped/extra hits.
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		aEnv = EnvGen.ar(Env.perc(attack.clip(0.0005, 2), (decay * (clkLen + 0.2)).clip(0.01, 8), 1, ampCurve.clip(-8, -1)), clkTrig);
		mEnv = EnvGen.kr(Env.perc(0.002, (decay * mDecay).clip(0.01, 6), 1, -3.5), clkTrig);
		ratios = [r1, r2, r3, r4, r5, r6].collect { |r| r.clip(0.01, 24) };
		ctlMatrix = ratios.collect { |rr| [(rr * f0).clip(1, 20000), 0, 1] };
		modMatrix = 6.collect { |i| 6.collect { |j|
			var w = Select.kr(algo.clip(0, 5), algos.collect { |a| a[\mods][i][j] });
			(i == j).if({ w * fb.clip(0, 1) * 1.6 }, { w * index.clip(0, 12) * mEnv }) } };
		ops = FM7.ar(ctlMatrix, modMatrix);
		sig = 6.collect { |k| ops[k] * Select.kr(algo.clip(0, 5), algos.collect { |a| a[\carr][k] }) }.sum;
		sig = sig * aEnv;
		fcut = (cutoff * bright.clip(0.1, 4)).clip(60, 19000);
		sig = LPF.ar(sig, fcut);
		sig = LeakDC.ar(sig) * v * amp * 0.5 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- BUCHLOID: complex/west-coast osc (ported from poundhard), wonky-clock articulated ----
	// A Buchla-style complex oscillator: two FM modulators into a sine/varsaw core,
	// wavefolding + timbre bloom, pressure/drive saturation, and a resonant lowpass
	// swept by a per-hit filter env. Poundhard fired it once per step; here the
	// unstable internal clock re-gates the amp + filter envelopes so it self-articulates.
	SynthDef(("buchloid" ++ suf).asSymbol, { |out = 0, note = 45,
		fm1Ratio = 0.66, fm1Amount = 0.0, fm2Ratio = 3.3, fm2Amount = 0.0,
		waveShape = 0.0, waveFolds = 0.0, timbre = 0.0,
		attack = 0.02, decay = 1.0, peak = 8000, res = 0.2,
		pressure = 0.0, drive = 1.0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1,
		amp = 0.25, pan = 0|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, f0, aEnv, fEnv, m1, m2, osc, sig, fc;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		aEnv = EnvGen.ar(Env.new([0, 1, 0], [attack.clip(0.001, 4), decay.clip(0.01, 8)], -4), clkTrig);
		fEnv = EnvGen.ar(Env.perc(attack.clip(0.001, 4), decay.clip(0.01, 8), 1, -3), clkTrig);
		m1 = SinOsc.ar(f0 * fm1Ratio.clip(0.1, 12)) * fm1Amount.clip(0, 1) * f0 * 4;
		m2 = SinOsc.ar(f0 * fm2Ratio.clip(0.1, 24)) * fm2Amount.clip(0, 1) * f0 * 4;
		osc = SelectX.ar(waveShape.clip(0, 1), [
			SinOsc.ar((f0 + m1 + m2).clip(10, 18000)),
			VarSaw.ar((f0 + m1 + m2).clip(10, 18000), 0, 0.3 + (timbre.clip(0, 1) * 0.6))]);
		osc = (osc * (1 + (waveFolds.clip(0, 3) * 2) + (timbre.clip(0, 1) * 3))).fold2(1);
		sig = osc * aEnv;
		sig = (sig * (1 + (pressure.clip(0, 1) * 4))).tanh * (1 + (drive.clip(0.1, 4)));
		fc = (peak.clip(100, 12000) * (0.4 + (fEnv * 0.9))).clip(80, 14000);
		sig = RLPF.ar(sig, fc, (1 - (res.clip(0, 1) * 0.9)).clip(0.05, 1));
		sig = LeakDC.ar(sig) * v * amp * 0.6 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- MOLLY: Molly-the-Poly analogue voice (ported from poundhard), wonky-clock articulated ----
	SynthDef(("molly" ++ suf).asSymbol, { |out = 0, note = 48, detune = 7, oscShape = 0.5, pulseWidth = 0.5,
		subLevel = 0, noiseLevel = 0, cutoff = 1200, resonance = 0.2, lpType = 1, filterEnvAmt = 0.3,
		fAtk = 0.05, fDec = 0.3, fSus = 0.6, fRel = 0.6, aAtk = 0.01, aDec = 0.3, aSus = 0.8, aRel = 0.5,
		lfoRate = 4, lfoToCutoff = 0, lfoToPitch = 0, lfoToPW = 0, lfoToAmp = 0,
		ringMod = 0, drive = 0, chorus = 0,
		fmAmt = 0, fold = 0, crush = 0, downsample = 0, grit = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1,
		amp = 0.3, pan = 0|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var freq, lfo, fA, fB, pw, wave, modB, oscA, oscB, sub, noise, mix, fenv, aenv, fc, filt, sig, cho, bits, lv, dsr;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		freq = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		lfo = SinOsc.kr(lfoRate);
		fA = freq * (1 + (lfo * lfoToPitch * 0.06));
		fB = fA * (2 ** (detune / 1200));
		pw = (pulseWidth + (lfo * lfoToPW * 0.4)).clip(0.05, 0.95);
		wave = { |f| SelectX.ar(oscShape * 2, [VarSaw.ar(f, 0, 0.5), Saw.ar(f), Pulse.ar(f, pw)]) };
		modB = SinOsc.ar(fB) * fmAmt * fA * 3;
		oscA = wave.((fA + modB).clip(20, 12000));
		oscB = wave.(fB);
		sub = Pulse.ar(fA * 0.5, 0.5) * subLevel;
		noise = WhiteNoise.ar(noiseLevel) + (Dust2.ar((2000 * grit).max(1)) * grit * 0.7);
		mix = ((oscA + oscB) * 0.5) + sub + noise;
		fenv = EnvGen.ar(Env.adsr(fAtk, fDec, fSus, fRel), gOn);
		aenv = EnvGen.ar(Env.adsr(aAtk, aDec, aSus, aRel), gOn);
		fc = (cutoff * (2 ** ((filterEnvAmt * fenv * 4) + (lfo * lfoToCutoff * 2)))).clip(20, 18000);
		mix = XFade2.ar(mix, oscA * oscB * 2, (ringMod * 2 - 1));
		mix = XFade2.ar(mix, (mix * (1 + (fold * 10))).fold2(1) * 0.7, (fold.clip(0, 1) * 2 - 1));
		filt = Select.ar(lpType.clip(0, 1), [
			RLPF.ar(mix, fc, (1 - (resonance * 0.95)).max(0.05)),
			MoogFF.ar(mix, fc, (resonance * 4).clip(0, 3.95))]);
		sig = filt * (1 + (drive * 14));
		sig = (sig + (drive * 0.3)).tanh;
		sig = sig / (1 + (drive * 2.2));
		bits = (16 - (crush * 13)).clip(3, 16);
		lv = 2 ** bits;
		sig = XFade2.ar(sig, (sig * lv).round / lv, (crush.clip(0, 1) * 2 - 1));
		dsr = (SampleRate.ir / (1 + (downsample * 80))).clip(800, SampleRate.ir);
		sig = XFade2.ar(sig, Latch.ar(sig, Impulse.ar(dsr)), (downsample.clip(0, 1) * 2 - 1));
		sig = sig * aenv * (1 - (lfoToAmp * (lfo * 0.5 + 0.5)));
		cho = Mix(DelayC.ar(sig, 0.06, LFNoise2.kr([0.5, 0.7]).range(0.004, 0.02))) * 0.7;
		sig = XFade2.ar(sig, cho, (chorus * 2 - 1));
		sig = LeakDC.ar(sig) * v * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;
	// ---- BEN: Benjolin (rungler cross-mod chaos), wonky-clock articulated ----
	SynthDef(("ben" ++ suf).asSymbol, { |out = 0, note = 48,
		freq2 = 4, scale = 1, rungler1 = 0.16, rungler2 = 0.0, runglerFilt = 9,
		filtFreq = 40, q = 0.82, filterType = 0, outSignal = 6, gain = 1,
		decay = 1.0, amp = 0.3, pan = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, f0;
		var fb, rIn, f1, f2, tri1, tri2, osc1, osc2, pwm, sh0, sh, prev;
		var dac = 0, rungler, ffreq, qq, filt, sig, aEnv;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		fb = LocalIn.ar(2);
		rIn = fb[0];
		f1 = ((rIn * rungler1) + f0).clip(0.01, 12000);
		f2 = ((rIn * rungler2) + freq2).clip(0.01, 12000);
		tri1 = LFTri.ar(f1); tri2 = LFTri.ar(f2);
		osc1 = PulseDPW.ar(f1); osc2 = PulseDPW.ar(f2);
		pwm = (tri1 + tri2) > 0;
		sh0 = (osc1 > 0.5);
		sh = Array.newClear(8);
		prev = sh0;
		8.do { |i| sh[i] = Latch.ar(prev, osc2); prev = Delay1.ar(sh[i]) };
		8.do { |i| dac = dac + (sh[i] / (2 ** (8 - i))) };
		rungler = (dac * scale.clip(0, 1).linlin(0, 1, 0, 127)).midicps;
		LocalOut.ar([rungler, DC.ar(0)]);
		ffreq = ((rungler * runglerFilt) + filtFreq).clip(20, 16000);
		qq = ((q.clip(0, 1) * -1) + 1).max(0.02);
		filt = Select.ar(filterType.clip(0, 3), [
			RLPF.ar(pwm, ffreq, qq),
			RHPF.ar(pwm, ffreq, qq),
			SVF.ar(pwm, ffreq, q.clip(0, 1)),
			DFM1.ar(pwm, ffreq, q.clip(0, 1))
		]);
		sig = Select.ar(outSignal.clip(0, 6), [tri1, osc1, tri2, osc2, pwm, sh0, filt]);
		sig = (sig * gain.max(0.01)).tanh;
		aEnv = EnvGen.ar(Env([0, 1, 1, 0], [0.004, (clkLenV * decay.clip(0.1, 6)).clip(0.01, 8), 0.12], [0, 0, -3]), clkTrig);
		sig = LeakDC.ar(sig) * aEnv * v * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- NOIZEOP: deeg's NoizeOp (four sines through six nonlinear algos), clocked ----
	SynthDef(("noizeop" ++ suf).asSymbol, { |out = 0, note = 48,
		freq01 = 1.0, freq02 = 1.5, freq03 = 2.0, freq04 = 3.0,
		mul01 = 1, mul02 = 1, mul03 = 1, mul04 = 1,
		a_mod_01 = 1, a_mod_02 = 1, a_mod_03 = 0.02, a_mod_04 = 1, a_mod_05 = 1, a_mod_06 = 1,
		a_vol_01 = 0.5, a_vol_02 = 0.5, a_vol_03 = 0.5, a_vol_04 = 0.5, a_vol_05 = 0.5, a_vol_06 = 0.5,
		ffreq01 = 40, ffreq02 = 12000, ffreq03 = 1200, q01 = 1, q02 = 1, q03 = 1,
		gain = 1, decay = 1.0, amp = 0.3, pan = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, f0;
		var f01, f02, f03, f04, osc01, osc02, osc03, osc04;
		var algo01, algo02, algo03, algo04, algo05, algo06, combo, sig, aEnv;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		f01 = (f0 * freq01).clip(0.001, 18000);
		f02 = (f0 * freq02).clip(0.001, 18000);
		f03 = (f0 * freq03).clip(0.001, 18000);
		f04 = (f0 * freq04).clip(0.001, 18000);
		osc01 = SinOsc.ar(f01, 0, mul01, 0);
		osc02 = SinOsc.ar(f02, 0, mul02, 0);
		osc03 = SinOsc.ar(f03, 0, mul03, 0);
		osc04 = SinOsc.ar(f04, 0, mul04, 0);
		algo01 = ((osc01 * osc02) / ((osc03 * osc04) + 1e-9)) / a_mod_01.max(1e-3);
		algo02 = ((osc02 * osc04) / ((osc03 - osc01) + 1e-9)) / a_mod_02.max(1e-3);
		algo03 = ((osc01 + osc02) - (osc03 + osc04)).trunc(a_mod_03.max(1e-4));
		algo01 = algo01.clip2(50); algo02 = algo02.clip2(50); algo03 = algo03.clip2(50);
		algo04 = ((algo01 * algo02 * algo03).abs.sqrt) / a_mod_04.max(1e-3);
		algo05 = (hypot(osc01, osc02) + hypot(osc03, osc04)) * a_mod_05;
		algo06 = ((osc01 ** 2) + (osc02 ** 2) + (osc03 ** 2) + (osc04 ** 2)) * a_mod_06;
		combo = (algo01 * a_vol_01) + (algo02 * a_vol_02) + (algo03 * a_vol_03)
		      + (algo04 * a_vol_04) + (algo05 * a_vol_05) + (algo06 * a_vol_06);
		combo = combo.clip2(20);
		sig = BLowPass4.ar(BHiPass4.ar(combo, ffreq01.clip(20, 18000), q01, 1, 0),
			ffreq02.clip(20, 18000), q02, 1, 0);
		sig = Resonz.ar(sig, ffreq03.clip(20, 18000), q03, 1.0, 0.0);
		sig = (sig * gain.max(0.01)).tanh;
		aEnv = EnvGen.ar(Env([0, 1, 1, 0], [0.004, (clkLenV * decay.clip(0.1, 6)).clip(0.01, 8), 0.12], [0, 0, -3]), clkTrig);
		sig = LeakDC.ar(sig) * aEnv * v * amp * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- CHAOS: chaotic-map oscillators (core ChaosUGens), wonky-clock articulated ----
	SynthDef(("chaos" ++ suf).asSymbol, { |out = 0, note = 48,
		type = 0, chaosA = 1.1, chaosB = 0.5, fold = 0.0, cutoff = 6000, res = 0.2,
		attack = 0.003, decay = 0.5, ampCurve = -4, amp = 0.2, pan = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkVelV, v, f0, aEnv, srcs, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(20, 8000);
		aEnv = EnvGen.ar(Env.perc(attack.clip(0.0005, 2), (decay * (clkLen + 0.2)).clip(0.01, 8), 1, ampCurve.clip(-8, -1)), clkTrig);
		srcs = [
			FBSineC.ar(f0 * 2, chaosA.clip(0.1, 4), chaosB.clip(0, 3), 1.0, 0.5),
			LatoocarfianC.ar(f0, chaosA.linlin(0, 4, -3, 3), chaosB.linlin(0, 3, 0.5, 1.5), 1.5, 0.5),
			HenonC.ar(f0, chaosA.linlin(0, 4, 1.0, 1.4), chaosB.linlin(0, 3, 0.2, 0.4)),
			StandardL.ar(f0, chaosA.linlin(0, 4, 0.3, 3.0)),
			CuspL.ar(f0, chaosA.linlin(0, 4, 0.9, 1.2), chaosB.linlin(0, 3, 1.7, 2.0))
		];
		sig = Select.ar(type.round.clip(0, 4), srcs).tanh;
		sig = SelectX.ar(fold.clip(0, 1), [sig, (sig * (1 + (fold * 4))).fold2(1)]);
		sig = RLPF.ar(sig, cutoff.clip(40, 16000), (1 - (res.clip(0, 1) * 0.9)).clip(0.05, 1));
		sig = LeakDC.ar(sig) * aEnv * v * amp * 2.8 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;
	// ---- ICARUS: schollz's Icarus "dreamcrusher" pad/drone (ported from poundhard) ----
	SynthDef(("icarus" ++ suf).asSymbol, { |out = 0, note = 48,
		subpitch = 1, sublevel = 0.3, detuning = 0.1, portamento = 0.1,
		pwmcenter = 0.5, pwmwidth = 0.05, pwmfreq = 10,
		lpf = 6000, resonance = 0.2, feedback = 0.5, delaytime = 0.25, destruction = 0,
		attack = 0.02, decay = 1.0, release = 1.5, sustain = 0.8,
		gain = 1, amp = 0.3, pan = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v;
		var hz, ender, in, subdiv, hzd, hzs, ampcheck, local, snd;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 20);   // drone: allow very long sustains
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		hz = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		ender = EnvGen.ar(Env.new(
			curve: 'cubed', levels: [0, 1, sustain, 0],
			times: [attack + 0.015, clkLenV, release]), clkTrig);
		hzd = (Lag.kr(hz + (SinOsc.kr(LFNoise0.kr(1)) * (((hz.cpsmidi + 1).midicps) - hz) * detuning), portamento).cpsmidi).midicps;
		in = VarSaw.ar(hzd, width: LFTri.kr(pwmfreq + Rand(0.1, 0.3), mul: pwmwidth / 2, add: pwmcenter), mul: 0.5);
		subdiv = 2 ** subpitch;
		hzs = (Lag.kr((hz / subdiv) + (SinOsc.kr(LFNoise0.kr(1)) * ((((hz / subdiv).cpsmidi + 1).midicps) - (hz / subdiv)) * detuning), portamento).cpsmidi).midicps;
		in = in + Pulse.ar(hzs, width: LFTri.kr(pwmfreq + Rand(0.1, 0.3), mul: pwmwidth / 2, add: pwmcenter), mul: 0.5 * sublevel);
		in = Splay.ar(in);
		in = Balance2.ar(in[0], in[1], SinOsc.kr(LinLin.kr(LFNoise0.kr(0.1), -1, 1, 0.05, 0.2)) * 0.1);
		in = in * ender;
		ampcheck = Amplitude.kr(Mix.ar(in));
		in = in * (ampcheck > 0.006);
		local = LocalIn.ar(2);
		local = OnePole.ar(local, 0.4);
		local = OnePole.ar(local, -0.08);
		local = Rotate2.ar(local[0], local[1], 0.2);
		local = DelayC.ar(local, 0.5, Lag.kr(delaytime, 0.2));
		local = LeakDC.ar(local);
		local = ((local + in) * 1.25).softclip;
		local = MoogLadder.ar(local, Lag.kr(lpf.clip(20, 18000), 1), res: Lag.kr(resonance.clip(0, 1), 1));
		local = ((local * (1 - EnvGen.kr(Env([0, 1, 0], [0.1, 0.1], curve: \sine), gate: Dust.kr(destruction)))) + local) / 2;
		LocalOut.ar(local * Lag.kr(feedback.clip(0, 0.98), 1));
		snd = Balance2.ar(local[0] * 0.9, local[1] * 0.9, SinOsc.kr(LinLin.kr(LFNoise0.kr(0.1), -1, 1, 0.05, 0.2)) * 0.1);
		snd = Mix.ar([Pan2.ar(snd[0], -1 + (2 * pan.clip(-1, 1)), 1), Pan2.ar(snd[1], 1 + (2 * pan.clip(-1, 1)), 1)]);
		snd = snd * v * amp * gain.max(0.01) * enable;
		Out.ar(out, ~killbad.(snd * (1 - bypass) * wet));
	}).add;

	// ---- PLAITS: real MiPlaits macro-oscillator (mi-UGens), ported from poundhard ----
	SynthDef(("plaits" ++ suf).asSymbol, { |out = 0, note = 48,
		model = 0, harm = 0.5, timbre = 0.5, morph = 0.5,
		decay = 0.5, lpgColour = 0.5, aux = 0.0, amp = 0.4, pan = 0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn, mdl, clkMul, modelDec, envAtk, envDec;
		var pitchN, sig, mono, trim, aEnv, s;
		mdl = model.round.clip(0, 15);
		// Per-model CLOCK + ENVELOPE — each Plaits engine is articulated to suit the sound
		// it makes. clkMul scales the trigger rate: pitched/sustained models (0-7) fire
		// slowly, textural (8-9) medium, particle/drums (10,13-15) fast, string/modal (11-12)
		// medium. modelDec = the internal LPG decay base (sustained long, perc short), scaled
		// by the user decay. envAtk/envDec = the outer amp envelope (sustained hold the note
		// length; perc are short).
		clkMul = Select.kr(mdl, [
			0.5, 0.6, 0.6, 0.7, 0.5, 0.6, 0.4, 0.5,
			0.7, 0.8, 1.4, 0.9, 0.9, 1.8, 1.9, 2.4]);
		clkR = (clkRate * clkMul * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		pitchN = (note.clip(0, 127) + pitchShift).clip(0, 127);
		modelDec = Select.kr(mdl, [
			0.85, 0.80, 0.75, 0.70, 0.88, 0.80, 0.90, 0.85,
			0.62, 0.55, 0.22, 0.42, 0.42, 0.16, 0.22, 0.12]);
		sig = MiPlaits.ar(
			pitch: pitchN,
			engine: mdl,
			harm: harm.clip(0, 1), timbre: timbre.clip(0, 1), morph: morph.clip(0, 1),
			trigger: clkTrig,
			level: v.clip(0, 1),
			decay: (modelDec * (0.55 + (decay.clip(0, 1) * 0.9))).clip(0.05, 1), lpg_colour: lpgColour.clip(0, 1));
		mono = XFade2.ar(sig[0], sig[1], (aux.clip(0, 1) * 2) - 1);
		trim = Select.kr(mdl, [
			0.76, 0.99, 0.82, 1.54, 0.97, 1.01, 0.74, 1.77,
			0.78, 0.74, 1.25, 6.00, 1.29, 2.70, 1.44, 1.71]);
		envAtk = Select.kr(mdl, [
			0.012, 0.010, 0.008, 0.012, 0.014, 0.010, 0.020, 0.015,
			0.012, 0.008, 0.001, 0.003, 0.003, 0.001, 0.001, 0.0005]);
		envDec = Select.kr(mdl, [
			clkLenV, clkLenV, clkLenV, clkLenV, clkLenV, clkLenV, clkLenV, clkLenV,
			clkLenV * 0.8, clkLenV * 0.6, 0.10, (clkLenV * 0.5).clip(0.15, 1.2), (clkLenV * 0.5).clip(0.15, 1.0),
			0.14, 0.16, 0.06]);
		aEnv = EnvGen.ar(Env.perc(envAtk, envDec.max(0.02), 1, -3), gOn);
		s = LeakDC.ar(mono) * trim * amp * aEnv * enable;
		Out.ar(out, ~killbad.(~spread.(s, nchan, pan) * (1 - bypass) * wet));
	}).add;
	// ---- SHAKER: STK stochastic shakers (StkUGens), wonky-clock re-excited ----
	SynthDef(("shaker" ++ suf).asSymbol, { |out = 0, note = 60,
		instr = 0, energy = 90, decay = 70, objects = 40, resfreq = 64,
		atk = 0.001, dec = 0.35,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.35, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var eEnv, aEnv, rf, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		eEnv = EnvGen.kr(Env.perc(0.001, (dec * (clkLenV + 0.2)).clip(0.02, 3), 1, -4), clkTrig);
		aEnv = EnvGen.ar(Env.perc(atk.clip(0.0002, 0.2), (dec * 1.5 * (clkLenV + 0.2)).clip(0.02, 4), 1, -3), clkTrig);
		rf = (resfreq + ((note + pitchShift) - 60)).clip(0, 128);
		sig = StkShakers.ar(instr.round.clip(0, 22), (energy * eEnv).clip(0, 128),
			decay.clip(0, 128), objects.clip(0, 128), rf);
		sig = LeakDC.ar(sig) * aEnv * v * amp * 4.5 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- MEMBRANE: 2D waveguide struck membrane (MembraneUGens), wonky-clock re-struck ----
	SynthDef(("membrane" ++ suf).asSymbol, { |out = 0, note = 48,
		tension = 0.05, loss = 0.9995, tone = 0.5, strike = 0.5,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.35, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var sEnv, exc, tns, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		sEnv = EnvGen.ar(Env.perc(0.0002, (0.002 + (strike * 0.03)).clip(0.001, 0.08), 1, -6), clkTrig);
		exc = LPF.ar(WhiteNoise.ar, tone.clip(0, 1).linexp(0, 1, 600, 9000)) * sEnv * (0.4 + (v * 0.6));
		tns = (tension * (2 ** (((note + pitchShift).clip(24, 96) - 48) / 24))).clip(0.004, 0.22);
		sig = MembraneCircle.ar(exc, tns, loss.clip(0.9, 0.99998));
		sig = LeakDC.ar(sig) * v * amp * 2.0 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- MALLET: STK ModalBar (StkInst), wonky-clock struck ----
	SynthDef(("mallet" ++ suf).asSymbol, { |out = 0, note = 60,
		instrument = 0, stickhardness = 64, stickposition = 28, vibratogain = 8,
		vibratofreq = 20, directmix = 40, decay = 1.0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.3, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var f0, sig, aEnv;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(20, 8000);
		sig = StkModalBar.ar(f0, instrument.round.clip(0, 8), stickhardness.clip(0, 128),
			stickposition.clip(0, 128), vibratogain.clip(0, 128), vibratofreq.clip(0, 128),
			directmix.clip(0, 128), (100 * v).clip(0, 128), T2A.ar(clkTrig));
		aEnv = EnvGen.ar(Env.perc(0.001, (decay * (clkLenV + 0.2)).clip(0.05, 6), 1, -3), clkTrig);
		sig = LeakDC.ar(sig) * aEnv * v * amp * 4.0 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- BOWED: STK BandedWG (StkInst), wonky-clock bowed ----
	SynthDef(("bowed" ++ suf).asSymbol, { |out = 0, note = 60,
		instr = 0, bowpressure = 70, bowmotion = 30, integration = 0, modalresonance = 90,
		bowvelocity = 80, striking = 0, decay = 1.5,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.3, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var f0, bowV, aEnv, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(20, 7000);
		bowV = (bowvelocity * (0.15 + (gOn * 0.85 * v))).clip(0, 128);
		sig = StkBandedWG.ar(f0, instr.round.clip(0, 3), bowpressure.clip(0, 128),
			bowmotion.clip(0, 128), integration.round.clip(0, 1), modalresonance.clip(0, 128),
			bowV, striking.round.clip(0, 1), T2A.ar(clkTrig));
		aEnv = EnvGen.ar(Env.asr(0.01, 1, decay.clip(0.02, 4), -3), gOn);
		sig = LeakDC.ar(sig) * aEnv * v * amp * 4.0 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- PLUCK: DWG plucked stiff string (DWGPlucked), wonky-clock re-plucked ----
	SynthDef(("pluck" ++ suf).asSymbol, { |out = 0, note = 60,
		pos = 0.14, decay = 1.0, damp = 30, bright = 0.5, excite = 0.008,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.4, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var f0, exc, c1, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(20, 6000);
		exc = WhiteNoise.ar * EnvGen.ar(Env.perc(0.0005, excite.clip(0.001, 0.05), 1, -4), clkTrig) * (0.5 + (v * 0.5));
		c1 = (1.0 / (decay * (clkLenV + 0.2)).clip(0.05, 8)).clip(0.1, 30);
		sig = DWGPluckedStiff.ar(f0, v, 1, pos.clip(0.02, 0.5), c1, damp.clip(1, 80),
			exc, 0.1, bright.clip(0, 1) * 6);
		sig = LeakDC.ar(sig) * v * amp * 1.5 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- TUBE: TwoTube waveguide (vocal-tract-ish), wonky-clock breathed ----
	SynthDef(("tube" ++ suf).asSymbol, { |out = 0, note = 60,
		k = 0.01, loss = 0.99, balance = 0.5, excite = 0.01, decay = 1.0,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.4, pan = 0, bypass = 0, wet = 1, enable = 1|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var f0, totLen, bal, d1, d2, breath, aEnv, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(30, 4000);
		totLen = (SampleRate.ir / (2 * f0)).clip(4, 2000);
		bal = balance.clip(0.1, 0.9);
		d1 = (totLen * bal).clip(2, 1500);
		d2 = (totLen * (1 - bal)).clip(2, 1500);
		breath = WhiteNoise.ar
			* EnvGen.ar(Env.asr((0.001 + excite).clip(0.001, 0.2), 1, 0.03, -3), gOn)
			* (0.4 + (v * 0.6));
		aEnv = EnvGen.ar(Env.asr(0.004, 1, decay.clip(0.02, 4), -3), gOn);
		sig = TwoTube.ar(breath, k.clip(0.001, 0.2), loss.clip(0.9, 1.0), d1, d2);
		sig = LeakDC.ar(sig) * aEnv * v * amp * 1.5 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;


	// ---- WTABLE: Ableton "Wavetable" rebuild (ported from poundhard) ----
	// Two morphing wavetable oscillators (BufRd over concatenated 1024-sample single-cycle
	// frames) + sub + noise -> 3-mode filter with drive -> AR/sustain amp env; the osc
	// POSITION is swept per-hit by an envelope + LFO. buf1/buf2 are sprite buffers set by
	// the engine at spawn (Move's built-in wavetable bank). Wonky-clock articulated.
	SynthDef(("wtable" ++ suf).asSymbol, { |out = 0, note = 48, buf1 = 0, buf2 = 0,
		pos1 = 0.0, pos2 = 0.0, oscmix = 0.5, detune = 0.0, transpose2 = 0,
		suboct = 1, sublevel = 0.0, noiselevel = 0.0,
		cutoff = 8000, res = 0.2, filttype = 0, drive = 1.0, filtenv = 0.3,
		posenv = 0.35, poslfoRate = 0.5, poslfoAmt = 0.0,
		attack = 0.01, decay = 0.5, sustain = 0.7, release = 0.7,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1,
		amp = 0.3, pan = 0|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn;
		var f0, aEnv, pEnv, fEnv, posMod, p1, p2, o1, o2, sub, noise, sig, fcut, rq;
		var wtOsc = { |buf, freq, position|
			var n = (BufFrames.kr(buf) / 1024).max(1);
			var ph = Phasor.ar(0, freq * 1024 / SampleRate.ir, 0, 1024);
			var fp = position.clip(0, 1) * (n - 1);
			var fa = fp.floor, frac = fp - fp.floor;
			var wa = BufRd.ar(1, buf, (fa * 1024) + ph, 1, 2);
			var wb = BufRd.ar(1, buf, ((fa + 1).min(n - 1) * 1024) + ph, 1, 2);
			(wa * (1 - frac)) + (wb * frac);
		};
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		f0 = ((note.clip(0, 127) + pitchShift).midicps).clip(8, 16000);
		// clock-triggered AD envelope (like the rest of the fleet) — a sustained-gate ADSR
		// barely opens under the short clock gate, so re-fire a perc env on each hit instead.
		aEnv = EnvGen.ar(Env.perc(attack.clip(0.001, 4), (decay * (clkLenV + 0.3) + (sustain * release)).clip(0.02, 8), 1, -3), clkTrig);
		pEnv = EnvGen.kr(Env.perc(attack.clip(0.001, 4), (decay * 1.5).clip(0.01, 8), 1, -3), clkTrig);
		fEnv = EnvGen.kr(Env.perc(attack.clip(0.001, 2), (decay * 1.2).clip(0.01, 6), 1, -4), clkTrig);
		posMod = (pEnv * posenv) + (SinOsc.kr(poslfoRate.clip(0.01, 30)) * poslfoAmt * 0.5);
		p1 = (pos1 + posMod).clip(0, 1);
		p2 = (pos2 + posMod).clip(0, 1);
		o1 = wtOsc.(buf1, f0, p1);
		o2 = wtOsc.(buf2, f0 * (2 ** (transpose2.round / 12)) * (2 ** (detune / 1200)), p2);
		sig = XFade2.ar(o1, o2, oscmix.clip(0, 1) * 2 - 1);
		sub = SinOsc.ar(f0 / (2 ** suboct.round.clip(0, 3))) * sublevel.clip(0, 1);
		noise = PinkNoise.ar * noiselevel.clip(0, 1) * 0.6;
		sig = sig + sub + noise;
		rq = (1 - (res.clip(0, 1) * 0.9)).clip(0.05, 1);
		fcut = (cutoff * (1 + (fEnv * filtenv * 4))).clip(30, 18000);
		sig = Select.ar(filttype.round.clip(0, 2),
			[RLPF.ar(sig, fcut, rq), BPF.ar(sig, fcut, rq), RHPF.ar(sig, fcut, rq)]);
		sig = (sig * (1 + (drive.clip(0.1, 6) * 1.5))).tanh;
		sig = LeakDC.ar(sig) * aEnv * v * amp * 1.4 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;

	// ---- BYTEBEAT: 8-bit bytebeat voice (ByteBeat plugin), wonky-clock articulated ----
	// A bytebeat expression clocked at a note-scaled sample rate; the expression is pushed
	// per-voice via the /eval unit command (see engine ~bbExprs / ~bbUgenIndex). The unstable
	// clock re-gates the amp envelope (gOn), so it self-articulates like the rest of the fleet.
	~bbDef = SynthDef(("bytebeat" ++ suf).asSymbol, { |out = 0, note = 48, rate = 8000,
		cutoff = 12000, res = 0.1, drive = 1.0,
		attack = 0.004, decay = 0.5, sustain = 0.7, release = 0.3,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.3, clkVel = 0.5, densityMul = 1, pitchShift = 0, bypass = 0, wet = 1, enable = 1,
		amp = 0.4, pan = 0|
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, v, gOn, clk, t, raw, aEnv, rq, sig;
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(0.3) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.01, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		v = clkVelV.max(0.03);
		gOn = Trig.kr(clkTrig, clkLenV);
		clk = (rate * (2 ** (((note.clip(0, 127) + pitchShift) - 48) / 12))).clip(200, 44100);
		t = PulseCount.ar(Impulse.ar(clk));
		raw = ByteBeat.ar(t);
		aEnv = EnvGen.ar(Env.new([0, 1, sustain.clip(0, 1), 0],
			[attack.clip(0.001, 4), decay.clip(0.005, 8), release.clip(0.005, 8)], \sin), gOn);
		rq = (1 - res.clip(0, 0.96)).clip(0.05, 1);
		sig = RLPF.ar(raw, cutoff.clip(40, 18000), rq);
		sig = (sig * (1 + (drive.clip(0.1, 6) * 1.5))).tanh;
		sig = LeakDC.ar(sig) * aEnv * v * amp * 1.2 * enable;
		Out.ar(out, ~killbad.(~spread.(sig, nchan, pan) * (1 - bypass) * wet));
	}).add;
	// capture the ByteBeat UGen's graph index so the engine can /eval a per-voice expression.
	~bbUgenIndex = ~bbDef.children.detect({ |u| u.class.asSymbol == 'ByteBeat' }).synthIndex;

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
		var xin = Mix(dry) / nchan;                    // clean mono reference (for level-match)
		var x = xin;
		var fb = LocalIn.ar(1);
		var d, shaped, folded, levels, y, dryAmp, wetAmp;
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
		// OUTPUT LEVEL COMPENSATION: driving into a saturator pins the wet signal to
		// near full-scale — ~10 dB hotter AND harmonically denser than the source — so
		// even a sliver of wet in the dry/wet crossfade buried the input. Track both
		// envelopes and rescale the wet to sit at the CLEAN input's level, so `drive`
		// changes timbre (not loudness) and the wet knob is a genuine blend.
		dryAmp = Amplitude.kr(xin, 0.01, 0.12);
		wetAmp = Amplitude.kr(y,   0.01, 0.12);
		y = y * (dryAmp / (wetAmp + 1e-3)).clip(0, 8);   // clamp so silence can't blow up
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

	// 1970s string-machine phaser: a 6-stage cascade of TRUE first-order all-pass
	// filters whose break frequency is swept logarithmically (musical) by an LFO;
	// the phased signal is summed with the dry to form the classic moving notches,
	// with resonant feedback for the vocal/animated character. Stereo comes from two
	// LFOs a quarter-cycle apart (the wide swirl of an ARP/Solina + Small Stone). A
	// fixed makeup gain compensates the notch-sum level loss so it stays ~unity loud.
	SynthDef(("phaser" ++ suf).asSymbol, { |out = 0, in = 0, rate = 0.4, depth = 0.7,
		feedback = 0.3, spread = 0.8, amp = 1, pan = 0, bypass = 0, wet = 1, enable = 1|
		var dry = In.ar(in, nchan), x = Mix(dry) / nchan;
		var sr = SampleRate.ir, fmin = 200, fmax = 2200, makeup = 1.2;   // ~unity loudness (the dry+notch sum measures avg +0.3 dB / min -0.8 dB)
		var fbAmt = feedback.clip(0, 0.9), dep = depth.clip(0, 1);
		// two swept notch positions, LFOs 90 deg * spread apart for a wide stereo image
		var lfoL = SinOsc.kr(rate, 0) * dep;
		var lfoR = SinOsc.kr(rate, spread.clip(0, 1) * (0.5pi)) * dep;
		var fcL = fmin * ((fmax / fmin) ** (lfoL * 0.5 + 0.5));
		var fcR = fmin * ((fmax / fmin) ** (lfoR * 0.5 + 0.5));
		var tL = tan(pi * fcL.clip(40, 9000) / sr), dL = (tL - 1) / (tL + 1);
		var tR = tan(pi * fcR.clip(40, 9000) / sr), dR = (tR - 1) / (tR + 1);
		var fb = LocalIn.ar(2);
		var sL = x + (fb[0] * fbAmt), sR = x + (fb[1] * fbAmt), yL, yR, w;
		6.do { sL = FOS.ar(sL, dL, 1.0, dL.neg) };      // H(z) = (d + z^-1)/(1 + d z^-1)
		6.do { sR = FOS.ar(sR, dR, 1.0, dR.neg) };
		LocalOut.ar(~killbad.(LeakDC.ar([sL, sR])));
		yL = (x + sL) * 0.5 * makeup;                   // dry+phased notch sum, level-matched
		yR = (x + sR) * 0.5 * makeup;
		w = (nchan == 1).if(
			{ LeakDC.ar((yL + yR) * 0.5) * amp * enable },
			{ (nchan == 2).if(
				{ Balance2.ar(LeakDC.ar(yL), LeakDC.ar(yR), pan.clip(-1, 1)) * amp * enable },
				{ PanAz.ar(nchan, LeakDC.ar((yL + yR) * 0.5), pan * 0.5 + 0.5, 1.0) * amp * enable }) });
		Out.ar(out, ~killbad.(XFade2.ar(dry, w, (wet * 2 - 1)) * (1 - bypass) + (dry * bypass)));
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

	// ---- WAVIARY: sophisticated morphing-wavetable voice (abrasive & rich) ----
	// A 3-voice detuned-unison VOsc sweeps through a bank of consecutive
	// wavetables (smooth -> rich -> abrasive, see ~waviaryWaves) under `position`.
	// Abrasion stages: freq-domain FM sidebands, a crossfaded hard-sync (SyncSaw)
	// scream layer, wavefolding, tanh drive, and an env-swept resonant MoogFF that
	// self-oscillates near max resonance. An unstable internal clock (shared idiom
	// with DX7) re-gates the amp + filter envelopes so the voice stays alive and
	// rhythmic instead of a static drone; pitch is NOT sequenced.
	SynthDef(("waviary" ++ suf).asSymbol, { |out = 0, in = 0,
		pitch = 110, position = 0.3, posMod = 0.4, posDrift = 0.15,
		detune = 0.12, sub = 0.25, noise = 0.0, fold = 0.0, drive = 1.0,
		fmRatio = 2.0, fmAmount = 0.0, sync = 0.0, syncRatio = 1.5,
		cutoff = 2200, res = 0.4, envDepth = 0.5, attack = 0.01,
		clkRate = 2, clkChaos = 0.4, clkDrift = 0.3, clkLen = 0.35, clkVel = 0.5, densityMul = 1, pitchShift = 0,
		amp = 0.5, pan = 0, bypass = 0, wet = 1, enable = 1|
		var wtBase = ~waviaryBufs[0].bufnum, wtN = ~waviaryBufN;
		var clkR, clkBase, clkTrig, clkLenV, clkVelV, gOn;
		var env, fenv, pos, freqs, mod, voices, sig, fcut;
		pitch = (pitch * (2 ** (pitchShift / 12))).clip(38, 12000);   // pitchShift semis; floor 38Hz avoids sub gargle
		// unstable internal clock: drifting rate, dropped/extra hits, per-hit length+vel
		clkR = (clkRate * densityMul * (2 ** (LFNoise2.kr(LFNoise1.kr(0.2).exprange(0.05, 0.6)) * clkDrift * 1.5))).clip(0.03, 110);
		clkBase = CoinGate.kr((1 - (clkChaos * 0.65)).clip(0.15, 1), Impulse.kr(clkR));
		clkTrig = clkBase + Dust.kr(clkR * clkChaos * 0.7) + Impulse.kr(0);
		clkLenV = (clkLen * (2 ** (TRand.kr(-1.2, 0.6, clkTrig) * clkChaos))).clip(0.02, 8);
		clkVelV = Latch.kr((1 - clkVel) + (TRand.kr(0, 1, clkTrig) * clkVel), clkTrig).lag(0.004);
		gOn = Trig.kr(clkTrig, clkLenV);
		env  = EnvGen.kr(Env.perc(attack.clip(0.001, 2), clkLenV.max(0.05), 1, [-2, -4]), gOn) * clkVelV;
		fenv = EnvGen.kr(Env.perc((attack * 0.5).clip(0.001, 1), (clkLenV * 0.8).max(0.04), 1, -4), gOn);
		// morphing wavetable position: static + envelope sweep + slow drift -> evolving spectra
		pos = (position + (posMod * env) + (LFNoise2.kr(0.07) * posDrift)).clip(0, 0.999);
		pos = wtBase + (pos * (wtN - 1));
		// 3-voice detuned unison (beating richness) + freq-domain FM (inharmonic abrasion)
		freqs = pitch * [1, (detune * 0.6).midiratio, (detune * 0.6).neg.midiratio];
		mod = SinOsc.ar((pitch * fmRatio.max(0.01)).clip(1, 18000)) * fmAmount * pitch;
		voices = VOsc.ar(pos, (freqs + mod).clip(1, 18000));
		sig = Mix(voices * [1, 0.7, 0.7]) / 2.4;
		// hard-sync scream layer, crossfaded in by `sync`
		sig = XFade2.ar(sig, SyncSaw.ar(pitch.clip(1, 9000), (pitch * syncRatio.max(1)).clip(1, 18000)) * 0.6, (sync.clip(0, 1) * 2 - 1));
		sig = sig + (SinOsc.ar((pitch * 0.5).max(1)) * sub) + (LPF.ar(WhiteNoise.ar, (cutoff * 1.5).clip(40, 18000)) * noise);
		// abrasion: crossfaded wavefold, then tanh drive
		sig = SelectX.ar(fold.clip(0, 1), [sig, sin((sig * (1 + (fold * 6))).fold2(1) * (pi * 0.5))]);
		sig = (sig * (1 + (drive * 4))).tanh;
		// screaming resonant filter: env-swept + velocity-opened, self-oscillates near max res
		fcut = (cutoff * (2 ** ((fenv * envDepth * 4) + (clkVelV * 1.2)))).clip(30, 18000);
		sig  = MoogFF.ar(sig, fcut, (res.clip(0, 1) * 4).clip(0, 3.97));
		// output: env-gated, leveled to sit ~-12 dB with the other generators
		sig = ~spread.(LeakDC.ar(sig) * env * amp * enable * 0.4, nchan, pan);
		Out.ar(out, ~killbad.(sig * (1 - bypass) * wet));
	}).add;


	// ---- BUCHLOID: west-coast complex oscillator / FM / wave-fold voice ----
	// Port of markwheeler/passersby's Engine_Passersby.sc synth voice.
	// Simplified for Wildrider: no internal LFOs/reverb; ASR envelope gated by
	// enable so each node is a sustained drone voice.


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
		// JPverb's DEIND UGen won't register under supernova, so on that server
		// route VERB through GVerb (a supernova-safe algorithmic reverb) mapped to
		// the same knobs. scsynth keeps the original JPverb sound untouched.
		var rev = (~wrSupernova == true).if({
			var mono = (inL + inR) * 0.5;
			// GVerb allocates its delay lines from maxroomsize at init, so that
			// stays a fixed constant; roomsize tracks `size` but is clipped below it.
			var g = GVerb.ar(mono,
				size.linlin(0.5, 5, 8, 120).clip(1, 295),   // roomsize (m)
				t60.clip(0.1, 240),                          // revtime (t60)
				damp.clip(0, 1),                             // damping (HF)
				earlyDiff.clip(0, 1),                        // input bandwidth
				15,                                          // spread
				0,                                           // drylevel (we mix wet ourselves)
				0.7,                                         // early-reflection level
				0.6,                                         // tail level
				300);                                        // maxroomsize (const >= roomsize)
			LPF.ar(HPF.ar(g, lowCut), highCut) * enable;     // match JPverb's band-limit
		}, {
			JPverb.ar([inL, inR], t60, damp, size, earlyDiff, modDepth, modFreq,
				1.0, 1.0, 1.0, lowCut, highCut) * enable;
		});
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

	// ---- RINGS: Mutable Instruments modal/string resonator (MiRings) ------
	(\MiRings.asClass.notNil).if {
	SynthDef(("rings" ++ suf).asSymbol, { |out = 0, in = 0, pit = 48, struct = 0.25,
		bright = 0.5, damp = 0.7, pos = 0.25, model = 0, poly = 0, internalExc = 1,
		easteregg = 0, trigRate = 2, trigMode = 0, densityMul = 1, pitchShift = 0, amp = 0.9, bypass = 0, wet = 1, enable = 1, pan = 0|
		var dry = In.ar(in, nchan);
		var inL = (nchan == 1).if({ dry }, { dry[0] });
		var inR = (nchan == 1).if({ dry }, { dry[1] });
		var mono = (inL + inR) * 0.5;
		// unstable internal clock (matches WAVIARY/FMTONE): drifting rate + dropped/extra
		// hits. RINGS exposes no chaos/drift params, so bake in the same default feel
		// (chaos ~0.4, drift ~0.3); trigMode blends an extra pure-random (Dust) layer on top.
		var tr = (trigRate * densityMul * (2 ** (LFNoise2.kr(LFNoise1.kr(0.2).exprange(0.05, 0.6)) * 0.45))).clip(0.02, 110);
		var trig = CoinGate.kr(0.74, Impulse.kr(tr))     // drifting rate, ~26% of hits dropped
		         + Dust.kr(tr * 0.28)                    // occasional extra hits
		         + Dust.kr(tr * trigMode.clip(0, 1));    // trigMode -> more randomness
		var polyN = Select.kr(poly.clip(0, 2), [1, 2, 4]);
		var pitS = (pit + pitchShift).clip(24, 108);   // pitchShift in semitones; keep out of sub-gargle territory
		// MiRings: in, trig, pit, struct, bright, damp, pos, model, poly, intern_exciter, easteregg, bypass
		var sig = MiRings.ar(mono, trig, pitS, struct, bright, damp, pos, model, polyN, internalExc, easteregg, 0);
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

	// ---- SAMPLER: capture the global master mix; loop-play a recorded slot ----
	// Record reads ~masterBus (the full patch + sample-playback mix) into a buffer;
	// it self-frees when the buffer fills (doneAction:2) or when the controller
	// frees it on stop. Playback loops the recorded region [0, frames) into the
	// master bus via a Phasor so a short take loops seamlessly (not the whole buffer).
	SynthDef(("samplerRec" ++ suf).asSymbol, { |buf = 0, in = 0|
		RecordBuf.ar(In.ar(in, nchan), buf, recLevel: 1, loop: 0, doneAction: 2);
	}).add;
	// SEAMLESS LOOP via an equal-power CROSSFADE at the seam. A plain wrap clicks
	// because buf[end] -> buf[0] is a discontinuity. Zero-crossing search only
	// half-fixes it (no derivative continuity; stereo channels cross at different
	// points). Instead, the take's TAIL is crossfaded into its HEAD so the join is
	// windowed away — content-independent and truly seamless. The loop period is
	// (frames - fade): the body plays straight; only the last `xfade` is blended.
	//   phase 0..period: head read = buf[phase] (fades IN over the first `fade`),
	//   tail read = buf[phase+period] = buf[frames-fade .. frames] (fades OUT).
	//   At the wrap the playhead is continuous (end gHead=1 at buf[period-] -> start
	//   gTail=1 at buf[period]), and out-of-take tail reads land on `fade`=0 -> x0.
	// loopStart/loopEnd are NORMALISED (0..1 of the take). They are lagged so a live
	// CTRL-ALL knob scrub glides the loop region smoothly instead of clicking. The
	// crossfade follows the chosen region [s, e): tail buf[e-fade..e] fades out, head
	// buf[s..s+fade] fades in.
	SynthDef(("samplerPlay" ++ suf).asSymbol, { |buf = 0, frames = 44100, out = 0, amp = 1.0, fadein = 0.02, xfade = 0.03, loopStart = 0, loopEnd = 1, pan = 0, cutoff = 18000, res = 0, gate = 1, release = 0.5, pitch = 0|
		var sr = BufSampleRate.kr(buf);
		// varispeed pitch (+-24 semis): scale the read rate. At pitch 0 rate=1 so reads
		// land on integer positions (bit-exact); cubic interp keeps quality when pitched.
		var rate = BufRateScale.kr(buf) * pitch.lag(0.02).midiratio;
		var startF = (loopStart * frames).max(0);
		var endF = (loopEnd * frames).max(startF + (sr * 0.02));   // end > start, >= 20ms
		var len = endF - startF;
		var fade = (xfade * sr).clip(64, len * 0.25);
		// NATIVE start/end loop: Phasor only consults `start` when it WRAPS, so moving
		// loopStart never jumps/glides the live read position -> NO pitch-bend
		// ("squeaky bip"); the new start just takes effect at the next wrap, where the
		// crossfade hides the seam. The body is [startF+fade, endF) so the crossfaded
		// head region [startF, startF+fade] is not also played as the body (no double).
		var pos = Phasor.ar(0, rate, startF + fade, endF);
		var u = ((pos - (endF - fade)) / fade).clip(0, 1);        // 0 in body, 0->1 across the seam
		var tail = BufRd.ar(nchan, buf, pos, loop: 1, interpolation: 4);
		var head = BufRd.ar(nchan, buf, startF + (pos - (endF - fade)), loop: 1, interpolation: 4);
		var sig = (tail * (1 - u).sqrt) + (head * u.sqrt);
		// resonant filter (RLPF cascade — keeps passband volume at high res, see
		// below). It is BYPASSED when fully open so playback is BIT-EXACT to the take:
		// an always-on LP even at 18k rolls off the top octave -> the lost crispiness.
		// `wet` blends the filtered path in (50ms) only once cutoff drops / res rises.
		var fc = cutoff.lag(0.02).clip(20, 18000);
		var rq = (0.08 ** res.lag(0.02).clip(0, 1)).clip(0.08, 1);
		var filtered = RLPF.ar(RLPF.ar(sig, fc, 1.0), fc, rq);
		var wet = ((cutoff < 17000) + (res > 0.02)).clip(0, 1).lag(0.05);
		sig = (sig * (1 - wet)) + (filtered * wet);
		// transparent stereo balance: pan 0 leaves L/R untouched (no centre gain change).
		sig = (nchan == 2).if({ [sig[0] * (1 - pan).clip(0, 1), sig[1] * (1 + pan).clip(0, 1)] }, { sig });
		// amp env: fast fade-IN on start; 0.5s fade-OUT on stop (gate->0) then self-free.
		// fade-OUT on stop; doneAction frees the play synth (the slot's FX chain is
		// reaped separately by the engine after the release tail).
		sig = sig * EnvGen.kr(Env.asr(fadein, 1, release), gate, doneAction: 2) * amp.lag(0.03);
		Out.ar(out, ~killbad.(sig));
	}).add;
};
