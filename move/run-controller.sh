#!/bin/sh
# Run the headless Wildrider controller daemon on the Move.
# Talks OSC to the local sclang engine (127.0.0.1:57120), runs the 60Hz control
# loop, serves the ui.js control channel (:57150) + writes share/snapshot.json.
WR=/data/UserData/wildrider
export HOME=/data/UserData          # menu launch has HOME unset (see run-engine.sh)
export SC_HOST=127.0.0.1
export SC_PORT=57120              # sclang langPort (engine.scd OSCdefs)
export CONTROLLER_PORT=57140      # engine -> controller telemetry/ready
export WR_CONTROL_PORT=57150      # ui.js -> controller commands
export ATELIER_CONTROL_RATE=30      # modulation loop; lower = less preemption
export WR_SHARE=$WR/share
# atelier package + vendored pythonosc.
export PYTHONPATH=$WR/controller:$WR/controller/vendor
# Pin the controller to core 0 so its Python churn never preempts scsynth's
# audio thread (which run-engine.sh isolates onto cores 1-2). Core 3 = SPI.
exec taskset -c 0 python3 -m atelier.headless
