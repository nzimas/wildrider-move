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
export ATELIER_BLOCK=128            # match the shadow JACK period (128) -> one
                                   # control block per audio callback = less
                                   # per-cycle overhead (fewer XRuns)
# Telemetry / handshake target = the local headless controller.
export CONTROLLER_HOST=127.0.0.1
export CONTROLLER_PORT=57140
export PATH=$WR/bin:$PATH
# Engine: the multithreaded SUPERNOVA server is the DEFAULT now — it spreads the
# patch across all four cores (see docs/supernova.md). ATELIER_THREADS = the DSP
# thread count. To fall back to single-core scsynth, set it to 0 (wr-boot.scd
# treats <1 as scsynth). Overridable from the environment for one-off tests.
export ATELIER_THREADS="${ATELIER_THREADS:-3}"
# Logs live under the (ableton-owned) wildrider tree, NOT /tmp: the runner is
# launched as `ableton` from the Schwung menu and cannot write root-owned files.
LOGS=$WR/logs; mkdir -p "$LOGS"
JACKLOG=$LOGS/jackd.log; ENGLOG=$LOGS/engine.log

echo "[engine] starting jackd -R -d shadow (realtime)"
# Realtime audio chain — THE fix for the residual clicks/pops. -R -P70 puts jackd
# on SCHED_FIFO; libjack then promotes scsynth's audio callback thread to RT too
# (scsynth has cap_sys_nice). Without this the whole chain runs SCHED_OTHER prio 0
# and is preempted by the controller / sclang / the Move host under load (the box
# sits at load ~6 on 4 cores), so scsynth misses its 2.9ms JACK block -> XRuns.
# Needs cap_sys_nice+cap_ipc_lock on the jackd binary (deploy-controller.sh sets
# them) because the ableton user's rtprio ulimit is 0. Priority 70 stays BELOW the
# SPI/IRQ kernel threads (chrt 90/91) so the DAC/display path is never starved.
pgrep -x jackd >/dev/null 2>&1 || { $RNBO/bin/jackd -R -P 70 -d shadow > "$JACKLOG" 2>&1 & sleep 2; }
grep -q "attached to shared memory" "$JACKLOG" 2>/dev/null && echo "[engine] shadow attached"

echo "[engine] starting sclang (boot.scd) — pinned to cores 0-2"
taskset 0x7 $WR/bin/sclang -l $WR/share/sclang_conf.yaml $WR/sc/wr-boot.scd \
    > "$ENGLOG" 2>&1 &
echo "[engine] sclang pid=$!  (log: $ENGLOG)"
echo "[engine] waiting for boot ..."
i=0
while [ $i -lt 60 ]; do
    grep -q "server ready\|SuperCollider 3 server ready\|Supernova ready" "$ENGLOG" 2>/dev/null && break
    grep -qi "ERROR\|FAILURE\|Exception" "$ENGLOG" 2>/dev/null && { echo "[engine] error:"; tail -n 20 "$ENGLOG"; exit 1; }
    i=$((i+1)); sleep 1
done
echo "[engine] --- log tail ---"; tail -n 12 "$ENGLOG"

# Core pinning (secondary to the RT priority above): keep the audio thread
# (scsynth + jackd) on cores 1-2, sclang + the Python controller on core 0, and
# leave core 3 for the SPI/display driver. This reduces cross-core cache/lock
# contention; the SCHED_FIFO priority from `jackd -R` is what actually prevents
# the preemption that causes XRuns.
for p in $(pgrep -x scsynth) $(pgrep -x supernova) $(pgrep -x jackd); do taskset -pc 1-3 "$p" >/dev/null 2>&1; done
for p in $(pgrep -x sclang); do taskset -pc 0 "$p" >/dev/null 2>&1; done

# Verify the audio chain actually came up realtime (read-only; logs to engine log).
for p in $(pgrep -x jackd) $(pgrep -x scsynth) $(pgrep -x supernova); do
    echo "[engine] $(cat /proc/$p/comm 2>/dev/null) sched: $(chrt -p $p 2>/dev/null | tr '\n' ' ')"
done
