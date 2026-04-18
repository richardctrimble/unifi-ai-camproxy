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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("camproxy")


def load_config(path: str = "/config/config.yml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Auto-adoption orchestration ────────────────────────────────────────────


async def ensure_adoption_token(cfg: dict) -> str:
    """
    Return a valid adoption token. Preference order:
      1. explicit `unifi.token` in config.yml (manual override)
      2. fetched automatically via `unifi.username` + `unifi.password`
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
        raise SystemExit(
            "No adoption token and no credentials in config.yml. "
            "Either set unifi.token manually or provide "
            "unifi.username + unifi.password to auto-fetch."
        )

    logger.info("Fetching adoption token from %s as %s", host, username)
    try:
        async with UniFiProtectClient(host, username, password) as client:
            return await client.fetch_adoption_token()
    except UniFiAuthError as e:
        raise SystemExit(f"Auto-adoption failed: {e}")


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


async def run_camera(cam_cfg: dict, global_cfg: dict, token: str):
    """Spawn one camera worker — each becomes a spoofed UniFi camera in Protect."""
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
        tool = LineTool(camera_registry, cfg, config_path=config_path)
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
        await asyncio.gather(*tasks, return_exceptions=False)
        return

    # 2. Adoption token (auto or manual)
    token = await ensure_adoption_token(cfg)

    # 3. Fill in optional per-camera defaults
    local_ip = detect_local_ip()
    logger.info("Detected local IP: %s", local_ip)
    for cam in cameras:
        if cam.get("disabled"):
            logger.info("Skipping disabled camera: %s", cam.get("name", "<unnamed>"))
            continue
        fill_camera_defaults(cam, local_ip)

    # 4. Start all camera workers + the background auto-adopt task
    tasks.extend(
        asyncio.create_task(run_camera(cam, cfg, token))
        for cam in cameras
        if not cam.get("disabled")
    )
    tasks.append(asyncio.create_task(auto_adopt_pending(cfg, [c for c in cameras if not c.get("disabled")])))

    await asyncio.gather(*tasks, return_exceptions=False)


if __name__ == "__main__":
    asyncio.run(main())
