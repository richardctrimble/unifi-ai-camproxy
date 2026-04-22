# unifi-ai-camproxy

[![Docker](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml)

A DIY UniFi AI camera proxy — spoofs RTSP cameras into UniFi Protect and
injects real-time person / vehicle detections and virtual line crossing
events driven by your own YOLOv8 inference.

## How it works

```
RTSP camera → YOLOv8 inference → UniFi WebSocket protocol → Protect
```

Each entry under `cameras:` becomes an independent adopted camera in
Protect with smart-detect events, bounding boxes, thumbnails and
timeline entries driven from our AI pipeline.

## Prerequisites

- Docker + Compose v2 on an x86 Linux host
- UniFi Protect running on a UDM / UDM Pro / UNVR
- At least one RTSP camera on the LAN
- A **UniFi OS** user with Protect admin rights (local Protect-app-only
  accounts do not work)

## Quick start

```bash
git clone https://github.com/richardctrimble/unifi-ai-camproxy.git
cd unifi-ai-camproxy
docker compose up -d --build
```

Open `http://<docker-host>:8091/` and:

1. **UniFi** tab — enter host + username + password, click
   *Test username + password*, then *Save to config.yml*.
2. **Setup** tab — click *+ Add Camera*, enter an RTSP URL, *Save All*.
3. Restart the container (`docker compose restart` or the *Restart
   Container* button on the Status tab).

The camera auto-adopts into Protect. If a pending record is stuck with
a bad IP, the UniFi tab's *Cameras in Protect* panel can remove it —
Protect 7.x's own UI no longer offers a Forget action for pending
cameras.

## Images

| Tag | Size | Covers |
|---|---|---|
| `unifi-ai-camproxy:latest` | ~2.5 GB | CPU, Intel iGPU/dGPU/NPU, Apple MPS |
| `unifi-ai-camproxy:cuda` | ~3 GB | NVIDIA CUDA + CPU fallback |

Build manually:

```bash
docker build -t unifi-ai-camproxy:latest .
docker build -f Dockerfile.cuda -t unifi-ai-camproxy:cuda .
```

## Hardware acceleration

Inference device is per-camera — a host with both an Intel iGPU and an
NVIDIA card can run each camera on a different backend. Set it in the
Setup tab or leave as `auto` (probes in order: `cuda` → `intel:gpu` →
`intel:npu` → `mps` → `cpu`). The Status tab shows which backends this
image can reach and which one each camera is actually running on.

**Intel iGPU / dGPU / NPU** (default image, OpenVINO):

```bash
docker compose -f docker-compose.yml -f docker-compose.intel.yml up -d
```

Host needs `/dev/dri` passed through and your user in the `render`
group (`sudo usermod -aG render $USER && newgrp render`).

**NVIDIA CUDA** (requires [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

On first run YOLOv8 exports to OpenVINO IR (~30 s) and caches under
`./config/yolov8n_openvino_model/` so subsequent starts are instant.

## Web UI

Five tabs at `http://<docker-host>:8091/`:

- **Status** — live health: per-camera state, inference device, frame
  counters, detection totals, auth lockout / token refresh stats,
  disk/memory/heartbeat, advertised LAN IP (amber if loopback — a trap
  Protect caches forever).
- **UniFi** — credentials with per-type Test buttons, plus a *Cameras
  in Protect* panel that lists everything the controller knows about
  and can remove stuck pending adoptions.
- **Setup** — add/edit cameras. Per-camera knobs: RTSP URL + transport,
  Protect model (AI variants by default), AI device + model,
  confidence thresholds, frame skip, include-audio toggle, H.265 →
  H.264 transcode toggle.
- **Lines** — click two points on a live frame to draw a virtual
  crossing line. Saves to `config.yml`; restart to apply.
- **Logs** — tails `/config/camproxy.log` with password redaction,
  optional auto-refresh.

All edits write to `config/config.yml`. Most changes need a container
restart; the UI says so.

## Configuration reference

Minimum `config/config.yml`:

```yaml
unifi:
  host: 192.168.1.1
  username: "your-unifi-os-username"
  password: "your-unifi-os-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

Everything else has sensible defaults. The web UI is the canonical
editor for per-camera knobs. See `config/config.example.yml` for a
fully-commented template covering every option.

## Troubleshooting

Most problems surface clearly in the **Logs** tab.

**Camera stuck pending / "An error occurred" on Adopt**
Protect cached the wrong IP (commonly `127.0.0.1` from an early
adoption attempt before network was ready). UniFi tab → *Cameras in
Protect* → Refresh → **Remove** the stuck entry → restart.

**Video is black in Protect**
Source is probably H.265 — Protect only accepts H.264 from spoofed
cameras. Setup tab → check *Transcode to H.264* for that camera. If
you enabled *Include audio* and the source has none, uncheck it
(ffmpeg fails silently when the AAC encoder has no input).

**UniFi login rejected / rate-limited**
Must be a **UniFi OS** user, not a Protect-app-only account. Use the
UniFi tab's *Test username + password* button. After repeated failures
the app backs off token refresh for 10–15 min to avoid a hard Protect
lockout; saving new creds clears that cooldown immediately.

**Inference device shows as `cpu` when you expected GPU**
Status tab → *Available backends*. Any ✗ grey entry means the
host-side passthrough isn't wired up (Intel `/dev/dri` missing, or
NVIDIA runtime not installed).

**Detections not showing on Protect's timeline**
Smart-detect is gated by the camera model string. Default is
`UVC AI Pro`; if you switched to a non-AI model (e.g. `UVC G4 Pro`),
Protect silently drops our events. Setup tab → *Protect model*.

## TrueNAS Scale

A ready-to-paste compose file lives at `truenas/docker-compose.yaml`:

1. TrueNAS 24.10+ (Electric Eel) → **Apps** → **Discover Apps** →
   **Custom App** → **Install via YAML**.
2. Paste the compose file; edit the four `CHANGE ME` lines (dataset
   path, `UNIFI_HOST`, username, password).
3. Optionally uncomment the Intel GPU or NVIDIA GPU block at the bottom.
4. Save. Container starts in web-only mode.
5. Open `http://<truenas-ip>:8091/` → Setup → add cameras → restart
   the app from TrueNAS.

## Versioning

Calendar versioning `YYYY.M.R` (e.g. `2026.4.14`). Pushing a tag
triggers CI to publish `:<tag>` and `:<tag>-cuda` images to GHCR.
`:latest` tracks `main` HEAD.

## Known limitations

- Smart-detect injection can break across Protect firmware updates —
  the protocol is reverse-engineered and a moving target. See
  `SECONDBRAIN.md` for the current landscape and what's worth watching.
- Line crossings appear as person / vehicle detections; Protect has no
  separate "line crossing" event type.
- Facial recognition and LPR are not implemented — those require
  Ubiquiti's closed AI Key pipeline.
- H.265 sources need transcoding (CPU cost). No public reversing of
  the modern protocol that natively accepts H.265.

## Credits

Protocol implementation built on
[unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) by
keshavdv. Upstream is pinned to a reviewed commit in the Dockerfile —
see `SECONDBRAIN.md` for the pin rationale and upstream PRs to watch.
