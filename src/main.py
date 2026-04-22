from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import yaml

from ai_engine import probe_available_devices
from auto_config import detect_local_ip, generate_mac
from build_info import get_build_info
from cert_gen import ensure_cert
from unifi_auth import UniFiAuthError, UniFiProtectClient
from unifi_client import AIPortCamera
from web_tool import LineTool
from unifi.core import Core

# Shared registry of live camera objects, keyed by camera name. Populated
# by run_camera() as each AIPortCamera is constructed, consumed by the
# web tool so it can pull latest frames from each camera's AIEngine.
camera_registry: Dict[str, AIPortCamera] = {}

# Shared error registry for cameras that failed to start, keyed by camera
# name. The web tool reads this to display errors on the status page.
camera_errors: Dict[str, str] = {}

# Per-camera reconnect counters. Incremented every time run_camera()
# catches an exception and retries. Lets the Status tab show which
# cameras are flapping without having to scrape the logs.
camera_reconnects: Dict[str, int] = {}

# Wall-clock time of the last heartbeat_logger tick. When this stops
# advancing the main event loop has wedged — the Status tab uses it as
# a liveness indicator.
_last_heartbeat_epoch: float = 0.0


def get_heartbeat_state() -> dict:
    """Snapshot of the heartbeat timestamp for the web UI."""
    return {"last_epoch": _last_heartbeat_epoch or None}

