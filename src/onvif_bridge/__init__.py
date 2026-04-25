"""
onvif_bridge — pivot mode for unifi-ai-camproxy.

Instead of spoofing a camera into UniFi Protect and running local YOLO
inference, this mode lets your real ONVIF cameras keep their native
adoption (Protect handles video, including H.265) and bridges each
camera's onboard ONVIF event stream into Protect's timeline as
bookmarks + Alarm Manager webhooks.

Module layout:

  protect_discovery   — list ONVIF-adopted cameras from Protect
  onvif_subscriber    — subscribe to each camera's ONVIF events
  protect_pusher      — POST bookmarks + fire Alarm Manager webhooks
  web_tool            — lightweight web UI (status + setup)
  main                — wire everything together; the entrypoint

See SECONDBRAIN.md "ONVIF bridge mode" for architectural rationale,
the alternative (spoof) mode, and the verification questions still
open about Protect's bookmark/Alarm Manager APIs.
"""

__version__ = "0.0.1-skeleton"
