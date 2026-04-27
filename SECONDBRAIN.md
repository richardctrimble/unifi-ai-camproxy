# unifi-ai-camproxy — Second Brain

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
unifi-ai-camproxy/
├── Dockerfile                  # Default: CPU torch + OpenVINO + Intel runtime (~1.5GB)
├── Dockerfile.cuda             # NVIDIA variant: CUDA 12.1 PyTorch wheel (~2.5GB)
├── docker-compose.yml          # Builds default Dockerfile; host network + /config mount
├── docker-compose.gpu.yml      # Override → switches build to Dockerfile.cuda + nvidia device
├── docker-compose.intel.yml    # Override → passes /dev/dri into default image (no rebuild)
├── docker-entrypoint.sh        # Generates config.yml from env vars (TrueNAS mode)
├── .dockerignore               # Keeps build context lean
├── requirements.txt            # ultralytics, opencv-python-headless, pyyaml, aiohttp, websockets...
├── README.md                   # Setup instructions
├── SECONDBRAIN.md              # ← this file
├── config/
│   ├── config.yml              # GITIGNORED — user's real config with credentials
│   └── config.example.yml      # Committed — 3-camera template for users
├── truenas/                    # TrueNAS Scale install files
│   └── docker-compose.yaml     # Ready-to-paste compose for TrueNAS "Install via YAML"
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

### web_tool.py — embedded config + line-drawing UI
- Single-file aiohttp server running in-process alongside the camera
  workers. `LineTool(registry, config, config_path).run(port=8091)` is
  spawned as another asyncio task from `main.py`.
- `GET /` → serves `INDEX_HTML` — inline single-page app, no external
  assets, works on iPad Safari / any mobile browser.
- **Setup tab (IN PROGRESS):** add/edit/remove cameras + per-camera AI settings.
  `GET /api/config` returns camera + AI config (not passwords).
  `POST /api/config` writes changes back to config.yml via pyyaml.
  Shows "restart to apply" banner after save.
- **Lines tab:** existing line-drawing tool. Pick camera → click two points
  on live frame → set name + direction → save directly to config.yml
  (no more copy-paste).
- `GET /api/cameras` → list of configured camera names.
- `GET /api/frame/<name>` → JPEG from `AIEngine.get_latest_frame()`.
- `GET /api/lines/<name>` → existing lines for the overlay.
- Coordinates are normalised 0-1 from `getBoundingClientRect` fraction,
  matching `LineCrossingDetector`'s expectations.

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
- `_run_inference()` — YOLO on every Nth frame (frame_skip), normalises bbox to 0-1. Thresholds are resolved per class: `confidence_person` and `confidence_vehicle` override the shared `confidence` fallback. This lets you keep persons at e.g. 0.45 (catch distant pedestrians) while being stricter on vehicles at 0.60 (avoid car/truck/bus flip-flops from YOLO).
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
- [x] Per-class confidence thresholds (person vs vehicle) — shared `confidence` is still the fallback
- [x] Universal device auto-detection (`ai.device: auto`) — probes every runtime that's reachable and picks the fastest in order: `cuda → intel:gpu → intel:npu → mps → cpu`. Logs the winner on startup.
- [x] Split image strategy: default `Dockerfile` (~1.5GB) ships CPU torch + OpenVINO + Intel compute runtime — covers CPU and Intel iGPU/dGPU/NPU hosts in a single build. Sibling `Dockerfile.cuda` (~2.5GB) swaps in the CUDA 12.1 PyTorch wheel for NVIDIA hosts. Split keeps each image targeted instead of shipping 3.5GB of runtimes nobody uses.
- [x] `docker-compose.intel.yml` just passes through `/dev/dri` — no rebuild needed, the default image already has OpenVINO. Auto-exports YOLOv8 to OpenVINO IR format on first run and caches under `/config/<model>_openvino_model/`. Targets N100 hardware recommendation — 2–3× CPU throughput on integrated UHD.
- [x] `docker-compose.gpu.yml` swaps the build to `Dockerfile.cuda` and reserves the NVIDIA device. Tags the CUDA image as `unifi-ai-camproxy:cuda` so it doesn't collide with the default tag.
- [x] TrueNAS Scale Docker Compose YAML (`truenas/docker-compose.yaml`) — ready-to-paste into TrueNAS 24.10+ "Install via YAML" feature. User edits 4 values (host, creds, storage path), docker-entrypoint.sh generates a seed config.yml from env vars. All camera and AI configuration moves to the web UI. (Old Helm chart catalog removed — TrueNAS 24.10+ dropped custom catalog support entirely.)
- [x] `.dockerignore` — keeps .git, __pycache__, SECONDBRAIN, secrets out of the Docker build context
- [x] Multi-camera config example (3 cameras: full options, minimal, vehicles-only with two lines)
- [x] Full config reference tables in README + troubleshooting section
- [x] Git repo live at github.com/richardctrimble/unifi-ai-camproxy

