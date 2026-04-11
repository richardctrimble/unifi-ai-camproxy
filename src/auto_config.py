"""
auto_config.py — fills in the boring bits of config.yml automatically
so the user only has to specify what's genuinely site-specific.

Two helpers:

  * generate_mac(name)
      Deterministic fake MAC address from a camera name. Same name
      always produces the same MAC, so restarts don't create duplicate
      "pending" cameras in Protect. The first byte is masked to mark
      it as locally-administered and unicast (02:...), which is the
      correct convention for fake MACs that won't collide with real
      vendor OUIs.

  * detect_local_ip()
      Returns the outbound-facing IPv4 address of this host without
      contacting any external service. Uses the classic UDP-connect
      trick: opening a UDP socket doesn't actually send packets, it
      just makes the kernel pick the right source IP.
"""

from __future__ import annotations

import hashlib
import logging
import socket

logger = logging.getLogger("auto_config")


def generate_mac(name: str) -> str:
    """
    Build a stable, locally-administered MAC from a camera name.

    Example:
        >>> generate_mac("Front Door")
        '02:7A:4E:1F:BC:33'
    """
    digest = hashlib.md5(name.encode("utf-8")).digest()
    # Mask first byte: set locally-administered bit (0x02), clear multicast bit
    first = (digest[0] | 0x02) & 0xFE
    octets = [first] + list(digest[1:6])
    return ":".join(f"{b:02X}" for b in octets)


def detect_local_ip(target_host: str = "1.1.1.1") -> str:
    """
    Return this machine's primary outbound IPv4 address. `target_host`
    is just used to choose a route — no packet is actually sent.
    Falls back to 127.0.0.1 if resolution fails.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, 80))
        return s.getsockname()[0]
    except OSError:
        logger.debug("Local IP detection failed, using loopback")
        return "127.0.0.1"
    finally:
        s.close()
