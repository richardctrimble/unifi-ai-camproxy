# happy-ai-port — Second Brain

Everything we know, decided, and built. Pick this up in Claude Code and continue seamlessly.

---

## What this project is

A DIY clone of the **UniFi Protect AI Port** ($199 hardware device). Instead of buying Ubiquiti's
appliance, we run a Docker container on an x86 machine that:

1. Spoofs itself as a UniFi G4 Pro camera to UniFi Protect (using the reverse-engineered WebSocket protocol)
2. Pulls RTSP streams from real cameras
3. Runs YOLOv8 AI inference locally (person + vehicle detection)
4. Injects smart detection events back into Protect — appearing natively in the UI with bounding boxes, thumbnails, and timeline events
5. Supports virtual line crossing detection

---

## Research findings

### The UniFi AI Port (real device)
- $199 per unit, supports up to 5 cameras per unit (fewer for 4K)
- Adds AI smart detections (person, vehicle, face, LPR) to third-party ONVIF cameras
- Edge AI — all processing on-device, no cloud
- Connects to Protect via proprietary WebSocket protocol on port 7442
- Also exists: **AI Key** ($x) — does facial recognition + LPR + NLP search on top of existing detections

### Protocol reverse engineering
The entire Protect device-side protocol has been reverse engineered by the community:

**Key project: `keshavdv/unifi-cam-proxy`** (GitHub, 1.8k stars)
- Python, MIT licence
- Successfully spoofs a UniFi camera to Protect
- Has working Frigate integration that injects smart detections
- Known fragility: detection injection breaks after some Protect firmware updates, needs proxy restart
- This is our protocol foundation — we import its base classes directly

**Protocol details (from unifi-cam-proxy source):**
- WebSocket: `wss://HOST:7442/camera/1.0/ws?token=TOKEN`
- Header: `camera-mac: AA:BB:CC:...`
- SSL: requires client certificate (self-signed works)
- Adoption: send `ubnt_avclient_hello` JSON with MAC, IP, model, token
- All messages: `{"from": "ubnt_avclient", "to": "UniFiVideo", "functionName": "...", "payload": {...}}`
- Smart detection inject: `EventSmartDetect` with `objectTypes: ["person"]` and `edgeType: "enter"/"leave"`
- Motion events: `EventAnalytics` with `edgeType: "start"/"stop"`
- Snapshots uploaded via HTTP POST to a URL provided by Protect
- Video streams: FFmpeg RTSP → FLV → piped via netcat to Protect

**Other relevant projects found:**
- `hjdhjd/unifi-protect` — nearly complete Protect API in TypeScript, reverse engineered the binary WebSocket updates protocol (consumer side / read events OUT of Protect)
- `daniela-hase/onvif-server` — virtual ONVIF server, splits multi-channel cameras for Protect 5+
- `tykateetee/rtsp-to-onvif` — Docker, wraps RTSP as ONVIF for Protect 5+ adoption (no AI detections)
- `Gamer08YT/unifi-proxy` — integrates third-party hardware into Protect, uses QR adoption token
- `fxkr/unifi-protocol-reverse-engineering` — older UniFi AP inform protocol docs (AES-128-CBC, TLV format)
- `jeffreykog/unifi-inform-protocol` — inform protocol detail (adoption, encryption keys)

**What has NOT been done publicly:**
- Nobody has cloned the AI Port specifically (device-side AI detection pipeline into Protect)
- No public capture of the AI Port ↔ Protect protocol traffic
- We are likely the first open source attempt at this

### Adoption token
Obtained from Protect at: `https://<unifi>/proxy/protect/api/cameras/qr`
Decode the QR code → copy the token string → paste into `config/config.yml`

---

## Architecture

