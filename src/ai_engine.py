"""
AIEngine — pulls frames from RTSP, runs YOLOv8 inference,
emits detection events as an async generator.

Detection lifecycle:
  - Object enters frame  → emit "start" event
  - Object absent for N frames → emit "stop" event
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, List, Optional

import cv2
import numpy as np

# Ultralytics YOLOv8 — CPU-friendly on x86, GPU if available
from ultralytics import YOLO

from line_crossing import LineCrossingDetector


# YOLO class IDs we care about
PERSON_CLASS = 0
VEHICLE_CLASSES = {2, 3, 5, 7}  # car, motorcycle, bus, truck

# How many frames without a detection before we call it "gone"
DEBOUNCE_STOP_FRAMES = 10


# ─── Shared device probing (module-level, cached) ───────────────────────────
#
# probe_available_devices() is called by AIEngine._resolve_device() and by the
# web UI's /api/status endpoint so it can surface the full list of reachable
# backends. The probe itself is cheap but imports torch/openvino, so we
# memoise the result for the lifetime of the process.

_probe_cache: Optional[dict] = None


def probe_available_devices(force: bool = False) -> dict:
    """
    Enumerate every inference backend this image can actually reach.

    Returns a dict like::

        {
          "cuda":      {"available": False, "detail": "torch.cuda.is_available() == False"},
          "mps":       {"available": False, "detail": "not macOS / not Apple Silicon"},
          "intel:cpu": {"available": True,  "detail": "OpenVINO CPU"},
          "intel:gpu": {"available": True,  "detail": "OpenVINO GPU"},
          "intel:npu": {"available": False, "detail": "not in OpenVINO devices"},
          "cpu":       {"available": True,  "detail": "always available"},
        }

    Callers use this for (a) "auto" device selection ordering and
    (b) surfacing the list to the user in the Status dashboard so they
    can tell at a glance whether /dev/dri or the NVIDIA runtime was
    actually passed through correctly.
    """
    global _probe_cache
    if _probe_cache is not None and not force:
        return _probe_cache

    result: dict[str, dict[str, object]] = {}

    # ── CUDA ──────────────────────────────────────────────────────────────
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0) if torch.cuda.device_count() else "CUDA"
            result["cuda"] = {"available": True, "detail": name}
        else:
            result["cuda"] = {"available": False, "detail": "torch reports no CUDA device"}
    except Exception as e:
        result["cuda"] = {"available": False, "detail": f"torch import failed: {e}"}

    # ── MPS (Apple Silicon) ───────────────────────────────────────────────
    try:
        import torch
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            result["mps"] = {"available": True, "detail": "Apple Metal Performance Shaders"}
        else:
            result["mps"] = {"available": False, "detail": "not macOS / not Apple Silicon"}
    except Exception:
        result["mps"] = {"available": False, "detail": "torch import failed"}

    # ── OpenVINO (intel:cpu / intel:gpu / intel:npu) ──────────────────────
    #
    # We enumerate the devices OpenVINO can actually reach. If GPU/NPU
    # are NOT in available_devices we probe the plugin explicitly so we
    # can surface the underlying reason (missing /dev/dri, permission
    # denied on renderD128, unsupported iGPU, etc.) instead of silently
    # falling back to CPU.
    core = None
    ov_devices: set[str] = set()
    ov_error = ""
    try:
        import openvino as ov
        core = ov.Core()
        ov_devices = set(core.available_devices)
    except Exception as e:
        ov_error = str(e)

    def _gpu_diagnostic() -> str:
        """Return a human-readable reason the GPU plugin isn't visible."""
        dri_path = "/dev/dri"
        if not os.path.isdir(dri_path):
            return "no /dev/dri inside container (passthrough missing)"
        render_nodes = [n for n in os.listdir(dri_path) if n.startswith("renderD")]
        if not render_nodes:
            return "/dev/dri present but no renderD* node (iGPU driver not loaded on host)"
        node = f"{dri_path}/{render_nodes[0]}"
        if not os.access(node, os.R_OK | os.W_OK):
            return (
                f"{node} present but not accessible by this process — "
                f"add the render group GID to group_add in your compose file"
            )
        if core is not None:
            try:
                core.get_property("GPU", "FULL_DEVICE_NAME")
                return "GPU plugin reported no device"
            except Exception as e:
                return f"OpenVINO GPU plugin error: {e}"
        return "OpenVINO core not initialised"

    for plugin, key in (("CPU", "intel:cpu"), ("GPU", "intel:gpu"), ("NPU", "intel:npu")):
        if plugin in ov_devices:
            try:
                full_name = core.get_property(plugin, "FULL_DEVICE_NAME") if core else plugin
            except Exception:
                full_name = plugin
            result[key] = {"available": True, "detail": f"OpenVINO {plugin} ({full_name})"}
        elif ov_error:
            result[key] = {"available": False, "detail": f"OpenVINO unavailable: {ov_error}"}
        elif plugin == "GPU":
            result[key] = {"available": False, "detail": _gpu_diagnostic()}
        elif ov_devices:
            result[key] = {"available": False, "detail": f"OpenVINO reachable but no {plugin}"}
        else:
            result[key] = {"available": False, "detail": "OpenVINO not installed in image"}

    # ── CPU (always available as a last resort) ───────────────────────────
    result["cpu"] = {"available": True, "detail": "native PyTorch CPU"}

    _probe_cache = result
    return result