### Recently completed

- [x] **Web UI config management** — `web_tool.py` rewritten with tabbed interface:
  - **Setup** tab: add/edit/remove cameras + per-camera AI settings (model, confidence, frame_skip, detect toggles). Save writes directly to config.yml via pyyaml.
  - **Lines** tab: draw lines on live frames, save/delete directly to config.yml (no more copy-paste YAML).
  - New endpoints: `GET/POST /api/config`, `POST /api/lines/{name}`, `DELETE /api/lines/{name}/{idx}`
  - `LineTool` now takes `config_path` parameter, has `_reload_config()` and `_write_config()` methods.
  - After save, UI shows "Restart the container to apply changes" banner.
- [x] **Zero-camera startup** — `main.py` handles `cameras: []` gracefully: starts the web UI first, logs "running in web-only mode", skips adoption + camera workers. Container stays alive so users can add cameras via the Setup tab.
- [x] **TrueNAS README** — added step-by-step install guide (paste Docker Compose YAML, add cameras via web UI, draw lines, how it works under the hood). Updated for 24.10+ which removed custom catalog support.

### Key design decision (current session)

**TrueNAS app philosophy**: TrueNAS 24.10+ (Electric Eel) removed custom app catalogs entirely, replacing the old Kubernetes/Helm system with native Docker Compose. We provide a ready-to-paste `truenas/docker-compose.yaml` that users paste into the "Install via YAML" feature. The compose file asks for just enough to get the container running (Protect host, credentials, storage path). Everything else — cameras, RTSP URLs, AI model, confidence thresholds, detection toggles, virtual lines — is configured through the embedded web UI at `:8091`. This keeps the compose file simple (4 values to edit) and means the same UI works for standalone Docker and TrueNAS users.

Flow:
1. TrueNAS UI → Apps → Custom App → Install via YAML → paste compose, edit 4 values → Save
2. Container starts in web-only mode (no cameras yet)
3. User opens `http://<host>:8091/` → Setup tab → adds cameras → saves
4. User restarts container → cameras start, detection begins
5. User returns to Lines tab → draws virtual lines → saves → restarts

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
- **Recommended:** Intel N100 mini PC (~£100–150) — silent, low power, default image ships OpenVINO so the iGPU lights up automatically (2–3× CPU throughput). Handles 4–6 cameras at yolov8n easily.
- **NVIDIA GPU:** Build the CUDA variant via `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build` — AIEngine auto-detects and prefers CUDA when available.

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
- Initialised git, pushed to github.com/richardctrimble/unifi-ai-camproxy
- User on iPad, private repo — pushed via PAT (token should be rotated after use)
- User asked to automate the adoption process as much as possible, avoiding
  third-party services. Added `src/unifi_auth.py` + `src/auto_config.py` and
  rewired `main.py` so the only required config is host, username, password,
  camera name and RTSP URL. Adoption token, fake MAC, local IP and the final
  "accept adoption" click are now all handled automatically against the
  local UniFi controller.

---

## Protocol state — Apr 2026 landscape (for later)

Research done after Protect 7.x adoption pain. Nobody has publicly
reverse-engineered the modern camera-side protocol used by G5 Pro / AI
Pro 4K to stream H.265 natively. All community effort is still patching
the legacy AVClient path (ws 7442 + FLV upload to 7550) against Protect
7.x regressions. The only sanctioned "new protocol" that accepts H.265
from third-party sources is **ONVIF via Protect 5.0+**, and that path
loses motion / PTZ / smart-detect / two-way audio.

### Things actually worth watching

- **keshavdv/unifi-cam-proxy PR #419** — by `nalditopr`, open Apr 2026.
  Replaces the broken FLV-to-7550 pipe with a call to Protect's internal
  EvoStream `ms` binary on `localhost:1112` using the text `pullStream`
  command. This is how real UniFi cameras are ingested on 7.x internally.
  Still H.264-only; no HEVC. If our video-ingest starts misbehaving on a
  future Protect update, cherry-pick this into the vendored proxy.
  <https://github.com/keshavdv/unifi-cam-proxy/pull/419>

