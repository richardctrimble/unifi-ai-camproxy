# ── Stage 1: build unifi-cam-proxy (for its protocol layer) ──────────────────
FROM python:3.11-slim AS proxy-builder

WORKDIR /build
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth=1 https://github.com/keshavdv/unifi-cam-proxy.git

# ── Stage 2: final image ──────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="unifi-ai-port"
LABEL description="DIY UniFi AI Port — person/vehicle detection + line crossing"

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
RUN pip install --no-cache-dir -e /app/unifi-cam-proxy

# ── Accelerator selection ─────────────────────────────────────────────────────
# TORCH_DEVICE picks the runtime that ships in the image:
#   cpu    → CPU-only PyTorch (default, ~800MB image)
#   cu121  → CUDA 12.1 PyTorch (~2.5GB, needs nvidia-container-toolkit)
#   cu118  → CUDA 11.8 PyTorch (older NVIDIA drivers)
#   intel  → CPU PyTorch + OpenVINO + Intel compute runtime
#            (~1.5GB, needs /dev/dri passthrough — see docker-compose.intel.yml)
#
# Override with: docker compose build --build-arg TORCH_DEVICE=<value>
# or use the matching docker-compose.<gpu|intel>.yml override file.
ARG TORCH_DEVICE=cpu

# Always install torch (CPU index for intel builds — OpenVINO does the heavy
# lifting, torch is just there so ultralytics loads).
RUN if [ "${TORCH_DEVICE}" = "intel" ]; then \
        pip install --no-cache-dir \
          --index-url https://download.pytorch.org/whl/cpu \
          torch torchvision; \
    else \
        pip install --no-cache-dir \
          --index-url https://download.pytorch.org/whl/${TORCH_DEVICE} \
          torch torchvision; \
    fi

# For Intel builds, layer on OpenVINO + the Intel compute runtime that lets
# it reach the iGPU via /dev/dri. intel-opencl-icd provides the Level Zero
# OpenCL driver; libze1 is the Level Zero loader OpenVINO talks to.
RUN if [ "${TORCH_DEVICE}" = "intel" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            intel-opencl-icd \
            libze1 \
            ocl-icd-libopencl1 \
         && rm -rf /var/lib/apt/lists/* \
         && pip install --no-cache-dir openvino>=2024.0.0; \
    fi

# Python deps for our AI stack (ultralytics will see torch is already
# present and not re-install a different flavour over the top).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy our source
COPY src/ /app/src/

# Pre-download YOLOv8n model so first run is fast
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# Config volume
VOLUME ["/config"]

# Embedded line-drawing web UI (served when web_tool.enabled in config.yml).
# With network_mode: host the port is reachable directly; EXPOSE is for docs.
EXPOSE 8091

WORKDIR /app/src

CMD ["python", "main.py", "/config/config.yml"]
