"""
AIEngine — pulls frames from RTSP, runs YOLOv8 inference,
emits detection events as an async generator.

Detection lifecycle:
  - Object enters frame  → emit "start" event
  - Object absent for N frames → emit "stop" event
"""

import asyncio
import logging
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

        model_path = config.get("model", "yolov8n.pt")
        self.logger.info(f"Loading YOLO model: {model_path}")
        self.model = YOLO(model_path)

        self.detect_persons = config.get("detect_persons", True)
        self.detect_vehicles = config.get("detect_vehicles", True)
        self.confidence_threshold = config.get("confidence", 0.45)
        self.frame_skip = config.get("frame_skip", 3)  # analyse every Nth frame

        self._tracked: dict[str, TrackedObject] = {}  # id → TrackedObject
        self._next_id = 0
        self._detection_queue: asyncio.Queue = asyncio.Queue()

        # Virtual line crossing — lives here so we can check per-frame
        # against the freshly-updated centroid pair on each tracked object.
        lines_config = config.get("lines", []) or []
        self.line_detector = LineCrossingDetector(
            lines_config, logger=logger.getChild("lc")
        )

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
                self.logger.warning(f"Lost stream, retrying in {reconnect_delay}s...")
                cap.release()
                time.sleep(reconnect_delay)
                cap = cv2.VideoCapture(self.rtsp_url)
                reconnect_delay = min(reconnect_delay * 2, 30)
                continue

            reconnect_delay = 2
            frame_count += 1
            self._latest_frame = frame

            if frame_count % self.frame_skip != 0:
                continue

            # Save snapshot periodically
            if frame_count % (self.frame_skip * 30) == 0:
                self._save_snapshot(frame)

            self._run_inference(frame)

        cap.release()

    def _run_inference(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        results = self.model(frame, verbose=False)[0]

        current_detections = []

        for box in results.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])

            if conf < self.confidence_threshold:
                continue

            if cls_id == PERSON_CLASS and self.detect_persons:
                obj_type = "person"
            elif cls_id in VEHICLE_CLASSES and self.detect_vehicles:
                obj_type = "vehicle"
            else:
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
            self._snapshot_path = tmp
            return tmp
        except Exception:
            return None

    def _emit(self, event_type: str, obj_type: str, bbox: list,
              confidence: float, snapshot_path: Optional[Path] = None,
              line_crossing: Optional[str] = None):
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
