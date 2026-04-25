"""
onvif_bridge.main — entrypoint for the lightweight bridge image.

Wires together:

  protect_discovery    — periodic list of ONVIF-adopted cameras from Protect.
  onvif_subscriber     — per-camera PullPoint task, normalises events.
  protect_pusher       — bookmarks + Alarm Manager webhooks back into Protect.
  web_tool             — Status / Setup / Logs UI (mirrors full image's UX).

The discovery loop reconciles Protect's camera list against the running
subscriptions every DISCOVERY_INTERVAL_S. New ONVIF cameras get a
subscription task; removed ones get cancelled. Per-event data flows:

  PullPoint message → OnvifEvent → ProtectPusher.push() → bookmark + webhook

Everything is best-effort — a flaky camera, a bad credential, or a
Protect outage shouldn't kill the bridge. State is surfaced on the
Status dashboard so the user can see what's working without reading logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict

import yaml

# Reuse the build-info banner from the legacy entrypoint.
sys.path.insert(0, "/app/src")
from build_info import get_build_info  # noqa: E402

from onvif_bridge.onvif_subscriber import (  # noqa: E402
    CameraSubscription, subscribe_camera,
)
from onvif_bridge.protect_discovery import (  # noqa: E402
    DiscoveredCamera, discover_adopted_onvif_cameras,
)
from onvif_bridge.protect_pusher import ProtectPusher  # noqa: E402
from unifi_auth import UniFiAuthError  # noqa: E402

CONFIG_PATH = Path("/config/config.yml")

# How often to re-poll Protect for new / removed cameras.
DISCOVERY_INTERVAL_S = 60


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
        # zeep is chatty at INFO when XML round-trips; tone it down.
        logging.getLogger("zeep").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("camproxy")


# ── Config ─────────────────────────────────────────────────────────────────


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(
            "No %s found. Bridge mode needs unifi.host + creds. "
            "Mount a config.yml or set UNIFI_HOST + UNIFI_USERNAME + "
            "UNIFI_PASSWORD env vars.", CONFIG_PATH,
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
    logger.info("─────────────────────────────────────────────────────────────")


# ── Live state (read by the web UI) ────────────────────────────────────────


# Most recent discovery result, projected for the dashboard.
discovered_cameras: list[dict] = []

# Active subscription tasks keyed by Protect camera id.
subscriptions: Dict[str, CameraSubscription] = {}
subscription_tasks: Dict[str, asyncio.Task] = {}

# Last discovery error, surfaced on the dashboard.
last_discovery_error: str = ""
last_discovery_epoch: float = 0.0


# ── Discovery + reconciliation ─────────────────────────────────────────────


def _onvif_creds_for(cam: DiscoveredCamera, cfg: dict) -> tuple[str, str, int]:
    """Resolve username, password, port for one camera.

    Looks for a per-camera override under cfg['cameras'][...] keyed by
    Protect ID, then falls back to fleet-wide cfg['onvif'] creds.
    Default port 80, override per-camera or fleet-wide.
    """
    onvif_global = cfg.get("onvif", {}) or {}
    user = onvif_global.get("username", "")
    pwd = onvif_global.get("password", "")
    port = int(onvif_global.get("port", 80))

    for entry in cfg.get("cameras", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("protect_id") != cam.protect_id:
            continue
        user = entry.get("onvif_username") or user
        pwd = entry.get("onvif_password") or pwd
        port = int(entry.get("onvif_port", port))
        break

    return user, pwd, port


async def _run_subscription(sub: CameraSubscription, pusher: ProtectPusher) -> None:
    """Pump events from one camera into the pusher until cancelled."""
    try:
        async for event in subscribe_camera(sub):
            try:
                outcome = await pusher.push(event)
                if not outcome.ok:
                    logger.warning(
                        "Push failed for %s (%s): %s",
                        sub.name, event.kind, outcome.message,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Pusher crashed on event from %s", sub.name)
    except asyncio.CancelledError:
        sub.is_connected = False
        raise


async def _reconcile(cfg: dict, pusher: ProtectPusher) -> None:
    """Pull adopted ONVIF cameras from Protect and reconcile against
    the running subscription tasks. Called once per DISCOVERY_INTERVAL_S."""
    global last_discovery_error, last_discovery_epoch, discovered_cameras

    unifi_cfg = cfg.get("unifi", {}) or {}
    host = unifi_cfg.get("host", "")
    user = unifi_cfg.get("username", "")
    pwd = unifi_cfg.get("password", "")
    api_key = unifi_cfg.get("api_key", "")

    if not host or not (user and pwd):
        last_discovery_error = "missing unifi host / username / password"
        return

    try:
        cams = await discover_adopted_onvif_cameras(host, user, pwd, api_key=api_key)
    except UniFiAuthError as exc:
        last_discovery_error = f"Protect login failed: {exc}"
        last_discovery_epoch = time.time()
        return
    except Exception as exc:  # noqa: BLE001
        last_discovery_error = f"discovery error: {exc}"
        last_discovery_epoch = time.time()
        return

    last_discovery_error = ""
    last_discovery_epoch = time.time()

    # Project for the dashboard.
    discovered_cameras = [
        {
            "protect_id": c.protect_id, "name": c.name, "host": c.host,
            "type": c.type, "state": c.state, "is_adopted": c.is_adopted,
        }
        for c in cams
    ]

    # Reconcile: start new, cancel removed.
    seen_ids: set[str] = set()
    for cam in cams:
        if not cam.protect_id:
            continue
        seen_ids.add(cam.protect_id)
        if cam.protect_id in subscription_tasks:
            existing = subscriptions.get(cam.protect_id)
            if existing is not None and existing.onvif_host != cam.host:
                # IP changed — restart the subscription
                logger.info("Camera %s IP changed (%s → %s) — resubscribing",
                            cam.name, existing.onvif_host, cam.host)
                subscription_tasks[cam.protect_id].cancel()
                subscription_tasks.pop(cam.protect_id, None)
                subscriptions.pop(cam.protect_id, None)
            else:
                continue

        u, p, port = _onvif_creds_for(cam, cfg)
        if not (u and p):
            logger.info(
                "Skipping %s — no ONVIF credentials configured. Add "
                "`onvif: { username, password }` to config.yml or set "
                "per-camera `onvif_username` / `onvif_password`.",
                cam.name,
            )
            continue

        sub = CameraSubscription(
            protect_id=cam.protect_id, name=cam.name,
            onvif_host=cam.host, onvif_port=port,
            username=u, password=p,
        )
        subscriptions[cam.protect_id] = sub
        subscription_tasks[cam.protect_id] = asyncio.create_task(
            _run_subscription(sub, pusher),
            name=f"onvif-sub-{cam.name}",
        )
        logger.info("Tracking %s (Protect id=%s, %s:%d)",
                    cam.name, cam.protect_id, cam.host, port)

    # Cancel subscriptions for cameras that vanished from Protect.
    for stale_id in list(subscription_tasks.keys()):
        if stale_id in seen_ids:
            continue
        logger.info("Camera %s removed from Protect — stopping subscription",
                    subscriptions.get(stale_id, CameraSubscription(
                        protect_id="", name=stale_id, onvif_host="", onvif_port=0,
                        username="", password="",
                    )).name)
        subscription_tasks[stale_id].cancel()
        subscription_tasks.pop(stale_id, None)
        subscriptions.pop(stale_id, None)


async def _discovery_loop(cfg: dict, pusher: ProtectPusher) -> None:
    """Run reconciliation forever, with a steady cadence and clean
    cancellation."""
    while True:
        try:
            await _reconcile(cfg, pusher)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Reconciliation loop hiccup")
        await asyncio.sleep(DISCOVERY_INTERVAL_S)


# ── Main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    log_banner()
    cfg = load_config()

    unifi_cfg = cfg.get("unifi", {}) or {}
    host = unifi_cfg.get("host", "")
    api_key = unifi_cfg.get("api_key", "")
    alarms_cfg = cfg.get("alarms", {}) or {}
    webhook_template = alarms_cfg.get("webhook_id_template") or ""

    pusher = ProtectPusher(
        host=host, api_key=api_key,
        webhook_id_template=webhook_template,
    )
    await pusher.start()

    # Lightweight web UI — run alongside the discovery loop.
    web_task: asyncio.Task | None = None
    web_cfg = cfg.get("web_tool", {}) or {}
    if web_cfg.get("enabled", True):
        try:
            from onvif_bridge.web_tool import BridgeWebTool
            port = int(web_cfg.get("port", 8091))
            state_provider = lambda: {  # noqa: E731 — short enough
                "discovered_cameras": discovered_cameras,
                "subscriptions": subscriptions,
                "pusher_stats": pusher.stats,
                "last_discovery_error": last_discovery_error,
                "last_discovery_epoch": last_discovery_epoch,
            }
            web = BridgeWebTool(cfg, state_provider)
            logger.info("Starting bridge web UI on port %d", port)
            web_task = asyncio.create_task(web.run(port), name="web")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bridge web UI failed to start: %s", exc)

    # Discovery + reconciliation loop. The pusher logs in lazily on
    # the first event, so a Protect outage at startup just delays the
    # first push, doesn't crash the bridge.
    discovery_task = asyncio.create_task(
        _discovery_loop(cfg, pusher), name="discovery",
    )

    tasks: list[asyncio.Task] = [discovery_task]
    if web_task is not None:
        tasks.append(web_task)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for t in subscription_tasks.values():
            t.cancel()
        await pusher.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