```
[RTSP Cameras]
      ↓  (OpenCV VideoCapture)
[AIEngine]  — YOLOv8 inference, IoU tracker, snapshot capture
      ↓  (async generator of detection events)
[LineCrossingDetector]  — centroid path × virtual line segment intersection
      ↓
[AIPortCamera]  — extends unifi-cam-proxy UnifiCamBase
      ↓  (WebSocket wss://:7442)
[UniFi Protect]  — sees native smart detections, thumbnails, timeline
```

**Multi-camera:** each entry in `config.yml` becomes a separate adopted device in Protect.
Each needs a unique fake MAC address.

---

## Tech stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.11 | unifi-cam-proxy is Python, easiest integration |
| AI inference | YOLOv8 (ultralytics) | Fast on CPU x86, easy API, good accuracy |
| Object tracking | Custom IoU tracker | Simple, no extra deps, sufficient for this use case |
| Line crossing | Segment intersection (cross product) | Reliable, no ML needed |
| Video capture | OpenCV VideoCapture | Standard RTSP support |
| Video streaming | FFmpeg → FLV → netcat | Same as unifi-cam-proxy, Protect expects this |
| Protocol layer | unifi-cam-proxy (imported) | Battle-tested, saves weeks of work |
| Container | Docker (x86 amd64) | Single image, user just edits config.yml |
| Config | YAML | Human-friendly |

---

## File structure

```
happy-ai-port/
├── Dockerfile                  # Two-stage: clones unifi-cam-proxy, builds final image
├── docker-compose.yml          # network_mode: host (required), volume: ./config:/config
├── requirements.txt            # ultralytics, opencv-python-headless, pyyaml, aiohttp, websockets...
├── README.md                   # Setup instructions
├── SECONDBRAIN.md              # ← this file
├── config/
│   ├── config.yml              # GITIGNORED — user's real config with token
│   └── config.example.yml      # Committed — template for users
└── src/
    ├── main.py                 # Entry point — loads config, spawns camera coroutines
    ├── unifi_client.py         # AIPortCamera — extends UnifiCamBase, bridges AI → Protect
    ├── ai_engine.py            # YOLOv8 inference, IoU tracker, async detection generator
    ├── line_crossing.py        # VirtualLine + LineCrossingDetector
    └── cert_gen.py             # Auto-generates self-signed TLS cert if not present
```

---

## Key code details

### unifi_client.py — AIPortCamera
- Extends `UnifiCamBase` from unifi-cam-proxy
- `run()` starts the AI loop as a background asyncio task
- `_ai_loop()` consumes the async generator from AIEngine
- `_handle_detection()` calls `trigger_motion_start(SmartDetectObjectType.PERSON)` or VEHICLE
- `get_snapshot()` returns latest frame captured by AIEngine
- `get_stream_source()` returns the RTSP URL for all stream qualities

### ai_engine.py — AIEngine
- `detections()` — async generator, runs blocking OpenCV+YOLO in thread executor
- `_capture_loop()` — blocking, runs in executor. Reconnects on stream loss with exponential backoff
- `_run_inference()` — YOLO on every Nth frame (frame_skip), normalises bbox to 0-1
- `_update_tracker()` — IoU matching, emits "start" on new object, "stop" after DEBOUNCE_STOP_FRAMES (10) missing frames
- YOLO classes: PERSON=0, VEHICLES={2,3,5,7} (car, motorcycle, bus, truck)

### line_crossing.py — LineCrossingDetector
- Lines defined as two normalised points (0-1 coordinate space)
- Uses cross product sign to determine crossing direction
- `check(prev_centroid, curr_centroid)` called per tracked object per frame
- Returns line name if crossed, None otherwise
- Supports direction filter: `both | left_to_right | right_to_left`

### cert_gen.py
- Checks if `/config/client.pem` exists, generates via openssl if not
- UniFi requires a client cert on the WebSocket — self-signed is fine

---

## Configuration reference

