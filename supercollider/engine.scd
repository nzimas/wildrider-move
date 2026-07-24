// Atelier engine — server-side DSP host + OSC dispatch (blueprint section 10).
//
// Responsibilities:
//   * server boot profile (sr, block, channels) — driven by /atelier/boot
//   * group topology: Root -> analysis -> sources -> processors -> mix -> recorder
//   * bus allocator: master (hardware), live input, per-module busses
//   * build the DSP graph from controller commands; keep groups stable, replace
//     synths inside them (hot edits without clicks)
//   * serial lane routing: each lane chains module outBus -> next module in
//   * feedback edges (armed only) and recorder/resampling into PLAY buffers
//   * forward SendReply meters/analysis to the controller's NetAddr
//
// The controller (Python) is the authoritative state manager; this engine just
// realizes the graph and streams telemetry back.

(
~ctrlHost = "CONTROLLER_HOST".getenv ? "controller";   // docker service name
~ctrlPort = ("CONTROLLER_PORT".getenv ? "57140").asInteger;
~nchan = 2;
~modules = IdentityDictionary.new;
~dx7mods = IdentityDictionary.new;  // mid -> DX7 voice state (vr, mod buses, LFO, per-node note/vel)
~lanes = IdentityDictionary.new;   // laneIndex -> List of module ids (in order)
~feedback = IdentityDictionary.new;
~built = false;

~ctrl = { NetAddr(~ctrlHost, ~ctrlPort) };

~ensureGroups = {
	// Execution order must follow lane/chain order, not module category: a
	// generative module (e.g. COMB as a resonator) can sit mid-chain after an
	// insert, so all modules live in one ordered group and run in creation
	// (lane) order. Topology: input -> modules -> mix -> analysis.
	// input -> modules -> mix(lane sums) -> master(limiter) -> analysis
	~gIn       = Group.new(s, \addToHead);
	~gModules  = Group.after(~gIn);
	~gMix      = Group.after(~gModules);
	// Sampler playback sits AFTER the patch mix and BEFORE the master: played
	// samples sum into ~masterBus so they are heard through the master limiter AND
	// re-captured by a new recording (the future sample-manipulation playground).
	~gSampPlay = Group.after(~gMix);
	~gMaster   = Group.after(~gSampPlay);
	~gRec      = Group.after(~gMaster);
};

// --- multicore layering (supernova) ------------------------------------------
// The graph is a DAG of module chains. supernova only runs sibling nodes of a
// ParGroup in parallel, and only when they have NO ordering dependency on each
// other. So we assign each module a topological LAYER:
//   layer(m) = 0                         if m has no incoming edge (a source)
//            = 1 + max(layer(src) ...)   over m's incoming edges
// Every edge then runs strictly low-layer -> high-layer. Modules that share a
// layer are mutually independent, so a ParGroup per layer parallelizes them; the
// layer ParGroups themselves sit in creation order inside the serial ~gModules,
// and inter-layer cords go in that serial parent (before the destination layer),
// so data dependencies stay same-block correct. On scsynth ParGroup == a plain
// serial group, so this is behaviourally identical to the old single ordered
// group; on supernova each layer spreads across the idle cores.
//
// Only module groups run in parallel, and each touches only its own private in/
// outBus — no shared-bus writes inside a layer — so there are no cross-thread
// races. The bus-summing cords live in the serial parent, never in a ParGroup.
~graphLayers = {
	// returns (layerOf: mid->Int, max: Int) computed over ~graphOrder/~graphEdges.
	// ~graphOrder is already sources-first (Kahn), so each node's predecessors are
	// resolved before it; residual-cycle nodes (appended last) default preds to 0.
	var layerOf = IdentityDictionary.new, maxL = 0;
	(~graphOrder ? []).do { |mid|
		var lyr = 0;
		(~graphEdges ? []).clump(2).do { |pair|
			(pair[1] == mid).if { lyr = max(lyr, (layerOf[pair[0].asSymbol] ? 0) + 1) };
		};
		layerOf[mid.asSymbol] = lyr;
		maxL = max(maxL, lyr);
	};
	(layerOf: layerOf, max: maxL);
};
// (Re)create the layer ParGroups fresh inside ~gModules and reparent every module
// group into its layer. Reparenting a live group is glitch-free (no synth restart).
// Old layer groups are freed AFTER their children moved out, so nothing is dropped.
// Returns the layerOf dict so callers can place cords before the right layer.
~applyLayers = {
	var lay = ~graphLayers.value, layerOf = lay[\layerOf], newGroups;
	newGroups = Array.fill(lay[\max] + 1, { ParGroup.tail(~gModules) });
	(~graphOrder ? []).do { |mid|
		var info = ~modules[mid.asSymbol];
		info.notNil.if { info[\group].moveToTail(newGroups[layerOf[mid.asSymbol]]) };
	};
	(~layerGroups ? []).do(_.free);   // old groups now empty
	~layerGroups = newGroups;
	~layerOf = layerOf;               // remembered for the incremental grow path
	layerOf;
};

// Rebuild the entire routing from the free-form connection graph. The controller
// sends the topo order, edges and terminals; we realize it with:
//   * a clear synth per module inBus + master (private busses are not auto-zeroed)
//   * one ParGroup per topological layer, module groups reparented into theirs
//   * a patch-cord synth per edge, placed before the destination LAYER so it reads
//     a fresh src.outBus (its whole layer has finished) and sums into dst.inBus
//   * terminal cords (no outgoing edge) summed into the master bus
~rebuildGraph = {
	var layerOf;
	(~clears ? []).do(_.free);
	(~cords ? []).do(_.free);
	~clears = []; ~cords = [];
	// clear every module input bus (head of ~gModules, before all layers) and master
	~modules.do { |info|
		~clears = ~clears.add(Synth.head(~gModules, ("busClear_" ++ ~nchan).asSymbol,
			[\bus, info[\inBus].index]));
	};
	~clears = ~clears.add(Synth.head(~gMix, ("busClear_" ++ ~nchan).asSymbol,
		[\bus, ~masterBus.index]));
	// layer the modules into ParGroups
	layerOf = ~applyLayers.value;
	// inter-module cords: place each right before its destination's layer ParGroup
	(~graphEdges ? []).clump(2).do { |pair|
		var s = ~modules[pair[0].asSymbol], d = ~modules[pair[1].asSymbol];
		(s.notNil and: { d.notNil }).if {
			~cords = ~cords.add(Synth.before(~layerGroups[layerOf[pair[1].asSymbol]],
				("cord_" ++ ~nchan).asSymbol,
				[\inb, s[\outBus].index, \outb, d[\inBus].index]));
		};
	};
	// terminals -> master (in ~gMix, after all modules and the master clear)
	(~graphTerminals ? []).do { |mid|
		var info = ~modules[mid.asSymbol];
		info.notNil.if {
			~cords = ~cords.add(Synth.tail(~gMix, ("cord_" ++ ~nchan).asSymbol,
				[\inb, info[\outBus].index, \outb, ~masterBus.index, \gain, (-12.dbamp)]));
		};
	};
};

// Grow: a module was ADDED. Fade in ONLY its new cords (and clear its input bus),
// leaving every existing cord untouched — so the new module eases in over `dur`
// with no rebuild click and no onset pop. The new group runs at the tail (its
// edges to downstream sinks are then a block late, inaudible); its input cord and
// terminal cord are same-block correct.
~growGraph = { |mid, dur = 0.2|
	var info = ~modules[mid.asSymbol];
	info.notNil.if {
		var lyr = 0;
		~layerOf = ~layerOf ? IdentityDictionary.new;
		~layerGroups = ~layerGroups ? [];
		~clears = ~clears.add(Synth.head(~gModules, ("busClear_" ++ ~nchan).asSymbol,
			[\bus, info[\inBus].index]));
		// A freshly added module is a terminal (no outgoing edge yet), so only its
		// incoming edges — from modules already layered — set its layer.
		(~graphEdges ? []).clump(2).do { |pair|
			(pair[1] == mid).if { lyr = max(lyr, (~layerOf[pair[0].asSymbol] ? 0) + 1) };
		};
		// grow (don't rebuild) the layer stack if this module needs a deeper layer,
		// then drop its group into that layer ParGroup — existing layers untouched.
		while { ~layerGroups.size <= lyr } {
			~layerGroups = ~layerGroups.add(ParGroup.tail(~gModules));
		};
		info[\group].moveToTail(~layerGroups[lyr]);
		~layerOf[mid.asSymbol] = lyr;
		// its cords, faded in, each before its destination layer
		(~graphEdges ? []).clump(2).do { |pair|
			((pair[0] == mid) or: { pair[1] == mid }).if {
				var s = ~modules[pair[0].asSymbol], d = ~modules[pair[1].asSymbol];
				(s.notNil and: { d.notNil }).if {
					var dl = min((~layerOf[pair[1].asSymbol] ? lyr), ~layerGroups.size - 1);
					~cords = ~cords.add(Synth.before(~layerGroups[dl], ("cord_" ++ ~nchan).asSymbol,
						[\inb, s[\outBus].index, \outb, d[\inBus].index, \gain, 1, \fade, dur]));
				};
			};
		};
		(~graphTerminals ? []).includes(mid).if {
			~cords = ~cords.add(Synth.tail(~gMix, ("cord_" ++ ~nchan).asSymbol,
				[\inb, info[\outBus].index, \outb, ~masterBus.index, \gain, (-12.dbamp), \fade, dur]));
		};
	};
};

// Gapless rewire: crossfade the OLD cords out while the NEW cords fade in over
// `dur` seconds. The modules (and their buses/clears) are untouched — only the
// routing morphs — so the audio never drops out. Reorder groups to the new order
// up front so the new cords are same-block correct; the old (fading) cords may run
// a block late on changed edges, which is inaudible as they fade.
~morphGraph = { |dur = 2.0|
	var oldCords = ~cords, layerOf;
	oldCords.do { |c| c.set(\lagTime, dur, \xf, 0) };     // fade old routing out
	~cords = [];
	// re-layer into fresh ParGroups (reparenting live groups is glitch-free). The
	// old cords keep pointing at the same per-module buses, so they still carry
	// audio while they fade — any block-lateness from their now-earlier position is
	// inaudible under the crossfade, exactly as before.
	layerOf = ~applyLayers.value;
	(~graphEdges ? []).clump(2).do { |pair|
		var s = ~modules[pair[0].asSymbol], d = ~modules[pair[1].asSymbol];
		(s.notNil and: { d.notNil }).if {
			~cords = ~cords.add(Synth.before(~layerGroups[layerOf[pair[1].asSymbol]],  // fade new routing in
				("cord_" ++ ~nchan).asSymbol,
				[\inb, s[\outBus].index, \outb, d[\inBus].index, \gain, 1, \fade, dur]));
		};
	};
	(~graphTerminals ? []).do { |mid|
		var info = ~modules[mid.asSymbol];
		info.notNil.if {
			~cords = ~cords.add(Synth.tail(~gMix, ("cord_" ++ ~nchan).asSymbol,
				[\inb, info[\outBus].index, \outb, ~masterBus.index, \gain, (-12.dbamp), \fade, dur]));
		};
	};
	SystemClock.sched(dur + 0.15, { oldCords.do { |c| c.free }; nil });   // reap faded cords
};

~freeAll = {
	~modules.do { |info| info[\group].free; info[\outBus].free; info[\inBus].free };
	~modules = IdentityDictionary.new;
	(~clears ? []).do(_.free); (~cords ? []).do(_.free);
	(~layerGroups ? []).do(_.free);
	~clears = []; ~cords = []; ~layerGroups = []; ~layerOf = IdentityDictionary.new;
	~analysisSynth.notNil.if { ~analysisSynth.free };
};

// Free EVERY module synth/group (orphans included) but keep master + analysis.
// The controller reuses module ids (molly1, molly2 ...) across patches, so a
// single missed /module/free leaves an orphan synth still sounding but unknown
// to the model/grid. Teardown calls this to clear all of them at the source.
~freeModules = {
	~modules.do { |info| info[\group].free; info[\outBus].free; info[\inBus].free };
	~modules = IdentityDictionary.new;
	~dx7mods = IdentityDictionary.new;
	(~clears ? []).do(_.free); (~cords ? []).do(_.free);
	(~layerGroups ? []).do(_.free);
	~clears = []; ~cords = []; ~layerGroups = []; ~layerOf = IdentityDictionary.new;
};

~boot = { |sr, block, channels|
	~nchan = channels;
	s.options.numOutputBusChannels = channels;
	s.options.numInputBusChannels = channels;
	s.options.sampleRate = sr;
	s.options.blockSize = block;
	s.options.memSize = 256 * 1024;
	s.options.maxNodes = 4096;
	s.waitForBoot({
		// DISTORT: a Chebyshev transfer-function buffer for the Shaper waveshaper
		// (rich, controlled harmonic series). Allocated before ~buildDefs so the
		// SynthDef can bake its bufnum; content fills async, ready before audio runs.
		~distChebyBuf = Buffer.alloc(s, 1024, 1);
		~distChebyBuf.cheby([1, 0, 0.4, 0, 0.3, 0, 0.2, 0, 0.15, 0, 0.1]);
		// WAVIARY: a bank of CONSECUTIVE wavetables (same size, wavetable format)
		// for the VOsc morphing voice. The morph axis runs smooth -> rich -> abrasive
		// (pure sine ... buzzing near-noise). Allocated before ~buildDefs so the
		// synthdef bakes the base bufnum + bank size; content fills async.
		~waviaryWaves = [
			[1],                                                                          // 0 pure sine
			Array.fill(16, { |i| var h = i + 1; (h % 2 == 1).if({ 1 / (h * h) }, { 0 }) }), // 1 triangle (soft)
			Array.fill(12, { |i| 1 / (i + 1) }),                                          // 2 warm saw
			Array.fill(40, { |i| 1 / (i + 1) }),                                          // 3 bright saw
			Array.fill(31, { |i| var h = i + 1; (h % 2 == 1).if({ 1 / h }, { 0 }) }),      // 4 square (hollow)
			Array.fill(24, { |i| var h = i + 1; (1 / h) * (1 + (sin(h * 0.9) * 0.85)).abs }), // 5 nasal/formant
			Array.fill(44, { |i| var h = i + 1; ((h % 3 == 0) or: { h % 5 == 0 }).if({ 1 / (h.sqrt) }, { 0.04 / h }) }), // 6 metallic
			Array.fill(64, { |i| 1 / ((i + 1) ** 0.5) })                                  // 7 harsh near-buzz
		];
		~waviaryBufN = ~waviaryWaves.size;
		~waviaryBufs = Buffer.allocConsecutive(~waviaryBufN, s, 2048, 1);
		~waviaryWaves.do { |amps, i|
			~waviaryBufs[i].loadCollection(
				Signal.sineFill(1024, amps, 0.dup(amps.size)).normalize.asWavetable);
		};
		~buildDefs.(channels);
		// patch cord: sum one bus into another (NaN-killed), used for all routing
		// `gain` is the static routing gain. The fade envelope is either:
		//  • `fade`>0  -> Line.kr(0,1,fade): a fade-IN that starts at 0 at synth BIRTH
		//    (reliable regardless of when params are .set — used for grow + morph-in), or
		//  • else      -> sqrt(Ramp.kr(xf,lagTime)): an equal-power, slewable level used
		//    for static cords (xf=1,lagTime=0 -> 1, unchanged) and morph fade-OUTs (set
		//    xf=0,lagTime=dur). sqrt keeps a rewire crossfade constant-power.
		SynthDef(("cord_" ++ channels).asSymbol, { |inb = 0, outb = 0, gain = 1, xf = 1, lagTime = 0, fade = 0|
			var lineFade = Line.kr(0, 1, fade.max(0.001));
			var xfFade = Ramp.kr(xf, lagTime).max(0).sqrt;
			Out.ar(outb, ~killbad.(In.ar(inb, channels)) * gain * Select.kr(fade > 0, [xfFade, lineFade]));
		}).add;
		// bus clear: private audio busses are not auto-zeroed; clear before summing
		SynthDef(("busClear_" ++ channels).asSymbol, { |bus = 0|
			ReplaceOut.ar(bus, Silent.ar(channels));
		}).add;
		// audition test source (patch-view processor audition): a repeating plucked saw
		// (2/sec, varying pitch) over a low drone -> transients + sustain so ANY processor's
		// character is obvious (reverb tails, delay repeats, gate chopping, filter sweeps...).
		SynthDef(("audtone_" ++ channels).asSymbol, { |out = 0, amp = 0.32|
			var trig = Impulse.kr(2);
			var env = EnvGen.kr(Env.perc(0.004, 0.42), trig);
			var pit = TIRand.kr(0, 5, trig).linexp(0, 5, 165, 660);
			var sig = (Saw.ar(pit) * env) + (LFSaw.ar(82.5) * 0.12) + (SinOsc.ar(165) * 0.1);
			Out.ar(out, (~killbad.(sig) * amp) ! channels);
		}).add;
		// audition output: sums a module's out bus straight to the hardware out (bus 0)
		// through its own limiter — BYPASSING the master synth's gain/mute, so audition
		// is heard even when the live patch is silenced by the Play toggle.
		SynthDef(("audout_" ++ channels).asSymbol, { |in = 0|
			Out.ar(0, Limiter.ar(~killbad.(In.ar(in, channels)), 0.45, 0.01));
		}).add;
		// play a (mono) TTS WAV once, straight to the hardware out (bypasses master mute).
		SynthDef(("audsay_" ++ channels).asSymbol, { |buf = 0, amp = 0.9|
			var sig = PlayBuf.ar(1, buf, BufRateScale.kr(buf), doneAction: 2);
			Out.ar(0, (Limiter.ar(~killbad.(sig) * amp, 0.6, 0.005)) ! channels);
		}).add;
		// master loudness guard: NaN/Inf kill + brickwall limiter so runaway
		// feedback can never clip the output (blueprint panic/loudness policy).
		// Master makeup gain into the brickwall limiter. Tunable via \gain (the
		// Move's main volume knob drives it). Captured output is glitch-free; the
		// limiter is the loudness/safety stage.
		SynthDef(("master_" ++ channels).asSymbol, { |in = 0, out = 0, gain = 6, cut = 18000, res = 0|
			/* Ceiling 0.45, NOT ~1.0: our output feeds the schwung shadow mixer
			 * whose slot-0 gain (~1.995) would push a near-full-scale signal to
			 * ~1.9 and hard-clip the DAC (constant clicks). Stay ~0.45 so the
			 * post-shadow peak lands near unity. Pair w/ the lower normalize target. */
			/* gain is LAGGED (40ms): patch swaps fade the master out->rebuild->in,
			 * so a lagged gain makes those ramps smooth instead of a click. */
			/* Global master lowpass (patch-view knobs 3/4): cut = cutoff Hz (lagged to
			 * avoid zipper), res 0..1 -> RLPF rq (1 = open, ->0.12 = resonant). Fully
			 * open by default (18k / 0) so it is transparent until the user sweeps it. */
			var src = ~killbad.(In.ar(in, channels));
			var flt = RLPF.ar(src, cut.lag(0.05).clip(20, 18000), (1 - (res.clip(0, 1) * 0.88)).clip(0.12, 1));
			Out.ar(out, Limiter.ar(flt * gain.lag(0.04), 0.45, 0.01));
		}).add;
		s.sync;
		// DX7: allocate LFO wavetable buffers + load the bundled factory bank once.
		~dx7_allocBufs.(s);
		~dx7_factory.isNil.if { ~dx7_loadFactory.(~dx7afxPath ? "DX7.afx") };
		s.sync;
		// STK rawwave path: set once before any StkInst voice (MALLET/BOWED) spawns.
		Synth(\stkInit);
		s.sync;
		// WTABLE: scan Move's built-in single-cycle wavetable sprite bank + load a default
		// oscillator pair (buf1/buf2 passed at spawn; a silent buffer if the bank is empty).
		~silentBuf ?? { ~silentBuf = Buffer.alloc(s, 1024, 1) };
		~wtSprites = "/opt/move/Dsp/Vector/Sprites/*.wav".pathMatch.sort;
		(~wtSprites.size > 0).if {
			Buffer.readChannel(s, ~wtSprites[0], channels: [0], action: { |b| ~wtBufA = b });
			Buffer.readChannel(s, ~wtSprites[(~wtSprites.size * 0.34).asInteger.clip(0, ~wtSprites.size - 1)],
				channels: [0], action: { |b| ~wtBufB = b });
		};
		// BYTEBEAT: a curated bank of bytebeat expressions; one is pushed to each voice's
		// ByteBeat UGen via the /eval unit command on spawn (see ~moduleBuild + ~bbUgenIndex).
		~bbExprs = [
			"t*(t>>5|t>>8)", "t*(((t>>12)|(t>>8))&(63&(t>>4)))",
			"(t>>6|t|t>>(t>>16))*10+((t>>11)&7)", "t*5&(t>>7)|t*3&(t*4>>10)",
			"t*9&t>>4|t*5&t>>7|t*3&t/1024", "t>>4|t&(t>>5)|t>>7",
			"t&t>>8", "t*(42&t>>10)", "(t*5&t>>7)|(t*3&t>>10)",
			"t*((t>>9|t>>13)&25&t>>6)", "(t>>7|t|t>>6)*10+4*(t&t>>13|t>>6)",
			"t*(t>>11&t>>8&123&t>>3)", "(t*(t>>5|t>>8))>>(t>>16)",
			"t*(t>>7|t>>10)^(t>>5)", "t>>4|t*(t>>12|t>>8)&(63&t>>4)", "(t&t>>12)*(t>>4|t>>8)"
		];
		s.sync;
		~ensureGroups.value;
		~inBus = Bus.audio(s, channels);
		~masterBus = Bus.audio(s, channels);
		~clears = []; ~cords = []; ~layerGroups = []; ~layerOf = IdentityDictionary.new;
		~graphOrder = []; ~graphEdges = []; ~graphTerminals = [];
		// live input into inBus
		SynthDef(\atelierInput, { |out = 0| ReplaceOut.ar(out, SoundIn.ar((0..channels-1))) }).add;
		s.sync;
		Synth.head(~gIn, \atelierInput, [\out, ~inBus.index]);
		~masterSynth = Synth.tail(~gMaster, ("master_" ++ channels).asSymbol, [\in, ~masterBus.index, \out, 0]);
		/* Tap the PRE-gain master bus (not bus 0): the controller can then measure
		 * a new patch's level for normalization while the output is faded to silence
		 * during a click-free swap. */
		~analysisSynth = Synth.tail(~gRec, ("analysis_" ++ channels).asSymbol, [\in, ~masterBus.index]);
		// SAMPLER slot state — TWO SEPARATE BANKS in one index space: slots 0..31 are
		// the RECORDER bank, slots 32..55 are the TRANSFORMERS bank. The engine treats
		// them uniformly (just more slots); the views address disjoint ranges so a
		// recorded take never appears among the transform variations and vice-versa.
		// Each slot: a buffer + rec synth + play synth + frame count. Buffers are
		// allocated ON DEMAND when a slot is recorded (empty slots cost no RAM).
		~sampN = 56;
		~sampBufs = Array.newClear(~sampN); ~sampRecSyn = Array.newClear(~sampN);
		~sampPlaySyn = Array.newClear(~sampN); ~sampFrames = Array.newClear(~sampN);
		~sampMaxFrames = (30 * sr).asInteger;     // 30s max take per slot
		// per-slot params: loop region (0..1), volume (0..~1.3), pan (-1..1),
		// filter cutoff (Hz) + resonance (0..4 MoogFF gain)
		~sampLs = Array.fill(~sampN, 0.0); ~sampLe = Array.fill(~sampN, 1.0);
		~sampVol = Array.fill(~sampN, 1.0); ~sampPan = Array.fill(~sampN, 0.0);
		~sampCut = Array.fill(~sampN, 18000.0); ~sampRes = Array.fill(~sampN, 0.0);
		~sampPit = Array.fill(~sampN, 0.0);   // pitch in semitones, -24..24
		// per-slot step-button FX: an ordered insert chain of real modules. Step button
		// index = position in ~sampFxChain (= the `which` index used by /fxset). Only the
		// ENABLED modules are built (heavy ones like CLOUDS cost nothing when off).
		~sampFxChain = [\gate, \distort, \comb, \clouds];
		~sampNfx = ~sampFxChain.size;
		~sampFxOn = Array.fill(~sampN, { Array.fill(~sampNfx, 0) });        // on/off per slot per fx
		~sampFxP  = Array.fill(~sampN, { Array.fill(~sampNfx, { [] }) });   // last param args per slot per fx
		~sampFxActiveSyn = Array.fill(~sampN, { Array.newClear(~sampNfx) });// which -> live synth (when built)
		~sampFxNodes = Array.fill(~sampN, { [] });  // routing synths to free on rebuild (fx + clears + cord)
		~sampFxBus   = Array.fill(~sampN, { [] });  // intermediate busses to free on rebuild
		~sampPlayBus = Array.newClear(~sampN);      // stable per-slot bus the play synth writes to
		~sampSlotGrp = Array.newClear(~sampN);
		~sampPatchFx = Array.newClear(~sampN);      // Rec+slot: target patch-module id to route this slot THROUGH (or nil)
		~sampPatchCord = Array.newClear(~sampN);    // the ~gModules cord: playBus -> target module inBus
		~sampRelease = 0.5;               // fade-out tail (must match samplerPlay release)
		~built = true;
		s.startAliveThread(0.0, 0.5);   // poll /status so avgCPU / numSynths stay live
		~ctrl.value.sendMsg('/atelier/ready');
		"Atelier engine ready: % ch @ % Hz / % block".format(channels, sr, block).postln;
	});
};

// ---- DX7 voice helpers --------------------------------------------------
// Recompute one node's ~150 FM/envelope args from the module's current preset
// (vr) + that node's note/velocity, and push them onto the persistent synth.
~dx7RecalcNode = { |mid, node|
	var st = ~dx7mods[mid.asSymbol], info = ~modules[mid.asSymbol];
	(st.notNil and: { info.notNil } and: { node < info[\nodes].size }).if {
		var nt = ((st[\notes][node] ? 60) + st[\transpose]).clip(0, 127).round.asInteger;
		var vl = (st[\vels][node] ? 100).clip(0, 127).round.asInteger;
		info[\nodes][node].set(*~dx7_voiceArgs.(st[\vr], nt, vl));
	};
};
~dx7RecalcAll = { |mid|
	var info = ~modules[mid.asSymbol];
	info.notNil.if { info[\nodes].size.do { |n| ~dx7RecalcNode.(mid, n) } };
};
// Rebuild vr for the module's current mode (factory preset or manual controls),
// refresh the global LFO synth, then recompute every voice.
~dx7ApplyVoice = { |mid|
	var st = ~dx7mods[mid.asSymbol];
	st.notNil.if {
		(st[\mode] == 1).if({
			st[\vr] = ~dx7_buildManualVr.(st[\manual]);
		}, {
			(~dx7_factory.notNil).if {
				~dx7_loadPreset.(st[\vr], ~dx7_factory[st[\preset] % (~dx7_factory.size.max(1))]);
			};
		});
		st[\lfo].notNil.if { st[\lfo].set(*~dx7_lfoArgs.(st[\vr])) };
		~dx7RecalcAll.(mid);
	};
};
// Intercept set/setvec for the params that drive a voice recompute (mode, preset,
// transpose, note, velocity, and the manual timbre controls). Everything else —
// amp/pan/enable/gate — is a direct SynthDef arg. Returns true when the param was
// consumed here.
~dx7HandleSet = { |mid, st, pname, node, value|
	var consumed = true;
	pname.asString.switch(
		"mode",      { st[\mode] = value.round.asInteger; ~dx7ApplyVoice.(mid); },
		"preset",    { st[\preset] = value.round.asInteger; (st[\mode] == 0).if { ~dx7ApplyVoice.(mid) }; },
		"transpose", { st[\transpose] = value.round.asInteger; ~dx7RecalcAll.(mid); },
		"note", {
			(node < 0).if(
				{ st[\notes].size.do { |n| st[\notes][n] = value; ~dx7RecalcNode.(mid, n) } },
				{ (node < st[\notes].size).if { st[\notes][node] = value; ~dx7RecalcNode.(mid, node) } });
		},
		"velocity", {
			(node < 0).if(
				{ st[\vels].size.do { |n| st[\vels][n] = value; ~dx7RecalcNode.(mid, n) } },
				{ (node < st[\vels].size).if { st[\vels][node] = value; ~dx7RecalcNode.(mid, node) } });
		},
		{
			// manual timbre param? store it; recompute only when manual mode is live.
			~dx7_isManual.(pname).if({
				st[\manual][pname.asSymbol] = value;
				(st[\mode] == 1).if { ~dx7ApplyVoice.(mid) };
			}, { consumed = false });
		});
	consumed;
};
~dx7HandleSetVec = { |mid, st, pname, values|
	var consumed = true;
	pname.asString.switch(
		"note",     { values.do { |v, i| (i < st[\notes].size).if { st[\notes][i] = v; ~dx7RecalcNode.(mid, i) } }; },
		"velocity", { values.do { |v, i| (i < st[\vels].size).if { st[\vels][i] = v; ~dx7RecalcNode.(mid, i) } }; },
		{ consumed = false });
	consumed;
};

// ---- module lifecycle ---------------------------------------------------
~moduleNew = { |mid, type, channels, nodes, laneIndex|
	type = type.asString;   // OSC string args arrive as Symbols
	~built.if {
		var grp, inBus, outBus, defName, nodeSynths;
		~modules[mid.asSymbol].notNil.if { ~moduleFree.(mid) };  // idempotent rebuild
		grp = Group.tail(~gModules);
		inBus = Bus.audio(s, ~nchan);
		outBus = Bus.audio(s, ~nchan);
		defName = (type.toLower ++ "_" ++ ~nchan).asSymbol;
		(type.toUpper == "DX7").if({
			// DX7: per-module pitch/amp mod buses + a global LFO synth at the group
			// head (writes the buses before the voices read them). Voices get the
			// bus indices; preset 0 is loaded immediately so they self-sound.
			var pbus = Bus.audio(s, 1), abus = Bus.audio(s, 1), lfo;
			lfo = Synth.head(grp, \InfEfx, [\pmodbus, pbus.index, \amodbus, abus.index,
				\lfo_wave, ~dx7_wavebufs[4].bufnum]);
			nodeSynths = Array.fill(nodes, {
				Synth.tail(grp, defName, [\out, outBus.index, \pmodbus, pbus.index, \amodbus, abus.index]);
			});
			~modules[mid.asSymbol] = (group: grp, nodes: nodeSynths, type: type, inBus: inBus, outBus: outBus);
			~dx7mods[mid.asSymbol] = (vr: ~dx7_freshVr.(), pbus: pbus, abus: abus, lfo: lfo,
				mode: 0, preset: 0, transpose: 0, manual: IdentityDictionary.new,
				notes: Array.fill(nodes, 60), vels: Array.fill(nodes, 100));
			~dx7ApplyVoice.(mid);
		}, {
			(type.toUpper == "FMTONE").if({
				// FMTONE: generative FM voice. Reuses the per-module vibrato LFO (InfEfx
				// -> pmodbus) like DX7, but its params are set DIRECTLY (no preset map).
				var pbus = Bus.audio(s, 1), abus = Bus.audio(s, 1), lfo;
				lfo = Synth.head(grp, \InfEfx, [\pmodbus, pbus.index, \amodbus, abus.index,
					\lfo_wave, ~dx7_wavebufs[4].bufnum]);
				nodeSynths = Array.fill(nodes, {
					Synth.tail(grp, defName, [\out, outBus.index, \pmodbus, pbus.index, \amodbus, abus.index]);
				});
				~modules[mid.asSymbol] = (group: grp, nodes: nodeSynths, type: type, inBus: inBus,
					outBus: outBus, pbus: pbus, abus: abus, lfo: lfo);
			}, {
				(type.toUpper == "WTABLE").if({
					// WTABLE: each voice reads a wavetable-sprite buffer pair (buf1/buf2).
					nodeSynths = Array.fill(nodes, {
						Synth.tail(grp, defName, [\out, outBus.index,
							\buf1, ~wtBufA.notNil.if({ ~wtBufA.bufnum }, { ~silentBuf.bufnum }),
							\buf2, ~wtBufB.notNil.if({ ~wtBufB.bufnum }, { ~silentBuf.bufnum })]);
					});
				}, {
					(type.toUpper == "BYTEBEAT").if({
						// BYTEBEAT: expression pushed to each voice's ByteBeat UGen via /eval
						// right after /s_new (ordered on the server).
						nodeSynths = Array.fill(nodes, { |i|
							var syn = Synth.tail(grp, defName, [\out, outBus.index]);
							(~bbUgenIndex.notNil and: { ~bbExprs.size > 0 }).if {
								s.sendMsg('/u_cmd', syn.nodeID, ~bbUgenIndex, '/eval',
									~bbExprs.wrapAt(i));
							};
							syn;
						});
					}, {
						nodeSynths = Array.fill(nodes, {
							Synth.tail(grp, defName, [\out, outBus.index, \in, inBus.index]);
						});
					});
				});
				~modules[mid.asSymbol] = (group: grp, nodes: nodeSynths, type: type, inBus: inBus, outBus: outBus);
			});
		});
	};
};

~moduleFree = { |mid|
	var info = ~modules[mid.asSymbol];
	var st = ~dx7mods[mid.asSymbol];
	info.notNil.if {
		info[\group].free;
		info[\inBus].free;
		info[\outBus].free;
		info[\pbus].notNil.if { info[\pbus].free; info[\abus].free };   // FMTONE vibrato buses
		~modules.removeAt(mid.asSymbol);
	};
	st.notNil.if {
		st[\pbus].free; st[\abus].free;
		~dx7mods.removeAt(mid.asSymbol);
	};
};

~moduleNodes = { |mid, count|
	var info = ~modules[mid.asSymbol];
	var st = ~dx7mods[mid.asSymbol];
	info.notNil.if {
		var cur = info[\nodes].size;
		var defName = (info[\type].toLower ++ "_" ++ ~nchan).asSymbol;
		(count > cur).if {
			(count - cur).do {
				st.notNil.if({
					info[\nodes] = info[\nodes].add(Synth.tail(info[\group], defName,
						[\out, info[\outBus].index, \pmodbus, st[\pbus].index, \amodbus, st[\abus].index]));
					st[\notes] = st[\notes].add(60); st[\vels] = st[\vels].add(100);
					~dx7RecalcNode.(mid, info[\nodes].size - 1);
				}, {
					info[\pbus].notNil.if({                                  // FMTONE generative voice
						info[\nodes] = info[\nodes].add(Synth.tail(info[\group], defName,
							[\out, info[\outBus].index, \pmodbus, info[\pbus].index, \amodbus, info[\abus].index]));
					}, {
						info[\nodes] = info[\nodes].add(Synth.tail(info[\group], defName,
							[\out, info[\outBus].index, \in, info[\inBus].index]));
					});
				});
			};
		} {
			(cur - count).do {
				info[\nodes].pop.free;
				st.notNil.if { st[\notes].pop; st[\vels].pop };
			};
		};
	};
};

// ---- parameter updates --------------------------------------------------
~setParam = { |mid, pname, node, value|
	var info = ~modules[mid.asSymbol];
	var st = ~dx7mods[mid.asSymbol];
	info.notNil.if {
		(st.notNil and: { ~dx7HandleSet.(mid, st, pname, node, value) }).if({
			// consumed: a DX7 recompute param (preset/note/velocity/...)
		}, {
			(node < 0).if {
				info[\nodes].do { |syn| syn.set(pname.asSymbol, value) };  // global -> all nodes
			} {
				(node < info[\nodes].size).if { info[\nodes][node].set(pname.asSymbol, value) };
			};
		});
	};
};

~setVec = { |mid, pname, values|
	var info = ~modules[mid.asSymbol];
	var st = ~dx7mods[mid.asSymbol];
	info.notNil.if {
		(st.notNil and: { ~dx7HandleSetVec.(mid, st, pname, values) }).if({
			// consumed: per-node note/velocity vector -> recompute
		}, {
			values.do { |v, i| (i < info[\nodes].size).if { info[\nodes][i].set(pname.asSymbol, v) } };
		});
	};
};

// ---- OSC responders -----------------------------------------------------
// ---- SAMPLER manager: per-slot record/playback of the master mix ----
~sampRecStart = { |slot|
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		~sampPlaySyn[slot].notNil.if { ~sampPlaySyn[slot].free; ~sampPlaySyn[slot] = nil };
		~sampRecSyn[slot].notNil.if { ~sampRecSyn[slot].free; ~sampRecSyn[slot] = nil };
		~sampBufs[slot].notNil.if { ~sampBufs[slot].free };
		~sampBufs[slot] = Buffer.alloc(s, ~sampMaxFrames, ~nchan, { |b|
			~sampRecSyn[slot] = Synth.tail(~gRec, ("samplerRec_" ++ ~nchan).asSymbol,
				[\buf, b.bufnum, \in, ~masterBus.index]);
		});
	};
};
~sampRecStop = { |slot, frames|
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		~sampRecSyn[slot].notNil.if { ~sampRecSyn[slot].free; ~sampRecSyn[slot] = nil };
		~sampFrames[slot] = frames.asInteger.clip(0, ~sampMaxFrames);
	};
};
// CDP source capture: record the live master bus for `dur` seconds into a scratch
// buffer, then write it to `path` as a WAV. The headless controller polls for the
// file to appear + settle (shared filesystem), then runs the CDP job — so no
// engine->controller reply is needed. Deletes the file first so the poll can't see
// a stale one from the previous capture.
// ---- patch-view palette audition -----------------------------------------
// Build a throwaway module (id \_aud) with ~moduleNew (so FMTONE's LFO buses etc. are
// set up correctly), route its output to master, and — for a processor — feed a test
// tone through its input with sensible defaults (enable/wet on). Frees on stop/switch.
~audTone = nil; ~audOut = nil; ~audSayBuf = nil; ~audSaySyn = nil;
~auditionStop = {
	~audOut.notNil.if { ~audOut.free; ~audOut = nil };
	~audTone.notNil.if { ~audTone.free; ~audTone = nil };
	~audSaySyn.notNil.if { ~audSaySyn.free; ~audSaySyn = nil };
	~audSayBuf.notNil.if { ~audSayBuf.free; ~audSayBuf = nil };
	~modules[\_aud].notNil.if { ~moduleFree.(\_aud) };
};
// speak a processor's name: play the WAV the controller just rendered with espeak-ng.
~auditionSay = { |path|
	var p = path.asString;
	~auditionStop.value;
	File.exists(p).if {
		~audSayBuf = Buffer.read(s, p, action: { |b|
			~audSaySyn = Synth.tail(~gRec, ("audsay_" ++ ~nchan).asSymbol, [\buf, b.bufnum]);
		});
	};
};
// speak a generator's name OVER a sounding audition: same WAV playback but WITHOUT
// ~auditionStop, so the self-sounding voice (audout) keeps running underneath. audsay
// and audout both sum to bus 0, so they simply mix. Only a prior spoken clip is freed.
~auditionSayOver = { |path|
	var p = path.asString;
	~audSaySyn.notNil.if { ~audSaySyn.free; ~audSaySyn = nil };
	~audSayBuf.notNil.if { ~audSayBuf.free; ~audSayBuf = nil };
	File.exists(p).if {
		~audSayBuf = Buffer.read(s, p, action: { |b|
			~audSaySyn = Synth.tail(~gRec, ("audsay_" ++ ~nchan).asSymbol, [\buf, b.bufnum]);
		});
	};
};
~auditionGen = { |type|
	~auditionStop.value;
	~moduleNew.(\_aud, type, ~nchan, 1, 0);
	~modules[\_aud].notNil.if {
		// straight to the hardware out via audout (bypasses the master mute)
		~audOut = Synth.tail(~gRec, ("audout_" ++ ~nchan).asSymbol,
			[\in, ~modules[\_aud][\outBus].index]);
	};
};
// (processor audition is now spoken by the host TTS in ui.js; kept for completeness)
~auditionFx = { |type|
	~auditionStop.value;
	~moduleNew.(\_aud, type, ~nchan, 1, 0);
	~modules[\_aud].notNil.if {
		~audTone = Synth.head(~modules[\_aud][\group], ("audtone_" ++ ~nchan).asSymbol,
			[\out, ~modules[\_aud][\inBus].index]);
		~modules[\_aud][\nodes].do { |n| n.set(\enable, 1, \wet, 1, \bypass, 0) };
		~audOut = Synth.tail(~gRec, ("audout_" ++ ~nchan).asSymbol,
			[\in, ~modules[\_aud][\outBus].index]);
	};
};

