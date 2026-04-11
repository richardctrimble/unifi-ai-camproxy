"""
UniFi Protect requires a client certificate on the WebSocket connection.
This generates a self-signed cert if one doesn't already exist.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("cert_gen")


def ensure_cert(path: str = "/config/client.pem") -> str:
    cert = Path(path)
    if cert.exists():
        logger.info(f"Using existing cert: {path}")
        return path

    cert.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Generating self-signed cert at {path}")

    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", path,
        "-out", path,
        "-days", "3650",
        "-nodes",
        "-subj", "/CN=unifi-ai-port",
    ], check=True, capture_output=True)

    logger.info("Certificate generated OK")
    return path
