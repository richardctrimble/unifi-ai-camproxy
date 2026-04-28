# unifi-ai-camproxy

[![Docker](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml)

A DIY UniFi camera AI tooling. Two modes ship side by side:

- **ONVIF bridge** *(`:latest`, primary, lightweight ~150 MB)* — your
  cameras are adopted natively in Protect; we subscribe to their ONVIF
  event streams and bridge person/vehicle/motion/line-crossing events
  into Protect's timeline as bookmarks + Alarm Manager triggers. No
  spoofing, no transcoding, native H.265.
- **Full mode** *(`:full`, heavier ~2.5 GB)* — spoofs an RTSP camera as
  a UniFi camera in Protect and runs YOLOv8 inference locally
  (CPU/Intel/CUDA). Useful when your cameras don't have onboard AI.

> **Status**: ONVIF bridge is **in development** — discovery is live,
> ONVIF subscriptions work, and alarm webhooks fire. The web UI now has
> credential management tabs (UniFi + ONVIF). Full mode (`:full` tag) is
> stable and production-ready; bridge mode is actively being tested.

## How it works (full mode, today)

```
RTSP camera → YOLOv8 inference → UniFi WebSocket protocol → Protect
```

Each entry under `cameras:` becomes an independent adopted camera in
Protect with smart-detect events, bounding boxes, thumbnails and
timeline entries driven by our AI pipeline.

## How it will work (ONVIF bridge, target)

```
ONVIF camera → adopted natively in Protect (video, H.265 native)
            ↓
            ONVIF event subscription (motion / person / vehicle / line)
            ↓
        unifi-ai-camproxy bridge
            ↓
        Protect bookmarks + Alarm Manager webhooks
```

No video transit through the bridge, no GPU on our side. Camera's own
onboard AI does the heavy lifting; we translate its events into
something Protect's UI surfaces.

## Prerequisites

- Docker + Compose v2 on a Linux host
- UniFi Protect on a UDM / UDM Pro / UNVR
- A **UniFi OS** user with Protect admin rights
- For full mode: at least one RTSP camera on the LAN
- For bridge mode: cameras adopted to Protect via ONVIF

## Quick start (full mode — works today)

```bash
git clone https://github.com/richardctrimble/unifi-ai-camproxy.git
cd unifi-ai-camproxy
docker compose -f docker-compose.yml -f docker-compose.full.yml up -d --build
```

Open `http://<docker-host>:8091/` and:

1. **UniFi** tab — enter host + username + password, click *Test*, *Save*.
2. **Setup** tab — *+ Add Camera*, paste an RTSP URL, *Save All*.
3. Restart the container.

The camera auto-adopts. If a pending record gets stuck with a bad IP,
the UniFi tab's *Cameras in Protect* panel can remove it.

## Quick start (ONVIF bridge — in development)

```bash
docker compose up -d --build
```

This builds the `:latest` image. It discovers ONVIF cameras adopted in Protect,
subscribes to their event streams, and bridges events into Protect's Alarm
Manager. Open `http://<docker-host>:8091/` and configure:

1. **UniFi Creds** tab — enter Protect host, username + password (click Test),
   API key (click Test), Save.
2. **ONVIF Creds** tab — enter fleet ONVIF creds (username + password + port).
   Per-camera topics appear once subscriptions connect.
3. **Alarm Setup** tab — for each (camera, kind) row you want to react to,
   create a matching Custom Webhook alarm rule in Protect with the Trigger ID
   shown here.

Discovery runs every 60s. Cameras show on the **Status** tab.

## Images

| Tag | Built by | Size | Use |
|---|---|---|---|
| `:latest` | `Dockerfile` | ~150 MB | ONVIF bridge (primary, in preparation) |
| `:full` | `Dockerfile.full` | ~2.5 GB | Full spoof+inference: CPU + Intel OpenVINO |
| `:full-cuda` | `Dockerfile.full-cuda` | ~3 GB | Full + NVIDIA CUDA (opt-in CI build) |

Calver tags get matching variants:
`:2026.4.14`, `:2026.4.14-full`, `:2026.4.14-full-cuda`.

## Hardware acceleration (full mode only)

Inference device is per-camera. Set it in the Setup tab or leave as
`auto` (probes in order: `cuda` → `intel:gpu` → `intel:npu` → `mps` →
`cpu`).

**Intel iGPU / dGPU / NPU**:

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.full.yml \
  -f docker-compose.intel.yml up -d
```

Host needs `/dev/dri` and your user in the `render` group.

**NVIDIA CUDA** (requires [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)):

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.full.yml \
  -f docker-compose.gpu.yml up -d --build
```

The bridge mode has no GPU dependency — that's the point.

## Web UI

### Bridge mode (`:latest`, primary)

Five tabs at `http://<docker-host>:8091/`:

- **Status** — live health: discovered cameras, per-camera ONVIF subscription
  state, last event, alarm trigger counters, discovery error (with link to fix).
- **UniFi Creds** — Protect host, username + password (Test button), API key
  (Test button), save to config.yml.
- **ONVIF Creds** — fleet ONVIF username + password + port, per-camera topics
  that each camera advertised via GetEventProperties.
- **Alarm Setup** — per-(camera, kind) webhook IDs with copy buttons and live
  firing status. Guides user through creating one Custom Webhook Alarm Manager
  rule per row in Protect.
- **Logs** — tails `/config/camproxy.log` with password redaction.

### Full mode (`:full`, legacy)

Five tabs at `http://<docker-host>:8091/`:

- **Status** — live health: per-camera state, inference device, frame
  counters, detection totals, auth lockout / token refresh stats,
  disk/memory/heartbeat, advertised LAN IP.
- **UniFi** — credentials with per-type Test buttons, plus a *Cameras
  in Protect* panel that lists everything the controller knows about
  and can remove stuck pending adoptions.
- **Setup** — add/edit cameras. Per-camera knobs: RTSP URL + transport,
  Protect model, AI device + model, confidence thresholds, frame skip,
  include-audio toggle, H.265 → H.264 transcode toggle.
- **Lines** — click two points on a live frame to draw a virtual
  crossing line.
- **Logs** — tails `/config/camproxy.log` with password redaction.

## Configuration reference

Minimum `config/config.yml` for bridge mode:

```yaml
unifi:
  host: 192.168.1.1
  username: "your-unifi-os-username"
  password: "your-unifi-os-password"
  api_key: "your-protect-api-key"

onvif:
  username: "admin"
  password: "password"
  port: 80
```

Cameras are auto-discovered from Protect. Per-camera ONVIF credential overrides
can be added under `cameras: [...]` (see `SECONDBRAIN.md`).

Minimum `config/config.yml` for full mode:

```yaml
unifi:
  host: 192.168.1.1
  username: "your-unifi-os-username"
  password: "your-unifi-os-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

## Troubleshooting

Most problems surface clearly in the **Logs** tab.

**Camera stuck pending / "An error occurred" on Adopt** *(full mode)*
Protect cached a bad IP from an early adoption attempt. UniFi tab →
*Cameras in Protect* → Refresh → **Remove** the stuck entry → restart.

**Video is black in Protect** *(full mode)*
Source is probably H.265 — Setup → *Transcode to H.264*. If you
enabled *Include audio* and the source has none, uncheck it.

**UniFi login rejected / rate-limited**
Must be a **UniFi OS** user, not a Protect-app-only account. Use the
UniFi tab's *Test username + password* button. After repeated failures
the app backs off for 10–15 min; saving new creds clears the cooldown.

## TrueNAS Scale

`truenas/docker-compose.yaml` has a paste-in template. Today it pulls
`:latest` (the bridge stub). **For working AI today, edit the image
line to `:full` (or a `:YYYY.M.R-full` tag) until the bridge ships.**

## Versioning

Calendar versioning `YYYY.M.R` (e.g. `2026.4.14`). Pushing a tag
triggers CI to publish `:<tag>`, `:<tag>-full`, and (opt-in)
`:<tag>-full-cuda` images to GHCR.

## Known limitations

- ONVIF bridge mode is in preparation — see SECONDBRAIN.md for the
  open verification questions and roadmap.
- Full mode's smart-detect injection can break across Protect firmware
  updates. The protocol is reverse-engineered and a moving target —
  this is the main reason for the bridge pivot.
- Line crossings appear as person/vehicle detections; Protect has no
  separate "line crossing" event type.
- Facial recognition and LPR are not implemented — those require
  Ubiquiti's closed AI Key pipeline.

## Credits

Full-mode protocol implementation built on
[unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) by
keshavdv. Upstream is pinned in `Dockerfile.full` — see
`SECONDBRAIN.md` for the pin rationale.