~cdpSrcBuf = nil; ~cdpRecSyn = nil;
~cdpCapture = { |dur, path|
	var frames, p = path.asString, srate = (s.sampleRate ? s.options.sampleRate ? 44100);
	dur = dur.asFloat.clip(0.5, 10.0);
	frames = (dur * srate).asInteger;
	File.delete(p);
	~cdpRecSyn.notNil.if { ~cdpRecSyn.free; ~cdpRecSyn = nil };
	~cdpSrcBuf.notNil.if { ~cdpSrcBuf.free; ~cdpSrcBuf = nil };
	~cdpSrcBuf = Buffer.alloc(s, frames, ~nchan, { |b|
		~cdpRecSyn = Synth.tail(~gRec, ("samplerRec_" ++ ~nchan).asSymbol,
			[\buf, b.bufnum, \in, ~masterBus.index]);
		SystemClock.sched(dur + 0.05, {
			~cdpRecSyn.notNil.if { ~cdpRecSyn.free; ~cdpRecSyn = nil };
			~cdpSrcBuf.write(p, "wav", "float", frames, 0);
			nil;   // one-shot
		});
	});
};
// Tear down a slot's whole chain (group frees play + FX + clears + cord; busses
// freed explicitly). Leaves the per-slot FX on/param state intact for a re-play.
~sampFreeChain = { |slot|
	~sampSlotGrp[slot].notNil.if { ~sampSlotGrp[slot].free; ~sampSlotGrp[slot] = nil };
	~sampPlayBus[slot].notNil.if { ~sampPlayBus[slot].free; ~sampPlayBus[slot] = nil };
	~sampPatchCord[slot].notNil.if { ~sampPatchCord[slot].free; ~sampPatchCord[slot] = nil };
	(~sampFxBus[slot] ? []).do { |b| b.free };
	~sampFxBus[slot] = []; ~sampFxNodes[slot] = [];
	~sampFxActiveSyn[slot] = Array.newClear(~sampNfx); ~sampPlaySyn[slot] = nil;
};
// (Re)build ONLY the enabled FX after the play synth, gapless. samplerPlay keeps
// running into ~sampPlayBus; this rewires play -> [active FX in chain order] ->
// master (or a straight cord when no FX are on). Heavy modules (CLOUDS) only exist
// while engaged, so disabled FX cost nothing.
~sampRebuildFx = { |slot|
	var grp = ~sampSlotGrp[slot], playBus = ~sampPlayBus[slot], oldNodes, oldBuses, active, nodes, buses, src;
	(grp.notNil and: { playBus.notNil }).if {
		oldNodes = ~sampFxNodes[slot] ? []; oldBuses = ~sampFxBus[slot] ? [];
		active = (0 .. (~sampNfx - 1)).select { |k| ~sampFxOn[slot][k] == 1 };
		nodes = []; buses = []; src = playBus;
		~sampFxActiveSyn[slot] = Array.newClear(~sampNfx);
		s.makeBundle(nil, {                         // free old + build new atomically (no gap)
			oldNodes.do { |n| n.free };
			active.do { |k, idx|
				var last = (idx == (active.size - 1)), outBus, syn;
				outBus = last.if({ ~masterBus }, {
					var b = Bus.audio(s, ~nchan); buses = buses.add(b);
					nodes = nodes.add(Synth.head(grp, ("busClear_" ++ ~nchan).asSymbol, [\bus, b.index]));
					b;
				});
				syn = Synth.tail(grp, (~sampFxChain[k].asString ++ "_" ++ ~nchan).asSymbol,
					[\in, src.index, \out, outBus.index, \enable, 1, \wet, 1, \bypass, 0] ++ ~sampFxP[slot][k]);
				nodes = nodes.add(syn); ~sampFxActiveSyn[slot][k] = syn; src = outBus;
			};
			active.isEmpty.if {
				nodes = nodes.add(Synth.tail(grp, ("cord_" ++ ~nchan).asSymbol,
					[\inb, playBus.index, \outb, ~masterBus.index]));
			};
		});
		~sampFxNodes[slot] = nodes; ~sampFxBus[slot] = buses;
		oldBuses.do { |b| b.free };                // release old bus indices after new allocated
	};
};
// Rec+slot: make the sample a SOURCE inside the patch graph, exactly like a generator.
// A generator lives in ~gModules and its output is cord-summed into an FX's input, in the
// same block. Sample playback normally lives in ~gSampPlay (AFTER the modules), so it can
// never feed them. When wired we therefore MOVE the slot's play group into ~gModules
// (head) so the sample is generated before the FX read, then cord its playBus into the
// target module's input (gain 1, like a generator edge) — no separate dry path. When
// unwired we move it back to ~gSampPlay and restore the normal dry -> master routing.
~sampSyncPatchRoute = { |slot|
	var tgt = ~sampPatchFx[slot], info = tgt.notNil.if({ ~modules[tgt] }, { nil });
	var pb = ~sampPlayBus[slot], grp = ~sampSlotGrp[slot];
	~sampPatchCord[slot].notNil.if { ~sampPatchCord[slot].free; ~sampPatchCord[slot] = nil };
	(grp.notNil and: { pb.notNil }).if {
		(info.notNil).if({
			// WIRED: sample is a graph source. Drop the dry/master routing, run the play
			// group among the modules (head), and edge it into the target module's input.
			// playBus -> target module inBus. The play group runs at ~gModules head
			// (before every layer), so place the cord before the TARGET'S LAYER
			// ParGroup in the serial parent — never inside a ParGroup, where its
			// order vs the module would be undefined under supernova.
			var tl = (~layerOf ? IdentityDictionary.new)[tgt], lg = ~layerGroups ? [];
			(~sampFxNodes[slot] ? []).do { |n| n.free };
			~sampFxNodes[slot] = []; ~sampFxActiveSyn[slot] = Array.newClear(~sampNfx);
			grp.moveToHead(~gModules);
			~sampPatchCord[slot] = (tl.notNil and: { tl < lg.size }).if({
				Synth.before(lg[tl], ("cord_" ++ ~nchan).asSymbol,
					[\inb, pb.index, \outb, info[\inBus].index]);
			}, {
				Synth.before(info[\group], ("cord_" ++ ~nchan).asSymbol,   // pre-layer fallback
					[\inb, pb.index, \outb, info[\inBus].index]);
			});
		}, {
			// UNWIRED: back to a normal sampler take (play group after the patch mix).
			grp.moveToHead(~gSampPlay);
			~sampRebuildFx.(slot);
		});
	};
};
// Rec+slot: (un)wire a sample slot INTO the patch graph. midStr = target module id
// ("" = off). Re-sent by the controller after every patch rebuild so it points at a live
// module.
~sampWirePatch = { |slot, midStr|
	var m = midStr.asString;
	(slot >= 0 and: { slot < ~sampN }).if {
		~sampPatchFx[slot] = (m.size > 0).if({ m.asSymbol }, { nil });
		~sampSyncPatchRoute.(slot);
	};
};
~sampPlayStart = { |slot|
	var buf, fr, grp, playBus;
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		buf = ~sampBufs[slot]; fr = ~sampFrames[slot] ? 0;
		(buf.notNil and: { fr > 0 }).if {
			~sampFreeChain.(slot);                  // re-play: drop old chain immediately, no fade
			grp = Group.head(~gSampPlay);
			playBus = Bus.audio(s, ~nchan);
			~sampSlotGrp[slot] = grp; ~sampPlayBus[slot] = playBus;
			~sampFxNodes[slot] = []; ~sampFxBus[slot] = [];
			Synth.head(grp, ("busClear_" ++ ~nchan).asSymbol, [\bus, playBus.index]);
			~sampPlaySyn[slot] = Synth.tail(grp, ("samplerPlay_" ++ ~nchan).asSymbol,
				[\buf, buf.bufnum, \frames, fr, \out, playBus.index,
				 \loopStart, ~sampLs[slot], \loopEnd, ~sampLe[slot],
				 \amp, ~sampVol[slot], \pan, ~sampPan[slot],
				 \cutoff, ~sampCut[slot], \res, ~sampRes[slot], \pitch, ~sampPit[slot]]);
			~sampRebuildFx.(slot);                  // wire up whatever FX are enabled
			~sampSyncPatchRoute.(slot);             // if this slot is wired into the patch, route it there
		};
	};
};
// Per-slot insert FX: which = index into ~sampFxChain, on = 0/1, pairs = flat
// [name, val, ...]. Store; if the on-state changed, rebuild the chain composition,
// else (re-randomize while engaged) set the live module's params.
~sampSetFx = { |slot, which, on, pairs|
	var args = [], wasOn;
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } and: { which >= 0 and: { which < ~sampNfx } } }).if {
		pairs.pairsDo { |k, v| args = args ++ [k.asSymbol, v] };
		wasOn = ~sampFxOn[slot][which];
		~sampFxOn[slot][which] = on; ~sampFxP[slot][which] = args;
		~sampPlaySyn[slot].notNil.if {
			(wasOn != on).if {
				~sampRebuildFx.(slot);
			} {
				~sampFxActiveSyn[slot][which].notNil.if { ~sampFxActiveSyn[slot][which].set(*args) };
			};
		};
	};
};
// CTRL-ALL: set the resonant filter (cutoff Hz, res 0..4) on EVERY slot + playing.
~sampSetFilter = { |cutoff, res|
	(~sampBufs.notNil).if {
		~sampN.do { |i| ~sampCut[i] = cutoff; ~sampRes[i] = res };
		~sampPlaySyn.do { |syn| syn.notNil.if { syn.set(\cutoff, cutoff, \res, res) } };
	};
};
// CTRL-ALL: set the loop region (0..1) on EVERY slot + push to all playing.
~sampSetLoop = { |start, end|
	(~sampBufs.notNil).if {
		~sampN.do { |i| ~sampLs[i] = start.clip(0, 1); ~sampLe[i] = end.clip(0, 1) };
		~sampPlaySyn.do { |syn| syn.notNil.if { syn.set(\loopStart, start.clip(0, 1), \loopEnd, end.clip(0, 1)) } };
	};
};
// Per-slot params (slot-selection mode): vol, pan, loop, filter cut/res, pitch.
~sampSetSlot = { |slot, vol, pan, ls, le, cutoff, res, pitch|
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		~sampVol[slot] = vol.max(0); ~sampPan[slot] = pan.clip(-1, 1);
		~sampLs[slot] = ls.clip(0, 1); ~sampLe[slot] = le.clip(0, 1);
		~sampCut[slot] = cutoff; ~sampRes[slot] = res; ~sampPit[slot] = pitch.clip(-24, 24);
		~sampPlaySyn[slot].notNil.if {
			~sampPlaySyn[slot].set(\amp, ~sampVol[slot], \pan, ~sampPan[slot],
				\loopStart, ~sampLs[slot], \loopEnd, ~sampLe[slot],
				\cutoff, ~sampCut[slot], \res, ~sampRes[slot], \pitch, ~sampPit[slot]);
		};
	};
};
// CTRL-ALL pitch: set every slot's pitch + push to all playing.
~sampSetPitchAll = { |pitch|
	(~sampBufs.notNil).if {
		~sampN.do { |i| ~sampPit[i] = pitch.clip(-24, 24) };
		~sampPlaySyn.do { |syn| syn.notNil.if { syn.set(\pitch, pitch.clip(-24, 24)) } };
	};
};
~sampPlayStop = { |slot|
	var grp, playBus, fxBuses;
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		~sampPlaySyn[slot].notNil.if {
			// gate -> 0: the play synth fades out over `release`; capture THIS chain and
			// reap it (group + busses) after the tail so the FX hear the fade-out too.
			~sampPlaySyn[slot].set(\gate, 0);
			grp = ~sampSlotGrp[slot]; playBus = ~sampPlayBus[slot]; fxBuses = ~sampFxBus[slot] ? [];
			~sampPlaySyn[slot] = nil; ~sampSlotGrp[slot] = nil; ~sampPlayBus[slot] = nil;
			~sampFxBus[slot] = []; ~sampFxNodes[slot] = []; ~sampFxActiveSyn[slot] = Array.newClear(~sampNfx);
			SystemClock.sched(~sampRelease + 0.2, {
				grp.notNil.if { grp.free }; playBus.notNil.if { playBus.free };
				fxBuses.do { |b| b.free }; nil
			});
		};
	};
};
~sampFree = { |slot|
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		~sampFreeChain.(slot);                                  // group + busses + synth refs
		~sampFxOn[slot] = Array.fill(~sampNfx, 0);              // clear stored FX state
		~sampFxP[slot] = Array.fill(~sampNfx, { [] });
		~sampRecSyn[slot].notNil.if { ~sampRecSyn[slot].free; ~sampRecSyn[slot] = nil };
		~sampBufs[slot].notNil.if { ~sampBufs[slot].free; ~sampBufs[slot] = nil };
		~sampFrames[slot] = nil;
	};
};

