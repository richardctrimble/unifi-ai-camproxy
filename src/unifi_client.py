"""
AIPortCamera — spoofs a UniFi G4 Pro camera to Protect,
then injects smart detections from our own AI pipeline.

Protocol notes (from unifi-cam-proxy reverse engineering):
  - Connects to wss://HOST:7442/camera/1.0/ws?token=TOKEN
  - Sends ubnt_avclient_hello to adopt
  - Injects detections via EventSmartDetect with edgeType start/stop

Single-pull design
------------------
Older versions opened two independent RTSP connections to the camera: one
for ffmpeg (relaying video to Protect) and one for OpenCV (AI inference).
Many cameras reject concurrent connections and the load is simply wasteful.

The fix: when Protect requests the primary stream (video1), we spawn ffmpeg
with a second output — an MPEGTS copy piped to a loopback UDP socket.
AIEngine reads from that UDP socket instead of the camera directly, so
there is exactly one RTSP connection per camera at runtime.

Port allocation
---------------
Each AIPortCamera instance claims the next port in a class-level counter
(default starts at 5200).  Override per-camera with ``ai.ai_udp_port`` in
config.yml if you need deterministic ports or have port-range constraints.
"""

import asyncio
import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

import aiohttp

# Reuse the battle-tested base from unifi-cam-proxy
sys.path.insert(0, "/app/unifi-cam-proxy")
from unifi.cams.base import UnifiCamBase, SmartDetectObjectType

from ai_engine import AIEngine


class AIPortCamera(UnifiCamBase):
    """
    Our custom camera class. Video comes from RTSP.
    AI inference runs in a background task and triggers
    Protect smart detection events when persons/vehicles are found.
    """

    # Class-level counter so each instance gets a unique default port.
    _next_ai_port: int = 5200
    _port_lock: threading.Lock = threading.Lock()

    def __init__(self, args, logger: logging.Logger, rtsp_url: str,
                 snapshot_url: Optional[str], ai_config: dict):
        super().__init__(args, logger)
        self.rtsp_url = rtsp_url
        self.snapshot_url = snapshot_url
        self.ai_config = ai_config

        # Local UDP port where video1's ffmpeg will send an MPEGTS copy for AI.
        # Each camera must use a distinct port; auto-assigned unless overridden.
        with AIPortCamera._port_lock:
            self._ai_udp_port: int = ai_config.get("ai_udp_port", AIPortCamera._next_ai_port)
            AIPortCamera._next_ai_port = max(
                AIPortCamera._next_ai_port + 1,
                self._ai_udp_port + 1,
            )

        # AI engine reads from the loopback UDP feed, not the camera RTSP URL.
        ai_source = f"udp://127.0.0.1:{self._ai_udp_port}"
        self.ai_engine = AIEngine(
            rtsp_url=ai_source,
            config=ai_config,
            logger=logger.getChild("ai"),
        )

        self._snapshot_path: Optional[Path] = None
        self._ai_task: Optional[asyncio.Task] = None

        # Set by start_video_stream once video1's ffmpeg process is live.
        # run() awaits this before starting the AI loop so there is no
        # log spam while waiting for Protect to request the stream.
        self._video1_ready: asyncio.Event = asyncio.Event()

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

    async def start_video_stream(
        self, stream_index: str, stream_name: str, destination: tuple
    ) -> None:
        """
        For video1: spawn ffmpeg with two outputs so AI inference and the
        Protect relay share a single RTSP connection.

          Output 1 — FLV → stdout → clock_sync → nc → Protect  (unchanged)
          Output 2 — MPEGTS (video only) → UDP loopback → AIEngine

        For video2 / video3: delegate to the base class as normal.
        """
        if stream_index != "video1":
            await super().start_video_stream(stream_index, stream_name, destination)
            return

        has_spawned = stream_index in self._ffmpeg_handles
        is_dead = has_spawned and self._ffmpeg_handles[stream_index].poll() is not None

        if has_spawned and not is_dead:
            return

        source = await self.get_stream_source(stream_index)
        host, port = destination
        ai_dst = f"udp://127.0.0.1:{self._ai_udp_port}?pkt_size=1316"

        cmd = (
            "ffmpeg -nostdin -loglevel error -y"
            f" {self.get_base_ffmpeg_args(stream_index)}"
            f" -rtsp_transport {self.args.rtsp_transport}"
            f' -i "{source}"'
            # Output 1: FLV stream for Protect (stdout → clock_sync → nc)
            f" {self.get_extra_ffmpeg_args(stream_index)}"
            f" -metadata streamName={stream_name} -f flv pipe:1"
            # Output 2: MPEGTS video-only copy for AI inference via UDP loopback
            f" -map 0:v:0 -c:v copy -an -f mpegts '{ai_dst}'"
            f" | {sys.executable} -m unifi.clock_sync"
            f" {'--write-timestamps' if self._needs_flv_timestamps else ''}"
            f" | nc {host} {port}"
        )

        if is_dead:
            self.logger.warning("Previous ffmpeg process for %s died.", stream_index)

        self.logger.info("Spawning ffmpeg for %s (%s): %s", stream_index, stream_name, cmd)
        self._ffmpeg_handles[stream_index] = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, shell=True
        )

        # Unblock run() so the AI loop can start reading from the UDP feed.
        self._video1_ready.set()

    async def fetch_to_file(self, url: str, dest: Path) -> bool:
        """Download a URL to a local file. Returns True on success."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        dest.write_bytes(await resp.read())
                        return True
        except Exception:
            self.logger.debug("fetch_to_file failed for %s", url, exc_info=True)
        return False

    # ─── Background AI loop ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Called by Core alongside _run(). AI loop is gated on video1 being active."""
        # Reset for reconnect cycles (Core reuses the same camera object).
        self._video1_ready.clear()
        self.ai_engine.reset()

        # Wait until video1's ffmpeg has been spawned and is sending UDP frames.
        # CancelledError from Core will propagate cleanly through this await.
        await self._video1_ready.wait()

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