- **keshavdv/unifi-cam-proxy Issue #416** — Mar 2026, no PR. Captured an
  NVR `ChangeVideoSettings` showing Protect 7.x now expects
  **extendedFLV + Opus 24 kHz** (Ubiquiti's `ExVideoTagHeader` wrapper).
  AAC streams get "broken pipe" on 7550. Our current setup works because
  we default audio OFF (`-an`) — when Protect tightens the check this
  might bite even H.264 streams. Best public intel on what changed.
  <https://github.com/keshavdv/unifi-cam-proxy/issues/416>

- **p10tyr/rtsp-to-onvif** — Not a spoofer. Wraps RTSP as a virtual
  ONVIF device that Protect 5+ discovers and adopts. H.265 passes
  through unmodified, but there's no hook for injecting our YOLO
  detections — so no AI-camera integration, no smart-detect events.
  Viable as a fallback path for cameras where we only need footage,
  not AI overlays. <https://github.com/p10tyr/rtsp-to-onvif>

### Dead ends — don't revisit

- `hjdhjd/unifi-protect`, `uilibs/uiprotect` — NVR client-side APIs only,
  zero camera-side reversing in either repo.
- Go / Rust / TypeScript reimplementations in the search — all NVR
  clients, none spoof the camera side.
- Gists, HAR files, blog posts on `ubnt_ipcs` or similar terms — the
  string doesn't appear to be real public nomenclature. (I had
  hallucinated it earlier.)

### Decision

Commit to the H.265 → H.264 transcode path. Not worth waiting 6 months
for a native HEVC spoofer to materialise — there's no momentum. The
Setup tab now has a Transcode checkbox per camera; cost is one CPU
encode per H.265 source but Intel iGPUs can offload via `h264_qsv` if
it becomes a problem.

### What could force a revisit

- Ubiquiti actually enforces extendedFLV + Opus on 7550 → even H.264
  breaks → we'd need to implement PR #419's pullStream path or adopt
  the ONVIF fallback.
- Someone lands a working modern-protocol spoof in keshavdv upstream
  (unlikely in the next 6 months based on current velocity).

---

## Operational details worth remembering

### ai_udp_port plumbing

Each camera pipes `video1`'s ffmpeg output to *two* destinations:

1. FLV stream → Protect's ingest (the user-visible feed).
2. MPEGTS-copy (`-c:v copy -an`) → `udp://127.0.0.1:<port>` → AIEngine.

The UDP loopback is how the AI engine gets frames without opening a
second RTSP connection to the camera (many cameras reject concurrent
connections). Ports are auto-assigned from a class-level counter
starting at 5200; override per-camera with `ai.ai_udp_port` in
`config.yml` when you need deterministic ports or have firewall rules
constraining the default range. Each camera MUST use a unique port;
the counter prevents collisions but a manual override can clash.

### TrueNAS app first-boot workflow

The Docker Compose file passes Protect creds as env vars. On first
boot, `docker-entrypoint.py` generates a minimal `config.yml` with
those creds and `cameras: []`. The app starts in **web-only mode**
(no cameras → nothing to adopt). The user adds cameras via the
Setup tab; config is persisted to the mounted dataset. After an
app restart, `main.py` reads the full config, adopts cameras, starts
inference.

Env vars override the stored config at every container start — if
the user changes `UNIFI_HOST` in TrueNAS, that propagates to
`config.yml` on next boot. The web UI's UniFi tab is the more
ergonomic path; env vars are for TrueNAS-wizard-driven installs.

### Protect model strings and AI behaviour

Protect gates the "AI camera" UI on the `model` string advertised in
the adoption payload. The defaults that trigger AI UI in 7.x:

- `UVC AI Pro` — default we ship
- `UVC AI 360` — fisheye
- `UVC AI Bullet` — bullet

`UVC G4 Pro` and older G-series names adopt successfully but Protect
treats them as non-AI — our `EventSmartDetect` events are ignored.
Changing the model on an already-adopted camera requires re-adopting
(Protect caches capability flags).

### Intel GPU driver choice

