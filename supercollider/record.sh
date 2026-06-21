#!/usr/bin/env bash
# On-demand CD-quality (44.1 kHz / 16-bit PCM) WAV capture of the SuperCollider
# master, taken straight off JACK — the same capture path the HLS stream uses,
# but written losslessly instead of AAC. Driven by sclang (engine.scd) which
# shells out here in response to /atelier/record/{start,stop} OSC from the
# controller. Files land in the shared /data volume so the controller can list
# and serve them.
set -u

CH="${ATELIER_CHANNELS:-2}"
CLIENT="atelier_rec"               # JACK client name ffmpeg registers (-i <name>)
PIDFILE="/tmp/atelier_rec.pid"

case "${1:-}" in
  start)
    path="${2:?usage: record.sh start <wavpath>}"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "[rec] already recording (pid $(cat "$PIDFILE"))"; exit 0
    fi
    mkdir -p "$(dirname "$path")"
    # ffmpeg's JACK input reads silence until we wire SC's outputs to it; it will
    # not exit while waiting, so launching first then connecting is safe.
    ffmpeg -hide_banner -loglevel warning -nostdin \
      -f jack -i "$CLIENT" -ac "$CH" -ar 44100 -c:a pcm_s16le \
      "$path" &
    pid=$!
    echo "$pid" > "$PIDFILE"
    # Wire SuperCollider:out_i -> atelier_rec:input_i as soon as ffmpeg's ports
    # register (a few hundred ms). Retry briefly, then stop (HLS keeps its own
    # persistent wiring loop; this one only needs to land once).
    ( for _ in $(seq 1 40); do
        ok=1
        for i in $(seq 1 "$CH"); do
          jack_connect "SuperCollider:out_${i}" "${CLIENT}:input_${i}" 2>/dev/null || ok=0
        done
        [ "$ok" = 1 ] && break
        sleep 0.25
      done ) &
    echo "[rec] started pid=$pid -> $path"
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      pid="$(cat "$PIDFILE")"
      # SIGINT (not KILL) so ffmpeg flushes and finalizes the WAV/RIFF header.
      kill -INT "$pid" 2>/dev/null || true
      for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      rm -f "$PIDFILE"
      echo "[rec] stopped pid=$pid"
    else
      echo "[rec] not recording"
    fi
    ;;
  *)
    echo "usage: record.sh {start <wavpath>|stop}"; exit 1;;
esac
