"""
protect_discovery — pull ONVIF-adopted cameras out of Protect.

Reuses the existing UniFiProtectClient.list_cameras() so we don't grow a
second auth implementation. The only new logic here is filtering the
returned list to ONVIF entries and projecting the fields the bridge
actually needs.

Status: SKELETON — list_cameras() integration is wired, but the exact
field on Protect's API that distinguishes ONVIF cameras from native UVC
cameras still needs to be confirmed against a live Protect 7.x. The
candidate fields are documented in identify_onvif_camera() below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

# unifi_auth lives at /app/src/unifi_auth.py (shared with the legacy
# spoof entrypoint). The Dockerfile.onvif copies it alongside the
# bridge package so this import works without extra path tricks.
import sys
sys.path.insert(0, "/app/src")
from unifi_auth import UniFiProtectClient, UniFiAuthError  # noqa: E402

logger = logging.getLogger("onvif_bridge.discovery")


@dataclass
class DiscoveredCamera:
    """A Protect-adopted camera that looks like an ONVIF source."""
    protect_id: str          # use this when POSTing bookmarks / events
    name: str
    host: str                # camera's LAN IP, from Protect's "host" field
    mac: str
    model_key: str           # Protect's modelKey, e.g. "camera"
    type: str                # Protect's type / displayName
    state: str               # CONNECTED / DISCONNECTED / etc.
    is_adopted: bool

    def onvif_endpoint(self, port: Optional[int] = None) -> str:
        """Build the standard ONVIF device-service URL for this camera.

        Most cameras serve ONVIF on port 80 (Hikvision, Dahua, Amcrest,
        many generic ONVIF devices). Reolink uses 8000 or 2020. If port
        is None we'll start with 80 — onvif_subscriber retries other
        common ports on connection refusal.
        """
        port = port or 80
        return f"http://{self.host}:{port}/onvif/device_service"


def identify_onvif_camera(cam: dict) -> bool:
    """Return True if a Protect /api/cameras entry looks like an ONVIF
    third-party camera rather than a native UVC device.

    OPEN QUESTION (verify against live Protect): the most likely fields
    that flag an ONVIF adoption are:

      cam.get("modelKey")  — "camera" for native, possibly something
                             else for third-party
      cam.get("type")      — usually contains "ONVIF" or the third-party
                             vendor's name for non-UVC adoptions
      cam.get("isThirdPartyCamera")  — speculative, may exist on 7.x
      cam.get("featureFlags")        — often differs significantly

    Until we've confirmed the exact field, the heuristic below errs on
    the side of including a camera (return True) rather than excluding
    it. The discovery UI will let the user untick anything that's a
    false positive.
    """
    if not isinstance(cam, dict):
        return False

    # Native UVC cameras have a "UVC ..." type string. If we see that,
    # it's almost certainly a Ubiquiti camera and not ONVIF.
    type_str = (cam.get("type") or cam.get("displayName") or "").lower()
    if type_str.startswith("uvc "):
        return False

    # Hard signal — explicit flag if present
    if cam.get("isThirdPartyCamera"):
        return True

    # Heuristic — generic / ONVIF type strings
    if any(k in type_str for k in ("onvif", "third party", "thirdparty", "rtsp")):
        return True

    # Fallback — if the camera has a host/IP and isn't UVC, treat it as
    # a candidate. The user can deselect false positives.
    return bool(cam.get("host"))


async def discover_adopted_onvif_cameras(
    host: str,
    username: str,
    password: str,
    api_key: str = "",
) -> list[DiscoveredCamera]:
    """List ONVIF-adopted cameras from Protect. Raises UniFiAuthError
    on credential failure; returns empty list on a transport error so
    the bridge can keep running and retry later."""
    try:
        async with UniFiProtectClient(host, username, password, api_key=api_key) as client:
            raw = await client.list_cameras()
    except UniFiAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list cameras from Protect: %s", exc)
        return []

    out: list[DiscoveredCamera] = []
    for cam in raw:
        if not isinstance(cam, dict):
            continue
        if not cam.get("isAdopted"):
            continue
        if not identify_onvif_camera(cam):
            continue
        out.append(DiscoveredCamera(
            protect_id=cam.get("id") or "",
            name=cam.get("name") or "<unnamed>",
            host=cam.get("host") or "",
            mac=cam.get("mac") or "",
            model_key=cam.get("modelKey") or "",
            type=cam.get("type") or cam.get("displayName") or "",
            state=cam.get("state") or "",
            is_adopted=True,
        ))
    return out
