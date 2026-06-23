// Generate a self-contained NRT test score for the Move scsynth bundle.
// Run under sclang (headless). Embeds the synthdef bytes via /d_recv so the
// score needs no external files on the device. Writes /out/tone.osc.
(
var sd, score;
sd = SynthDef(\wrtone, { |freq = 440, amp = 0.2|
    // Simple stereo sine with a gentle 2s fade so the WAV is unmistakably
    // non-silent and bounded. Uses only core UGens (no plugins needed).
    var env = EnvGen.kr(Env([0, 1, 1, 0], [0.05, 1.9, 0.05]), doneAction: 2);
    Out.ar(0, SinOsc.ar(freq, 0, amp * env) ! 2);
});

score = [
    [0.0, ['/d_recv', sd.asBytes]],
    [0.0, ['/s_new', \wrtone, 1000, 0, 0, \freq, 440, \amp, 0.2]],
    [2.0, ['/c_set', 0, 0]]   // end marker beyond synth lifetime
];

Score.write(score, "/out/tone.osc");
("wrote /out/tone.osc, bytes=" ++ File.fileSize("/out/tone.osc")).postln;
0.exit;
)
