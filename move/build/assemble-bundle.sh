#!/bin/bash
# Assemble a relocatable aarch64 SuperCollider bundle for the Ableton Move.
#
# Runs INSIDE the ubuntu:22.04 arm64 build container. Collects scsynth, its core
# UGen plugins, sc3-plugins, the compiled mi-UGens, and every transitive shared
# library they need — EXCEPT:
#   - the glibc core (libc/libm/libpthread/libdl/librt/libresolv + ld-linux):
#     the Move ships glibc 2.35, identical to ubuntu:22.04, so we use the device's.
#   - libjack*: we link against the Move's RNBO-bundled JACK client at runtime so
#     the JACK client/server ABI matches the running `jackd -d shadow`.
# The result lives in /bundle and is exported to the host via buildx --output.
set -euo pipefail

BUNDLE=/bundle
mkdir -p "$BUNDLE"/{bin,lib,plugins,share}

# --- scsynth binary --------------------------------------------------------
SCSYNTH="$(command -v scsynth)"
cp -v "$SCSYNTH" "$BUNDLE/bin/scsynth"

# --- sclang (the engine host) ----------------------------------------------
# sclang runs boot.scd/engine.scd/synthdefs.scd and drives scsynth. Bundle the
# binary, libsclang.so, the SCClassLibrary, and the Qt offscreen platform
# plugin (dlopen'd at runtime, so ldd won't find it — copy explicitly).
SCLANG="$(command -v sclang)"
cp -v "$SCLANG" "$BUNDLE/bin/sclang"
find /usr -name "libsclang.so*" -exec cp -vL {} "$BUNDLE/lib/" \;
CLSDIR="$(find /usr -type d -name SCClassLibrary | head -1)"
mkdir -p "$BUNDLE/share/SuperCollider"
cp -a "$CLSDIR" "$BUNDLE/share/SuperCollider/SCClassLibrary"

# Qt plugins are only needed for the apt (Qt-linked) sclang. The Qt-less
# source build (SC_QTLESS=1) has no Qt at all — skip.
if [ -z "${SC_QTLESS:-}" ]; then
    QT_PLUGINS_SRC="$(find /usr -type d -path '*qt5/plugins' | head -1)"
    if [ -n "$QT_PLUGINS_SRC" ]; then
        mkdir -p "$BUNDLE/lib/qt5-plugins"
        for sub in platforms imageformats iconengines platforminputcontexts; do
            [ -d "$QT_PLUGINS_SRC/$sub" ] && cp -a "$QT_PLUGINS_SRC/$sub" "$BUNDLE/lib/qt5-plugins/"
        done
    fi
fi

# sclang config: point at the bundled class library; exclude the GUI/IDE class
# trees (they reference Qt primitives absent in a headless engine). Paths are
# absolute on the *device* (/data/UserData/wildrider/...), where deploy.sh puts
# the bundle.
DEV=/data/UserData/wildrider
CL="$DEV/share/SuperCollider/SCClassLibrary"
cat > "$BUNDLE/share/sclang_conf.yaml" <<YAML
includePaths:
  - $CL
  - $DEV/share/SuperCollider/Extensions
excludePaths:
  - $CL/scide_scqt
  - $CL/Common/GUI
  - $CL/JITLib/GUI
  - $CL/Platform/linux/GUI
  - $CL/deprecated/3.10/GUI
postInlineWarnings: false
YAML