```yaml
unifi:
  host: 192.168.1.1        # UniFi Dream Machine / UNVR IP
  token: "..."             # From QR code at /proxy/protect/api/cameras/qr

cameras:
  - name: "Front Door"
    mac: "AA:BB:CC:11:22:33"   # Fake — must be unique per camera
    ip: "192.168.1.101"        # This machine's IP (or any unused IP)
    rtsp_url: "rtsp://..."
    snapshot_url: "http://..."  # Optional HTTP snapshot endpoint
    ai:
      model: "yolov8n.pt"      # n=fast, s=balanced, m=accurate
      confidence: 0.50
      detect_persons: true
      detect_vehicles: true
      frame_skip: 3
      lines:                   # Optional virtual lines
        - name: "EntryLine"
          x1: 0.5  y1: 0.0
          x2: 0.5  y2: 1.0
          direction: "both"
```

---

## What works (as designed, untested end-to-end)

- [x] Full project scaffold
- [x] Docker image (x86)
- [x] Protocol layer (adoption, video streaming, detection injection) — via unifi-cam-proxy base
- [x] YOLOv8 inference pipeline
- [x] IoU object tracker with start/stop debouncing
- [x] Virtual line crossing (segment intersection)
- [x] Multi-camera support
- [x] Auto cert generation
- [x] Config file design
- [x] Git repo live at github.com/richardctrimble/happy-ai-port

---

## Next steps (in order)

1. **Get adoption token** from your Protect instance (`/proxy/protect/api/cameras/qr`)
2. **Set up Docker** on an x86 machine (Docker Desktop on Windows/Mac, or Docker on Linux)
3. **Edit `config/config.yml`** — fill in host, token, one camera's RTSP URL, pick a fake MAC
4. **`docker compose up`** — watch logs, verify camera appears as "pending adoption" in Protect
5. **Walk through camera adoption** in Protect UI
6. **Verify video stream** is live in Protect
7. **Test smart detections** — walk in front of camera, check Protect timeline
8. **Test line crossing** — define a line, walk across it
9. **Debug/fix** whatever breaks (detection injection is the most fragile part)
10. **Multi-camera** — add second camera once first is stable

---

## Known issues / risks

- Smart detection injection is the most fragile part — unifi-cam-proxy's Frigate integration has had repeated breakage after Protect firmware updates. May need debugging against your specific Protect version.
- `network_mode: host` is required in docker-compose — the container needs to be on the same subnet as Protect and cameras
- The fake MAC addresses must not clash with any real device on your network
- Line crossing events surface in Protect as person/vehicle detections — there is no dedicated "line crossing" event type in Protect's UI
- Facial recognition and LPR are not in scope (those require AI Key's closed pipeline)

---

## Hardware recommendation

For running this container:
- **Minimum:** Any x86 machine with 2GB RAM free (Raspberry Pi 5 also works but needs ARM build)
- **Recommended:** Intel N100 mini PC (~£100–150) — silent, low power, handles 4–6 cameras at yolov8n
- **GPU:** If you have an NVIDIA GPU, uncomment the GPU section in docker-compose.yml — YOLO will use CUDA automatically

YOLO model sizing:
| Model | Speed | RAM | Use when |
|---|---|---|---|
| yolov8n.pt | Fastest | ~400MB | Multiple cameras, weaker hardware |
| yolov8s.pt | Fast | ~600MB | 1-2 cameras, good balance |
| yolov8m.pt | Slower | ~1.2GB | Single camera, best accuracy |

---

## Conversation history summary

- Started from user asking about cloning a UniFi AI Port
- Researched existing community work — found keshavdv/unifi-cam-proxy is the key foundation
- Discovered the device-side AI injection pipeline has never been open-sourced
- Decided to build on unifi-cam-proxy's protocol layer + add our own YOLO inference
- User: x86 Docker, person/vehicle + line crossing, some Python experience
- Built full project: 5 Python source files + Dockerfile + docker-compose + config
- Initialised git, pushed to github.com/richardctrimble/happy-ai-port
- User on iPad, private repo — pushed via PAT (token should be rotated after use)
