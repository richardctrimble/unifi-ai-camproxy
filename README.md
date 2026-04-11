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