# --- UGen plugins ----------------------------------------------------------
# Core SuperCollider plugins (SinOsc, Out, EnvGen, ... live in these .so files).
for CORE_PLUGINS in \
    /usr/local/lib/SuperCollider/plugins \
    /usr/lib/SuperCollider/plugins \
    /usr/lib/aarch64-linux-gnu/SuperCollider/plugins ; do
    [ -d "$CORE_PLUGINS" ] && cp -v "$CORE_PLUGINS"/*.so "$BUNDLE/plugins/" 2>/dev/null || true
done

# sc3-plugins (spectral analysis UGens etc.) — search common install roots.
for d in \
    /usr/share/SuperCollider/Extensions/SC3plugins \
    /usr/lib/SuperCollider/Extensions/SC3plugins \
    /usr/share/SuperCollider/Extensions ; do
    if [ -d "$d" ]; then
        find "$d" \( -name "*.so" -o -name "*.scx" \) -exec cp -v {} "$BUNDLE/plugins/" \; 2>/dev/null || true
    fi
done

# mi-UGens compiled in the earlier stage (Clouds/Rings/Plaits).
if [ -d /miugens ]; then
    find /miugens -maxdepth 1 \( -name "*.so" -o -name "*.scx" \) -exec cp -v {} "$BUNDLE/plugins/" \;
fi

# --- UGen sclang CLASS files (.sc) -----------------------------------------
# scsynth loads the .so above; sclang needs the matching .sc class definitions
# (FM7, PulseDPW, MiClouds, ...) or SynthDefs referencing them fail to compile.
# Gather them into an Extensions tree that sclang_conf.yaml includes.
EXT="$BUNDLE/share/SuperCollider/Extensions"
mkdir -p "$EXT"
# sc3-plugins ships its classes under .../Extensions/SC3plugins.
for d in /usr/share/SuperCollider/Extensions /usr/local/share/SuperCollider/Extensions; do
    [ -d "$d" ] && cp -a "$d/." "$EXT/" 2>/dev/null || true
done
# mi-UGens classes compiled/collected in the miugens stage.
if [ -d /miugens/classes ]; then
    mkdir -p "$EXT/mi-UGens"
    cp -v /miugens/classes/*.sc "$EXT/mi-UGens/"
fi

# --- gather transitive shared libraries ------------------------------------
# Exclude the glibc core (device-supplied) and libjack (device RNBO-supplied).
exclude_lib() {
    case "$1" in
        ld-linux-aarch64.so.*|libc.so.*|libm.so.*|libpthread.so.*|\
        libdl.so.*|librt.so.*|libresolv.so.*|libjack.so.*|libjackserver.so.*)
            return 0 ;;
        *) return 1 ;;
    esac
}

collect_deps() {
    local target="$1"
    ldd "$target" 2>/dev/null | awk '/=>/ {print $3} /ld-linux/ {print $1}' | while read -r so; do
        [ -f "$so" ] || continue
        local base; base="$(basename "$so")"
        exclude_lib "$base" && continue
        [ -e "$BUNDLE/lib/$base" ] && continue
        cp -vL "$so" "$BUNDLE/lib/$base"
    done
}

# Iterate to a fixed point so libs-of-libs are pulled in too.
for pass in 1 2 3 4 5 6; do
    before="$(ls -1 "$BUNDLE/lib" 2>/dev/null | wc -l)"
    collect_deps "$BUNDLE/bin/scsynth"
    collect_deps "$BUNDLE/bin/sclang"
    for p in "$BUNDLE"/plugins/*.so "$BUNDLE"/plugins/*.scx; do
        [ -e "$p" ] && collect_deps "$p"
    done
    for l in "$BUNDLE"/lib/*.so* "$BUNDLE"/lib/qt5-plugins/*/*.so; do
        [ -e "$l" ] && collect_deps "$l"
    done
    after="$(ls -1 "$BUNDLE/lib" 2>/dev/null | wc -l)"
    [ "$before" = "$after" ] && break
done

# --- bake $ORIGIN-relative RPATH (for capability/secure-exec) ---------------
# scsynth needs cap_sys_nice for real-time audio (no clicks), but a file with
# capabilities runs in secure-execution mode where the loader IGNORES
# LD_LIBRARY_PATH *and* refuses to expand $ORIGIN in RPATH. So bake an ABSOLUTE
# DT_RPATH to the on-device lib dir (the bundle always deploys to $DEV). This is
# honoured in secure mode; the bundle is no longer relocatable, which is fine.
if command -v patchelf >/dev/null 2>&1; then
    RP="$DEV/lib"
    # Binaries also need libjack.so.0, which lives in the device's RNBO lib dir
    # (we deliberately don't bundle it — see exclude_lib). Add it to their RPATH.
    RP_BIN="$DEV/lib:/data/UserData/rnbo/lib"
    patchelf --force-rpath --set-rpath "$RP_BIN" "$BUNDLE/bin/scsynth" "$BUNDLE/bin/sclang" 2>/dev/null || true
    for f in "$BUNDLE"/plugins/*.so "$BUNDLE"/plugins/*.scx "$BUNDLE"/lib/*.so*; do
        [ -e "$f" ] && patchelf --force-rpath --set-rpath "$RP" "$f" 2>/dev/null || true
    done
    echo "patched absolute RPATH on bin ($RP_BIN) + plugins/libs ($RP)"
fi

# --- manifest --------------------------------------------------------------
{
    echo "# Wildrider-Move scsynth bundle"
    echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "ubuntu: $(. /etc/os-release; echo "$VERSION")"
    echo "glibc:  $(ldd --version | head -n1)"
    echo "scsynth: $("$BUNDLE/bin/scsynth" -v 2>&1 | head -n1 || echo unknown)"
    echo "--- plugins ---"; ls -1 "$BUNDLE/plugins"
    echo "--- libs ---"; ls -1 "$BUNDLE/lib"
} > "$BUNDLE/MANIFEST.txt"

cat "$BUNDLE/MANIFEST.txt"
echo "Bundle assembled at $BUNDLE"
