# ---- Stage 1: compile the Mutable Instruments UGens (v7b1/mi-UGens) ---------
# These are C++ SuperCollider plugins (Clouds, Rings, ...). We build them from
# source against the SuperCollider plugin headers so MiClouds/MiRings are
# available as native UGens.
FROM ubuntu:24.04 AS miugens
ENV DEBIAN_FRONTEND=noninteractive
# Under QEMU user-mode emulation, apt's privilege-drop sandbox (the _apt user)
# breaks gpgv -> spurious "invalid signature" errors. Running apt's fetchers as
# root is the documented fix for foreign-arch image builds.
RUN printf 'APT::Sandbox::User "root";\nAcquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/00qemu-sandbox && \
    apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential pkg-config ca-certificates \
        supercollider-dev libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --recurse-submodules --shallow-submodules \
        https://github.com/v7b1/mi-UGens.git /src
# mi-UGens' cmake expects a SuperCollider *source* layout:
#   ${SC_PATH}/include/{plugin_interface,common} and
#   ${SC_PATH}/external_libraries/libsndfile/sndfile.h
# Map the Debian supercollider-dev headers + system libsndfile into that shape.
RUN mkdir -p /scsdk/external_libraries \
    && ln -s /usr/include/SuperCollider /scsdk/include \
    && ln -s /usr/include /scsdk/external_libraries/libsndfile
WORKDIR /src/build
# Build only the plugins we port (avoids unrelated plugins' build issues).
RUN cmake .. -DSC_PATH=/scsdk -DCMAKE_BUILD_TYPE=Release \
    && make -j"$(nproc)" MiClouds MiRings MiPlaits
# Plugins build as .scx (scsynth loads .scx too); collect them + the .sc classes.
RUN mkdir -p /miugens \
    && find /src/build \( -name "*.scx" -o -name "*.so" \) -exec cp {} /miugens/ \; \
    && cp /src/sc/Classes/MiClouds.sc /src/sc/Classes/MiRings.sc /src/sc/Classes/MiPlaits.sc /miugens/

# ---- Stage 2: the headless engine runtime -----------------------------------
# Headless SuperCollider engine + JACK dummy backend + ffmpeg audio streamer.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# supercollider-language provides sclang; supercollider-server provides scsynth.
# sc3-plugins supplies the spectral-analysis UGens (SpecCentroid/SpecFlatness).
# jackd2 already ships jack_connect / jack_lsp.
RUN apt-get update && apt-get install -y --no-install-recommends \
        supercollider-server \
        supercollider-language \
        sc3-plugins \
        jackd2 \
        ffmpeg \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Mutable Instruments UGens (compiled in stage 1): drop .so + .sc into Extensions
# so both scsynth (plugins) and sclang (classes) pick them up.
COPY --from=miugens /miugens /usr/share/SuperCollider/Extensions/mi-UGens

# Allow jackd to start without realtime privileges in the container.
ENV JACK_NO_AUDIO_RESERVATION=1
ENV SC_JACK_DEFAULT_OUTPUTS=system

# sclang links Qt/QtWebEngine; run it headless (offscreen) and without the
# Chromium sandbox (required when running as root in a container).
ENV QT_QPA_PLATFORM=offscreen \
    QTWEBENGINE_DISABLE_SANDBOX=1 \
    QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox

WORKDIR /opt/atelier
COPY boot.scd engine.scd synthdefs.scd run.sh ./
RUN chmod +x run.sh

EXPOSE 57120/udp 8200
CMD ["./run.sh"]
