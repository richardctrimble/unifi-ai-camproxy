"""
onvif_bridge.main — entrypoint for the lightweight bridge image.

Status: SKELETON. The pieces it wires together are:

  protect_discovery.discover_adopted_onvif_cameras()
      → returns a list of DiscoveredCamera from Protect's API.
  onvif_subscriber.subscribe_camera()
      → yields OnvifEvent for each camera (currently stubbed).
  protect_pusher.ProtectPusher.push(event)
      → translates events into bookmarks + alarm webhooks (currently stubbed).
  web_tool
      → status/setup UI; shows discovered cameras and last events.

What runs today:
  * Loads /config/config.yml.
  * Logs an obvious "preparation phase" banner.
  * Starts the web UI so the user can see something.
  * Idles. No event subscription, no Protect pushes yet.

What's missing (next sessions):
  * Real ONVIF subscription via onvif-zeep.
  * Verified bookmark endpoint shape.
  * Alarm Manager webhook trigger contract verification.
  * Per-camera config UI (currently a placeholder).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List

import yaml

# Reuse the build-info banner from the legacy entrypoint.
sys.path.insert(0, "/app/src")
from build_info import get_build_info  # noqa: E402

from onvif_bridge.onvif_subscriber import CameraSubscription  # noqa: E402

CONFIG_PATH = Path("/config/config.yml")


# ── Logging (mirrors src/main.py so log lines look familiar) ───────────────


def _configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = os.environ.get("LOG_FILE", "/config/camproxy.log")
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
            ))
        except OSError as exc:
            print(f"WARNING: Could not open LOG_FILE={log_file}: {exc}",
                  file=sys.stderr)

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    if level > logging.DEBUG:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("camproxy")


# ── Config ─────────────────────────────────────────────────────────────────


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(
            "No %s found. Bridge mode needs at least unifi.host + creds. "
            "Mount a config.yml or set UNIFI_HOST + UNIFI_USERNAME + "
            "UNIFI_PASSWORD env vars and use docker-entrypoint.py to "
            "seed one.", CONFIG_PATH,
        )
        return {}
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.error("Could not read %s: %s", CONFIG_PATH, e)
        return {}
    if not isinstance(cfg, dict):
        logger.error("Config root is not a YAML mapping: %s", CONFIG_PATH)
        return {}
    return cfg


# ── Banner ─────────────────────────────────────────────────────────────────


def log_banner() -> None:
    build = get_build_info()
    logger.info("─── unifi-ai-camproxy / ONVIF bridge ────────────────────────")
    logger.info("Build: %s (ref: %s) @ %s",
                build["git_sha_short"], build["git_ref"], build["build_time"])
    logger.info("Variant: %s", os.environ.get("APP_IMAGE_VARIANT", "onvif"))
    logger.info("PREPARATION PHASE — event subscription is stubbed.")
    logger.info("For the working spoof+inference flow, use the :full image.")
    logger.info("─────────────────────────────────────────────────────────────")


# ── Main ───────────────────────────────────────────────────────────────────


# Live state surfaced to the web UI dashboard.
discovered_cameras: List[dict] = []
subscriptions: Dict[str, CameraSubscription] = {}


async def main() -> None:
    log_banner()
    cfg = load_config()

    # Web UI — currently a thin placeholder. The full Status / Setup /
    # Logs UI from the spoof image will be ported across once the real
    # subscription + pusher pieces land. For now we expose `/api/status`
    # so a curl can confirm the container is alive.
    web_cfg = cfg.get("web_tool", {}) or {}
    if web_cfg.get("enabled", True):
        try:
            from onvif_bridge.web_tool import BridgeWebTool
            port = int(web_cfg.get("port", 8091))
            web = BridgeWebTool(cfg, discovered_cameras, subscriptions)
            logger.info("Starting bridge web UI on port %d", port)
            asyncio.create_task(web.run(port))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bridge web UI failed to start: %s", exc)

    # TODO (next milestone):
    #   - Periodically run protect_discovery.discover_adopted_onvif_cameras
    #     and reconcile the result against `subscriptions`.
    #   - For each new camera, spawn an onvif_subscriber task.
    #   - Pipe events through protect_pusher.ProtectPusher.push.

    # Idle so the container stays up and `docker logs` is meaningful.
    while True:
        await asyncio.sleep(3600)
        logger.info("Bridge idle (skeleton phase) — see SECONDBRAIN.md")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
