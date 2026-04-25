"""
protect_pusher — translate OnvifEvent into something Protect's UI shows.

Two surfaces, used together:

  1. **Bookmarks** (`POST /proxy/protect/api/cameras/{id}/bookmarks`)
     Marker on the camera's timeline with a label. Persists with the
     recording. Cheap and survives Protect restarts. Best for
     "something happened, here it is on the scrub bar".

     OPEN QUESTION (verify against live Protect 7.x): the exact path
     and JSON body shape. Expected payload is roughly:
        { "name": "<label>", "time": <epoch_ms>, "color": "<hex?>" }
     but this needs confirming. The pusher handles a 404 / 405 by
     falling back to a PATCH attempt, similar to how unadopt_camera
     defends against unknown method routing.

  2. **Alarm Manager custom-webhook trigger**
     A URL the user configures inside Protect's Alarm Manager that,
     when called, fires an automation rule (notification, clip
     extension, downstream webhook, etc.). Per-event metadata can
     be passed in the query string or body, depending on how the
     trigger was configured. This is what drives mobile push
     notifications.

Status: SKELETON — both surfaces are stubbed. The bookmark POST is
the higher-confidence path; we'll wire that first once the endpoint
shape is verified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from onvif_bridge.onvif_subscriber import OnvifEvent

logger = logging.getLogger("onvif_bridge.pusher")


@dataclass
class PushOutcome:
    ok: bool
    method: str           # "bookmark" | "alarm_webhook" | "skipped"
    message: str = ""


class ProtectPusher:
    """Stateless push helper.

    Holds a reference to a logged-in UniFiProtectClient (cookie + CSRF)
    for the bookmark path, and a separate aiohttp session for posting
    Alarm Manager webhook URLs (no auth — the URL itself is the secret).
    """

    def __init__(
        self,
        client,                    # UniFiProtectClient (already logged in)
        alarm_webhook_url: str = "",
    ):
        self._client = client
        self._alarm_webhook_url = alarm_webhook_url

    async def push(self, event: OnvifEvent) -> PushOutcome:
        """Bridge one ONVIF event into Protect.

        Strategy:
          - Always attempt to write a bookmark on `is_active=True`
            transitions (event START). Stop events are dropped — the
            bookmark already marks the moment.
          - If an alarm webhook URL is configured, fire it in parallel.

        Both attempts are best-effort: a failure on one doesn't block
        the other, and neither raises — the dashboard displays the
        last outcome so the user can see what's flowing.
        """
        if not event.is_active:
            return PushOutcome(ok=True, method="skipped",
                               message="event STOP — no bookmark")

        # Bookmark — TODO: verify endpoint + payload shape
        outcome = await self._write_bookmark(event)

        # Alarm Manager webhook — fire-and-forget, don't override
        # the bookmark outcome unless the bookmark itself failed.
        if self._alarm_webhook_url:
            await self._fire_alarm_webhook(event)

        return outcome

    async def _write_bookmark(self, event: OnvifEvent) -> PushOutcome:
        """POST a bookmark to the camera's timeline.

        Verified: the legacy `/proxy/protect/api/*` path uses cookie+CSRF
        auth (UniFiProtectClient handles that). Unverified: exact route
        and payload. Defensive try-list:

          1. POST /proxy/protect/api/cameras/{id}/bookmarks
                  body: {"name": ..., "time": <ms>, "color": "#..."}
          2. POST /proxy/protect/api/bookmarks
                  body: {"cameraId": ..., "name": ..., "time": <ms>}
          3. PATCH /proxy/protect/api/cameras/{id}
                  body: {"bookmarks": [<existing>, <new>]}

        The 404/405 fall-through is the same defensive pattern we use
        in unifi_auth.UniFiProtectClient.unadopt_camera.

        Until verified, this is a stub that logs the intended call.
        """
        logger.info(
            "[stub] would write bookmark on %s (id=%s) at %s: %s/%s",
            event.camera_name, event.camera_protect_id,
            event.timestamp_epoch, event.kind, event.topic,
        )
        return PushOutcome(ok=True, method="bookmark",
                           message="stub — verification pending")

    async def _fire_alarm_webhook(self, event: OnvifEvent) -> None:
        """GET / POST the Alarm Manager custom-webhook URL.

        Most "Custom Webhook" triggers in Protect 7.x just check that
        the URL was hit; per-event metadata is usually passed via
        query string (so it shows up in the Alarm Manager log even if
        the trigger ignores it).

        Until the trigger contract is verified: simple GET with
        query params, no auth, short timeout, exceptions logged but
        not raised.
        """
        params = {
            "camera_id": event.camera_protect_id,
            "camera_name": event.camera_name,
            "kind": event.kind,
            "topic": event.topic,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                async with s.get(self._alarm_webhook_url, params=params) as r:
                    if r.status >= 400:
                        logger.warning(
                            "Alarm webhook for %s returned HTTP %s",
                            event.camera_name, r.status,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alarm webhook call failed for %s: %s",
                           event.camera_name, exc)