We ship Debian's `intel-opencl-icd` (compute-runtime 22.43) rather
than Intel's "noble unified" apt repo. Debian 22.43 still bundles
Gen8–Gen12 in one package, covering every TrueNAS-class CPU
(N100/N200, UHD 610/620/630, i3–i7 iGPUs). Intel's modern repo
split legacy drivers into `-legacy1` packages that aren't reliably
in the channel — builds kept going green while leaving Gen9 users
without a driver. Trade-off: Arc / Xe dGPUs get the older 22.43
driver rather than the latest perf tuning, but practically no one
runs those in a TrueNAS box.

`libze1` (Level Zero loader) is from Debian too; `intel-level-zero-gpu`
is installed best-effort (not in every Debian snapshot). OpenVINO
falls back to OpenCL when L0 isn't available, which is fine for
Gen9–Gen11.

`intel-opencl-icd` lives in Debian's `non-free-firmware` component
(binary firmware blobs), which `python:3.11-slim` doesn't enable by
default — Dockerfile adds a supplementary sources.list entry for it.

### Versioning scheme

Calendar versioning `YYYY.M.R`:
- `YYYY` four-digit year
- `M` month, no leading zero
- `R` release number within that month (1, 2, 3…)

Tags: `2026.4.1`, `2026.4.14`, `2026.5.1`. Pushing a tag triggers
the CI workflow to build and publish three image variants per tag:
- `:<tag>` — ONVIF bridge (primary)
- `:<tag>-full` — full spoof+inference (CPU + Intel)
- `:<tag>-full-cuda` — full + CUDA (opt-in build)

`:latest` tracks main's ONVIF bridge build; `:full` and `:full-cuda`
track the corresponding full-mode builds.

---

## ONVIF bridge mode (architectural pivot, Apr 2026)

### Why the pivot

The legacy spoof+inference path has three structural weaknesses we
keep paying for:

