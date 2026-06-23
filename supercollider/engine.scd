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
	~gIn      = Group.new(s, \addToHead);
	~gModules = Group.after(~gIn);
	~gMix     = Group.after(~gModules);
	~gMaster  = Group.after(~gMix);
	~gRec     = Group.after(~gMaster);
};

// Rebuild the entire routing from the free-form connection graph. The controller
// sends the topo order, edges and terminals; we realize it with:
//   * a clear synth per module inBus + master (private busses are not auto-zeroed)
//   * module groups ordered topologically (sources first)
//   * a patch-cord synth per edge, placed after src / before dst so it reads a
//     fresh src.outBus and sums into dst.inBus
//   * terminal cords (no outgoing edge) summed into the master bus
~rebuildGraph = {
	(~clears ? []).do(_.free);
	(~cords ? []).do(_.free);
	~clears = []; ~cords = [];
	// clear every module input bus (head of ~gModules) and the master bus (head of ~gMix)
	~modules.do { |info|
		~clears = ~clears.add(Synth.head(~gModules, ("busClear_" ++ ~nchan).asSymbol,
			[\bus, info[\inBus].index]));
	};
	~clears = ~clears.add(Synth.head(~gMix, ("busClear_" ++ ~nchan).asSymbol,
		[\bus, ~masterBus.index]));
	// order module groups topologically (after the clears)
	(~graphOrder ? []).do { |mid|
		var info = ~modules[mid.asSymbol];
		info.notNil.if { info[\group].moveToTail(~gModules) };
	};
	// inter-module cords: place each right before its destination group
	(~graphEdges ? []).clump(2).do { |pair|
		var s = ~modules[pair[0].asSymbol], d = ~modules[pair[1].asSymbol];
		(s.notNil and: { d.notNil }).if {
			~cords = ~cords.add(Synth.before(d[\group], ("cord_" ++ ~nchan).asSymbol,
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

~freeAll = {
	~modules.do { |info| info[\group].free; info[\outBus].free; info[\inBus].free };
	~modules = IdentityDictionary.new;
	(~clears ? []).do(_.free); (~cords ? []).do(_.free);
	~clears = []; ~cords = [];
	~analysisSynth.notNil.if { ~analysisSynth.free };
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
		~buildDefs.(channels);
		// patch cord: sum one bus into another (NaN-killed), used for all routing
		SynthDef(("cord_" ++ channels).asSymbol, { |inb = 0, outb = 0, gain = 1|
			Out.ar(outb, ~killbad.(In.ar(inb, channels)) * gain);
		}).add;
		// bus clear: private audio busses are not auto-zeroed; clear before summing
		SynthDef(("busClear_" ++ channels).asSymbol, { |bus = 0|
			ReplaceOut.ar(bus, Silent.ar(channels));
		}).add;
		// master loudness guard: NaN/Inf kill + brickwall limiter so runaway
		// feedback can never clip the output (blueprint panic/loudness policy).
		// Master makeup gain into the brickwall limiter. Wildrider-Move patches
		// (ambient aesthetics + long wet/dry FX chains) often sit ~20-30 dB down;
		// the gain lifts them to an audible level on the Move speaker while the
		// Limiter still guarantees no clipping. Tunable via the \gain arg.
		SynthDef(("master_" ++ channels).asSymbol, { |in = 0, out = 0, gain = 6|
			Out.ar(out, Limiter.ar(~killbad.(In.ar(in, channels)) * gain, 0.95, 0.01));
		}).add;
		s.sync;
		// DX7: allocate LFO wavetable buffers + load the bundled factory bank once.
		~dx7_allocBufs.(s);
		~dx7_factory.isNil.if { ~dx7_loadFactory.(~dx7afxPath ? "DX7.afx") };
		s.sync;
		~ensureGroups.value;
		~inBus = Bus.audio(s, channels);
		~masterBus = Bus.audio(s, channels);
		~clears = []; ~cords = [];
		~graphOrder = []; ~graphEdges = []; ~graphTerminals = [];
		// live input into inBus
		SynthDef(\atelierInput, { |out = 0| ReplaceOut.ar(out, SoundIn.ar((0..channels-1))) }).add;
		s.sync;
		Synth.head(~gIn, \atelierInput, [\out, ~inBus.index]);
		~masterSynth = Synth.tail(~gMaster, ("master_" ++ channels).asSymbol, [\in, ~masterBus.index, \out, 0]);
		~analysisSynth = Synth.tail(~gRec, ("analysis_" ++ channels).asSymbol, [\in, 0]);
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
			nodeSynths = Array.fill(nodes, {
				Synth.tail(grp, defName, [\out, outBus.index, \in, inBus.index]);
			});
			~modules[mid.asSymbol] = (group: grp, nodes: nodeSynths, type: type, inBus: inBus, outBus: outBus);
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
					info[\nodes] = info[\nodes].add(Synth.tail(info[\group], defName,
						[\out, info[\outBus].index, \in, info[\inBus].index]));
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
~defs = [
	OSCdef(\boot, { |m| ~boot.(m[1], m[2], m[3]) }, '/atelier/boot'),
	OSCdef(\modnew, { |m| ~moduleNew.(m[1], m[2], m[3], m[4], m[5]) }, '/atelier/module/new'),
	OSCdef(\modfree, { |m| ~moduleFree.(m[1]) }, '/atelier/module/free'),
	OSCdef(\modnodes, { |m| ~moduleNodes.(m[1], m[2]) }, '/atelier/module/nodes'),
	// free-form graph: controller sends order/edges/terminals, then commit
	OSCdef(\graphOrder, { |m| ~graphOrder = m[1..] }, '/atelier/graph/order'),
	OSCdef(\graphEdges, { |m| ~graphEdges = m[1..] }, '/atelier/graph/edges'),
	OSCdef(\graphTerm, { |m| ~graphTerminals = m[1..] }, '/atelier/graph/terminals'),
	OSCdef(\graphCommit, { |m| ~rebuildGraph.value }, '/atelier/graph/commit'),
	OSCdef(\bypass, { |m| ~setParam.(m[1], "bypass", -1, m[2]) }, '/atelier/module/bypass'),
	OSCdef(\wet, { |m| ~setParam.(m[1], "wet", -1, m[2]) }, '/atelier/module/wet'),
	OSCdef(\set, { |m| ~setParam.(m[1], m[2], m[3], m[4]) }, '/atelier/set'),
	OSCdef(\setvec, { |m| ~setVec.(m[1], m[2], m[3..]) }, '/atelier/setvec'),
	OSCdef(\panic, { |m|
		~feedback.do { |fb| fb.notNil.if { fb.free } };
		~feedback = IdentityDictionary.new;
		s.freeAll(~gModules.nodeID);
		~modules.do { |info| info[\nodes].do { |syn| syn.set(\amp, 0) } };
		"PANIC".postln;
	}, '/atelier/panic'),
	OSCdef(\spatial, { |m| /* channel changes require rebuild on next boot */ }, '/atelier/spatial'),
	OSCdef(\reseed, { |m| /* seeds live in the controller; nothing server-side */ }, '/atelier/reseed'),
	// Only confirm readiness once the boot block has actually finished (buses +
	// groups exist). Replying mid-boot makes the controller build_graph against a
	// nil ~masterBus -> 'index' errors. ~built is set at the end of ~boot.
	OSCdef(\ping, { |m| (~built ? false).if { ~ctrl.value.sendMsg('/atelier/ready') } }, '/atelier/ping'),
	OSCdef(\feedback, { |m| /* armed feedback edges: see ~rebuildLane refinement */ }, '/atelier/feedback'),
	OSCdef(\recorder, { |m| /* recorder/resampling hook */ }, '/atelier/recorder'),
	// CD-quality WAV capture off JACK (see record.sh). The controller picks the
	// timestamped path; we just launch/stop the ffmpeg capture.
	OSCdef(\recStart, { |m|
		var path = m[1].asString;
		("/opt/atelier/record.sh start " ++ path.quote).unixCmd;
		("[atelier-sc] recording -> " ++ path).postln;
	}, '/atelier/record/start'),
	OSCdef(\recStop, { |m|
		"/opt/atelier/record.sh stop".unixCmd;
		"[atelier-sc] recording stopped".postln;
	}, '/atelier/record/stop'),
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
		~built.if { ~ctrl.value.sendMsg('/atelier/cpu', s.avgCPU / 100, s.peakCPU / 100, s.numSynths) };
		1.0.wait;
	}
}).play(SystemClock);

"Atelier engine loaded; awaiting /atelier/boot".postln;
)
