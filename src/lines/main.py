"""
lines.main — entrypoint for the virtual line-crossing image (:lines).

Runs the full spoof+inference pipeline with the virtual line-crossing
feature enabled. Users draw crossing lines in the web UI (Lines tab);
any tracked person or vehicle that crosses a line fires a dedicated
detection event in UniFi Protect.

Line-crossing requires line_crossing.py to be present (it is, in this
image). The :detect image intentionally omits it — see Dockerfile.detect.

This module is a thin dispatcher: it sets APP_IMAGE_VARIANT and then
delegates to the shared src/main.py entrypoint.

src/ is on sys.path because the Dockerfiles set WORKDIR /app/src before
launching `python -m lines.main`.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("APP_IMAGE_VARIANT", "lines")

import main as _base  # noqa: E402 — shared entrypoint from parent src/


if __name__ == "__main__":
    try:
        asyncio.run(_base.main())
    except KeyboardInterrupt:
        _base.logger.info("Shutting down (KeyboardInterrupt)")
