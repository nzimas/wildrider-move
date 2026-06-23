#!/bin/bash
# Deploy the Wildrider Schwung overtake module to the Move and rescan modules.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-move.local}"
DEST="/data/UserData/schwung/modules/overtake/wildrider"

ssh "root@$HOST" "mkdir -p $DEST"
COPYFILE_DISABLE=1 tar -C "$HERE/schwung-module/wildrider" --exclude="._*" -czf - . | ssh "root@$HOST" "tar -C $DEST -xzf -"
ssh "root@$HOST" "chmod +x $DEST/exit-hook.sh; chown -R ableton:users $DEST; ls -la $DEST"
echo "Deployed. Re-open the Schwung menu (or rescan) to see 'Wildrider' under overtake runners."
