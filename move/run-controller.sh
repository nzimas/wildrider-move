#!/bin/sh
# Run the headless Wildrider controller daemon on the Move.
# Talks OSC to the local sclang engine (127.0.0.1:57120), runs the 60Hz control
# loop, serves the ui.js control channel (:57150) + writes share/snapshot.json.
WR=/data/UserData/wildrider
export SC_HOST=127.0.0.1
export SC_PORT=57120              # sclang langPort (engine.scd OSCdefs)
export CONTROLLER_PORT=57140      # engine -> controller telemetry/ready
export WR_CONTROL_PORT=57150      # ui.js -> controller commands
export ATELIER_CONTROL_RATE=60
export WR_SHARE=$WR/share
# atelier package + vendored pythonosc.
export PYTHONPATH=$WR/controller:$WR/controller/vendor
exec python3 -m atelier.headless
