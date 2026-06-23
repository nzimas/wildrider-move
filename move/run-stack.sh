#!/bin/sh
# Launch the full Wildrider stack (engine + headless controller) non-blocking.
# Called once by the overtake ui.js. Each sub-script daemonises its processes;
# we background the launchers so host_system_cmd returns immediately.
WR=/data/UserData/wildrider

# Engine: jackd -d shadow + sclang(boot). run-engine.sh launches sclang in the
# background then polls for boot, so run the whole thing detached.
nohup sh "$WR/run-engine.sh" > /tmp/wr_stack_engine.log 2>&1 &

# Suspend-detection flag (mirrors RNBO): mark that shadow JACK is up.
echo 1 > /data/UserData/schwung/jack_running 2>/dev/null

# Controller: starts in parallel — it pings the engine until ready, so it does
# not need the engine fully booted first.
nohup sh "$WR/run-controller.sh" > /tmp/wr_controller.log 2>&1 &
