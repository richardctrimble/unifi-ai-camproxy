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

# Python deps for our AI stack
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
