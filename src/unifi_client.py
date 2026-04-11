"""
AIPortCamera — spoofs a UniFi G4 Pro camera to Protect,
then injects smart detections from our own AI pipeline.

Protocol notes (from unifi-cam-proxy reverse engineering):
  - Connects to wss://HOST:7442/camera/1.0/ws?token=TOKEN
  - Sends ubnt_avclient_hello to adopt
  - Injects detections via EventSmartDetect with edgeType start/stop
"""

import asyncio
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp

# Reuse the battle-tested base from unifi-cam-proxy
sys.path.insert(0, "/app/unifi-cam-proxy")
from unifi.cams.base import UnifiCamBase, SmartDetectObjectType

from ai_engine import AIEngine
from line_crossing import LineCrossingDetector


class AIPortCamera(UnifiCamBase):
    """
    Our custom camera class. Video comes from RTSP.
    AI inference runs in a background task and triggers
    Protect smart detection events when persons/vehicles are found.
    """

    def __init__(self, args, logger: logging.Logger, rtsp_url: str,
                 snapshot_url: Optional[str], ai_config: dict):
        super().__init__(args, logger)
        self.rtsp_url = rtsp_url
        self.snapshot_url = snapshot_url
        self.ai_config = ai_config

        # AI engine (YOLO inference + line crossing)
        self.ai_engine = AIEngine(
            rtsp_url=rtsp_url,
            config=ai_config,
            logger=logger.getChild("ai"),
        )

        # Line crossing detector (optional)
        lines = ai_config.get("lines", [])
        self.line_detector = LineCrossingDetector(lines, logger=logger.getChild("lc"))

        self._snapshot_path: Optional[Path] = None
        self._ai_task: Optional[asyncio.Task] = None

    # ─── Required abstract methods ──────────────────────────────────────────

    async def get_snapshot(self) -> Path:
        """Return latest snapshot frame for Protect thumbnails."""
        path = await self.ai_engine.get_snapshot()
        if path:
            self._snapshot_path = path
            return path

        # Fallback: fetch from camera's HTTP snapshot URL
        if self.snapshot_url:
            tmp = Path(tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name)
            if await self.fetch_to_file(self.snapshot_url, tmp):
                return tmp

        # Last resort: blank file so Protect doesn't error
        tmp = Path(tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name)
        tmp.touch()
        return tmp

    async def get_stream_source(self, stream_index: str) -> str:
        """All stream qualities point at the same RTSP source."""
        return self.rtsp_url

    # ─── Background AI loop ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Called by Core alongside _run(). This is where our AI loop lives."""
        self._ai_task = asyncio.create_task(self._ai_loop())
        await self._ai_task

    async def _ai_loop(self) -> None:
        """
        Continuously pull detections from the AI engine and
        translate them into Protect smart detection events.
        """
        self.logger.info("AI detection loop started")

        async for detection in self.ai_engine.detections():
            try:
                await self._handle_detection(detection)
            except Exception:
                self.logger.exception("Error handling detection")

    async def _handle_detection(self, detection: dict) -> None:
        """
        detection = {
            "type": "start" | "stop",
            "object": "person" | "vehicle",
            "bbox": [x1, y1, x2, y2],   # normalised 0-1
            "confidence": 0.87,
            "line_crossing": "LineA" | None,
            "snapshot_path": Path | None,
        }
        """
        obj_type = (
            SmartDetectObjectType.PERSON
            if detection["object"] == "person"
            else SmartDetectObjectType.VEHICLE
        )

        if detection["type"] == "start":
            if detection.get("snapshot_path"):
                self.update_motion_snapshot(detection["snapshot_path"])

            self.logger.info(
                f"Detection START: {detection['object']}"
                + (f" crossed {detection['line_crossing']}" if detection.get("line_crossing") else "")
                + f" conf={detection['confidence']:.2f}"
            )
            await self.trigger_motion_start(obj_type)

        elif detection["type"] == "stop":
            self.logger.info(f"Detection STOP: {detection['object']}")
            await self.trigger_motion_stop()

    async def close(self):
        if self._ai_task and not self._ai_task.done():
            self._ai_task.cancel()
        await self.ai_engine.stop()
        await super().close()
