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
    rsync -av --delete "$SRC" "root@$HOST:$DEST/"
else
    echo "(no rsync on device; using tar-over-ssh)"
    tar -C "$HERE/dist/bundle" -czf - . | ssh "root@$HOST" "tar -C $DEST -xzf -"
fi
ssh "root@$HOST" "chmod +x $DEST/bin/* $DEST/*.sh 2>/dev/null; ls -la $DEST"
echo "Done."
