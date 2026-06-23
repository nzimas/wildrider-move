#!/bin/bash
# Build the aarch64 scsynth bundle on the Mac and export it to move/dist/bundle.
# Requires Docker Desktop with buildx (arm64 emulation/native on Apple Silicon).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIST="$HERE/../dist/bundle"

rm -rf "$DIST"
mkdir -p "$DIST"

# DOCKERFILE: Dockerfile.engine (Qt-less source build, on-device default) or
# Dockerfile.scsynth (apt scsynth+Qt sclang, P0/reference). Default: engine.
DOCKERFILE="${DOCKERFILE:-Dockerfile.engine}"

docker buildx build \
    --platform linux/arm64 \
    -f "$HERE/$DOCKERFILE" \
    --target export \
    --output "type=local,dest=$DIST" \
    "$HERE"

echo
echo "=== Bundle exported to $DIST ==="
cat "$DIST/MANIFEST.txt" 2>/dev/null || true
