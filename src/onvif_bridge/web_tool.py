"""
web_tool — minimal aiohttp UI for the ONVIF bridge.

Skeleton only. Mirrors the full image's web UI ergonomics (same port,
same dark theme, same tab layout) but only the **Status** and a
placeholder **Setup** tab are wired. The Lines tab is gone (no AI in
this image) and the UniFi tab will return once we plumb cred-saving
into the bridge.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List

from aiohttp import web

from build_info import get_build_info

logger = logging.getLogger("onvif_bridge.web")


_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>unifi-ai-camproxy / ONVIF bridge</title>
<style>
 body { font-family: -apple-system, system-ui, sans-serif; background:#1a1a1a; color:#ddd; margin:0; padding:20px; }
 h1 { font-size:18px; margin:0 0 14px; }
 .banner { background:#5f3a1e; border:1px solid #eb8225; border-radius:4px; padding:10px 14px; margin-bottom:16px; font-size:14px; }
 .card { background:#222; border:1px solid #333; border-radius:5px; padding:14px 18px; margin-bottom:14px; }
 .card h3 { margin:0 0 10px; font-size:15px; }
 .row { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #2a2a2a; font-size:13px; }
 .row:last-child { border-bottom:0; }
 .label { color:#888; }
 .empty { color:#666; font-style:italic; }
 code { background:#111; padding:1px 5px; border-radius:3px; font-size:12px; }
</style></head><body>
<h1>unifi-ai-camproxy / ONVIF bridge</h1>
<div class="banner">
  <strong>Preparation phase.</strong> This image bridges ONVIF camera events
  into Protect bookmarks + Alarm Manager webhooks. The wiring is in place
  but the subscription loop and the Protect-side POST endpoints are still
  being verified. For the working spoof+inference flow, run the
  <code>:full</code> image instead.
</div>
<div class="card" id="build-card">
  <h3>Build</h3>
  <div id="build"></div>
</div>
<div class="card">
  <h3>Discovered cameras</h3>
  <div id="cams"><span class="empty">Loading…</span></div>
</div>
<script>
async function refresh() {
  try {
    const data = await (await fetch('/api/status')).json();
    const b = data.build || {};
    document.getElementById('build').innerHTML =
      '<div class="row"><span class="label">SHA</span><span>' + (b.git_sha_short || '?') + '</span></div>' +
      '<div class="row"><span class="label">Ref</span><span>' + (b.git_ref || '?') + '</span></div>' +
      '<div class="row"><span class="label">Built</span><span>' + (b.build_time || '?') + '</span></div>' +
      '<div class="row"><span class="label">Variant</span><span>' + (data.variant || 'onvif') + '</span></div>';

    const cams = data.cameras || [];
    const camsEl = document.getElementById('cams');
    if (!cams.length) {
      camsEl.innerHTML = '<span class="empty">No cameras discovered yet — discovery loop not running in skeleton phase.</span>';
    } else {
      camsEl.innerHTML = cams.map(c =>
        '<div class="row"><span>' + c.name + '</span><span>' + c.host + ' &middot; ' + c.state + '</span></div>'
      ).join('');
    }
  } catch (e) { /* ignore */ }
}
refresh();
setInterval(refresh, 5000);
</script></body></html>
"""


class BridgeWebTool:
    """Lightweight web app — read-only Status today, more tabs later."""

    def __init__(self, config: dict,
                 discovered_cameras: List[dict],
                 subscriptions: Dict[str, object]):
        self.config = config
        self.discovered_cameras = discovered_cameras
        self.subscriptions = subscriptions
        self._start_time = time.monotonic()
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/status", self._status)

    async def _index(self, _: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status(self, _: web.Request) -> web.Response:
        return web.json_response({
            "build": get_build_info(),
            "variant": "onvif",
            "uptime_seconds": int(time.monotonic() - self._start_time),
            "cameras": self.discovered_cameras,
            "subscription_count": len(self.subscriptions),
        })

    async def run(self, port: int) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web UI: http://0.0.0.0:%d/", port)
        # Hold the coroutine so the runner isn't garbage-collected.
        while True:
            import asyncio
            await asyncio.sleep(3600)
