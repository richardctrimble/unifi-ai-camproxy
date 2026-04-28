# unifi-ai-camproxy

[![Docker](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/richardctrimble/unifi-ai-camproxy/actions/workflows/docker-publish.yml)

DIY tooling to extend UniFi Protect with AI events. Two independent modes — pick one:

---

## Which mode do I need?

```
My cameras are already adopted in Protect (ONVIF / native adoption)
and have their own onboard AI (person/vehicle events)?
  → ONVIF bridge mode  (:latest)

I have RTSP-only cameras and want the server to run AI locally
(YOLOv8 person/vehicle detection + optional virtual line-crossing)?
  → Local AI mode  (:ai  or  :ai-cuda for NVIDIA)
```

---

## ONVIF bridge mode (`:latest`, ~150 MB)

```
ONVIF camera → adopted natively in Protect (video in Protect UI)
             ↓
         ONVIF event subscription (motion / person / vehicle / line)
             ↓
     unifi-ai-camproxy bridge
             ↓
     Protect bookmarks + Alarm Manager webhooks
```

No video through the bridge. No local AI. Camera's own onboard AI does
the work; the bridge translates events into something Protect surfaces.

**Quick start:**

```bash
git clone https://github.com/richardctrimble/unifi-ai-camproxy.git
cd unifi-ai-camproxy
docker compose up -d --build
```

Open `http://<docker-host>:8091/` and configure:

1. **UniFi Creds** — Protect host, username + password (Test), API key (Test), Save.
2. **ONVIF Creds** — fleet ONVIF username + password + port. Per-camera overrides available.
3. **Alarm Setup** — for each (camera, kind) row, create a matching Custom Webhook rule in
   Protect's Alarm Manager using the Trigger ID shown.

Discovery runs every 60 s. Camera status appears on the **Status** tab.

**Prerequisites:** ONVIF cameras already adopted in Protect natively. A Protect API key
(Settings → Control Plane → Integrations → Create API Key).

---

## Local AI mode (`:ai` / `:ai-cuda`, ~1.5–2.5 GB)

```
RTSP camera → YOLOv8 inference → UniFi WebSocket protocol → Protect
```

Each camera entry becomes an adopted camera in Protect with smart-detect
events, bounding boxes, thumbnails, and timeline entries. Optional virtual
line-crossing zones can be drawn in the web UI.

**Quick start (CPU + Intel OpenVINO):**

```bash
git clone https://github.com/richardctrimble/unifi-ai-camproxy.git
cd unifi-ai-camproxy
docker compose -f docker-compose.yml -f docker-compose.ai.yml up -d --build
```

**Quick start (NVIDIA GPU):**

Install [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) first, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.ai-cuda.yml up -d --build
```

Open `http://<docker-host>:8091/` and:

1. **UniFi** tab — enter host + credentials, click *Test*, *Save*.
2. **Setup** tab — *+ Add Camera*, paste an RTSP URL, *Save All*.
3. Restart the container.

The camera auto-adopts in Protect and fires person/vehicle detection events.

**Intel iGPU / dGPU / NPU** — pass `/dev/dri` through for hardware inference:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.ai.yml \
  -f docker-compose.intel.yml \
  up -d
```

Check `docker compose logs -f` — AIEngine logs its chosen device on startup
(`Running inference on: intel:gpu`). Auto-probes in order: `cuda` → `intel:gpu`
→ `intel:npu` → `mps` → `cpu`.

---

## Images

| Tag | Dockerfile | Size | Description |
|---|---|---|---|
| `:latest` | `Dockerfile` | ~150 MB | ONVIF bridge — primary image |
| `:ai` | `Dockerfile.ai` | ~1.5 GB | Local AI: person/vehicle detection + line crossing (CPU + Intel) |
| `:ai-cuda` | `Dockerfile.ai-cuda` | ~2.5 GB | Local AI: same, NVIDIA CUDA (opt-in build) |

Calver tags: `:2026.4.14`, `:2026.4.14-ai`, `:2026.4.14-ai-cuda`.

---

## Prerequisites

- Docker + Compose v2 on a Linux host
- UniFi Protect on a UDM / UDM Pro / UNVR
- A **UniFi OS** user with Protect admin rights
- **Bridge mode:** cameras adopted in Protect via ONVIF + a Protect API key
- **AI mode:** at least one RTSP camera on the LAN

---

## Web UI

Both modes expose a web UI at `http://<host>:8091/`.

### Bridge mode (`:latest`)

Five tabs:

- **Status** — live health: discovered cameras, per-camera ONVIF subscription state,
  last event, alarm trigger counters, discovery error.
- **UniFi Creds** — Protect host, username + password (Test button), API key (Test button).
- **ONVIF Creds** — fleet ONVIF credentials + per-camera overrides with live topic list.
- **Alarm Setup** — per-(camera, kind) webhook IDs with copy buttons. Guides you through
  creating one Custom Webhook Alarm Manager rule per row in Protect.
- **Logs** — tails `/config/camproxy.log` with password redaction.

### AI mode (`:ai` / `:ai-cuda`)

Five tabs:

- **Status** — per-camera state, inference device, frame counters, detection totals,
  auth lockout / token refresh stats, disk/memory/heartbeat, advertised LAN IP.
- **UniFi** — credentials with per-type Test buttons + *Cameras in Protect* panel
  (can remove stuck pending adoptions).
- **Setup** — add/edit cameras. Per-camera: RTSP URL + transport, Protect model,
  AI device + model, confidence thresholds, frame skip, include-audio, H.264 transcode.
- **Lines** — click two points on a live frame to draw a virtual crossing line.
- **Logs** — tails `/config/camproxy.log` with password redaction.

---

## Configuration reference

### Bridge mode (`config/config.yml`)

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

Cameras are auto-discovered from Protect. Per-camera ONVIF overrides
can be set in the **ONVIF Creds** tab or directly in `config.yml`:

```yaml
cameras:
  - protect_id: "abc123"
    onvif_username: "cam-specific-user"
    onvif_password: "cam-specific-pass"
    onvif_port: 8080
```

### AI mode (`config/config.yml`)

```yaml
unifi:
  host: 192.168.1.1
  username: "your-unifi-os-username"
  password: "your-unifi-os-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

---

## TrueNAS Scale

`truenas/docker-compose.yaml` is a paste-in template for both modes.
Defaults to `:latest` (ONVIF bridge). Switch to `:ai` / `:ai-cuda` by
editing the image tag — instructions are in the template comments.

---

## Troubleshooting

Most problems surface clearly in the **Logs** tab.

**Camera stuck pending / "An error occurred" on Adopt** *(AI mode)*
Protect cached a bad IP. UniFi tab → *Cameras in Protect* → Refresh → **Remove** → restart.

**Video is black in Protect** *(AI mode)*
Source is probably H.265 — Setup → *Transcode to H.264*.

**UniFi login rejected / rate-limited**
Must be a **UniFi OS** user, not a Protect-app-only account. After repeated
failures the app backs off 10–15 min; saving new creds clears the cooldown.

**ONVIF subscription never connects** *(bridge mode)*
Check the ONVIF Creds tab — per-camera status shows the last error. Common causes:
wrong port (try 80, 8080, or 554), wrong credentials.

---

## Versioning

Calendar versioning `YYYY.M.R` (e.g. `2026.4.14`). Pushing to main triggers CI
to publish `:latest` and `:ai` to GHCR (`:ai-cuda` is opt-in).

## Known limitations

- Line crossings appear as person/vehicle detections in Protect — there is no
  separate "line crossing" event type in the Protect UI.
- Facial recognition and LPR are not implemented.
- AI mode's smart-detect injection can break across Protect firmware updates —
  the protocol is reverse-engineered. This is the main reason the ONVIF bridge
  exists as the primary path.

## Credits

AI mode protocol implementation built on
[unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) by keshavdv.
