// Capture synthdef: write the master output (bus 0, stereo) to disk via DiskOut.
( SynthDef(\wrcapture, { |buf = 0| DiskOut.ar(buf, In.ar(0, 2)) }).writeDefFile("/out");
  ("wrote wrcapture.scsyndef " ++ File.fileSize("/out/wrcapture.scsyndef")).postln; 0.exit; )
