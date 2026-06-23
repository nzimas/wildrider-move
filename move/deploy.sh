#!/bin/bash
# Deploy the scsynth bundle to the Move over SSH.
# Usage: ./deploy.sh [move-host]   (default: move.local)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-move.local}"
SRC="$HERE/dist/bundle/"
DEST="/data/UserData/wildrider"

if [ ! -f "$HERE/dist/bundle/bin/scsynth" ]; then
    echo "No bundle at $SRC — run move/build/build.sh first." >&2
    exit 1
fi

echo "Deploying bundle -> root@$HOST:$DEST"
ssh "root@$HOST" "mkdir -p $DEST"
# BusyBox rsync may be absent; use tar-over-ssh for portability.
if ssh "root@$HOST" 'command -v rsync >/dev/null 2>&1'; then
    # No --delete: this DEST also holds controller/, sc/ and run-*.sh from
    # deploy-controller.sh, which --delete would wipe.
    rsync -av "$SRC" "root@$HOST:$DEST/"
else
    echo "(no rsync on device; using tar-over-ssh)"
    tar -C "$HERE/dist/bundle" -czf - . | ssh "root@$HOST" "tar -C $DEST -xzf -"
fi
ssh "root@$HOST" "chmod +x $DEST/bin/* $DEST/*.sh 2>/dev/null; chown -R ableton:users $DEST"
# Real-time audio: grant scsynth the same capabilities RNBO grants its DSP
# (cap_sys_nice = RT scheduling, cap_ipc_lock = mlock, cap_sys_resource = raise
# limits). Without this, scsynth runs at SCHED_OTHER as the ableton user, gets
# preempted by the overtake host, and the audio XRuns (clicks/pops). File caps
# are cleared when the binary is replaced, so re-apply on every deploy.
ssh "root@$HOST" "setcap cap_ipc_lock,cap_sys_nice,cap_sys_resource=eip $DEST/bin/scsynth && getcap $DEST/bin/scsynth"
echo "Done. (owned by ableton; scsynth has RT capabilities)"
