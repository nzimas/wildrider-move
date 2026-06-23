#!/bin/sh
# Bring up the Wildrider SC engine on the Move: shadow JACK + sclang(boot).
# Run on the device. Leaves jackd + sclang(+scsynth) running in the background;
# the headless controller (run-controller.sh) then drives it over OSC.
set -e
WR=/data/UserData/wildrider
RNBO=/data/UserData/rnbo
# The Schwung menu launches us with HOME unset; sclang then tries to mkdir
# /.local/share/SuperCollider (filesystem root) and fails -> Server.default is
# nil -> the engine never boots. Point HOME at an ableton-writable dir (as RNBO
# does). Without this it boots over ssh (su sets HOME) but NOT from the menu.
export HOME=/data/UserData
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
# Logs live under the (ableton-owned) wildrider tree, NOT /tmp: the runner is
# launched as `ableton` from the Schwung menu and cannot write root-owned files.
LOGS=$WR/logs; mkdir -p "$LOGS"
JACKLOG=$LOGS/jackd.log; ENGLOG=$LOGS/engine.log

echo "[engine] starting jackd -d shadow"
pgrep -x jackd >/dev/null 2>&1 || { $RNBO/bin/jackd -d shadow > "$JACKLOG" 2>&1 & sleep 2; }
grep -q "attached to shared memory" "$JACKLOG" 2>/dev/null && echo "[engine] shadow attached"

echo "[engine] starting sclang (boot.scd) — pinned to cores 0-2"
taskset 0x7 $WR/bin/sclang -l $WR/share/sclang_conf.yaml $WR/sc/wr-boot.scd \
    > "$ENGLOG" 2>&1 &
echo "[engine] sclang pid=$!  (log: $ENGLOG)"
echo "[engine] waiting for boot ..."
i=0
while [ $i -lt 60 ]; do
    grep -q "server ready\|SuperCollider 3 server ready" "$ENGLOG" 2>/dev/null && break
    grep -qi "ERROR\|FAILURE\|Exception" "$ENGLOG" 2>/dev/null && { echo "[engine] error:"; tail -n 20 "$ENGLOG"; exit 1; }
    i=$((i+1)); sleep 1
done
echo "[engine] --- log tail ---"; tail -n 12 "$ENGLOG"

# Core isolation for click-free audio: dedicate cores 1-2 to the audio thread
# (scsynth + jackd) and keep sclang on core 0 (with the controller). Core 3 is
# left for the SPI/display driver. Without this, the Python controller and
# sclang preempt scsynth at SCHED_OTHER -> JACK XRuns (clicks/pops).
for p in $(pgrep -x scsynth) $(pgrep -x jackd); do taskset -pc 1-2 "$p" >/dev/null 2>&1; done
for p in $(pgrep -x sclang); do taskset -pc 0 "$p" >/dev/null 2>&1; done
