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

## Quick start

Adoption is automated end-to-end. You only need to tell it **where** Protect
is, **who** you are, and **what RTSP URL** to watch. Everything else — the
adoption token, fake MAC addresses, the host IP, and even clicking "adopt" in
the Protect UI — is handled for you, locally, against your own UniFi
controller. No external services are contacted.

### 1. Configure

Copy `config/config.example.yml` to `config/config.yml` and fill in:

```yaml
unifi:
  host: 192.168.1.1          # your UDM / UNVR
  username: "your-protect-username"
  password: "your-protect-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

That's it — `mac`, `ip`, and `token` are all optional. If you'd rather paste
the adoption token manually instead of using credentials, see the commented
section in `config.example.yml`.

### 2. Run

```bash
docker compose up -d
```

On first run it downloads the YOLOv8n model (~6MB). The container will:

1. Log in to your UniFi controller and pull a fresh adoption token
2. Generate a stable fake MAC for each camera (derived from its name)
3. Register each camera with Protect
4. Auto-accept the pending adoption in Protect so you don't have to click through

Watch `docker compose logs -f` — you should see "Auto-adopted camera …"
messages within a minute.

## Multi-camera

Each entry under `cameras:` becomes an independent virtual device in Protect.
Fake MAC addresses are auto-generated from each camera's name (deterministic,
so restarts don't create duplicate "pending" entries). You can still specify
a `mac:` manually if you want to pick your own.

## Model options

| Model      | Speed  | Accuracy | RAM    |
|------------|--------|----------|--------|
| yolov8n.pt | Fast   | Good     | ~400MB |
| yolov8s.pt | Medium | Better   | ~600MB |
| yolov8m.pt | Slower | Best     | ~1.2GB |

## Acceleration

The default image (`Dockerfile`, ~1.5GB) ships **CPU PyTorch + OpenVINO +
the Intel compute runtime**, so on any CPU or Intel iGPU/dGPU/NPU host it
just works — no rebuild, no config twiddling. On startup AIEngine probes
every reachable runtime and picks the fastest one automatically:

```
cuda → intel:gpu → intel:npu → mps → cpu
```

Check `docker compose logs -f` for `Running inference on: …` to see which
it picked. You can override the probe by setting `ai.device` in
`config.yml` to any of those targets.

### Intel iGPU / dGPU / NPU (default image)

For Intel N100-class mini PCs and similar — the chip in a £100 Beelink box
can run YOLOv8n 2–3× faster on its integrated UHD graphics than on its
own CPU cores, via OpenVINO. Also works for Intel Arc discrete GPUs and
Meteor/Arrow Lake NPUs.

No separate image — layer `docker-compose.intel.yml` just to pass
`/dev/dri` into the container:

```bash
docker compose -f docker-compose.yml -f docker-compose.intel.yml up -d
```

Targets you can set in `ai.device` (or leave on `auto` and let it pick):

| Value | Target | Notes |
|---|---|---|
| `intel:gpu` | Integrated or discrete Intel GPU | Best default for N100 etc. |
| `intel:cpu` | Intel CPU via OpenVINO | Often faster than native PyTorch CPU |
| `intel:npu` | Meteor Lake / Arrow Lake NPU | Bleeding edge, lowest power |

On first run YOLOv8 is exported to OpenVINO IR format (one-time, ~30s);
the result is cached under `./config/yolov8n_openvino_model/` so restarts
are instant.

Requirements on the host: an Intel iGPU/dGPU with the in-kernel driver
(standard on any modern Linux — check `ls /dev/dri`), and the docker user
in the `render` group so the container can reach `/dev/dri/renderD128`:

```bash
sudo usermod -aG render $USER && newgrp render
```

### NVIDIA GPU (CUDA variant image)

NVIDIA CUDA lives in a separate image (`Dockerfile.cuda`, ~2.5GB) so CPU
and Intel users don't pay the CUDA wheel overhead they'd never use.
Requires an NVIDIA GPU, a recent driver, and
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Layer the GPU override on top of the base compose file:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The override tells Compose to build from `Dockerfile.cuda` (CUDA 12.1
PyTorch wheel) and reserves the GPU for the container. Check
`docker compose logs -f` — AIEngine logs `Running inference on: cuda`
when the GPU is live, or `cpu` if something went wrong with the
passthrough.

The CUDA variant image does NOT include OpenVINO / Intel runtime — if
you somehow have a host with both an NVIDIA dGPU and an Intel iGPU and
want to use both, build a custom merged image.

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

## Known limitations

- Smart detection injection can be fragile across Protect firmware updates
  (upstream issue in unifi-cam-proxy too)
- Line crossing events appear as person/vehicle detections in Protect — there
  is no separate "line crossing" event type in the Protect UI
- Facial recognition and LPR are not implemented (those require the AI Key's
  closed pipeline)

## Credits

Protocol implementation based on
[unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) by keshavdv.