def list_supported_devices() -> List[str]:
    """Return the canonical order of device choices the UI should expose."""
    return ["auto", "cpu", "cuda", "mps", "intel:cpu", "intel:gpu", "intel:npu"]


class TrackedObject:
    def __init__(self, obj_type: str, bbox: list, confidence: float):
        self.obj_type = obj_type
        self.bbox = bbox
        self.confidence = confidence
        self.frames_missing = 0
        self.is_active = True
        self.centroid = self._centroid(bbox)
        self.prev_centroid = self.centroid

    def update(self, bbox: list, confidence: float):
        self.prev_centroid = self.centroid
        self.bbox = bbox
        self.confidence = confidence
        self.centroid = self._centroid(bbox)
        self.frames_missing = 0

    def mark_missing(self):
        self.frames_missing += 1

    @staticmethod
    def _centroid(bbox):
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)


class AIEngine:
    def __init__(self, rtsp_url: str, config: dict, logger: logging.Logger):
        self.rtsp_url = rtsp_url
        self.config = config
        self.logger = logger
        self._stopped = False
        self._latest_frame: Optional[np.ndarray] = None
        self._snapshot_path: Optional[Path] = None

        # Pick the best available inference device. `ai.device` in
        # config can force a specific one:
        #   cpu | cuda | mps                  — native PyTorch backends
        #   intel:cpu | intel:gpu | intel:npu — OpenVINO backends for
        #                                       Intel integrated GPU / NPU
        #   auto                              — probe everything, pick the
        #                                       fastest available in this
        #                                       order: cuda > intel:gpu >
        #                                       intel:npu > mps > cpu
        #
        # Because the image ships every runtime, `auto` is the right
        # default — swap compose files to change device PASSTHROUGH
        # without rebuilding, and the engine will re-probe at startup.
        requested_device = config.get("device", "auto")
        self.device = self._resolve_device(requested_device)
        self.requested_device = requested_device
        if requested_device != self.device:
            self.logger.info(
                "Inference device: %s (requested %s — fell back after probe)",
                self.device, requested_device,
            )
        else:
            self.logger.info("Inference device: %s", self.device)

        model_path = config.get("model", "yolov8n.pt")
        self.model = self._load_model(model_path, self.device)

        self.detect_persons = config.get("detect_persons", True)
        self.detect_vehicles = config.get("detect_vehicles", True)

        # Per-class confidence thresholds. `confidence` is the shared
        # fallback so existing configs keep working; `confidence_person`
        # and `confidence_vehicle` override it per class. In practice
        # vehicles want a stricter threshold than people (YOLO flips
        # between car/truck/bus around ~0.5), so the defaults lean that
        # way out of the box.
        fallback = config.get("confidence", 0.45)
        self.confidence_person = config.get("confidence_person", fallback)
        self.confidence_vehicle = config.get("confidence_vehicle", fallback)

        self.frame_skip = config.get("frame_skip", 3)  # analyse every Nth frame

        self._tracked: dict[str, TrackedObject] = {}  # id → TrackedObject
        self._next_id = 0
        self._detection_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Runtime stats — read by the web status endpoint
        self._frames_captured: int = 0
        self._frames_analysed: int = 0
        self._detections_person: int = 0
        self._detections_vehicle: int = 0
        self._last_detection_ts: Optional[float] = None
        self._stream_connected: bool = False
        self._last_inference_ms: float = 0.0

        # Virtual line crossing — lives here so we can check per-frame
        # against the freshly-updated centroid pair on each tracked object.
        lines_config = config.get("lines", []) or []
        self.line_detector = LineCrossingDetector(
            lines_config, logger=logger.getChild("lc")
        )

    # ─── Device resolution & model loading ──────────────────────────────────

    @staticmethod
    def _resolve_device(preference: str) -> str:
        """
        Return the device name we should actually use.

        preference ∈ {
            "auto",
            "cpu", "cuda", "mps",        # native torch backends
            "intel:cpu", "intel:gpu",    # OpenVINO backends (for Intel
            "intel:npu",                 # iGPU / dGPU / NPU — N100 etc)
        }

        - "auto" probes every runtime the image carries and picks the
          fastest one actually reachable, in order:
              cuda → intel:gpu → intel:npu → mps → cpu
          NVIDIA discrete GPUs almost always beat anything else here,
          followed by an Intel iGPU/dGPU (via OpenVINO), then the NPU,
          then Apple Silicon (bare-metal only), then plain CPU.
        - explicit names are honoured if actually reachable, otherwise
          we log and fall back to cpu.
        """
        preference = (preference or "auto").lower()
        logger = logging.getLogger("ai_engine")
        probe = probe_available_devices()

        # ── Explicit Intel / OpenVINO preference ───────────────────────────
        if preference.startswith("intel:"):
            entry = probe.get(preference, {"available": False, "detail": "unknown"})
            if entry["available"]:
                return preference
            logger.warning(
                "ai.device=%s requested but unreachable (%s) — falling "
                "back to cpu. For intel:gpu/npu, make sure /dev/dri is "
                "passed into the container and your user is in the render group.",
                preference, entry["detail"],
            )
            return "cpu"

        # ── Explicit native torch preferences ──────────────────────────────
        if preference == "cuda":
            if probe["cuda"]["available"]:
                return "cuda"
            logger.warning(
                "ai.device=cuda but CUDA unreachable (%s) — falling back to cpu. "
                "Did you build the CUDA image (Dockerfile.cuda) and install "
                "nvidia-container-toolkit on the host?",
                probe["cuda"]["detail"],
            )
            return "cpu"
        if preference == "mps":
            if probe["mps"]["available"]:
                return "mps"
            logger.warning(
                "ai.device=mps but MPS unreachable (%s) — falling back to cpu.",
                probe["mps"]["detail"],
            )
            return "cpu"
        if preference == "cpu":
            return "cpu"

        # ── Auto: probe everything, prefer the fastest reachable ───────────
        if preference != "auto":
            logger.warning("Unknown ai.device=%r, using auto", preference)

        # Note: we deliberately do NOT pick intel:cpu automatically — the
        # OpenVINO CPU path is often marginally faster but requires the
        # ~30s IR export on first start, which is surprising for users
        # who didn't explicitly ask for it. Native CPU is the safe default.
        for choice in ("cuda", "intel:gpu", "intel:npu", "mps", "cpu"):
            if probe.get(choice, {}).get("available"):
                return choice
        return "cpu"

    def _load_model(self, model_path: str, device: str):
        """
        Load the YOLO model, picking between the native .pt path and
        the OpenVINO IR path based on the resolved device.

        For Intel/OpenVINO: ultralytics will export the .pt to an
        OpenVINO IR directory on first run, which takes ~30s. We
        cache the exported model under /config so subsequent restarts
        are instant.
        """
        if device.startswith("intel:"):
            from pathlib import Path as _Path

            base = _Path(model_path).stem  # e.g. "yolov8n"
            cache_dir = _Path("/config") / f"{base}_openvino_model"

            if not cache_dir.exists():
                self.logger.info(
                    "Exporting %s to OpenVINO IR (one-time, ~30s)…", model_path
                )
                tmp = YOLO(model_path)
                # ultralytics writes the export next to the source .pt;
                # we move it to /config so it persists across rebuilds.
                exported = tmp.export(format="openvino")
                shutil.move(str(exported), str(cache_dir))

            self.logger.info("Loading OpenVINO model from %s", cache_dir)
            model = YOLO(str(cache_dir))
            return model

        # Native torch path — just load and move to device.
        self.logger.info("Loading YOLO model: %s", model_path)
        model = YOLO(model_path)
        try:
            model.to(device)
        except Exception as e:
            self.logger.warning(
                "Could not move model to %s (%s); falling back to cpu",
                device, e,
            )
            self.device = "cpu"
            model.to("cpu")
        return model

    # ─── Public API ─────────────────────────────────────────────────────────

    async def detections(self) -> AsyncGenerator[dict, None]:
        """Async generator — yields detection events."""
        loop = asyncio.get_running_loop()

        # Run blocking capture+inference in a thread
        capture_task = loop.run_in_executor(None, self._capture_loop)

        while not self._stopped:
            try:
                event = await asyncio.wait_for(self._detection_queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue
            except Exception:
                self.logger.exception("Detection queue error")
                break

        await capture_task

    async def get_snapshot(self) -> Optional[Path]:
        return self._snapshot_path

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """Read-only accessor used by the web line-tool UI."""
        return self._latest_frame

    async def stop(self):
        self._stopped = True

    def reset(self):
        """Prepare the engine for a new connection cycle.

        Called by AIPortCamera before re-starting the AI loop after a
        Protect reconnect.  Resets the stop flag so detections() will
        run again; all other state (tracked objects, counters) is
        intentionally preserved across reconnects.
        """
        self._stopped = False

    # ─── Internal capture + inference ───────────────────────────────────────

    def _capture_loop(self):
        self.logger.info("Opening capture stream: %s", self.rtsp_url)
        cap = cv2.VideoCapture(self.rtsp_url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        frame_count = 0
        reconnect_delay = 2
        consecutive_inference_errors = 0

        while not self._stopped:
            ret, frame = cap.read()
            if not ret:
                if self._stream_connected:
                    self.logger.warning(
                        "Lost capture stream (%s) — retrying in %ds",
                        self.rtsp_url, reconnect_delay,
                    )
                else:
                    self.logger.debug(
                        "Capture stream not yet ready (%s) — retrying in %ds",
                        self.rtsp_url, reconnect_delay,
                    )
                self._stream_connected = False
                cap.release()
                # Interruptible wait so .stop() is snappy.
                slept = 0.0
                while slept < reconnect_delay and not self._stopped:
                    time.sleep(0.5)
                    slept += 0.5
                if self._stopped:
                    break
                cap = cv2.VideoCapture(self.rtsp_url)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                reconnect_delay = min(reconnect_delay * 2, 30)
                continue

            if not self._stream_connected:
                self.logger.info("Capture stream connected: %s", self.rtsp_url)
            self._stream_connected = True
            reconnect_delay = 2
            frame_count += 1
            self._frames_captured += 1
            self._latest_frame = frame

            if frame_count % self.frame_skip != 0:
                continue

            # Save snapshot periodically
            if frame_count % (self.frame_skip * 30) == 0:
                self._save_snapshot(frame)

            now = time.monotonic()
            try:
                self._run_inference(frame)
                consecutive_inference_errors = 0
            except Exception as exc:
                consecutive_inference_errors += 1
                # Log with a stack trace on the first failure of a burst,
                # then just a terse counter to avoid spamming the logs
                # if every frame is failing (e.g. a broken model file).
                if consecutive_inference_errors == 1:
                    self.logger.exception(
                        "Inference error on frame %d: %s", frame_count, exc
                    )
                elif consecutive_inference_errors % 50 == 0:
                    self.logger.error(
                        "Inference still failing (%d consecutive errors): %s",
                        consecutive_inference_errors, exc,
                    )
                continue

            self._frames_analysed += 1
            self._last_inference_ms = (time.monotonic() - now) * 1000.0

        cap.release()

    def _run_inference(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        # For OpenVINO-exported models ultralytics expects the plugin
        # name directly (e.g. "intel:gpu"). For native torch backends
        # it expects "cpu"/"cuda"/"mps". Both cases work with the same
        # self.device string — we just pass it straight through.
        results = self.model(frame, verbose=False, device=self.device)[0]

        current_detections = []

        for box in results.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])

            # Resolve the class first so we can apply the right
            # per-class threshold (persons are usually allowed in at
            # a looser confidence than vehicles).
            if cls_id == PERSON_CLASS and self.detect_persons:
                obj_type = "person"
                threshold = self.confidence_person
            elif cls_id in VEHICLE_CLASSES and self.detect_vehicles:
                obj_type = "vehicle"
                threshold = self.confidence_vehicle
            else:
                continue

            if conf < threshold:
                continue

            # Normalise bbox to 0-1
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            bbox = [x1 / w, y1 / h, x2 / w, y2 / h]
            current_detections.append((obj_type, bbox, conf))

        self._update_tracker(current_detections, frame)

    def _update_tracker(self, detections: list, frame: np.ndarray):
        """
        Simple IoU-based tracker. Matches new detections to existing
        tracked objects, emits start/stop events accordingly.
        """
        matched_ids = set()

        for obj_type, bbox, conf in detections:
            best_id = self._find_best_match(obj_type, bbox)

            if best_id:
                obj = self._tracked[best_id]
                obj.update(bbox, conf)
                matched_ids.add(best_id)

                # Check virtual line crossings against the freshly
                # updated centroid pair. A crossing emits a discrete
                # "start" event that Protect surfaces in the timeline.
                if self.line_detector.lines:
                    crossed = self.line_detector.check(
                        obj.prev_centroid, obj.centroid
                    )
                    if crossed:
                        snap = self._save_snapshot(frame)
                        self._emit(
                            "start",
                            obj.obj_type,
                            bbox,
                            conf,
                            snapshot_path=snap,
                            line_crossing=crossed,
                        )
            else:
                # New object
                obj_id = str(self._next_id)
                self._next_id += 1
                obj = TrackedObject(obj_type, bbox, conf)
                self._tracked[obj_id] = obj
                matched_ids.add(obj_id)

                snap = self._save_snapshot(frame)
                self._emit("start", obj_type, bbox, conf, snapshot_path=snap)

        # Mark unmatched objects as missing
        for obj_id, obj in list(self._tracked.items()):
            if obj_id not in matched_ids:
                obj.mark_missing()
                if obj.frames_missing >= DEBOUNCE_STOP_FRAMES:
                    self._emit("stop", obj.obj_type, obj.bbox, obj.confidence)
                    del self._tracked[obj_id]

    def _find_best_match(self, obj_type: str, bbox: list) -> Optional[str]:
        best_id = None
        best_iou = 0.3  # minimum IoU to match

        for obj_id, obj in self._tracked.items():
            if obj.obj_type != obj_type:
                continue
            iou = self._iou(bbox, obj.bbox)
            if iou > best_iou:
                best_iou = iou
                best_id = obj_id

        return best_id

    @staticmethod
    def _iou(a: list, b: list) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1 = max(ax1, bx1)
        iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        return inter / (area_a + area_b - inter)

    def _save_snapshot(self, frame: np.ndarray) -> Optional[Path]:
        try:
            tmp = Path(tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name)
            cv2.imwrite(str(tmp), frame)
            old = self._snapshot_path
            self._snapshot_path = tmp
            if old and old.exists():
                try:
                    old.unlink()
                except OSError:
                    self.logger.debug("Could not remove old snapshot %s", old, exc_info=True)
            return tmp
        except Exception:
            return None

    def _emit(self, event_type: str, obj_type: str, bbox: list,
              confidence: float, snapshot_path: Optional[Path] = None,
              line_crossing: Optional[str] = None):
        if event_type == "start":
            if obj_type == "person":
                self._detections_person += 1
            elif obj_type == "vehicle":
                self._detections_vehicle += 1
            self._last_detection_ts = time.time()

        event = {
            "type": event_type,
            "object": obj_type,
            "bbox": bbox,
            "confidence": confidence,
            "snapshot_path": snapshot_path,
            "line_crossing": line_crossing,
            "timestamp": time.time(),
        }
        try:
            self._detection_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop if queue full
