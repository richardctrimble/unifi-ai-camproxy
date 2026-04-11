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

### 1. Get your adoption token

In UniFi Protect:
- Go to **Settings → Integrations** (or open `https://<unifi>/proxy/protect/api/cameras/qr`)
- Decode the QR code — copy the adoption token string

### 2. Configure

Edit `config/config.yml`:
- Set your UniFi host IP and token
- Add your RTSP cameras with unique MAC addresses
- Define virtual lines if needed

### 3. Run

```bash
docker compose up -d
```

On first run it downloads the YOLOv8n model (~6MB). Then watch Protect —
your cameras should appear as pending adoption within ~30 seconds.

## Multi-camera

Each entry under `cameras:` becomes an independent virtual device in Protect.
Each needs a **unique MAC address** (these are fake — just pick random ones
that don't clash with real devices on your network).

## Model options

| Model      | Speed  | Accuracy | RAM    |
|------------|--------|----------|--------|
| yolov8n.pt | Fast   | Good     | ~400MB |
| yolov8s.pt | Medium | Better   | ~600MB |
| yolov8m.pt | Slower | Best     | ~1.2GB |

## Virtual line crossing

Lines are defined per-camera as two points in normalised (0–1) coordinates.
When a tracked object's path crosses the line, the detection is tagged and
triggers a Protect smart detection event.

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