// Performance save/load of a slot's sample audio. Save writes the recorded portion
// to a WAV; load reads a WAV back in and (optionally) resumes playback once it lands
// (Buffer.read is async, so per-slot settings/FX must be pushed BEFORE the load).
~sampSaveBuf = { |slot, path|
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		(~sampBufs[slot].notNil and: { (~sampFrames[slot] ? 0) > 0 }).if {
			~sampBufs[slot].write(path.asString, "wav", "float", ~sampFrames[slot], 0);
		};
	};
};
~sampLoadBuf = { |slot, path, autoplay|
	var oldBuf;
	(~sampBufs.notNil and: { slot >= 0 and: { slot < ~sampN } }).if {
		// Gapless swap: fade the OLD take out (it keeps reading its own buffer by
		// bufnum while it releases), read the NEW take into a fresh slot buffer, and
		// (auto)play it — so old fades down as new fades up. Free the old buffer only
		// after its release tail, never under the still-sounding old synth.
		oldBuf = ~sampBufs[slot];
		~sampPlaySyn[slot].notNil.if { ~sampPlayStop.(slot) };
		~sampBufs[slot] = Buffer.read(s, path.asString, action: { |b|
			~sampFrames[slot] = b.numFrames;
			(autoplay > 0).if { ~sampPlayStart.(slot) };
		});
		oldBuf.notNil.if { SystemClock.sched(~sampRelease + 0.4, { oldBuf.free; nil }) };
	};
};

