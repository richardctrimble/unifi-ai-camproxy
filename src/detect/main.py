"""
detect.main — entrypoint for the vehicle/person detection image (:detect).

Runs the full spoof+inference pipeline (ONVIF WebSocket adoption, YOLOv8
person/vehicle detection) without the virtual line-crossing feature.

Line-crossing is a separate image (:lines) so that changes to the line
logic don't risk breaking the detection pipeline and vice versa.

This module is a thin dispatcher: it sets the APP_IMAGE_VARIANT marker
so any variant-aware code downstream (banner, web UI tabs) can adapt,
then delegates to the shared src/main.py entrypoint.

src/ is on sys.path because the Dockerfiles set WORKDIR /app/src before
launching  `python -m detect.main`.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("APP_IMAGE_VARIANT", "detect")

import main as _base  # noqa: E402 — shared entrypoint from parent src/


if __name__ == "__main__":
    try:
        asyncio.run(_base.main())
    except KeyboardInterrupt:
        _base.logger.info("Shutting down (KeyboardInterrupt)")
