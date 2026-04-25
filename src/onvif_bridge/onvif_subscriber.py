"""
onvif_subscriber — per-camera ONVIF event subscription.

ONVIF Profile S devices expose events via WS-BaseNotification. There are
two flavours:

  * BaseSubscription (push) — the device POSTs events to a callback URL
    we provide. Reliable but requires a publicly reachable endpoint
    from the camera to us.
  * PullPointSubscription (pull) — we periodically PULL events from a
    subscription endpoint the camera issues. Works in any topology
    (camera doesn't need to reach back to us).

We use **PullPoint** — simpler and works inside containers.

Common topics worth bridging into Protect (vendor-dependent):

  tns1:VideoSource/MotionAlarm                    — basic motion
  tns1:RuleEngine/CellMotionDetector/Motion       — zoned motion
  tns1:RuleEngine/MyRuleDetector/PeopleDetect     — Hikvision "person"
  tns1:RuleEngine/MyRuleDetector/VehicleDetect    — Hikvision "vehicle"
  tns1:RuleEngine/FieldDetector/ObjectsInside     — line crossing / zones
  tns1:RuleEngine/LineDetector/Crossed            — virtual line crossing
  tns1:AudioAnalytics/Audio/DetectedSound         — audio alarm

Status: SKELETON — the contract and dataclasses are stable, the actual
zeep / onvif-zeep subscription loop is the next milestone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

logger = logging.getLogger("onvif_bridge.subscriber")


@dataclass
class OnvifEvent:
    """Normalised event from any ONVIF camera, regardless of vendor."""
    camera_protect_id: str   # forward this when POSTing to Protect
    camera_name: str         # for logging / dashboard
    topic: str               # raw ONVIF topic (e.g. tns1:RuleEngine/...)
    kind: str                # one of: motion / person / vehicle / line_crossing / audio / unknown
    is_active: bool          # True for "started", False for "ended"
    timestamp_epoch: float
    raw_data: dict = field(default_factory=dict)  # SimpleItem props from the event


@dataclass
class CameraSubscription:
    """Live PullPoint subscription state for a single camera."""
    protect_id: str
    name: str
    onvif_url: str
    username: str
    password: str
    last_event: Optional[OnvifEvent] = None
    last_pull_epoch: float = 0.0
    consecutive_failures: int = 0


# ── Topic → kind classification ────────────────────────────────────────────
#
# Vendor-agnostic mapping. Add entries here as new cameras come online.
# Anything unmatched falls through to "unknown" which is still
# bridge-able (as a generic motion bookmark).

_KIND_MAP: dict[str, str] = {
    "motionalarm":          "motion",
    "cellmotiondetector":   "motion",
    "peopledetect":         "person",
    "persondetect":         "person",
    "objectdetector":       "person",   # some cameras emit generic "object"
    "vehicledetect":        "vehicle",
    "linedetector":         "line_crossing",
    "linecross":            "line_crossing",
    "fielddetector":        "line_crossing",   # zone-entry treated as line
    "audioanalytics":       "audio",
    "detectedsound":        "audio",
}


def classify_topic(topic: str) -> str:
    t = topic.lower()
    for key, kind in _KIND_MAP.items():
        if key in t:
            return kind
    return "unknown"


# ── Subscription loop ──────────────────────────────────────────────────────


async def subscribe_camera(
    sub: CameraSubscription,
) -> AsyncIterator[OnvifEvent]:
    """Yield normalised events from one camera until cancelled.

    Implementation outline (TODO):

      1. Build an onvif-zeep ONVIFCamera client from sub.onvif_url +
         credentials.
      2. Get the EventService and call CreatePullPointSubscription with
         a 10-min termination time.
      3. Loop:
           a. PullMessages(timeout=PT30S, MessageLimit=10).
           b. For each NotificationMessage in the response, parse the
              topic + SimpleItem properties, classify_topic(), build
              an OnvifEvent, yield it.
           c. Update sub.last_event / sub.last_pull_epoch.
           d. On error: increment consecutive_failures; back off
              exponentially up to ~60s; recreate subscription if
              failures exceed a threshold.
      4. On cancel: Unsubscribe() to release controller-side state.

    Until step (1)/(2) is implemented this function is a stub that
    yields nothing — the bridge starts up cleanly and the dashboard
    will show "no events yet" instead of crashing.
    """
    logger.info(
        "ONVIF subscription stubbed for %s (%s) — bridge skeleton phase",
        sub.name, sub.onvif_url,
    )
    # Keep the coroutine alive so the bridge's gather() doesn't see
    # a task that finished instantly. Cancellation propagates cleanly.
    while True:
        await asyncio.sleep(60)
        sub.last_pull_epoch = asyncio.get_event_loop().time()
    # Yield is unreachable today; once the real implementation lands
    # the loop above will be replaced.
    if False:  # pragma: no cover
        yield OnvifEvent(  # type: ignore[unreachable]
            camera_protect_id="", camera_name="", topic="", kind="",
            is_active=False, timestamp_epoch=0.0,
        )