def _configure_logging() -> None:
    """Configure root logging.

    Environment variables:
      LOG_LEVEL  — INFO (default), DEBUG, WARNING, ERROR
      LOG_FILE   — path to a rotating log file. Defaults to
                   /config/camproxy.log so the web UI's Logs tab
                   always has content to show. Set to an empty
                   string to disable file logging (stdout only).
                   5 MB rotation, 3 backups — never more than
                   ~20 MB on disk.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = os.environ.get("LOG_FILE", "/config/camproxy.log")
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
                )
            )
        except OSError as exc:
            # Fall back to stdout-only — don't let logging bring the app down.
            print(f"WARNING: Could not open LOG_FILE={log_file}: {exc}", file=sys.stderr)

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)

    # ultralytics is chatty at INFO — tone it down a notch so our own
    # log lines stay readable. DEBUG still shows everything.
    if level > logging.DEBUG:
        logging.getLogger("ultralytics").setLevel(logging.WARNING)

    # aiohttp's per-request access log ("GET /api/status 200 ...") fires
    # on every Status-tab poll (every 3 s), which floods the logs and
    # makes real events hard to spot. Silence it unless the user asked
    # for DEBUG explicitly.
    if level > logging.DEBUG:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("camproxy")


def log_startup_banner() -> None:
    """Emit a banner describing the runtime environment.

    Makes troubleshooting user reports a lot easier — a single line in
    the log tells us the platform, Python version, and which inference
    backends the image can actually reach on this host.
    """
    from importlib import metadata

    try:
        version = metadata.version("ultralytics")
    except metadata.PackageNotFoundError:
        version = "unknown"

    build = get_build_info()

    logger.info("─── unifi-ai-camproxy starting ──────────────────────────────")
    logger.info("Build: %s (ref: %s) @ %s",
                build["git_sha_short"], build["git_ref"], build["build_time"])
    logger.info("Platform: %s %s / Python %s",
                platform.system(), platform.machine(), platform.python_version())
    logger.info("Ultralytics: %s", version)

    probe = probe_available_devices()
    available = [name for name, info in probe.items() if info["available"]]
    unavailable = [name for name, info in probe.items() if not info["available"]]
    logger.info("Available inference devices: %s",
                ", ".join(available) if available else "none")
    # Surface why the accelerators a user most likely wants are unreachable —
    # this turns a silent "fell back to CPU" into an actionable log line.
    for key in ("cuda", "intel:gpu", "intel:npu"):
        info = probe.get(key)
        if info and not info["available"]:
            logger.info("  %s unavailable: %s", key, info["detail"])
    if unavailable:
        logger.debug("Unreachable devices: %s", ", ".join(unavailable))
    logger.info("─────────────────────────────────────────────────────────────")


def load_config(path: str = "/config/config.yml") -> dict:
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("Config file not found: %s", path)
        return {}
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in config file %s: %s", path, e)
        return {}
    except OSError as e:
        logger.error("Could not read config file %s: %s", path, e)
        return {}
    if not isinstance(cfg, dict):
        logger.error("Config file %s does not contain a YAML mapping", path)
        return {}
    return cfg


# ─── Auto-adoption orchestration ────────────────────────────────────────────


async def ensure_adoption_token(cfg: dict) -> str | None:
    """
    Return a valid adoption token. Preference order:
      1. explicit `unifi.token` in config.yml (manual override)
      2. fetched automatically via `unifi.username` + `unifi.password`

    Returns None if the token cannot be obtained — callers must handle
    gracefully instead of crashing the container.
    """
    unifi_cfg = cfg.get("unifi", {})
    token = unifi_cfg.get("token")
    if token and token != "PASTE_TOKEN_HERE":
        logger.info("Using adoption token from config.yml")
        return token

    username = unifi_cfg.get("username")
    password = unifi_cfg.get("password")
    host = unifi_cfg.get("host")
    api_key = unifi_cfg.get("api_key")

    if not host:
        logger.error("No adoption token and no unifi.host in config.yml.")
        return None
    if not api_key and not (username and password):
        logger.error(
            "No adoption token and no credentials in config.yml. "
            "Either set unifi.token manually, provide unifi.api_key, "
            "or provide unifi.username + unifi.password to auto-fetch."
        )
        return None

    auth_label = "API key" if api_key else f"user {username}"
    logger.info("Fetching adoption token from %s (%s)", host, auth_label)
    try:
        async with UniFiProtectClient(
            host, username or "", password or "", api_key=api_key or "",
        ) as client:
            return await client.fetch_adoption_token()
    except UniFiAuthError as e:
        logger.error("Auto-adoption failed: %s", e)
        return None


def fill_camera_defaults(cam_cfg: dict, local_ip: str) -> dict:
    """
    Populate optional fields so the user only has to write rtsp_url + name.
    Returns the same dict, mutated.
    """
    name = cam_cfg.get("name") or "camera"
    if not cam_cfg.get("mac"):
        cam_cfg["mac"] = generate_mac(name)
        logger.info("Generated fake MAC for %s: %s", name, cam_cfg["mac"])
    if not cam_cfg.get("ip"):
        cam_cfg["ip"] = local_ip
    return cam_cfg


async def heartbeat_logger() -> None:
    """Log a compact summary of every running camera on a fixed cadence.

    Easy to grep out of `docker logs` for ops purposes and makes silent
    failures obvious — if the heartbeat keeps reporting 0 new frames
    for a camera, something is wrong even if no exception has been raised.
    """
    global _last_heartbeat_epoch
    last_counts: dict[str, tuple[int, int]] = {}
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        _last_heartbeat_epoch = time.time()
        if not camera_registry:
            continue
        parts = []
        for name, camera in camera_registry.items():
            eng = getattr(camera, "ai_engine", None)
            if eng is None:
                continue
            cap = getattr(eng, "_frames_captured", 0)
            ana = getattr(eng, "_frames_analysed", 0)
            prev_cap, prev_ana = last_counts.get(name, (0, 0))
            delta_cap = cap - prev_cap
            delta_ana = ana - prev_ana
            last_counts[name] = (cap, ana)
            conn = "up" if getattr(eng, "_stream_connected", False) else "DOWN"
            parts.append(
                f"{name}[{conn}] +{delta_cap}f/+{delta_ana}a "
                f"({getattr(eng, 'device', '?')})"
            )
        if parts:
            logger.info("Heartbeat — %s", " | ".join(parts))


async def auto_adopt_pending(cfg: dict, camera_specs: list) -> None:
    """
    After cameras have started connecting, walk the Protect API
    and accept any that are sat in pending-adoption for us.

    Runs in the background — failures are logged, not fatal. It's a
    convenience layer: the user *could* click "adopt" in the UI instead.
    """
    unifi_cfg = cfg.get("unifi", {})
    username = unifi_cfg.get("username")
    password = unifi_cfg.get("password")
    host = unifi_cfg.get("host")
    api_key = unifi_cfg.get("api_key")

    if not host or not (api_key or (username and password)):
        logger.info("No credentials provided — skipping auto-adopt step")
        return

    # Give cameras a head-start so they've announced themselves
    await asyncio.sleep(15)

    try:
        async with UniFiProtectClient(
            host, username or "", password or "", api_key=api_key or "",
        ) as client:
            for spec in camera_specs:
                ok = await client.approve_pending(spec["mac"], spec["name"])
                if not ok:
                    logger.info(
                        "Camera %s not auto-adopted — accept manually in "
                        "Protect UI if needed",
                        spec["name"],
                    )
    except UniFiAuthError as e:
        logger.warning("Auto-adopt step skipped: %s", e)


# ─── Per-camera worker ──────────────────────────────────────────────────────

# Maximum time between retries for a failing camera (seconds).
_MAX_CAMERA_RETRY_DELAY = 60

# How long to sleep before exiting after an unhandled exception, to avoid
# the orchestrator's restart policy creating a rapid crash loop.
_CRASH_BACKOFF_DELAY = 10

# How long the container idles when there's no web UI and no token — gives
# the user time to fix config before the orchestrator eventually restarts.
_IDLE_SLEEP_SECONDS = 3600

# Cadence of the heartbeat log. Purely informational — lets long-running
# containers show up in monitoring dashboards and makes "is anything
# happening?" trivially answerable from `docker logs`.
_HEARTBEAT_INTERVAL_SECONDS = 300


def _validate_camera_cfg(cam_cfg: dict) -> str | None:
    """Validate a camera config dict. Returns an error message or None if OK."""
    if not isinstance(cam_cfg, dict):
        return "camera config is not a dict"
    name = cam_cfg.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return "missing or empty 'name'"
    rtsp_url = cam_cfg.get("rtsp_url")
    if not rtsp_url or not isinstance(rtsp_url, str) or not rtsp_url.strip():
        return f"camera '{name}': missing or empty 'rtsp_url'"
    return None


# Cooldown state for credential-level failures (bad password → 401/403,
# or too many login attempts → 429). When Protect rejects us we MUST NOT
# keep hammering /api/auth/login every few seconds — that just trips the
# attempt limit and keeps us locked out forever. Instead we freeze refreshes
# for a few minutes and surface a loud error so the user knows to fix creds.
_auth_lockout_until: float = 0.0
_auth_lockout_reason: str = ""

# Adoption-token refresh stats. Exposed to the Status tab via the getters
# below so the user can see token rotation working (or not).
_token_refresh_ok_count: int = 0
_token_refresh_last_ok_epoch: float = 0.0
_token_refresh_last_err_epoch: float = 0.0
_token_refresh_last_err_status: Optional[int] = None
_token_refresh_last_err_msg: str = ""
_token_refresh_last_value: str = ""

# Guard flag: set to True once we've logged the "skipping refresh, no creds"
# warning, so the main loop doesn't spam it every retry. Cleared the next
# time creds are present so a later credential change re-logs.
_token_refresh_nocreds_warned: bool = False


def _mask_token(tok: str) -> str:
    """Show only the first 6 + last 4 chars of a token; never full secret."""
    if not tok:
        return ""
    if len(tok) <= 12:
        return tok[:2] + "…"
    return f"{tok[:6]}…{tok[-4:]}"


def get_adoption_state() -> dict:
    """Snapshot of adoption-token refresh stats (for web UI)."""
    return {
        "refresh_ok_count": _token_refresh_ok_count,
        "last_ok_epoch": _token_refresh_last_ok_epoch or None,
        "last_err_epoch": _token_refresh_last_err_epoch or None,
        "last_err_status": _token_refresh_last_err_status,
        "last_err_msg": _token_refresh_last_err_msg or None,
        "current_token_masked": _mask_token(_token_refresh_last_value),
    }


def get_auth_lockout_state() -> dict:
    """Snapshot of the auth cooldown (for web UI)."""
    remaining = _auth_lockout_remaining()
    return {
        "active": remaining > 0,
        "remaining_seconds": int(remaining),
        "reason": _auth_lockout_reason if remaining > 0 else "",
    }

# How long to back off after each kind of auth failure. 429s are Protect
# explicitly telling us "wait"; 401/403 just means the creds are wrong
# and no amount of retrying will help until the user intervenes.
_AUTH_LOCKOUT_RATE_LIMIT = 15 * 60   # 15 min after a 429
_AUTH_LOCKOUT_BAD_CREDS  = 10 * 60   # 10 min after a 401/403


def _set_auth_lockout(seconds: float, reason: str) -> None:
    """Arm the auth cooldown and record a short reason for the web UI."""
    global _auth_lockout_until, _auth_lockout_reason
    _auth_lockout_until = asyncio.get_event_loop().time() + seconds
    _auth_lockout_reason = reason


def _auth_lockout_remaining() -> float:
    """Seconds left on the cooldown (0 if not in cooldown)."""
    remaining = _auth_lockout_until - asyncio.get_event_loop().time()
    return max(0.0, remaining)


async def _refresh_adoption_token(global_cfg: dict) -> Optional[str]:
    """Force-fetch a fresh adoption token from Protect, bypassing any token
    saved in config.yml.

    Protect 7.x consumes the adoption token on first adoption — every
    reconnect after that is rejected unless we present a fresh token.
    Upstream unifi-cam-proxy doesn't rotate the token (it builds the WS
    URI once at Core.__init__ and reuses it forever), so we refresh
    here before each retry in run_camera().

    Returns None when we don't have credentials to fetch a new one (only
    a manual token in config.yml) — in that case the caller keeps the
    existing token and the camera will loop until the user adds an API
    key or username/password in the UniFi tab.

    Also returns None (without touching Protect) while an auth lockout
    is active — avoids compounding 401/403/429 failures into a hard
    rate-limit ban.
    """
    remaining = _auth_lockout_remaining()
    if remaining > 0:
        logger.debug(
            "Skipping token refresh — auth lockout active (%.0fs left, %s)",
            remaining, _auth_lockout_reason,
        )
        return None

    unifi_cfg = global_cfg.get("unifi", {}) or {}
    host = unifi_cfg.get("host")
    api_key = unifi_cfg.get("api_key")
    username = unifi_cfg.get("username")
    password = unifi_cfg.get("password")

    global _token_refresh_nocreds_warned
    if not host or not (api_key or (username and password)):
        if not _token_refresh_nocreds_warned:
            reason = ("no unifi.host" if not host
                      else "no api_key and no username+password")
            logger.warning(
                "Token refresh skipped (%s). Cameras will keep using the "
                "stored adoption token, which Protect 7.x consumes on first "
                "use — every reconnect will then fail. Add credentials in "
                "the UniFi tab to enable rotation.",
                reason,
            )
            _token_refresh_nocreds_warned = True
        return None
    # Creds are back — allow a future absence to log again.
    _token_refresh_nocreds_warned = False

    global _token_refresh_ok_count, _token_refresh_last_ok_epoch
    global _token_refresh_last_value
    global _token_refresh_last_err_epoch, _token_refresh_last_err_status
    global _token_refresh_last_err_msg

    try:
        async with UniFiProtectClient(
            host, username or "", password or "", api_key=api_key or "",
        ) as client:
            token = await client.fetch_adoption_token()
    except UniFiAuthError as exc:
        status = getattr(exc, "status", None)
        _token_refresh_last_err_epoch = time.time()
        _token_refresh_last_err_status = status
        _token_refresh_last_err_msg = str(exc)
        if status == 429:
            _set_auth_lockout(
                _AUTH_LOCKOUT_RATE_LIMIT,
                "Protect rate-limited our login (429)",
            )
            logger.error(
                "UniFi login rate-limited (429). Pausing refresh for %d min. "
                "Your password may still be wrong — once the lockout clears, "
                "fix creds in the UniFi tab before restarting.",
                _AUTH_LOCKOUT_RATE_LIMIT // 60,
            )
        elif status in (401, 403):
            _set_auth_lockout(
                _AUTH_LOCKOUT_BAD_CREDS,
                f"UniFi rejected creds ({status})",
            )
            logger.error(
                "═══ UNIFI CREDENTIALS REJECTED (HTTP %s) ═══ "
                "Open the web UI → UniFi tab, click 'Test login' to verify "
                "your username / password, then Save. Token refresh paused "
                "for %d min to avoid a rate-limit lockout.",
                status, _AUTH_LOCKOUT_BAD_CREDS // 60,
            )
        else:
            logger.warning("Token refresh failed: %s", exc)
        return None

    _token_refresh_ok_count += 1
    _token_refresh_last_ok_epoch = time.time()
    _token_refresh_last_value = token
    return token


async def run_camera(cam_cfg: dict, global_cfg: dict, token: str):
    """Spawn one camera worker — each becomes a spoofed UniFi camera in Protect.

    This function will retry indefinitely on failure with exponential backoff,
    so a single bad camera never crashes the entire container.

    Between retries we refresh the adoption token from Protect (Protect 7.x
    one-shot tokens) — see _refresh_adoption_token for rationale.
    """
    cam_name = cam_cfg.get("name", "<unnamed>")
    retry_delay = 5
    current_token = token

    while True:
        try:
            await _run_camera_once(cam_cfg, global_cfg, current_token)
            # If _run_camera_once returns cleanly, the camera session ended
            # normally (e.g. shutdown signal) — don't retry.
            break
        except asyncio.CancelledError:
            logger.info("Camera %s task cancelled", cam_name)
            raise
        except Exception as exc:
            camera_errors[cam_name] = str(exc)
            camera_reconnects[cam_name] = camera_reconnects.get(cam_name, 0) + 1
            logger.error(
                "Camera %s crashed: %s — retrying in %ds",
                cam_name, exc, retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _MAX_CAMERA_RETRY_DELAY)

            fresh = await _refresh_adoption_token(global_cfg)
            if fresh and fresh != current_token:
                logger.info("Rotated adoption token for %s", cam_name)
                current_token = fresh


async def _run_camera_once(cam_cfg: dict, global_cfg: dict, token: str):
    """Single attempt to run a camera. Exceptions propagate to run_camera()."""
    cert_path = ensure_cert("/config/client.pem")
    _token = token  # bind into the Args class body below

    _rtsp_transport = (cam_cfg.get("rtsp_transport") or "tcp").lower()
    if _rtsp_transport not in ("tcp", "udp"):
        logger.warning(
            "Unknown rtsp_transport '%s' for camera %s — defaulting to tcp",
            _rtsp_transport, cam_cfg.get("name"),
        )
        _rtsp_transport = "tcp"

    class Args:
        host = global_cfg["unifi"]["host"]
        token = _token
        mac = cam_cfg["mac"]
        ip = cam_cfg["ip"]
        name = cam_cfg["name"]
        model = cam_cfg.get("model", "UVC G4 Pro")
        fw_version = cam_cfg.get("fw_version", "4.69.55")
        cert = cert_path
        ffmpeg_args = "-c:v copy -ar 32000 -ac 1 -codec:a aac -b:a 32k"
        rtsp_transport = _rtsp_transport

    args = Args()

    camera = AIPortCamera(
        args=args,
        logger=logging.getLogger(f"cam.{cam_cfg['name']}"),
        rtsp_url=cam_cfg["rtsp_url"],
        snapshot_url=cam_cfg.get("snapshot_url"),
        ai_config=cam_cfg.get("ai", {}),
    )

    # Register before core.run() so the web tool can reach the AIEngine
    # as soon as the capture loop has its first frame.
    camera_registry[cam_cfg["name"]] = camera
    # Clear any previous error for this camera on successful start
    camera_errors.pop(cam_cfg["name"], None)

    core = Core(args, camera, logging.getLogger(f"core.{cam_cfg['name']}"))
    logger.info("Starting camera: %s (%s)", cam_cfg["name"], cam_cfg["mac"])
    await core.run()


# ─── Entry point ────────────────────────────────────────────────────────────


async def main():
    log_startup_banner()

    config_path = "/config/config.yml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    cfg = load_config(config_path)
    cameras = cfg.get("cameras", [])

    tasks = []

    # Heartbeat task always runs — it's the liveness indicator shown on
    # the Status tab, and it's cheap (a 5-minute sleep loop).
    tasks.append(asyncio.create_task(heartbeat_logger()))

    # 1. Start web UI first — it must be reachable even with zero cameras
    #    so users can add cameras via the Setup tab on first install.
    web_cfg = cfg.get("web_tool", {}) or {}
    if web_cfg.get("enabled", True):
        port = int(web_cfg.get("port", 8091))
        tool = LineTool(camera_registry, cfg, config_path=config_path,
                        error_registry=camera_errors,
                        reconnect_registry=camera_reconnects,
                        adoption_probe=get_adoption_state,
                        lockout_probe=get_auth_lockout_state,
                        heartbeat_probe=get_heartbeat_state)
        logger.info("Starting web UI on port %d", port)
        tasks.append(asyncio.create_task(tool.run(port)))

    if not cameras:
        if not tasks:
            logger.error(
                "No cameras configured and web UI is disabled — nothing to run. "
                "Enable the web UI or add cameras to config.yml."
            )
            sys.exit(1)
        logger.info(
            "No cameras configured — running in web-only mode. "
            "Open the web UI to add cameras, then restart the container."
        )
        await asyncio.gather(*tasks, return_exceptions=True)
        return

    # 2. Validate camera configs and filter out invalid entries
    valid_cameras = []
    for cam in cameras:
        if cam.get("disabled"):
            logger.info("Skipping disabled camera: %s", cam.get("name", "<unnamed>"))
            continue
        err = _validate_camera_cfg(cam)
        if err:
            cam_name = cam.get("name", "<unnamed>")
            logger.error("Invalid camera config (%s) — skipping: %s", cam_name, err)
            camera_errors[cam_name] = f"Config error: {err}"
            continue
        valid_cameras.append(cam)

    if not valid_cameras and not tasks:
        logger.error(
            "All camera configs are invalid and web UI is disabled — nothing to run."
        )
        sys.exit(1)

    if not valid_cameras:
        logger.warning(
            "All camera configs are invalid — running in web-only mode. "
            "Fix camera configurations via the web UI."
        )
        await asyncio.gather(*tasks, return_exceptions=True)
        return

    # 3. Adoption token (auto or manual) — non-fatal on failure
    token = await ensure_adoption_token(cfg)
    if not token:
        logger.warning(
            "Could not obtain adoption token — cameras will NOT start. "
            "Fix credentials in config.yml and restart, or set unifi.token manually. "
            "Web UI remains available for configuration."
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            return
        # No web UI and no token — nothing useful to do, but don't crash loop
        logger.error("No web UI and no token — container will idle. Fix config and restart.")
        await asyncio.sleep(_IDLE_SLEEP_SECONDS)
        return

    # 4. Fill in optional per-camera defaults
    local_ip = detect_local_ip()
    logger.info("Detected local IP: %s", local_ip)
    for cam in valid_cameras:
        fill_camera_defaults(cam, local_ip)

    # 5. Start all camera workers + the background auto-adopt task
    tasks.extend(
        asyncio.create_task(run_camera(cam, cfg, token))
        for cam in valid_cameras
    )
    tasks.append(asyncio.create_task(auto_adopt_pending(cfg, valid_cameras)))

    # Use return_exceptions=True so one task failure doesn't kill the others.
    # Individual camera tasks already have their own retry logic.
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception as exc:
        logger.critical("Unhandled exception in main: %s", exc, exc_info=True)
        # Sleep before exiting to avoid rapid crash loops when the
        # orchestrator's restart policy kicks in immediately.
        import time
        time.sleep(_CRASH_BACKOFF_DELAY)
        sys.exit(1)
