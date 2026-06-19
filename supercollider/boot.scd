// Atelier boot entry. Loads SynthDef families, then the engine OSC host.
// Run headless via: sclang boot.scd
(
var here = thisProcess.nowExecutingPath.dirname;
s = Server.default;
s.options.numOutputBusChannels = ("ATELIER_CHANNELS".getenv ? "2").asInteger;
s.options.numInputBusChannels = ("ATELIER_CHANNELS".getenv ? "2").asInteger;
s.options.sampleRate = ("ATELIER_SR".getenv ? "48000").asInteger;
s.options.blockSize = ("ATELIER_BLOCK".getenv ? "256").asInteger;
s.options.memSize = 256 * 1024;

// The engine builds SynthDefs after /atelier/boot; we also load the file that
// defines the ~buildDefs function used there.
(here +/+ "synthdefs.scd").load;
(here +/+ "engine.scd").load;

// Auto-boot the server so the engine is ready even before the first /atelier/boot
// (the controller will send /atelier/boot to (re)configure channel count).
~boot.(s.options.sampleRate, s.options.blockSize, s.options.numOutputBusChannels);
)
