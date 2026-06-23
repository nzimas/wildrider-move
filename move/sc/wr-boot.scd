// On-device boot wrapper for the Wildrider SC engine (Move takeover).
//
// The bundled sclang was built with prefix /usr/local, so its default scsynth
// path / resourceDir don't exist on the Move. Point the Server at our bundled
// scsynth + UGen plugins, then hand off to the unmodified desktop boot.scd
// (which loads dx7/synthdefs/engine and auto-boots the server).
(
var dir = "/data/UserData/wildrider";
Server.program = dir +/+ "bin/scsynth";
Server.default.options.ugenPluginsPath = [dir +/+ "plugins"];
// boot.scd lives next to this file; load it from its own path so its
// thisProcess.nowExecutingPath.dirname resolves dx7/synthdefs/engine correctly.
(thisProcess.nowExecutingPath.dirname +/+ "boot.scd").load;
)
