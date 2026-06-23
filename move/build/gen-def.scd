// Write a standalone synthdef file the live scsynth can /d_load by path.
(
SynthDef(\wrtone, { |freq = 440, amp = 0.2, gate = 1|
    var env = EnvGen.kr(Env([0,1,1,0],[0.05,1.9,0.05]), gate, doneAction: 2);
    Out.ar(0, SinOsc.ar(freq, 0, amp * env) ! 2);
}).writeDefFile("/out");
("wrote /out/wrtone.scsyndef size=" ++ File.fileSize("/out/wrtone.scsyndef")).postln;
0.exit;
)
