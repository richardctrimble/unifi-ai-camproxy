"""
protect_pusher — fire UniFi Protect Alarm Manager triggers on ONVIF events.

Single push surface, verified against Protect 7.x:

    POST /proxy/protect/integration/v1/alarm-manager/webhook/{id}
    Auth: X-API-Key
    Returns: 204 No Content on success, 400 if id is missing.

The {id} is a user-defined string ("alarmTriggerId"). To make it fire,
the user must create an Alarm Manager rule in Protect's UI with the
matching webhook ID. The body is empty — Protect doesn't propagate any
payload metadata to downstream rule actions, so we encode the
(camera, event-kind) discriminator into the ID itself.

Default ID convention:

    onvif-bridge:<camera_protect_id>:<kind>

The user creates one alarm rule per (camera, kind) they care about,
each with a webhook ID matching that pattern. They can also set
`alarms.webhook_id_template` in config to customise (e.g. a single
shared ID for "any event" or vendor-specific naming).

Bookmark POSTs were considered as a second surface (timeline markers)
but no public-documented endpoint exists in Protect 7.x. hjdhjd's
TS library and uilibs/uiprotect both lack bookmark creation, and the
official OpenAPI spec for Protect 7 has no bookmark path. Until
someone captures DevTools traffic of "Add Bookmark" in the UI on a
live controller, bookmarks are out of scope.

Reference for the alarm webhook endpoint:
    https://github.com/beezly/unifi-apis/blob/main/unifi-protect/7.0.107.json
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from onvif_bridge.onvif_subscriber import OnvifEvent

logger = logging.getLogger("onvif_bridge.pusher")

# Default webhook ID template. Substitutions: {protect_id}, {kind}, {name}.
DEFAULT_WEBHOOK_ID_TEMPLATE = "onvif-bridge:{protect_id}:{kind}"


@dataclass
class PushOutcome:
    ok: bool
    method: str = "alarm_webhook"
    status: int = 0
    message: str = ""
    webhook_id: str = ""


@dataclass
class WebhookFireStats:
    """Per-webhook-id counters for the Setup helper UI."""
    fires_ok: int = 0
    fires_failed: int = 0
    last_fire_epoch: float = 0.0
    last_status: int = 0


@dataclass
class PusherStats:
    """Lightweight rolling counters for the Status dashboard."""
    pushes_ok: int = 0
    pushes_failed: int = 0
    last_outcome: Optional[PushOutcome] = None
    last_outcome_epoch: float = 0.0
    last_event: Optional[OnvifEvent] = None
    last_event_epoch: float = 0.0
    # Per-webhook-id counters. Lets the Setup tab show the user
    # whether each alarm rule they've configured in Protect is
    # actually being fired by the bridge.
    webhook_stats: dict[str, WebhookFireStats] = field(default_factory=dict)


class ProtectPusher:
    """Fires Protect Alarm Manager webhook triggers via the integration API.

    Holds an aiohttp session for connection re-use. The integration
    API requires X-API-KEY auth, which the user configures in
    `unifi.api_key`. If no api_key is set, every push fails with a
    clear error rather than silently dropping events.
    """

    def __init__(
        self,
        host: str,
        api_key: str,
        webhook_id_template: str = DEFAULT_WEBHOOK_ID_TEMPLATE,
    ):
        self._base = host if host.startswith("http") else f"https://{host}"
        self._api_key = api_key
        # Public so the web UI Setup tab can render the exact IDs we'll fire.
        self.webhook_id_template = (
            webhook_id_template or DEFAULT_WEBHOOK_ID_TEMPLATE
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.stats = PusherStats()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        async with self._lock:
            if self._session is None:
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=10)
                self._session = aiohttp.ClientSession(
                    connector=connector, timeout=timeout,
                )

    async def stop(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    # ── Push ───────────────────────────────────────────────────────────────

    def _webhook_id(self, event: OnvifEvent) -> str:
        try:
            return self.webhook_id_template.format(
                protect_id=event.camera_protect_id,
                kind=event.kind,
                name=event.camera_name,
            )
        except (KeyError, IndexError) as exc:
            logger.warning("webhook_id_template error (%s) — falling back",
                           exc)
            return DEFAULT_WEBHOOK_ID_TEMPLATE.format(
                protect_id=event.camera_protect_id, kind=event.kind,
                name=event.camera_name,
            )

    async def push(self, event: OnvifEvent) -> PushOutcome:
        """Fire one Alarm Manager webhook trigger.

        Skips event STOPs — they'd just double the trigger count for
        no benefit (Alarm Manager rules already model "for X seconds
        after trigger" so a single START is enough).
        Skips events that didn't classify to a known kind — firing a
        webhook with id ``onvif-bridge:<id>:unknown`` is never useful.
        """
        # Track every event so the per-kind counters in CameraSubscription
        # stay accurate, but only feed last_event from a useful one.
        if event.kind and event.kind != "unknown":
            self.stats.last_event = event
            self.stats.last_event_epoch = time.time()

        if event.kind == "unknown":
            outcome = PushOutcome(
                ok=True, method="skipped",
                message=f"unclassified topic {event.topic!r} — not pushed",
            )
            self.stats.last_outcome = outcome
            self.stats.last_outcome_epoch = time.time()
            return outcome

        if not event.is_active:
            outcome = PushOutcome(
                ok=True, method="skipped",
                message="event STOP — alarm trigger not re-fired",
            )
            self.stats.last_outcome = outcome
            self.stats.last_outcome_epoch = time.time()
            return outcome

        if not self._api_key:
            outcome = PushOutcome(
                ok=False,
                message=("no unifi.api_key set — Alarm Manager webhooks "
                         "need an integration API key. Generate one in "
                         "Protect → Settings → Control Plane → Integrations."),
            )
            self.stats.pushes_failed += 1
            self.stats.last_outcome = outcome
            self.stats.last_outcome_epoch = time.time()
            return outcome

        webhook_id = self._webhook_id(event)
        outcome = await self._fire(webhook_id)
        outcome.webhook_id = webhook_id

        # Per-webhook counters drive the Setup tab's "is this rule
        # actually firing?" indicator.
        wstats = self.stats.webhook_stats.setdefault(
            webhook_id, WebhookFireStats(),
        )
        wstats.last_fire_epoch = time.time()
        wstats.last_status = outcome.status
        if outcome.ok:
            self.stats.pushes_ok += 1
            wstats.fires_ok += 1
        else:
            self.stats.pushes_failed += 1
            wstats.fires_failed += 1

        self.stats.last_outcome = outcome
        self.stats.last_outcome_epoch = time.time()
        return outcome

    async def _fire(self, webhook_id: str) -> PushOutcome:
        """POST to the alarm-manager webhook trigger.

        Protect responds:
          204 No Content   — trigger fired (or rule with this id doesn't
                             exist; Protect returns 204 either way to
                             avoid leaking which IDs are configured).
          400              — id missing / malformed
          401 / 403        — bad / missing X-API-KEY
        """
        await self.start()
        assert self._session is not None  # for type narrowing

        # urlencode the id segment to handle any colon / slash chars.
        from urllib.parse import quote
        path = f"/proxy/protect/integration/v1/alarm-manager/webhook/{quote(webhook_id, safe='')}"
        url = f"{self._base}{path}"
        headers = {"X-API-Key": self._api_key}

        try:
            async with self._session.post(url, headers=headers) as r:
                if 200 <= r.status < 300:
                    return PushOutcome(ok=True, status=r.status)
                body = (await r.text())[:200]
                return PushOutcome(ok=False, status=r.status,
                                   message=f"HTTP {r.status}: {body}")
        except Exception as exc:  # noqa: BLE001
            return PushOutcome(ok=False, message=f"request error: {exc}")
