# ── Stage 1: build unifi-cam-proxy (for its protocol layer) ──────────────────
FROM python:3.11-slim AS proxy-builder

WORKDIR /build
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth=1 https://github.com/keshavdv/unifi-cam-proxy.git

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

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        gnupg \
     && mkdir -p /etc/apt/keyrings \
     && wget -qO- https://repositories.intel.com/graphics/intel-graphics.key \
        | gpg --dearmor -o /etc/apt/keyrings/intel-graphics.gpg \
     && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/intel-graphics.gpg] \
        https://repositories.intel.com/graphics/ubuntu noble main" \
        > /etc/apt/sources.list.d/intel-graphics.list \
     && apt-get update && apt-get install -y --no-install-recommends \
        intel-opencl-icd \
        libze1 \
        ocl-icd-libopencl1 \
     && apt-get purge -y --auto-remove wget gnupg \
     && rm -rf /var/lib/apt/lists/* \
     && pip install --no-cache-dir "openvino>=2024.0.0"

# Python deps for our AI stack (ultralytics will see torch is already
# present and not re-install a different flavour over the top).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy our source + entrypoint
COPY docker-entrypoint.py /app/
COPY src/ /app/src/

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
