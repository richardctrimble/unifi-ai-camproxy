# unifi-ai-camproxy

[![Docker](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml)

A DIY UniFi AI camera proxy — runs on any x86 machine, spoofs as a UniFi camera in
Protect, and injects real-time person/vehicle detections and virtual line
crossing events from your own RTSP cameras.

## How it works

```
RTSP camera → YOLOv8 inference → UniFi WebSocket protocol → Protect
```

Each camera in your config appears as a separate adopted camera in UniFi
Protect with full smart detection support (person/vehicle bounding boxes,
thumbnails, timeline events).

## Prerequisites

- **Docker** and **Docker Compose** (v2) on an x86 Linux machine
- **UniFi Protect** running on a UDM, UDM Pro, UNVR, or similar
- At least one **RTSP camera** reachable on the same network
- A **local Protect account** (username + password) — used to fetch the
  adoption token and auto-accept cameras. No cloud account needed.

## Quick start

### 1. Clone

```bash
git clone https://github.com/richardctrimble/unifi-ai-camproxy.git
cd unifi-ai-camproxy
```

### 2. Configure

Copy the example and fill in your details:

```bash
cp config/config.example.yml config/config.yml
```

The minimum you need:

```yaml
unifi:
  host: 192.168.1.1          # your UDM / UNVR IP
  username: "your-protect-username"
  password: "your-protect-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

That's it — `mac`, `ip`, `token`, and all AI settings are optional with
sensible defaults. See [Configuration reference](#configuration-reference)
for every option.

### 3. Build and run

```bash
docker compose up -d --build
```

On first run the container will:

1. Log in to your UniFi controller and pull a fresh adoption token
2. Generate a stable fake MAC for each camera (derived from its name)
3. Register each camera with Protect over WebSocket
4. Auto-accept the pending adoption so you don't have to click through
5. Start YOLOv8 inference and inject detections into Protect's timeline

Watch the logs:

```bash
docker compose logs -f
```

You should see `Auto-adopted camera …` messages within a minute.

### 4. Verify

Open UniFi Protect. Each camera should appear as an adopted device with
smart detections (person/vehicle) showing on the timeline.

## Building the Docker image

The default image (~1.5GB) ships **CPU PyTorch + Intel OpenVINO** and
covers the majority of setups. Building is handled automatically by
`docker compose up --build`, but you can also build manually:

```bash
# Default image (CPU + Intel OpenVINO)
docker build -t unifi-ai-camproxy:latest .

# NVIDIA CUDA variant (~2.5GB) — for hosts with an NVIDIA GPU
docker build -f Dockerfile.cuda -t unifi-ai-camproxy:cuda .
```

| Image | Dockerfile | Size | Covers |
|---|---|---|---|
| `unifi-ai-camproxy:latest` | `Dockerfile` | ~1.5GB | CPU, Intel iGPU/dGPU/NPU, Apple MPS |
| `unifi-ai-camproxy:cuda` | `Dockerfile.cuda` | ~2.5GB | NVIDIA CUDA + CPU fallback |

## Acceleration

On startup AIEngine probes every reachable runtime and picks the fastest
automatically:

```
cuda → intel:gpu → intel:npu → mps → cpu
```

Check `docker compose logs -f` for `Running inference on: …` to see what
it picked. Override the probe with `ai.device` in `config.yml`.

### Intel iGPU / dGPU / NPU (default image)

The default image already ships OpenVINO — layer `docker-compose.intel.yml`
to pass `/dev/dri` into the container (no rebuild):

```bash
docker compose -f docker-compose.yml -f docker-compose.intel.yml up -d
```

Requirements on the host:

```bash
# Check that /dev/dri exists (card0 + renderD128)
ls /dev/dri

# Add your user to the render group so the container can open renderD128
sudo usermod -aG render $USER && newgrp render
```

| `ai.device` value | Target | Notes |
|---|---|---|
| `intel:gpu` | Intel iGPU / dGPU | Best for N100, Iris Xe, Arc |
| `intel:cpu` | Intel CPU via OpenVINO | Often faster than native PyTorch CPU |
| `intel:npu` | Meteor Lake / Arrow Lake NPU | Lowest power, bleeding edge |

On first run YOLOv8 is exported to OpenVINO IR format (~30s); the result
is cached under `./config/yolov8n_openvino_model/` so restarts are instant.

### NVIDIA GPU (CUDA variant image)

CUDA lives in a separate `Dockerfile.cuda` so CPU/Intel users don't pull
a ~2.5GB image they don't need. Requires
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

## Configuration reference

All settings live in `config/config.yml`. See `config/config.example.yml`
for a fully commented template with three example cameras.

### `unifi` — controller connection

| Key | Required | Default | Description |
|---|---|---|---|
| `host` | **yes** | — | IP or hostname of your UDM / UNVR |
| `username` | recommended | — | Local Protect account username |
| `password` | recommended | — | Local Protect account password |
| `token` | no | auto-fetched | Manual adoption token (skip username/password) |

### `web_tool` — embedded configuration + line-drawing UI

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Serve the web UI on the port below |
| `port` | `8091` | HTTP port for the config + line-drawing UI |

### `cameras[]` — one entry per virtual camera

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | **yes** | — | Display name in Protect |
| `rtsp_url` | **yes** | — | RTSP stream URL |
| `snapshot_url` | no | from AI engine | HTTP URL for still snapshots |
| `mac` | no | auto from name | Fake MAC (deterministic from name) |
| `ip` | no | auto-detected | IP advertised to Protect |

### `cameras[].ai` — per-camera AI settings

| Key | Default | Description |
|---|---|---|
| `model` | `yolov8n.pt` | YOLO model file (`yolov8n`/`yolov8s`/`yolov8m`) |
| `device` | `auto` | Inference device — see [Acceleration](#acceleration) |
| `confidence` | `0.45` | Fallback confidence threshold (0.0–1.0) |
| `confidence_person` | `confidence` | Override threshold for persons |
| `confidence_vehicle` | `confidence` | Override threshold for vehicles |
| `detect_persons` | `true` | Enable person detection |
| `detect_vehicles` | `true` | Enable vehicle detection |
| `frame_skip` | `3` | Analyse every Nth frame (lower = more CPU) |
| `ai_udp_port` | `5200` (auto) | Loopback UDP port used to share the video1 ffmpeg stream with the AI engine. Ports are auto-assigned per camera (5200, 5201, …). Override when you need deterministic ports or have a firewall rule blocking the default range. Each camera **must** use a unique port. |

### `cameras[].ai.lines[]` — virtual line crossing

Lines are defined per-camera in normalised (0–1) coordinates. Use the
web tool at `http://<docker-host>:8091/` to draw them visually.

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | **yes** | — | Label shown in detection events |
| `x1`, `y1` | **yes** | — | First endpoint (0.0–1.0) |
| `x2`, `y2` | **yes** | — | Second endpoint (0.0–1.0) |
| `direction` | no | `both` | `both`, `left_to_right`, `right_to_left`, `top_to_bottom`, `bottom_to_top` |

## Virtual line crossing

Lines are defined per-camera as two points in normalised (0–1) coordinates
where `(0, 0)` is the top-left of the frame and `(1, 1)` the bottom-right.
When a tracked object's centroid crosses the line segment, a discrete
smart-detection event is injected into Protect's timeline.

### Drawing a line visually

Don't try to eyeball coordinates — the container ships an embedded web
UI. Once `docker compose up` is running, open:

```
http://<docker-host-ip>:8091/
```

in any browser (phone / iPad works fine). Click the **Lines** tab, pick
a camera from the dropdown, click two points on the live frame, set the
name and direction, then click **Save Line**. The line is written
directly to `config.yml` — restart the container to apply.

Existing lines are rendered as dashed grey overlays so you can see what
you've already got. You can also delete lines from the list below the
frame.

Disable the web UI by setting `web_tool.enabled: false` in config.yml.

## Multi-camera

Each entry under `cameras:` becomes an independent virtual device in
Protect. Fake MAC addresses are auto-generated from each camera's name
(deterministic, so restarts don't create duplicate "pending" entries).
You can still specify a `mac:` manually if you want to pick your own.

Each camera can have its own AI settings — different model, confidence
thresholds, detection classes, and virtual lines. See
`config/config.example.yml` for a three-camera example.

## Model options

| Model      | Speed  | Accuracy | RAM    |
|------------|--------|----------|--------|
| yolov8n.pt | Fast   | Good     | ~400MB |
| yolov8s.pt | Medium | Better   | ~600MB |
| yolov8m.pt | Slower | Best     | ~1.2GB |

## Troubleshooting

**Camera stuck on "Adopting" in Protect**
- Check the container logs for errors during the WebSocket handshake
- Ensure `network_mode: host` is set in docker-compose.yml (the container
  must be on the same subnet as Protect)
- Try removing the camera from Protect and restarting the container

**"Running inference on: cpu" when you expected GPU**
- NVIDIA: make sure `docker-compose.gpu.yml` is layered on and
  nvidia-container-toolkit is installed on the host
- Intel: make sure `docker-compose.intel.yml` is layered on and
  `/dev/dri` exists. Check render group: `groups | grep render`
- Run `docker compose logs | grep "Running inference on"` to see what
  was auto-detected

**"Could not fetch an adoption token"**
- Verify your username/password work in the Protect web UI
- Some Protect firmware versions use different API paths — fall back to
  manual token: get it from `https://<host>/proxy/protect/api/cameras/qr`,
  decode the QR, and set `unifi.token` in config.yml

**Detections not showing in Protect timeline**
- Smart detection injection depends on the Protect firmware version —
  major firmware updates can break the protocol
- Check `docker compose logs | grep "Detection START"` to verify the AI
  engine is seeing objects
- Lower `confidence` or `confidence_person` if detections are being
  filtered out

**High CPU usage**
- Increase `frame_skip` (e.g. 5 or 10) to analyse fewer frames
- Use `yolov8n.pt` (fastest model)
- Enable hardware acceleration (Intel iGPU or NVIDIA GPU)

## TrueNAS Scale app

TrueNAS Scale 24.10+ (Electric Eel) replaced the old Kubernetes/Helm
app system with native Docker Compose. Custom app catalogs are no
longer supported — instead you deploy custom apps by pasting a Docker
Compose YAML directly in the TrueNAS UI.

A ready-to-paste compose file is included at `truenas/docker-compose.yaml`.

### Prerequisites

- TrueNAS Scale **24.10 (Electric Eel)** or newer
- A dataset for persistent config (e.g. create `apps/unifi-ai-camproxy`
  under your pool)
- Your UniFi Protect controller IP and a local account (username + password)

### Step 1 — Install via YAML

1. Open the TrueNAS web UI
2. Go to **Apps** in the left sidebar
3. Click **Discover Apps**
4. Click **Custom App**
5. Click **Install via YAML**
6. Paste the contents of `truenas/docker-compose.yaml` into the editor
   (or copy it from below)
7. Edit the four `CHANGE ME` values:
   - **Volume path** — your TrueNAS dataset (e.g. `/mnt/pool/apps/unifi-ai-camproxy`)
   - **UNIFI_HOST** — IP of your UDM / UDM Pro / UNVR
   - **UNIFI_USERNAME** — local Protect account username
   - **UNIFI_PASSWORD** — local Protect account password
8. (Optional) To enable Intel iGPU acceleration, uncomment the `devices`
   and `group_add` sections at the bottom
9. Click **Save** — TrueNAS pulls the container image and starts it

<details>
<summary>Click to expand the Docker Compose YAML</summary>

```yaml
services:
  unifi-ai-camproxy:
    image: ghcr.io/richardctrimble/unifi-ai-camproxy:latest
    restart: unless-stopped
    network_mode: host
    volumes:
      # CHANGE ME: set to a TrueNAS dataset path
      - /mnt/pool/apps/unifi-ai-camproxy:/config
    environment:
      - PYTHONUNBUFFERED=1
      # CHANGE ME: IP of your UDM / UDM Pro / UNVR
      - UNIFI_HOST=192.168.1.1
      # CHANGE ME: local Protect account username
      - UNIFI_USERNAME=your-username
      # CHANGE ME: local Protect account password
      - UNIFI_PASSWORD=your-password
      - WEB_TOOL_PORT=8091
    # Uncomment for Intel iGPU acceleration (OpenVINO):
    # devices:
    #   - /dev/dri:/dev/dri
    # group_add:
    #   - video
    #   - render
```

</details>

### Step 2 — Add cameras via the web UI

On first boot the container starts in **web-only mode** (no cameras yet).

1. Open your browser and go to `http://<truenas-ip>:8091/`
   (replace with your TrueNAS IP and the port you chose)
2. You'll see the **Setup** tab — click **+ Add Camera**
3. Fill in the camera details:
   - **Name** — what this camera will be called in Protect
   - **RTSP URL** — the camera's RTSP stream address
   - Adjust AI settings if needed (model, confidence, frame skip, etc.)
4. Add more cameras if you want (up to ~5 per instance, depending on hardware)
5. Click **Save All**
6. Go back to TrueNAS and **restart the app** (Apps > your app > Restart)

The container will now adopt each camera into Protect and start running
AI inference.

### Step 3 — Draw virtual lines (optional)

1. Open the web UI again (`http://<truenas-ip>:8091/`)
2. Click the **Lines** tab
3. Pick a camera from the dropdown — you'll see a live frame
4. Click two points on the frame to draw a line
5. Set the line name and direction, then click **Save Line**
6. Restart the app to apply

### How it works under the hood

The Docker Compose file passes your Protect credentials as environment
variables. On first boot, the container's entrypoint
(`docker-entrypoint.sh`) generates a minimal `config.yml` with your
credentials and `cameras: []`. The app starts in web-only mode, serving
the config UI.

Once you add cameras via the web UI, the config is written to your
dataset. After a restart, the app reads the full config, adopts the
cameras into Protect, and starts AI inference.

The config file persists on your TrueNAS dataset, so updates and
restarts don't lose your settings.

### Intel iGPU on TrueNAS

Uncomment the `devices` and `group_add` sections in the compose YAML
to pass `/dev/dri` into the container. This lets OpenVINO use the
Intel iGPU for 2-3x faster inference.

### Alternative: SSH / CLI install

Since TrueNAS 24.10 ships native Docker tools, you can also deploy
via SSH:

```bash
ssh root@<truenas-ip>
mkdir -p /mnt/pool/apps/unifi-ai-camproxy
cd /mnt/pool/apps/unifi-ai-camproxy
# copy docker-compose.yaml here and edit it, then:
docker compose up -d
```

### Updating

Pull the latest image and recreate:

```bash
docker compose pull && docker compose up -d
```

Or in the TrueNAS UI, edit the custom app and re-save to trigger a
fresh image pull.

## Versioning

This project uses **calendar versioning** in the format `YYYY.M.R`:

| Part | Meaning | Example |
|------|---------|---------|
| `YYYY` | Four-digit year | `2026` |
| `M` | Month (no leading zero) | `4` |
| `R` | Release number within that month | `1`, `2`, `3`, … |

Example tags: `2026.4.1`, `2026.4.2`, `2026.5.1`.

To create a release:

```bash
git tag 2026.4.1
git push origin 2026.4.1
```

This triggers the CI workflow to build and push Docker images tagged with the
version (e.g. `:2026.4.1` and `:2026.4.1-cuda`).

## Known limitations

- Smart detection injection can be fragile across Protect firmware updates
  (upstream issue in unifi-cam-proxy too)
- Line crossing events appear as person/vehicle detections in Protect —
  there is no separate "line crossing" event type in the Protect UI
- Facial recognition and LPR are not implemented (those require the AI
  Key's closed pipeline)

## Credits

Protocol implementation based on
[unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) by keshavdv.
