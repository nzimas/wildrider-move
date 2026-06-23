#!/bin/sh
# Launch the full Wildrider stack (engine + headless controller) non-blocking.
# Called once by the overtake ui.js. Each sub-script daemonises its processes;
# we background the launchers so host_system_cmd returns immediately.
WR=/data/UserData/wildrider
# Logs under the ableton-owned tree (the runner runs as `ableton`, not root).
LOGS=$WR/logs; mkdir -p "$LOGS"

# Engine: jackd -d shadow + sclang(boot). run-engine.sh launches sclang in the
# background then polls for boot, so run the whole thing detached. Guard against
# double-start (e.g. a re-entry) — two scsynths on the same port collide.
if ! pgrep -x sclang >/dev/null 2>&1; then
    nohup sh "$WR/run-engine.sh" > "$LOGS/stack_engine.log" 2>&1 &
fi

# Suspend-detection flag (mirrors RNBO): mark that shadow JACK is up.
echo 1 > /data/UserData/schwung/jack_running 2>/dev/null

# Controller: starts in parallel — it pings the engine until ready, so it does
# not need the engine fully booted first.
if ! pgrep -f atelier.headless >/dev/null 2>&1; then
    nohup sh "$WR/run-controller.sh" > "$LOGS/controller.log" 2>&1 &
fi
