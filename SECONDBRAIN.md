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
Can be obtained two ways:

1. **Automatic (preferred)** — put local Protect `username` + `password` in
   `config/config.yml` and the container will log in and fetch a fresh token
   against the local UniFi API (`/proxy/protect/api/bootstrap` then
   `/proxy/protect/api/cameras/qr`, with OpenCV QR decoding as a fallback).
   See `src/unifi_auth.py`.
2. **Manual** — grab the QR code from
   `https://<unifi>/proxy/protect/api/cameras/qr`, decode it, paste the token
   string into `config.yml` under `unifi.token`.

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
│   ├── config.yml              # GITIGNORED — user's real config with credentials
│   └── config.example.yml      # Committed — template for users
└── src/
    ├── main.py                 # Entry point — loads config, orchestrates auto-adoption
    ├── unifi_auth.py           # Local Protect API client (token fetch + accept adoption)
    ├── auto_config.py          # Deterministic fake-MAC + local-IP detection
    ├── web_tool.py             # aiohttp server + embedded HTML line-drawing UI
    ├── unifi_client.py         # AIPortCamera — extends UnifiCamBase, bridges AI → Protect
    ├── ai_engine.py            # YOLOv8 inference, IoU tracker, line crossing, async detection generator
    ├── line_crossing.py        # VirtualLine + LineCrossingDetector
    └── cert_gen.py             # Auto-generates self-signed TLS cert if not present
```

---

## Key code details

### main.py — orchestration
- `ensure_adoption_token()` — if `unifi.token` isn't set, logs in with
  `username`/`password` and fetches a token via `UniFiProtectClient`. Fails
  loudly with a SystemExit so the user sees what happened.
- `fill_camera_defaults()` — for every camera, if `mac`/`ip` isn't set, fills
  in `auto_config.generate_mac(name)` / `auto_config.detect_local_ip()`.
- `auto_adopt_pending()` — background task; sleeps 15s to let cameras
  announce themselves, then walks `GET /proxy/protect/api/cameras`, finds
  each of our fake MACs, and PATCHes them to `isAdopted: true`. Non-fatal on
  failure — user can still click "adopt" in the UI.

### unifi_auth.py — UniFiProtectClient
- Async-context-manager wrapping an `aiohttp.ClientSession` with SSL verify
  off (local self-signed cert is fine).
- `_login()` POSTs to `/api/auth/login`, grabs the `X-CSRF-Token` header.
- `fetch_adoption_token()` tries `/proxy/protect/api/bootstrap` → then
  `/proxy/protect/api/cameras/qr` as JSON → then same endpoint decoded as a
  PNG via `cv2.QRCodeDetector` (no extra deps — OpenCV is already in).
- `_extract_token()` walks the response dict looking for any of
  `authToken`/`adoptionToken`/`accessKey`/`token`/`a` at the top level or
  under `nvr`.
- `approve_pending(mac, name)` polls `/cameras` until a device with that
  fake MAC shows up in pending-adopt state, then PATCHes
  `{name, isAdopting: false, isAdopted: true}` to `/cameras/<id>`.

### web_tool.py — embedded line-drawing UI
- Single-file aiohttp server running in-process alongside the camera
  workers. `LineTool(registry, config).run(port=8091)` is spawned as
  another asyncio task from `main.py`.
- `GET /` → serves `INDEX_HTML` (inline single-page tool, no external
  assets, works on iPad Safari / any mobile browser).
- `GET /api/cameras` → list of configured camera names.
- `GET /api/frame/<name>` → JPEG encoded from `AIEngine.get_latest_frame()`
  via `cv2.imencode`.
- `GET /api/lines/<name>` → existing lines from `config.yml` so the UI
  can draw them as dashed grey overlays for reference.
- UI flow: pick camera → click two points on live frame → tweak name
  and direction → copy YAML → paste under that camera's `ai.lines:`.
  Coordinates are stored as normalised 0-1 from the click's
  `getBoundingClientRect` fraction, which matches exactly what
  `LineCrossingDetector` expects.
- Read-only — never writes back to `config.yml` (safer, avoids
  clobbering user formatting).

### auto_config.py — zero-config helpers
- `generate_mac(name)` — MD5 of the name, first byte masked to set the
  locally-administered bit (`0x02`) and clear the multicast bit. Same name →
  same MAC, so restarts don't create duplicate pending cameras in Protect.
- `detect_local_ip()` — classic UDP-connect trick (`socket.connect((1.1.1.1,
  80))` then `getsockname()`). No packet is sent; the kernel just picks the
  outbound-facing source IP. Falls back to 127.0.0.1 on failure.

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
- `_update_tracker()` — IoU matching, emits "start" on new object, "stop" after DEBOUNCE_STOP_FRAMES (10) missing frames. After every successful match it also runs `self.line_detector.check(prev_centroid, curr_centroid)` and emits an additional discrete "start" event with `line_crossing=<name>` when the centroid pair intersects a configured line.
- `get_latest_frame()` — thread-racy but atomic read for the web tool
- YOLO classes: PERSON=0, VEHICLES={2,3,5,7} (car, motorcycle, bus, truck)
- `LineCrossingDetector` now lives in `AIEngine` (not `AIPortCamera`) so the per-frame centroid check happens at the point where we actually have both `prev_centroid` and `curr_centroid`.

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

Minimum viable config (everything else is auto-generated):

```yaml
unifi:
  host: 192.168.1.1
  username: "your-protect-username"   # auto-fetches adoption token
  password: "your-protect-password"

