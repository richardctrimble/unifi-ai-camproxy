import asyncio
import logging
import sys
from pathlib import Path

import yaml

from cert_gen import ensure_cert
from unifi_client import AIPortCamera
from unifi.core import Core

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ai-port")


def load_config(path: str = "/config/config.yml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def run_camera(cam_cfg: dict, global_cfg: dict):
    """Spawn one camera worker — each becomes a spoofed UniFi camera in Protect."""
    cert_path = ensure_cert("/config/client.pem")

    class Args:
        host = global_cfg["unifi"]["host"]
        token = global_cfg["unifi"]["token"]
        mac = cam_cfg["mac"]
        ip = cam_cfg.get("ip", "192.168.1.100")
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

    core = Core(args, camera, logging.getLogger(f"core.{cam_cfg['name']}"))
    logger.info(f"Starting camera: {cam_cfg['name']} ({cam_cfg['mac']})")
    await core.run()


async def main():
    config_path = "/config/config.yml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    cfg = load_config(config_path)
    cameras = cfg.get("cameras", [])

    if not cameras:
        logger.error("No cameras defined in config.yml")
        sys.exit(1)

    tasks = [asyncio.create_task(run_camera(cam, cfg)) for cam in cameras]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
