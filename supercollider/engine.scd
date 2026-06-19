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
		SynthDef(("master_" ++ channels).asSymbol, { |in = 0, out = 0|
			Out.ar(out, Limiter.ar(~killbad.(In.ar(in, channels)), 0.95, 0.01));
		}).add;
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
		~ctrl.value.sendMsg('/atelier/ready');
		"Atelier engine ready: % ch @ % Hz / % block".format(channels, sr, block).postln;
	});
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
		nodeSynths = Array.fill(nodes, {
			Synth.tail(grp, defName, [\out, outBus.index, \in, inBus.index]);
		});
		~modules[mid.asSymbol] = (
			group: grp, nodes: nodeSynths, type: type, inBus: inBus, outBus: outBus
		);
	};
};

~moduleFree = { |mid|
	var info = ~modules[mid.asSymbol];
	info.notNil.if {
		info[\group].free;
		info[\inBus].free;
		info[\outBus].free;
		~modules.removeAt(mid.asSymbol);
	};
};

~moduleNodes = { |mid, count|
	var info = ~modules[mid.asSymbol];
	info.notNil.if {
		var cur = info[\nodes].size;
		var defName = (info[\type].toLower ++ "_" ++ ~nchan).asSymbol;
		(count > cur).if {
			(count - cur).do {
				info[\nodes] = info[\nodes].add(
					Synth.tail(info[\group], defName,
						[\out, info[\outBus].index, \in, info[\inBus].index]));
			};
		} {
			(cur - count).do { info[\nodes].pop.free };
		};
	};
};

// ---- parameter updates --------------------------------------------------
~setParam = { |mid, pname, node, value|
	var info = ~modules[mid.asSymbol];
	info.notNil.if {
		(node < 0).if {
			info[\nodes].do { |syn| syn.set(pname.asSymbol, value) };  // global -> all nodes
		} {
			(node < info[\nodes].size).if { info[\nodes][node].set(pname.asSymbol, value) };
		};
	};
};

~setVec = { |mid, pname, values|
	var info = ~modules[mid.asSymbol];
	info.notNil.if {
		values.do { |v, i| (i < info[\nodes].size).if { info[\nodes][i].set(pname.asSymbol, v) } };
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
	OSCdef(\ping, { |m| ~ctrl.value.sendMsg('/atelier/ready') }, '/atelier/ping'),
	OSCdef(\feedback, { |m| /* armed feedback edges: see ~rebuildLane refinement */ }, '/atelier/feedback'),
	OSCdef(\recorder, { |m| /* recorder/resampling hook */ }, '/atelier/recorder'),
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

// periodic CPU report
~cpuTask = Routine({
	loop {
		~built.if { ~ctrl.value.sendMsg('/atelier/cpu', s.avgCPU / 100, s.peakCPU / 100, s.numSynths) };
		1.0.wait;
	}
}).play(AppClock);

"Atelier engine loaded; awaiting /atelier/boot".postln;
)
