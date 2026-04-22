# ── Stage 1: build unifi-cam-proxy (for its protocol layer) ──────────────────
FROM python:3.11-slim AS proxy-builder

WORKDIR /build
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Pin upstream to a known-good commit so rebuilds are reproducible.
# Bump this after testing against a newer commit — don't track HEAD.
# Last reviewed: Apr 2026. See SECONDBRAIN.md "Protocol state" for the
# reversing landscape and which upstream PRs are worth cherry-picking.
ARG UNIFI_CAM_PROXY_REF=cc6d3fc7cdae9f1dfce575627089632aec696403
RUN git clone https://github.com/keshavdv/unifi-cam-proxy.git \
    && git -C unifi-cam-proxy checkout "$UNIFI_CAM_PROXY_REF"

# ── Stage 2: final image ──────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="unifi-ai-camproxy"
LABEL description="DIY UniFi AI camera proxy — person/vehicle detection + line crossing"

# System deps: ffmpeg for video streaming, openssl for cert gen, libGL for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    netcat-traditional \
    openssl \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy unifi-cam-proxy (we use its base classes directly)
COPY --from=proxy-builder /build/unifi-cam-proxy /app/unifi-cam-proxy
RUN for file in requirements.txt setup.cfg setup.py; do \
        path="/app/unifi-cam-proxy/$file"; \
        if [ -f "$path" ]; then sed -i '/pyunifiprotect/d' "$path"; fi; \
    done
RUN pip install --no-cache-dir -e /app/unifi-cam-proxy

# ── Default runtime image (CPU + Intel OpenVINO) ─────────────────────────────
# This image ships CPU PyTorch + OpenVINO + the Intel compute runtime, so
# at startup AIEngine auto-probes and picks the fastest reachable device
# in this order: intel:gpu → intel:npu → mps → cpu.
#
# For NVIDIA CUDA, build the sibling Dockerfile.cuda instead (via
# docker-compose.gpu.yml) — we keep CUDA in its own image so CPU/Intel
# users don't pay the ~2GB CUDA wheel cost they'd never use.
#
# Final image size: ~1.5GB.
#
# What's layered in:
#   1. CPU PyTorch wheel — small (~800MB), enough for the native backend
#      and to satisfy ultralytics' import.
#   2. OpenVINO runtime (pip) — Intel's inference engine. Sees 'CPU' by
#      default and picks up 'GPU' / 'NPU' once /dev/dri is passed through
#      via docker-compose.intel.yml.
#   3. Intel compute runtime (intel-opencl-icd + libze1) — the userspace
#      half of the Intel GPU driver stack. Harmless on non-Intel hosts;
#      required so OpenVINO can reach the iGPU via Level Zero.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision

# Intel compute runtime: we use Debian's own intel-opencl-icd rather
# than Intel's "noble unified" apt repo because:
#
#   * Debian bookworm ships intel-opencl-icd 22.43 (compute-runtime 22.43),
#     which still bundles Gen8–Gen12 support in a single package —
#     every iGPU from Broadwell through Alder Lake-N (so every CPU
#     anyone's likely to run TrueNAS on: Pentium Gold G6xxx, Celeron
#     J-series, N100/N200/N305, UHD 610/620/630, i3–i7 iGPUs).
#   * Intel's modern `noble unified` repo dropped Gen8–Gen11 support
#     in 2024 and split the legacy drivers into -legacy1 packages
#     that are not reliably present in that channel — the build
#     kept going green while silently leaving Gen9 users without
#     a usable driver.
#
# Trade-off: Arc / Xe discrete GPUs get the older 22.43 driver rather
# than the latest. They still work, just without the newest perf
# tuning — acceptable because practically no one runs those in a
# TrueNAS box. A future opt-in Dockerfile could add the modern
# driver if that ever matters.
#
# libze1 (Level Zero loader) is still from Debian's repo — OpenVINO
# uses it for the L0 path. intel-level-zero-gpu (the L0 GPU plugin)
# is best-effort since it's not in every Debian snapshot; OpenVINO
# falls back to OpenCL when L0 isn't available, which is fine for
# Gen9–Gen11 anyway.
# intel-opencl-icd in Debian bookworm lives in the `non-free-firmware`
# component because it ships binary firmware blobs. The python:3.11-slim
# base image only enables `main` by default, so we add a supplementary
# sources.list entry that turns on contrib + non-free + non-free-firmware
# across the three standard archives (main, security, updates).
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" \
        > /etc/apt/sources.list.d/nonfree.list \
     && echo "deb http://security.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware" \
        >> /etc/apt/sources.list.d/nonfree.list \
     && echo "deb http://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware" \
        >> /etc/apt/sources.list.d/nonfree.list \
     && apt-get update && apt-get install -y --no-install-recommends \
        intel-opencl-icd \
        ocl-icd-libopencl1 \
        libze1 \
     && (apt-get install -y --no-install-recommends intel-level-zero-gpu \
         && echo "OK: intel-level-zero-gpu installed (L0 GPU backend available)" \
         || echo "WARN: intel-level-zero-gpu not in Debian repo — OpenVINO will use OpenCL only") \
     && echo "--- Installed Intel GPU drivers ---" \
     && (dpkg -l | grep -E 'intel-(opencl|level-zero)|libze' || true) \
     && echo "--- Registered OpenCL ICDs ---" \
     && (ls /etc/OpenCL/vendors/ 2>/dev/null || true) \
     && rm -rf /var/lib/apt/lists/* \
     && pip install --no-cache-dir "openvino>=2024.0.0"

# Python deps for our AI stack (ultralytics will see torch is already
# present and not re-install a different flavour over the top).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy our source + entrypoint
COPY docker-entrypoint.py /app/
COPY src/ /app/src/

# Build metadata — surfaced in the startup banner and the Status tab
# so users can tell at a glance which image is actually running
# (otherwise "I pulled the new image, right?" becomes a painful
# debugging session). Values are passed by the GitHub Actions
# workflow (--build-arg GIT_SHA=... --build-arg BUILD_TIME=...).
ARG GIT_SHA=unknown
ARG GIT_REF=unknown
ARG BUILD_TIME=unknown
ENV APP_GIT_SHA=$GIT_SHA
ENV APP_GIT_REF=$GIT_REF
ENV APP_BUILD_TIME=$BUILD_TIME
RUN printf '%s\n' \
      "git_sha: ${GIT_SHA}" \
      "git_ref: ${GIT_REF}" \
      "build_time: ${BUILD_TIME}" \
      > /app/BUILD_INFO

# Pre-download YOLOv8n model so first run is fast
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# Config volume
VOLUME ["/config"]

# Embedded line-drawing web UI (served when web_tool.enabled in config.yml).
# With network_mode: host the port is reachable directly; EXPOSE is for docs.
EXPOSE 8091

WORKDIR /app/src

# Entrypoint: if config.yml exists, use it directly. If UNIFI_HOST env var
# is set (TrueNAS app mode), generate config.yml from env vars. This keeps
# backward compat for standalone Docker users who mount their own config.
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
