#!/usr/bin/env bash
# Boot the headless SuperCollider engine and expose a live audio stream.
#
# Docker-on-macOS has no CoreAudio, so scsynth runs against a JACK *dummy*
# backend (audio is computed normally; there is just no physical device).
#
# Streaming: a CONTINUOUS ffmpeg captures SuperCollider's JACK outputs and writes
# an HLS playlist + segments to the shared /data volume. This is decoupled from
# any client — ffmpeg never dies when a browser disconnects (the previous
# ffmpeg-as-HTTP-server with -listen 1 died on every disconnect and raced the
# JACK wiring, which is why the stream went silent). The controller serves the
# HLS files; the browser plays them (hls.js / native).
set -u

SR="${ATELIER_SR:-48000}"
CH="${ATELIER_CHANNELS:-2}"
STREAM_DIR="${STREAM_DIR:-/data/stream}"
mkdir -p "${STREAM_DIR}"

echo "[atelier-sc] starting jackd dummy backend (${SR} Hz, ${CH} ch)"
jackd -r -d dummy -r "${SR}" -p 1024 -C "${CH}" -P "${CH}" &
sleep 2

echo "[atelier-sc] starting SuperCollider (sclang boot.scd)"
sclang /opt/atelier/boot.scd &
sleep 8

# Keep SuperCollider's outputs wired to the ffmpeg capture client ('atelier').
# Idempotent + periodic, so the link survives boot races and ffmpeg restarts.
( while true; do
    for i in $(seq 1 "${CH}"); do
      jack_connect "SuperCollider:out_${i}" "atelier:input_${i}" 2>/dev/null || true
    done
    sleep 3
  done ) &

# Continuous HLS encode of the JACK master. Short segments for low-ish latency.
echo "[atelier-sc] starting continuous HLS capture -> ${STREAM_DIR}/atelier.m3u8"
while true; do
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -f jack -i atelier -ac "${CH}" \
    -c:a aac -b:a 128k \
    -f hls -hls_time 1 -hls_list_size 4 \
    -hls_flags delete_segments+independent_segments+omit_endlist \
    -hls_segment_filename "${STREAM_DIR}/seg_%05d.ts" \
    "${STREAM_DIR}/atelier.m3u8"
  echo "[atelier-sc] ffmpeg HLS ended; restarting in 2s (watchdog)"
  sleep 2
done
