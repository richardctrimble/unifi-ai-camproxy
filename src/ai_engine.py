"""
AIEngine — pulls frames from RTSP, runs YOLOv8 inference,
emits detection events as an async generator.

Detection lifecycle:
  - Object enters frame  → emit "start" event
  - Object absent for N frames → emit "stop" event
"""

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

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
        self.logger.info("Running inference on: %s", self.device)

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

        # ── Probe helpers ──────────────────────────────────────────────────
        def _cuda_ok() -> bool:
            try:
                import torch
                return torch.cuda.is_available()
            except Exception:
                return False

        def _mps_ok() -> bool:
            try:
                import torch
                return (
                    getattr(torch.backends, "mps", None) is not None
                    and torch.backends.mps.is_available()
                )
            except Exception:
                return False

        def _openvino_devices() -> set[str]:
            """
            Return the set of OpenVINO plugin names the runtime can
            actually reach — 'CPU', 'GPU', 'NPU', etc. Empty set if
            OpenVINO isn't installed in the image.
            """
            try:
                import openvino as ov
                return set(ov.Core().available_devices)
            except Exception:
                return set()

        # ── Explicit Intel / OpenVINO preference ───────────────────────────
        if preference.startswith("intel:"):
            target = preference.split(":", 1)[1].upper()  # gpu → GPU
            ov_devices = _openvino_devices()
            if not ov_devices:
                logger.warning(
                    "ai.device=%s but OpenVINO isn't available in this "
                    "image — falling back to cpu.",
                    preference,
                )
                return "cpu"
            if target not in ov_devices:
                logger.warning(
                    "ai.device=%s but OpenVINO can't reach %s (saw %s) — "
                    "falling back to cpu. Usually a /dev/dri passthrough "
                    "or render-group permissions issue.",
                    preference, target, sorted(ov_devices),
                )
                return "cpu"
            return preference

        # ── Explicit native torch preferences ──────────────────────────────
        if preference == "cuda":
            return "cuda" if _cuda_ok() else "cpu"
        if preference == "mps":
            return "mps" if _mps_ok() else "cpu"
        if preference == "cpu":
            return "cpu"

        # ── Auto: probe everything, prefer the fastest reachable ───────────
        if preference != "auto":
            logger.warning("Unknown ai.device=%r, using auto", preference)

        if _cuda_ok():
            return "cuda"

        ov_devices = _openvino_devices()
        if "GPU" in ov_devices:
            return "intel:gpu"
        if "NPU" in ov_devices:
            return "intel:npu"

        if _mps_ok():
            return "mps"

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
        self.logger.info(f"Opening RTSP stream: {self.rtsp_url}")
        cap = cv2.VideoCapture(self.rtsp_url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        frame_count = 0
        reconnect_delay = 2

        while not self._stopped:
            ret, frame = cap.read()
            if not ret:
                self._stream_connected = False
                self.logger.warning(f"Lost stream, retrying in {reconnect_delay}s...")
                cap.release()
                time.sleep(reconnect_delay)
                cap = cv2.VideoCapture(self.rtsp_url)
                reconnect_delay = min(reconnect_delay * 2, 30)
                continue

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

            self._frames_analysed += 1
            self._run_inference(frame)

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
