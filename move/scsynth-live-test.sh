#!/bin/sh
# P0 live audio test: bring up shadow JACK + bundled scsynth, play a tone out
# the Move speaker, then tear everything down. Safe to run while the Move app
# is up — the shadow driver mixes alongside it. Run on the device.
set -e
WR=/data/UserData/wildrider
RNBO=/data/UserData/rnbo
export LD_LIBRARY_PATH=$WR/lib:$RNBO/lib
export JACK_DRIVER_DIR=/data/UserData/schwung/lib/jack
export JACK_NO_AUDIO_RESERVATION=1
export SC_JACK_DEFAULT_OUTPUTS=system   # auto-connect scsynth out -> shadow playback
export SC_PLUGIN_PATH=$WR/plugins

cleanup() {
    echo "[cleanup] stopping scsynth + jackd"
    kill -9 "$SC_PID" 2>/dev/null || true
    killall -9 scsynth 2>/dev/null || true
    killall -9 jackd  2>/dev/null || true
    rm -f $WR/share/.scsynth-booted
}
trap cleanup EXIT INT TERM

echo "[1/4] starting jackd -d shadow"
$RNBO/bin/jackd -d shadow > /tmp/wr_jackd.log 2>&1 &
JACK_PID=$!
sleep 2
kill -0 "$JACK_PID" 2>/dev/null || { echo "jackd failed:"; cat /tmp/wr_jackd.log; exit 1; }
grep -q "attached to shared memory" /tmp/wr_jackd.log && echo "      shadow attached OK"

echo "[2/4] starting scsynth (JACK, realtime)"
# Pin to cores 0-2 like RNBO; leave core 3 for SPI/display.
taskset 0x7 $WR/bin/scsynth -u 57110 -U $WR/plugins -a 1024 -i 0 -o 2 \
    > /tmp/wr_scsynth.log 2>&1 &
SC_PID=$!
# Wait for boot (scsynth prints "SuperCollider 3 server ready.")
for i in $(seq 1 30); do
    grep -q "server ready" /tmp/wr_scsynth.log && break
    kill -0 "$SC_PID" 2>/dev/null || { echo "scsynth died:"; cat /tmp/wr_scsynth.log; exit 1; }
    sleep 0.3
done
grep -q "server ready" /tmp/wr_scsynth.log && echo "      scsynth ready" || { echo "scsynth boot timeout:"; tail /tmp/wr_scsynth.log; exit 1; }

echo "[3/4] playing 440Hz tone for 3s (listen to the Move speaker)"
python3 $WR/share/play_tone.py 57110 $WR/share/wrtone.scsyndef 3.0

echo "[4/4] scsynth log tail:"
tail -n 8 /tmp/wr_scsynth.log
echo "DONE"
