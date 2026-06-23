#!/bin/sh
# Bring up the Wildrider SC engine on the Move: shadow JACK + sclang(boot).
# Run on the device. Leaves jackd + sclang(+scsynth) running in the background;
# the headless controller (run-controller.sh) then drives it over OSC.
set -e
WR=/data/UserData/wildrider
RNBO=/data/UserData/rnbo
export LD_LIBRARY_PATH=$WR/lib:$RNBO/lib
export JACK_DRIVER_DIR=/data/UserData/schwung/lib/jack
export JACK_NO_AUDIO_RESERVATION=1
export SC_JACK_DEFAULT_OUTPUTS=system          # scsynth out -> shadow playback
export SC_PLUGIN_PATH=$WR/plugins              # UGen plugins (backup to wr-boot)
# Engine config (44.1k = the Move shadow rate; mono-in/stereo-out).
export ATELIER_SR=44100
export ATELIER_CHANNELS=2
export ATELIER_BLOCK=64
# Telemetry / handshake target = the local headless controller.
export CONTROLLER_HOST=127.0.0.1
export CONTROLLER_PORT=57140
export PATH=$WR/bin:$PATH

echo "[engine] starting jackd -d shadow"
pgrep -x jackd >/dev/null 2>&1 || { $RNBO/bin/jackd -d shadow > /tmp/wr_jackd.log 2>&1 & sleep 2; }
grep -q "attached to shared memory" /tmp/wr_jackd.log 2>/dev/null && echo "[engine] shadow attached"

echo "[engine] starting sclang (boot.scd) — pinned to cores 0-2"
taskset 0x7 $WR/bin/sclang -l $WR/share/sclang_conf.yaml $WR/sc/wr-boot.scd \
    > /tmp/wr_engine.log 2>&1 &
echo "[engine] sclang pid=$!  (log: /tmp/wr_engine.log)"
echo "[engine] waiting for boot ..."
i=0
while [ $i -lt 60 ]; do
    grep -q "server ready\|SuperCollider 3 server ready" /tmp/wr_engine.log 2>/dev/null && break
    grep -qi "ERROR\|FAILURE\|Exception" /tmp/wr_engine.log 2>/dev/null && { echo "[engine] error:"; tail -n 20 /tmp/wr_engine.log; exit 1; }
    i=$((i+1)); sleep 1
done
echo "[engine] --- log tail ---"; tail -n 12 /tmp/wr_engine.log
