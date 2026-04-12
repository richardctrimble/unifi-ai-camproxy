# unifi-ai-port

A DIY UniFi AI Port — runs on any x86 machine, spoofs as a UniFi camera in
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
git clone https://github.com/richardctrimble/happy-ai-port.git
cd happy-ai-port
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
docker build -t unifi-ai-port:latest .

# NVIDIA CUDA variant (~2.5GB) — for hosts with an NVIDIA GPU
docker build -f Dockerfile.cuda -t unifi-ai-port:cuda .
```

| Image | Dockerfile | Size | Covers |
|---|---|---|---|
| `unifi-ai-port:latest` | `Dockerfile` | ~1.5GB | CPU, Intel iGPU/dGPU/NPU, Apple MPS |
| `unifi-ai-port:cuda` | `Dockerfile.cuda` | ~2.5GB | NVIDIA CUDA + CPU fallback |

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

### `web_tool` — embedded line-drawing UI

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Serve the web UI on the port below |
| `port` | `8091` | HTTP port for the line-drawing tool |

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

Don't try to eyeball coordinates — the container ships an embedded line
tool. Once `docker compose up` is running, open:

```
http://<docker-host-ip>:8091/
```

in any browser (phone / iPad works fine). Pick a camera from the dropdown,
click two points on the live frame, tweak the name and direction, then
copy the generated YAML into your `config/config.yml` under that camera's
`ai.lines:` block and `docker compose restart` to apply.

Existing lines are rendered as dashed grey overlays so you can see what
you've already got.

Disable the tool by setting `web_tool.enabled: false` in config.yml.

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

The repo includes a TrueNAS Scale custom app catalog under `truenas/`.
TrueNAS 24.10+ (Electric Eel) runs apps as Docker Compose stacks, so
this works natively.

### Install from the catalog

1. In TrueNAS: **Apps > Discover > Manage Catalogs > Add Catalog**
2. Point it at this repo (or a fork), branch `main`, path `truenas`
3. The app appears in the catalog — click **Install**
4. Fill in the wizard: Protect host, credentials, camera RTSP URLs, model,
   GPU toggle
5. Choose a dataset path for persistent config (e.g.
   `/mnt/pool/apps/unifi-ai-port`)
6. Click **Install** — the container starts and adopts cameras automatically

### How it works

The TrueNAS install wizard (defined in `questions.yaml`) passes your
answers as environment variables. The container's entrypoint generates
`config.yml` from those env vars on first boot. After that the file
persists on your dataset — edit it directly for advanced settings like
virtual lines (or use the web tool at `http://<truenas-ip>:8091/`).

### Intel iGPU on TrueNAS

Enable "Intel GPU Passthrough" in the app settings. This passes
`/dev/dri` into the container so OpenVINO can use the iGPU. Your
TrueNAS user needs to be in the `render` group.

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
