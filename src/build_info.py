"""Runtime access to the image's build metadata.

Values are injected at image-build time via ARG / ENV in the Dockerfile
(GIT_SHA, GIT_REF, BUILD_TIME).  Exposed here so both the startup banner
(main.py) and the Status tab (web_tool.py) can surface the same info,
letting the user answer "which image am I actually running?" without
shelling into the container.
"""

from __future__ import annotations

import os


def get_build_info() -> dict:
    """Return the image's build metadata as a plain dict."""
    return {
        "git_sha": os.environ.get("APP_GIT_SHA", "unknown"),
        "git_sha_short": os.environ.get("APP_GIT_SHA", "unknown")[:7],
        "git_ref": os.environ.get("APP_GIT_REF", "unknown"),
        "build_time": os.environ.get("APP_BUILD_TIME", "unknown"),
    }
