from __future__ import annotations

import asyncio
import logging
import sys
from typing import Dict

import yaml

from auto_config import detect_local_ip, generate_mac
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("camproxy")


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

    if not (username and password and host):
        logger.error(
            "No adoption token and no credentials in config.yml. "
            "Either set unifi.token manually or provide "
            "unifi.username + unifi.password to auto-fetch."
        )
        return None

    logger.info("Fetching adoption token from %s as %s", host, username)
    try:
        async with UniFiProtectClient(host, username, password) as client:
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

    if not (username and password and host):
        logger.info("No credentials provided — skipping auto-adopt step")
        return

    # Give cameras a head-start so they've announced themselves
    await asyncio.sleep(15)

    try:
        async with UniFiProtectClient(host, username, password) as client:
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


async def run_camera(cam_cfg: dict, global_cfg: dict, token: str):
    """Spawn one camera worker — each becomes a spoofed UniFi camera in Protect.

    This function will retry indefinitely on failure with exponential backoff,
    so a single bad camera never crashes the entire container.
    """
    cam_name = cam_cfg.get("name", "<unnamed>")
    retry_delay = 5

    while True:
        try:
            await _run_camera_once(cam_cfg, global_cfg, token)
            # If _run_camera_once returns cleanly, the camera session ended
            # normally (e.g. shutdown signal) — don't retry.
            break
        except asyncio.CancelledError:
            logger.info("Camera %s task cancelled", cam_name)
            raise
        except Exception as exc:
            camera_errors[cam_name] = str(exc)
            logger.error(
                "Camera %s crashed: %s — retrying in %ds",
                cam_name, exc, retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _MAX_CAMERA_RETRY_DELAY)


async def _run_camera_once(cam_cfg: dict, global_cfg: dict, token: str):
    """Single attempt to run a camera. Exceptions propagate to run_camera()."""
    cert_path = ensure_cert("/config/client.pem")
    _token = token  # bind into the Args class body below

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
        rtsp_transport = "tcp"

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
    config_path = "/config/config.yml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    cfg = load_config(config_path)
    cameras = cfg.get("cameras", [])

    tasks = []

    # 1. Start web UI first — it must be reachable even with zero cameras
    #    so users can add cameras via the Setup tab on first install.
    web_cfg = cfg.get("web_tool", {}) or {}
    if web_cfg.get("enabled", True):
        port = int(web_cfg.get("port", 8091))
        tool = LineTool(camera_registry, cfg, config_path=config_path,
                        error_registry=camera_errors)
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