cameras:
  - name: "Front Door"
    rtsp_url: "rtsp://admin:password@192.168.1.50:554/stream1"
```

Full config (all fields):

```yaml
unifi:
  host: 192.168.1.1
  username: "..."          # OR use token below
  password: "..."
  # token: "..."           # manual override if you prefer

cameras:
  - name: "Front Door"
    # mac: "AA:BB:CC:11:22:33"   # OPTIONAL — auto-generated from name
    # ip:  "192.168.1.101"       # OPTIONAL — detected via UDP-connect trick
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
- [x] Auto adoption-token fetch from local Protect API (no QR-scanning step)
- [x] Deterministic fake-MAC generation (no duplicate pending cameras on restart)
- [x] Auto local-IP detection
- [x] Auto accept of pending adoption via Protect API (no UI click-through)
- [x] Line crossing actually plumbed through the tracker (was instantiated-but-dead in the first version)
- [x] Embedded web tool at port 8091 for drawing lines on live frames, iPad-friendly
- [x] Git repo live at github.com/richardctrimble/happy-ai-port

---

## Next steps (in order)

1. **Set up Docker** on an x86 machine (Docker Desktop on Windows/Mac, or Docker on Linux)
2. **Edit `config/config.yml`** — fill in host, username, password, one camera's name + RTSP URL (that's it)
3. **`docker compose up`** — logs should show:
   - "Logged in to UniFi controller"
   - "Fetched adoption token from /bootstrap" (or similar)
   - "Generated fake MAC for <name>: ..."
   - "Auto-adopted camera <name>" within ~30–60s
4. **Verify video stream** is live in Protect
5. **Test smart detections** — walk in front of camera, check Protect timeline
6. **Test line crossing** — define a line, walk across it
7. **Debug/fix** whatever breaks. Most likely failure points:
   - Token-fetch endpoint shape varies by Protect version — unifi_auth.py tries
     several paths and logs which one won. If none work, fall back to manual
     `unifi.token` in config.yml.
   - Detection injection is still the historically fragile bit (upstream
     unifi-cam-proxy issue).
8. **Multi-camera** — add second camera once first is stable. No extra config
   needed; just another `- name:` entry.

---

## Known issues / risks

- Smart detection injection is the most fragile part — unifi-cam-proxy's Frigate integration has had repeated breakage after Protect firmware updates. May need debugging against your specific Protect version.
- Auto-token-fetch assumes the Protect API exposes the token somewhere in `/bootstrap` or `/cameras/qr`. Protect versions differ; if yours doesn't, `unifi_auth.UniFiProtectClient` will raise and the container exits with a clear "fall back to manual token" message.
- Auto-adopt PATCH uses `{isAdopting: false, isAdopted: true}` — empirically that's enough on current firmware but Protect may tighten this in future.
- `network_mode: host` is required in docker-compose — the container needs to be on the same subnet as Protect and cameras
- Auto-generated fake MACs use the locally-administered bit (`02:...`) so they can't collide with real vendor OUIs, but they *could* theoretically collide with another auto-generated MAC if two cameras have identical names. Keep names unique.
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
- User asked to automate the adoption process as much as possible, avoiding
  third-party services. Added `src/unifi_auth.py` + `src/auto_config.py` and
  rewired `main.py` so the only required config is host, username, password,
  camera name and RTSP URL. Adoption token, fake MAC, local IP and the final
  "accept adoption" click are now all handled automatically against the
  local UniFi controller.
