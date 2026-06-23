#!/bin/bash
# Deploy the headless controller + SC engine scripts to the Move.
#   - controller/atelier  -> /data/UserData/wildrider/controller/atelier
#   - move/vendor/pythonosc -> .../controller/vendor/pythonosc
#   - supercollider/*.scd + DX7.afx + move/sc/wr-boot.scd -> .../sc
#   - run-engine.sh / run-controller.sh -> /data/UserData/wildrider
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HOST="${1:-move.local}"
DEST="/data/UserData/wildrider"

ssh "root@$HOST" "mkdir -p $DEST/controller/vendor $DEST/sc"

echo "-> controller (atelier + pythonosc)"
tar -C "$ROOT/controller" -czf - atelier | ssh "root@$HOST" "tar -C $DEST/controller -xzf -"
tar -C "$HERE/vendor" -czf - pythonosc | ssh "root@$HOST" "tar -C $DEST/controller/vendor -xzf -"

echo "-> SC engine (.scd + DX7.afx + wr-boot)"
tar -C "$ROOT/supercollider" -czf - boot.scd engine.scd synthdefs.scd dx7.scd DX7.afx \
    | ssh "root@$HOST" "tar -C $DEST/sc -xzf -"
tar -C "$HERE/sc" -czf - wr-boot.scd | ssh "root@$HOST" "tar -C $DEST/sc -xzf -"

echo "-> launch scripts"
scp "$HERE/run-engine.sh" "$HERE/run-controller.sh" "$HERE/run-stack.sh" "root@$HOST:$DEST/"
ssh "root@$HOST" "chmod +x $DEST/run-engine.sh $DEST/run-controller.sh $DEST/run-stack.sh"
ssh "root@$HOST" "chown -R ableton:users $DEST"
echo "Done."
