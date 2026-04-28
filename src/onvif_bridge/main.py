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

# Persisted snapshot of the last successful discovery. Loaded on startup
# so subscriptions resume immediately without re-querying Protect; saved
# after every reconciliation that produces a non-empty camera list.
STATE_PATH = Path("/config/state.yml")

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


def load_state() -> dict:
    """Load the persisted discovery snapshot, if any.

    Returns an empty dict when no state file exists or it can't be parsed —
    we'd rather start with an empty camera list than crash on bad YAML.
    """
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            state = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Could not read %s: %s — ignoring", STATE_PATH, e)
        return {}
    return state if isinstance(state, dict) else {}


def save_state(cameras: list[dict], epoch: float) -> None:
    """Write the latest discovery snapshot to /config/state.yml.

    Best-effort: log and continue on write failures so a read-only mount
    or full disk doesn't take the bridge down.
    """
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {"cameras": cameras, "last_discovery_epoch": epoch},
                f, default_flow_style=False, sort_keys=False,
            )
    except OSError as e:
        logger.warning("Could not save %s: %s", STATE_PATH, e)


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
is_discovering: bool = False


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

    if not host or not ((user and pwd) or api_key):
        last_discovery_error = "missing unifi host — need username+password or api_key in config"
        logger.warning("Discovery skipped: %s", last_discovery_error)
        return

    logger.info("Discovery starting — querying Protect at %s", host)
    try:
        cams = await discover_adopted_onvif_cameras(host, user, pwd, api_key=api_key)
    except UniFiAuthError as exc:
        last_discovery_error = f"Protect login failed: {exc}"
        last_discovery_epoch = time.time()
        logger.warning("Discovery failed — Protect login error: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001
        last_discovery_error = f"discovery error: {exc}"
        last_discovery_epoch = time.time()
        logger.warning("Discovery failed — %s", exc)
        return

    logger.info("Discovery found %d camera(s) in Protect", len(cams))

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
    new_count = 0
    skipped_count = 0
    locked_count = 0
    for cam in cams:
        if not cam.protect_id:
            continue
        seen_ids.add(cam.protect_id)
        if cam.protect_id in subscription_tasks:
            existing = subscriptions.get(cam.protect_id)
            if existing is not None and existing.auth_locked:
                # Auth failed previously — don't auto-restart; user must
                # update creds or press Retry to clear the lock.
                locked_count += 1
                continue
            if existing is not None and existing.onvif_host != cam.host:
                # IP changed — restart the subscription.
                logger.info("Camera %s IP changed (%s → %s) — resubscribing",
                            cam.name, existing.onvif_host, cam.host)
                subscription_tasks[cam.protect_id].cancel()
                subscription_tasks.pop(cam.protect_id, None)
                subscriptions.pop(cam.protect_id, None)
            else:
                logger.debug("Camera %s already tracked, no changes", cam.name)
                continue

        u, p, port = _onvif_creds_for(cam, cfg)
        if not (u and p):
            logger.info(
                "Skipping %s — no ONVIF credentials configured. Add "
                "`onvif: { username, password }` to config.yml or set "
                "per-camera `onvif_username` / `onvif_password`.",
                cam.name,
            )
            skipped_count += 1
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
        logger.info("Starting ONVIF subscription for %s (%s:%d, Protect id=%s)",
                    cam.name, cam.host, port, cam.protect_id)
        new_count += 1

    # Cancel subscriptions for cameras that vanished from Protect.
    removed_count = 0
    for stale_id in list(subscription_tasks.keys()):
        if stale_id in seen_ids:
            continue
        stale_name = subscriptions.get(stale_id, CameraSubscription(
            protect_id="", name=stale_id, onvif_host="", onvif_port=0,
            username="", password="",
        )).name
        logger.info("Camera %s no longer in Protect — stopping subscription", stale_name)
        subscription_tasks[stale_id].cancel()
        subscription_tasks.pop(stale_id, None)
        subscriptions.pop(stale_id, None)
        removed_count += 1

    logger.info(
        "Discovery complete: %d tracked, %d new, %d removed, %d skipped (no ONVIF creds)%s",
        len(subscriptions), new_count, removed_count, skipped_count,
        f", {locked_count} auth-locked (use Retry button)" if locked_count else "",
    )

    # Persist the camera list so the bridge can resume subscriptions on
    # the next start without re-querying Protect.
    if discovered_cameras:
        save_state(discovered_cameras, last_discovery_epoch)


def _restore_from_state(cfg: dict, pusher: ProtectPusher) -> int:
    """Re-create subscriptions from the persisted state file, if any.

    Returns the number of subscriptions started. Cameras whose ONVIF
    creds aren't yet configured are still listed (so the dashboard
    shows them) but no subscription is started until the user fills
    in the credentials.
    """
    global discovered_cameras, last_discovery_epoch

    state = load_state()
    cams_raw = state.get("cameras") if isinstance(state, dict) else None
    if not isinstance(cams_raw, list) or not cams_raw:
        return 0

    discovered_cameras = [c for c in cams_raw if isinstance(c, dict)]
    last_discovery_epoch = float(state.get("last_discovery_epoch") or 0)

    started = 0
    for cam_dict in discovered_cameras:
        protect_id = cam_dict.get("protect_id") or ""
        if not protect_id or protect_id in subscription_tasks:
            continue
        # Reuse the same per-camera ONVIF cred resolver as discovery.
        cam = DiscoveredCamera(
            protect_id=protect_id,
            name=cam_dict.get("name") or "<unnamed>",
            host=cam_dict.get("host") or "",
            mac=cam_dict.get("mac") or "",
            model_key=cam_dict.get("model_key") or "",
            type=cam_dict.get("type") or "",
            state=cam_dict.get("state") or "",
            is_adopted=bool(cam_dict.get("is_adopted", True)),
        )
        u, p, port = _onvif_creds_for(cam, cfg)
        if not (u and p and cam.host):
            logger.info(
                "Restored %s but skipping subscription — no creds or host",
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
        started += 1
        logger.info("Restored ONVIF subscription for %s (%s:%d)",
                    cam.name, cam.host, port)
    logger.info("Restored %d camera(s) from %s — press 'Get cameras from "
                "Protect' to refresh", started, STATE_PATH)
    return started


async def _discovery_loop(
    cfg: dict, pusher: ProtectPusher, trigger: asyncio.Event,
) -> None:
    """Wait for a trigger event, run reconciliation, repeat.

    Starts idle; only runs when trigger is set (via the "Get cameras from
    Protect" button or an explicit `trigger_discovery()` API call).
    """
    global is_discovering
    while True:
        await trigger.wait()
        trigger.clear()
        is_discovering = True
        try:
            await _reconcile(cfg, pusher)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Reconciliation loop hiccup")
        finally:
            is_discovering = False


# ── Main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    log_banner()
    cfg = load_config()

    unifi_cfg = cfg.get("unifi", {}) or {}
    host = unifi_cfg.get("host", "")
    api_key = unifi_cfg.get("api_key", "")
    alarms_cfg = cfg.get("alarms", {}) or {}
    webhook_template = alarms_cfg.get("webhook_id_template") or ""

    # Pre-populate the disabled-webhooks set from config so the pusher
    # respects the user's saved enable/disable choices from the moment
    # the first event arrives.
    disabled_webhooks = set(alarms_cfg.get("disabled_webhooks") or [])

    pusher = ProtectPusher(
        host=host, api_key=api_key,
        webhook_id_template=webhook_template,
        disabled_webhooks=disabled_webhooks,
    )
    await pusher.start()

    # Restore the previously-discovered camera list (if any) and re-start
    # subscriptions for them. Lets the bridge resume immediately on
    # restart without forcing the user to press "Get cameras from Protect".
    _restore_from_state(cfg, pusher)

    # Event-driven discovery: only fires when the user presses
    # "Get cameras from Protect". Not pre-set — bridge starts idle.
    discover_event = asyncio.Event()

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
                "subscription_tasks": subscription_tasks,
                "pusher_stats": pusher.stats,
                "last_discovery_error": last_discovery_error,
                "last_discovery_epoch": last_discovery_epoch,
                "is_discovering": is_discovering,
                "webhook_id_template": pusher.webhook_id_template,
            }
            web = BridgeWebTool(cfg, state_provider,
                                trigger_discovery=discover_event.set,
                                pusher=pusher)
            logger.info("Starting bridge web UI on port %d", port)
            web_task = asyncio.create_task(web.run(port), name="web")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bridge web UI failed to start: %s", exc)

    discovery_task = asyncio.create_task(
        _discovery_loop(cfg, pusher, discover_event), name="discovery",
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