// ---- PERFORMANCE RECORDER: long-form master-OUTPUT capture to disk (per project).
// Manual start/stop (the controller caps the length); DiskOut streams bus 0 to a WAV.
~recBuf = nil; ~recSynth = nil; ~recPlayBuf = nil; ~recPlaySyn = nil;
~recStop = {
	~recSynth.notNil.if { ~recSynth.free; ~recSynth = nil };          // stop writing first
	~recBuf.notNil.if { ~recBuf.close; ~recBuf.free; ~recBuf = nil }; // close finalizes the WAV
};
~recStart = { |path|
	~recStop.value;
	~recBuf = Buffer.alloc(s, 65536, 2);
	~recSynth = Synth.basicNew(\atelierRec, s);
	// open the file (leaveOpen) then start DiskOut via the write's completion message
	~recBuf.write(path.asString, "wav", "int16", 0, 0, true,
		~recSynth.newMsg(~gRec, [\buf, ~recBuf], \addToTail));
};
~recPlayStop = {
	~recPlaySyn.notNil.if { ~recPlaySyn.free; ~recPlaySyn = nil };
	~recPlayBuf.notNil.if { ~recPlayBuf.close; ~recPlayBuf.free; ~recPlayBuf = nil };
};
~recPlayStart = { |path|
	~recPlayStop.value;
	~recPlayBuf = Buffer.cueSoundFile(s, path.asString, 0, 2);
	~recPlaySyn = Synth.tail(~gRec, \atelierRecPlay, [\buf, ~recPlayBuf]);
	~recPlaySyn.register;
	~recPlaySyn.onFree({
		~recPlayBuf.notNil.if { ~recPlayBuf.close; ~recPlayBuf.free; ~recPlayBuf = nil };
		~recPlaySyn = nil;
	});
};

