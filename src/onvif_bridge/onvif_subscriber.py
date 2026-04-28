"""
onvif_subscriber — per-camera ONVIF event subscription via PullPoint.

We use onvif-zeep (sync), wrapping blocking calls in asyncio.to_thread
so the bridge stays responsive across many cameras. PullMessages is a
long poll (we ask for PT30S), which without to_thread would block the
event loop for the whole pull duration.

Topic normalisation maps vendor-specific event topics to a small set of
canonical kinds we know how to bridge into Protect. Anything unrecognised
falls through to "unknown" and is still bridge-able as a generic motion
bookmark — better to surface it noisily than silently drop a real
detection.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    onvif_host: str
    onvif_port: int
    username: str
    password: str
    last_event: Optional[OnvifEvent] = None
    last_pull_epoch: float = 0.0
    consecutive_failures: int = 0
    is_connected: bool = False
    last_error: str = ""
    # Set True when the subscription loop detects an auth failure.
    # _reconcile skips restarting locked cameras; clearing requires
    # a credential update or an explicit retry from the UI.
    auth_locked: bool = False
    supported_topics: list[str] = field(default_factory=list)


# ── Topic → kind classification ────────────────────────────────────────────
#
# Substring-match against a lowercased topic. First match wins. Order
# matters — put more specific before more generic.

_KIND_RULES: list[tuple[str, str]] = [
    ("peopledetect",       "person"),
    ("persondetect",       "person"),
    ("vehicledetect",      "vehicle"),
    ("linedetector",       "line_crossing"),
    ("linecross",          "line_crossing"),
    ("fielddetector",      "line_crossing"),
    ("objectsinside",      "line_crossing"),
    ("audioanalytics",     "audio"),
    ("detectedsound",      "audio"),
    ("cellmotiondetector", "motion"),
    ("motionalarm",        "motion"),
    ("motiondetect",       "motion"),
    # Generic ObjectDetector — many cameras use this for their on-board
    # AI. Treat as person by default since that's the most common use.
    ("objectdetector",     "person"),
]


def classify_topic(topic: str) -> str:
    t = (topic or "").lower()
    for needle, kind in _KIND_RULES:
        if needle in t:
            return kind
    return "unknown"


# ── ONVIF event parsing ────────────────────────────────────────────────────


def _parse_notification(msg) -> Optional[tuple[str, bool, dict]]:
    """Extract (topic, is_active, props) from a NotificationMessage.

    ONVIF messages nest the topic under msg.Topic._value_1 (zeep's name
    for the XML element value) and the data under msg.Message._value_1.
    The "is_active" boolean lives in a SimpleItem named IsMotion / Object
    / State / similar — vendors are inconsistent. We pull every
    SimpleItem into props and look for a true-ish value among the usual
    keys.
    """
    try:
        topic = ""
        topic_elem = getattr(msg, "Topic", None)
        if topic_elem is not None:
            topic = getattr(topic_elem, "_value_1", "") or ""

        props: dict = {}
        active: Optional[bool] = None

        message_elem = getattr(msg, "Message", None)
        if message_elem is not None:
            inner = getattr(message_elem, "_value_1", None)
            data = getattr(inner, "Data", None) if inner is not None else None
            simple_items = getattr(data, "SimpleItem", []) if data is not None else []
            for item in simple_items or []:
                name = getattr(item, "Name", "")
                value = getattr(item, "Value", "")
                if name:
                    props[name] = value
                # Active-state hints. Vendors vary widely.
                if name in ("IsMotion", "State", "Active", "Detected", "Object"):
                    s = str(value).lower()
                    if s in ("true", "1", "active", "on", "yes"):
                        active = True
                    elif s in ("false", "0", "inactive", "off", "no"):
                        active = False

        # Fallback: if no explicit state, treat the message as a START.
        if active is None:
            active = True

        return topic, active, props
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to parse ONVIF notification: %s", exc)
        return None


# ── Auth-error detection ───────────────────────────────────────────────────

_AUTH_NEEDLES = (
    "401", "403",
    "unauthorized", "authentication failed", "authentication error",
    "access denied", "not authorized", "sender not authorized",
    "invalid credentials", "wrong password",
)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(n in msg for n in _AUTH_NEEDLES)


# ── Subscription loop ──────────────────────────────────────────────────────


async def _build_camera(host: str, port: int, user: str, pwd: str):
    """Build an ONVIFCamera off the event loop."""
    from onvif import ONVIFCamera  # noqa: PLC0415 — keep import lazy
    cam = await asyncio.to_thread(ONVIFCamera, host, port, user, pwd)
    await asyncio.to_thread(cam.update_xaddrs)
    return cam


def _walk_topic_tree(node, parent_path: str = "") -> list[str]:
    """Flatten an ONVIF topic-set tree into a list of dotted paths.

    `events.GetEventProperties().TopicSet` returns nested zeep
    `_value_1` blocks like:

        Element MyTopic
          Element MyChild
            ... (leaf marked with topic="true")

    We walk the tree and emit every topic string the camera says it
    can publish. Vendors are wildly inconsistent in how deep these
    nest, so we accept whatever shape we find.
    """
    out: list[str] = []
    if node is None:
        return out
    # zeep returns AnyObject / lxml Element — iterate children with
    # tag and attrib accessors. Non-element objects are ignored.
    children = getattr(node, "_value_1", None)
    if children is None:
        try:
            children = list(node)
        except TypeError:
            children = []
    for child in children or []:
        tag = getattr(child, "tag", None) or getattr(child, "name", None) or ""
        # tag may be qualified `{ns}LocalName` — strip ns
        if isinstance(tag, str) and "}" in tag:
            tag = tag.split("}", 1)[1]
        if not tag:
            continue
        path = f"{parent_path}/{tag}" if parent_path else tag
        attrib = getattr(child, "attrib", {}) or {}
        is_topic = (
            attrib.get("topic") == "true"
            or attrib.get("{http://docs.oasis-open.org/wsn/t-1}topic") == "true"
        )
        if is_topic:
            out.append(path)
        out.extend(_walk_topic_tree(child, path))
    return out


async def _fetch_supported_topics(cam) -> list[str]:
    """Best-effort enumeration of topics this camera advertises.

    Uses ONVIF's GetEventProperties. Many cameras restrict it to admin
    users, and a handful of cheap clones don't implement it at all —
    those return an empty list and we silently fall through to the
    PullMessages-based discovery (whatever topics actually fire will
    eventually appear in last_event regardless).
    """
    try:
        events = cam.create_events_service()
        props = await asyncio.to_thread(events.GetEventProperties)
        topic_set = getattr(props, "TopicSet", None)
        topics = _walk_topic_tree(topic_set)
        # De-dupe + sort for stable display.
        return sorted(set(topics))
    except Exception as exc:  # noqa: BLE001
        logger.debug("GetEventProperties failed: %s", exc)
        return []


async def subscribe_camera(
    sub: CameraSubscription,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[OnvifEvent]:
    """Yield normalised events from one camera until cancelled.

    On any error (connection refusal, expired subscription, parse
    failure) we back off exponentially up to ~60 s and recreate the
    PullPoint. This keeps a flaky camera from spamming Protect and
    lets the dashboard show a clean "disconnected" state.
    """
    backoff = 5
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return

        try:
            cam = await _build_camera(sub.onvif_host, sub.onvif_port,
                                      sub.username, sub.password)

            events = cam.create_events_service()
            # Ask for 10-min termination — we'll renew well before then
            # by re-creating on errors, which is simpler than tracking
            # SubscriptionReferences.
            await asyncio.to_thread(
                events.CreatePullPointSubscription,
                {"InitialTerminationTime": "PT600S"},
            )
            pull = cam.create_pullpoint_service()

            sub.is_connected = True
            sub.consecutive_failures = 0
            sub.last_error = ""
            backoff = 5
            logger.info("Subscribed to ONVIF events on %s (%s:%d)",
                        sub.name, sub.onvif_host, sub.onvif_port)

            # Best-effort: ask the camera what topics it can emit so
            # the Setup tab can show real options instead of guessing.
            # Failure is silent — many cameras restrict this to admins.
            try:
                topics = await _fetch_supported_topics(cam)
                if topics:
                    sub.supported_topics = topics
                    logger.info("%s advertises %d ONVIF topics",
                                sub.name, len(topics))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Topic enumeration failed for %s: %s",
                             sub.name, exc)

            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return

                result = await asyncio.to_thread(
                    pull.PullMessages,
                    {"Timeout": "PT30S", "MessageLimit": 10},
                )
                sub.last_pull_epoch = time.time()
                messages = getattr(result, "NotificationMessage", None) or []
                for msg in messages:
                    parsed = _parse_notification(msg)
                    if parsed is None:
                        continue
                    topic, is_active, props = parsed
                    event = OnvifEvent(
                        camera_protect_id=sub.protect_id,
                        camera_name=sub.name,
                        topic=topic,
                        kind=classify_topic(topic),
                        is_active=is_active,
                        timestamp_epoch=time.time(),
                        raw_data=props,
                    )
                    sub.last_event = event
                    yield event

        except asyncio.CancelledError:
            sub.is_connected = False
            raise
        except Exception as exc:  # noqa: BLE001
            sub.is_connected = False
            sub.consecutive_failures += 1
            sub.last_error = str(exc)[:200]
            if _is_auth_error(exc):
                sub.auth_locked = True
                logger.warning(
                    "ONVIF auth failed for %s (%s:%d): %s — "
                    "stopping retries. Update credentials or use "
                    "the Retry button in the ONVIF Creds tab.",
                    sub.name, sub.onvif_host, sub.onvif_port, exc,
                )
                return
            logger.warning(
                "ONVIF subscription error on %s (%s:%d): %s — retrying in %ds",
                sub.name, sub.onvif_host, sub.onvif_port, exc, backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2, 60)
