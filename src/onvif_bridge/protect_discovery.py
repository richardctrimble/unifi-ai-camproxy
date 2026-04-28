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
    """Return True if a Protect /api/cameras entry is an ONVIF
    third-party camera rather than a native UVC device.

    Verified field (from hjdhjd/unifi-protect protect-types.ts and
    uilibs/uiprotect devices.py): the legacy `/proxy/protect/api/cameras`
    response carries `isThirdPartyCamera: boolean`. That's the
    canonical discriminator.

    `isAdoptedByOther` and `marketName` are secondary corroborating
    fields. `modelKey` is always "camera" for both natives and
    third-party — it does NOT discriminate.

    Note: the integration API (`/proxy/protect/integration/v1/cameras`)
    exposes a minimal schema that does NOT include
    `isThirdPartyCamera`, so discovery must use the legacy endpoint
    (cookie + CSRF auth, which UniFiProtectClient handles).
    """
    if not isinstance(cam, dict):
        return False

    # Primary: explicit boolean
    if "isThirdPartyCamera" in cam:
        return bool(cam["isThirdPartyCamera"])

    # Older Protect builds may not expose the flag — fall back to
    # type-string heuristic. Native UVC cameras have a "UVC ..." type
    # string; third-party cameras typically don't.
    type_str = (cam.get("type") or cam.get("marketName")
                or cam.get("displayName") or "").lower()
    if type_str.startswith("uvc "):
        return False
    if any(k in type_str for k in ("onvif", "third party", "thirdparty", "rtsp")):
        return True

    # Last resort — include if it has a host and isn't obviously UVC.
    # False positives are unticked by the user; false negatives are
    # silent failures, which is the worse mode.
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

    logger.info("Protect returned %d total camera record(s)", len(raw))

    out: list[DiscoveredCamera] = []
    for cam in raw:
        if not isinstance(cam, dict):
            continue
        name = cam.get("name") or cam.get("id") or "?"
        if not cam.get("isAdopted"):
            logger.debug("Skipping %s — not adopted (isAdopted=%s)", name, cam.get("isAdopted"))
            continue
        if not identify_onvif_camera(cam):
            logger.debug(
                "Skipping %s — not identified as ONVIF (isThirdPartyCamera=%s, type=%r)",
                name, cam.get("isThirdPartyCamera"), cam.get("type") or cam.get("displayName"),
            )
            continue
        logger.info(
            "Found ONVIF camera: %s (id=%s, host=%s, isThirdPartyCamera=%s, type=%r)",
            name, cam.get("id"), cam.get("host"),
            cam.get("isThirdPartyCamera"), cam.get("type") or cam.get("displayName"),
        )
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

    if not out:
        logger.warning(
            "No ONVIF cameras found in Protect. "
            "If your cameras are adopted but not showing up, check that "
            "isThirdPartyCamera=true in Protect (or that their type does not start with 'UVC '). "
            "Set LOG_LEVEL=DEBUG to see why each camera was skipped.",
        )
    return out