~defs = [
	OSCdef(\boot, { |m| ~boot.(m[1], m[2], m[3]) }, '/atelier/boot'),
	OSCdef(\sampSave, { |m| ~sampSaveBuf.(m[1].asInteger, m[2]) }, '/atelier/sampler/save'),
	OSCdef(\sampLoad, { |m| ~sampLoadBuf.(m[1].asInteger, m[2], m[3].asInteger) }, '/atelier/sampler/load'),
	OSCdef(\cdpCapture, { |m| ~cdpCapture.(m[1].asFloat, m[2]) }, '/atelier/cdp/capture'),
	OSCdef(\sampPatchFx, { |m| ~sampWirePatch.(m[1].asInteger, m[2]) }, '/atelier/sampler/patchfx'),
	OSCdef(\audGen, { |m| ~auditionGen.(m[1]) }, '/atelier/audition/gen'),
	OSCdef(\audFx, { |m| ~auditionFx.(m[1]) }, '/atelier/audition/fx'),
	OSCdef(\audSay, { |m| ~auditionSay.(m[1]) }, '/atelier/audition/say'),
	OSCdef(\audSayOver, { |m| ~auditionSayOver.(m[1]) }, '/atelier/audition/sayover'),
	OSCdef(\audStop, { |m| ~auditionStop.value }, '/atelier/audition/stop'),
	OSCdef(\sampRec, { |m| ~sampRecStart.(m[1].asInteger) }, '/atelier/sampler/rec'),
	OSCdef(\sampRecStop, { |m| ~sampRecStop.(m[1].asInteger, m[2].asInteger) }, '/atelier/sampler/recstop'),
	OSCdef(\sampPlay, { |m| ~sampPlayStart.(m[1].asInteger) }, '/atelier/sampler/play'),
	OSCdef(\sampStop, { |m| ~sampPlayStop.(m[1].asInteger) }, '/atelier/sampler/stop'),
	OSCdef(\wrRecStart, { |m| ~recStart.(m[1]) }, '/atelier/rec/start'),
	OSCdef(\wrRecStop, { |m| ~recStop.value }, '/atelier/rec/stop'),
	OSCdef(\wrRecPlay, { |m| ~recPlayStart.(m[1]) }, '/atelier/rec/play'),
	OSCdef(\wrRecPlayStop, { |m| ~recPlayStop.value }, '/atelier/rec/playstop'),
	OSCdef(\sampFree, { |m| ~sampFree.(m[1].asInteger) }, '/atelier/sampler/free'),
	OSCdef(\sampLoop, { |m| ~sampSetLoop.(m[1], m[2]) }, '/atelier/sampler/looprange'),
	OSCdef(\sampSlot, { |m| ~sampSetSlot.(m[1].asInteger, m[2], m[3], m[4], m[5], m[6], m[7], m[8]) }, '/atelier/sampler/slot'),
	OSCdef(\sampFilt, { |m| ~sampSetFilter.(m[1], m[2]) }, '/atelier/sampler/filterall'),
	OSCdef(\sampPitch, { |m| ~sampSetPitchAll.(m[1]) }, '/atelier/sampler/pitchall'),
	OSCdef(\sampFxSet, { |m| ~sampSetFx.(m[1].asInteger, m[2].asInteger, m[3].asInteger, m[4..]) }, '/atelier/sampler/fxset'),
	OSCdef(\modnew, { |m| ~moduleNew.(m[1], m[2], m[3], m[4], m[5]) }, '/atelier/module/new'),
	OSCdef(\modfree, { |m| ~moduleFree.(m[1]) }, '/atelier/module/free'),
	OSCdef(\modnodes, { |m| ~moduleNodes.(m[1], m[2]) }, '/atelier/module/nodes'),
	// free-form graph: controller sends order/edges/terminals, then commit
	OSCdef(\graphOrder, { |m| ~graphOrder = m[1..] }, '/atelier/graph/order'),
	OSCdef(\graphEdges, { |m| ~graphEdges = m[1..] }, '/atelier/graph/edges'),
	OSCdef(\graphTerm, { |m| ~graphTerminals = m[1..] }, '/atelier/graph/terminals'),
	OSCdef(\graphCommit, { |m| ~rebuildGraph.value }, '/atelier/graph/commit'),
	OSCdef(\graphMorph, { |m| ~morphGraph.value(m[1] ? 2.0) }, '/atelier/graph/morph'),
	OSCdef(\graphGrow, { |m| ~growGraph.value(m[1], m[2] ? 0.2) }, '/atelier/graph/grow'),
	OSCdef(\bypass, { |m| ~setParam.(m[1], "bypass", -1, m[2]) }, '/atelier/module/bypass'),
	OSCdef(\wet, { |m| ~setParam.(m[1], "wet", -1, m[2]) }, '/atelier/module/wet'),
	OSCdef(\set, { |m| ~setParam.(m[1], m[2], m[3], m[4]) }, '/atelier/set'),
	OSCdef(\setvec, { |m| ~setVec.(m[1], m[2], m[3..]) }, '/atelier/setvec'),
	OSCdef(\panic, { |m|
		~feedback.do { |fb| fb.notNil.if { fb.free } };
		~feedback = IdentityDictionary.new;
		s.freeAll(~gModules.nodeID);   // nukes layer ParGroups + module groups wholesale
		~layerGroups = []; ~layerOf = IdentityDictionary.new;
		~modules.do { |info| info[\nodes].do { |syn| syn.set(\amp, 0) } };
		"PANIC".postln;
	}, '/atelier/panic'),
	// Free every module synth (orphans included) on patch teardown — see
	// ~freeModules. Guards against reused-id orphans that keep sounding.
	OSCdef(\freemods, { |m| ~freeModules.value }, '/atelier/free_modules'),
	// Master makeup gain (the Move's main volume knob when no pad is held).
	OSCdef(\mastergain, { |m| ~masterSynth.notNil.if { ~masterSynth.set(\gain, m[1]) } }, '/atelier/mastergain'),
		// Global master lowpass (patch-view knobs 3/4): m[1] = cutoff Hz, m[2] = res 0..1.
		OSCdef(\masterfilter, { |m| ~masterSynth.notNil.if { ~masterSynth.set(\cut, m[1], \res, m[2]) } }, '/atelier/masterfilter'),
	OSCdef(\spatial, { |m| /* channel changes require rebuild on next boot */ }, '/atelier/spatial'),
	OSCdef(\reseed, { |m| /* seeds live in the controller; nothing server-side */ }, '/atelier/reseed'),
	// Only confirm readiness once the boot block has actually finished (buses +
	// groups exist). Replying mid-boot makes the controller build_graph against a
	// nil ~masterBus -> 'index' errors. ~built is set at the end of ~boot.
	OSCdef(\ping, { |m| (~built ? false).if { ~ctrl.value.sendMsg('/atelier/ready') } }, '/atelier/ping'),
	OSCdef(\feedback, { |m| /* armed feedback edges: see ~rebuildLane refinement */ }, '/atelier/feedback'),
];

// forward SendReply telemetry from scsynth to the controller
~meterFwd = OSCdef(\meterFwd, { |m|
	~ctrl.value.sendMsg('/atelier/meters', *m[3..]);
}, '/atelier/meters');
// SendReply carries numeric values only; attach source/feature labels here.
// m = [cmdName, nodeID, replyID, rms, centroid, flatness]
~analysisFwd = OSCdef(\analysisFwd, { |m|
	~ctrl.value.sendMsg('/atelier/analysis',
		"master", "rms", m[3], "master", "centroid", m[4], "master", "noisiness", m[5]);
}, '/atelier/analysisRaw');

// periodic CPU report. SystemClock (not AppClock) so it actually ticks in the
// headless engine; s.avgCPU / numSynths are filled by the server status watcher.
~cpuTask = Routine({
	loop {
		~built.if { ~ctrl.value.sendMsg('/atelier/cpu', s.avgCPU, s.peakCPU, s.numSynths) };  // avg/peak are already percent
		0.3.wait;   // fast enough for the live CPU guard to catch a runaway workout
	}
}).play(SystemClock);

"Atelier engine loaded; awaiting /atelier/boot".postln;
)