1. The AVClient protocol is brittle across Protect firmware bumps
   (Protect 7.x's extendedFLV/Opus change is the latest example;
   issue keshavdv #416).
2. We have to push video through ourselves, which means ffmpeg +
   transcoding for H.265 sources + a ~2.5 GB image.
3. Local YOLO inference is duplicative — modern ONVIF cameras
   (Hikvision, Dahua, Reolink, Amcrest) emit person/vehicle/line
   events from their own onboard AI for free.

The bridge mode avoids all three: cameras adopt natively in Protect
(Protect handles video, including H.265), and we just translate
their ONVIF events into something Protect's UI surfaces.

### Architecture

```
ONVIF camera → adopted natively in Protect (Protect owns the video)
            ↓
            ONVIF event subscription (PullPoint, BaseNotification)
            ↓
        unifi-ai-camproxy bridge container
            ↓
        Protect /api/cameras/{id}/bookmarks  (timeline marker)
        Protect Alarm Manager custom webhook (notifications)
```

No video transit, no inference, ~150 MB image. All Python: aiohttp +
onvif-zeep.

### Module layout

`src/onvif_bridge/`:

- `protect_discovery.py` — wraps `unifi_auth.UniFiProtectClient.list_cameras()`
  and filters for ONVIF-adopted cameras.
- `onvif_subscriber.py` — per-camera PullPoint subscription, normalises
  vendor topics into `kind ∈ {motion, person, vehicle, line_crossing,
  audio, unknown}`.
- `protect_pusher.py` — POST bookmarks + fire Alarm Manager webhooks.
- `web_tool.py` — minimal Status dashboard (skeleton today).
- `main.py` — entrypoint wiring all of the above.

`unifi_auth.py` is shared with the full image (one auth implementation,
two consumers). The bridge image's Dockerfile copies it alongside the
bridge package.

### Verified API contract (Apr 2026)

Research against the public OpenAPI spec for Protect 7.0.107 plus
hjdhjd's `protect-types.ts` and uilibs/uiprotect:

1. **Bookmark POST — no public-documented endpoint exists.** The
   official OpenAPI spec for Protect 7 lists 25 paths and none mention
   bookmarks. Neither hjdhjd's nor uilibs/uiprotect's libraries
   implement bookmark creation (despite both reverse-engineering the
   legacy `/proxy/protect/api/*` surface exhaustively). Long-standing
   feature request: <https://community.ui.com/questions/2dd382ee-ef33-45c8-b87b-2f241bc0cc88>.
   **Decision**: bookmarks dropped from the bridge. To revisit, capture
   browser DevTools traffic when clicking "Add Bookmark" in Protect's
   UI on a live controller.

2. **Alarm Manager Custom Webhook — VERIFIED.**
   - Path: `POST /proxy/protect/integration/v1/alarm-manager/webhook/{id}`
   - Method: POST only (no GET).
   - Auth: `X-API-Key` header.
   - `{id}` is a user-defined string ("alarmTriggerId"); the rule in
     Protect must be configured with the matching ID to fire.
   - Returns 204 on success, 400 if id missing.
   - **No body / metadata propagation** — the spec is silent on body
     schema and community write-ups never demonstrate template
     substitution from trigger to action. Treat the URL as the entire
     discriminator.
   - Implementation: encode the (camera, event-kind) pair into the
     ID itself via a template. Default:
     `onvif-bridge:{protect_id}:{kind}`. User creates one alarm rule
     per (camera, kind) combination they care about.
   - Source: <https://github.com/beezly/unifi-apis/blob/main/unifi-protect/7.0.107.json>

3. **Third-party ONVIF discriminator — VERIFIED.**
   - Field: `isThirdPartyCamera: boolean` on the legacy
     `GET /proxy/protect/api/cameras` response.
   - Secondary fields: `isAdoptedByOther: boolean`, `marketName: string`,
     `type: string`. `modelKey` is always `"camera"` and does NOT
     discriminate.
   - The integration API (`/proxy/protect/integration/v1/cameras`)
     does NOT expose `isThirdPartyCamera`, so discovery must use the
     legacy endpoint with cookie + CSRF auth.
   - Sources: hjdhjd `protect-types.ts` line 788, uilibs
     `uiprotect/data/devices.py` line 1054.

### Dual auth implications

The bridge needs **both** auth flavours because the surfaces it talks
to live on different APIs:

- Cookie + `X-CSRF-Token` for `GET /proxy/protect/api/cameras`
  (camera enumeration, `isThirdPartyCamera` filter).
- `X-API-Key` for
  `POST /proxy/protect/integration/v1/alarm-manager/webhook/{id}`
  (the alarm trigger).

`UniFiProtectClient.__aenter__` already runs the cookie login
whenever `username` + `password` are set, and `_headers()` attaches
`X-API-KEY` whenever `api_key` is set. So a single configured client
covers both. The bridge's pusher uses a separate aiohttp session for
the integration POST since it's a one-line call and doesn't need
CSRF state.

### Setup flow for the user

1. In Protect: Settings → Control Plane → Integrations → Create
   API Key. Paste into `unifi.api_key` (or set `UNIFI_API_KEY`).
2. Open the bridge UI → **Setup** tab. It enumerates one row per
   (discovered camera × supported kind) and shows the exact webhook
   ID to use, with a copy button per row.
3. In Protect: Alarm Manager → New Alarm. Add a "Custom Webhook"
   trigger, paste the row's ID into the trigger ID field. Configure
   downstream actions. Save.
4. Back in the bridge UI, the Setup tab's status column flips to
   "firing — last Xs ago" the next time that webhook ID is hit.

### Why no auto-create

Researched against the Protect 7.0.107 OpenAPI spec and hjdhjd's
legacy-API reverse: neither surface exposes alarm-rule CRUD. Only
the fire-trigger endpoint is published. Protect's own web UI must
hit *something* to save a rule, but no public reverse exists. To
add option 2 (auto-create rules) we'd need DevTools traffic
captured from a live Protect controller during "Save Alarm Rule"
— logged as a future possibility but not blocking.

The Setup tab is the workaround: copy-paste IDs is ~20 seconds
per rule, and the live "is this firing?" indicator gives instant
feedback on whether the user pasted correctly.

### Status flags (image variant detection)

Each image sets `APP_IMAGE_VARIANT` so the Status tab can label
itself. Values: `onvif` (bridge) or `full` (spoof+inference).
`build_info.py` already surfaces this via `get_build_info()`.

### Coexistence policy

Both images live indefinitely in the same GHCR package, distinguished
by tag (`:latest` = bridge, `:full` = spoof+inference). No removal
is planned for the full image — it's the only path for users with
cameras that lack onboard AI, and it's the fallback if Protect ever
breaks the bridge's APIs.

The `src/` directory keeps the legacy `main.py` / `unifi_client.py`
/ `ai_engine.py` / `web_tool.py` untouched; the bridge lives in
`src/onvif_bridge/` so the two are clearly separated and either
can evolve without the other.
