// On-device boot wrapper for the Wildrider SC engine (Move takeover).
//
// The bundled sclang was built with prefix /usr/local, so its default scsynth
// path / resourceDir don't exist on the Move. Point the Server at our bundled
// server binary + UGen plugins, then hand off to the unmodified desktop boot.scd
// (which loads dx7/synthdefs/engine and auto-boots the server).
//
// ATELIER_THREADS (env): the DSP thread count for the multithreaded SUPERNOVA
// server, which is the DEFAULT engine (run-engine.sh sets it to 3). A value < 1
// (e.g. ATELIER_THREADS=0) falls back to single-core scsynth. Supernova is a
// matching 3.11.2 build and loads the same *_supernova.so plugins.
(
var dir = "/data/UserData/wildrider";
var threads = ("ATELIER_THREADS".getenv ? "").asInteger;
// ~wrSupernova gates server-specific synthdef bodies (e.g. VERB uses GVerb under
// supernova, JPverb under scsynth). Set BEFORE boot.scd loads synthdefs.scd.
(threads > 0).if({
	~wrSupernova = true;
	Server.program = dir +/+ "bin/supernova";
	Server.default.options.threads = threads;
	Server.default.options.useSystemClock = true;   // supernova: drive scheduling off the JACK clock
	("[wr-boot] SUPERNOVA — " ++ threads ++ " DSP threads").postln;
}, {
	~wrSupernova = false;
	Server.program = dir +/+ "bin/scsynth";
	"[wr-boot] scsynth (single-threaded)".postln;
});
Server.default.options.ugenPluginsPath = [dir +/+ "plugins"];
// boot.scd lives next to this file; load it from its own path so its
// thisProcess.nowExecutingPath.dirname resolves dx7/synthdefs/engine correctly.
(thisProcess.nowExecutingPath.dirname +/+ "boot.scd").load;
)
