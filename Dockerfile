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

# ── PyTorch wheel selection ───────────────────────────────────────────────────
# TORCH_DEVICE picks which PyTorch wheel index to install from:
#   cpu    → CPU-only (default, ~800MB image)
#   cu121  → CUDA 12.1 (GPU, ~2.5GB image, needs nvidia-container-toolkit)
#   cu118  → CUDA 11.8 (older drivers)
# Override with: docker compose build --build-arg TORCH_DEVICE=cu121
# or use docker-compose.gpu.yml which sets it for you.
ARG TORCH_DEVICE=cpu
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/${TORCH_DEVICE} \
      torch torchvision

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
