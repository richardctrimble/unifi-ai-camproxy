"""
web_tool — minimal aiohttp UI for the ONVIF bridge.

Two tabs today: **Status** and **Logs**. Setup will follow once we
decide whether to drive ONVIF creds from env vars (TrueNAS-style) or
to ship a per-camera form here.

The UI deliberately mirrors the full image's dashboard layout (same
port 8091, dark theme, status-grid styling) so users moving between
the two images see something familiar.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict

from aiohttp import web

from build_info import get_build_info

logger = logging.getLogger("onvif_bridge.web")


_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>unifi-ai-camproxy / ONVIF bridge</title>
<style>
 * { box-sizing: border-box; }
 body { font-family: -apple-system, system-ui, sans-serif; background:#1a1a1a; color:#ddd; margin:0; padding:18px; max-width:1100px; }
 h1 { font-size:18px; margin:0 0 12px; }
 .tabs { display:flex; gap:6px; margin-bottom:14px; border-bottom:1px solid #333; }
 .tab { background:none; border:0; color:#888; padding:8px 14px; cursor:pointer; font-size:14px; border-bottom:2px solid transparent; }
 .tab.active { color:#ddd; border-bottom-color:#3b82f6; }
 .pane { display:none; }
 .pane.active { display:block; }
 .card { background:#222; border:1px solid #333; border-radius:5px; padding:14px 18px; margin-bottom:14px; }
 .card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
 .card h3 { margin:0; font-size:15px; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:6px 18px; }
 .row { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #2a2a2a; font-size:13px; }
 .row:last-child { border-bottom:0; }
 .label { color:#888; }
 .empty { color:#666; font-style:italic; }
 .ok { color:#4c4; }
 .err { color:#f87; }
 .warn { color:#fa4; }
 .pill { display:inline-block; padding:1px 8px; border-radius:9px; font-size:11px; background:#333; color:#bbd; }
 .pill.kind-person { background:#1e3a5f; color:#9bf; }
 .pill.kind-vehicle { background:#0d3b2c; color:#6f6; }
 .pill.kind-line_crossing { background:#5f3a1e; color:#fa4; }
 .pill.kind-motion { background:#333; color:#bbb; }
 .pill.kind-audio { background:#3b1e5f; color:#c8a; }
 code { background:#111; padding:1px 5px; border-radius:3px; font-size:12px; }
 .cam-row { display:grid; grid-template-columns:2fr 1.4fr 1fr 1fr 1fr 1.2fr; gap:8px; padding:8px 0; border-bottom:1px solid #2a2a2a; font-size:13px; align-items:center; }
 .cam-row.header { color:#888; font-weight:600; border-bottom:1px solid #444; }
 pre#log-output { background:#111; border:1px solid #333; border-radius:4px; padding:10px; max-height:65vh; overflow:auto; font-size:12px; line-height:1.4; white-space:pre-wrap; word-break:break-all; margin:0; }
</style></head><body>
<h1>unifi-ai-camproxy / ONVIF bridge</h1>
<div class="tabs">
  <button class="tab active" data-pane="status">Status</button>
  <button class="tab" data-pane="logs">Logs</button>
</div>

<div id="status" class="pane active">
  <div class="card">
    <div class="card-header"><h3>Bridge</h3></div>
    <div class="grid" id="bridge-grid"></div>
  </div>
  <div class="card">
    <div class="card-header"><h3>Cameras (ONVIF) discovered in Protect</h3></div>
    <div id="cams-block"></div>
  </div>
  <div class="card">
    <div class="card-header"><h3>Push activity</h3></div>
    <div class="grid" id="push-grid"></div>
  </div>
</div>

<div id="logs" class="pane">
  <div class="card">
    <div class="card-header">
      <h3>Container logs</h3>
      <div style="display:flex;gap:8px;align-items:center;">
        <label style="margin:0;">Lines:
          <select id="log-lines" style="margin-left:4px;">
            <option value="200">200</option>
            <option value="500" selected>500</option>
            <option value="1000">1000</option>
          </select>
        </label>
        <label style="margin:0;"><input type="checkbox" id="log-auto"> Auto-refresh (3s)</label>
        <button id="log-refresh">Refresh</button>
      </div>
    </div>
    <pre id="log-output">Loading…</pre>
  </div>
</div>

<script>
function esc(s){return String(s ?? "").replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function fmtAgo(epoch){if(!epoch)return '—';var s=Math.max(0,Math.floor(Date.now()/1000-epoch));if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}

document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById(t.dataset.pane).classList.add('active');
  if (t.dataset.pane === 'logs') refreshLogs();
}));

async function refreshStatus(){
  try {
    const data = await (await fetch('/api/status')).json();
    var b = data.build || {};
    var bridge = data.bridge || {};
    document.getElementById('bridge-grid').innerHTML =
      '<div class="row"><span class="label">Image variant</span><span><code>'+esc(data.variant||'onvif')+'</code></span></div>'+
      '<div class="row"><span class="label">Build</span><span>'+esc(b.git_sha_short||'?')+' ('+esc(b.git_ref||'?')+')</span></div>'+
      '<div class="row"><span class="label">Built</span><span>'+esc(b.build_time||'?')+'</span></div>'+
      '<div class="row"><span class="label">Uptime</span><span>'+(data.uptime_seconds||0)+'s</span></div>'+
      '<div class="row"><span class="label">Last discovery</span><span>'+fmtAgo(bridge.last_discovery_epoch)+'</span></div>'+
      '<div class="row"><span class="label">Discovery error</span><span class="'+(bridge.last_discovery_error?'err':'ok')+'">'+esc(bridge.last_discovery_error||'none')+'</span></div>';

    var cams = data.cameras || [];
    var camsEl = document.getElementById('cams-block');
    if (!cams.length) {
      camsEl.innerHTML = '<span class="empty">No ONVIF cameras discovered yet. Check unifi credentials, or wait '+(60)+'s for the next discovery cycle.</span>';
    } else {
      var html = '<div class="cam-row header"><span>Name</span><span>IP</span><span>State (Protect)</span><span>Subscription</span><span>Last event</span><span>Topic / kind</span></div>';
      cams.forEach(function(c){
        var sub = (data.subscriptions || {})[c.protect_id];
        var subStat = sub ? (sub.is_connected ? 'connected' : 'connecting…') : 'no creds';
        var subCls = sub ? (sub.is_connected ? 'ok' : 'warn') : 'err';
        var ev = sub && sub.last_event;
        var lastEv = ev ? fmtAgo(ev.timestamp_epoch) : '—';
        var kind = ev ? '<span class="pill kind-'+esc(ev.kind)+'">'+esc(ev.kind)+'</span> '+esc(ev.topic||'').slice(0,60) : '';
        html += '<div class="cam-row">'+
          '<span>'+esc(c.name)+'</span>'+
          '<span><code>'+esc(c.host||'?')+'</code></span>'+
          '<span>'+esc(c.state||'')+'</span>'+
          '<span class="'+subCls+'">'+esc(subStat)+(sub && sub.last_error ? ' <span class="err" title="'+esc(sub.last_error)+'">!</span>' : '')+'</span>'+
          '<span>'+lastEv+'</span>'+
          '<span>'+kind+'</span>'+
        '</div>';
      });
      camsEl.innerHTML = html;
    }

    var ps = data.pusher_stats || {};
    var lastEvent = ps.last_event;
    var lastOutcome = ps.last_outcome;
    document.getElementById('push-grid').innerHTML =
      '<div class="row"><span class="label">Alarm triggers OK / failed</span><span><span class="ok">'+(ps.pushes_ok||0)+'</span> / <span class="err">'+(ps.pushes_failed||0)+'</span></span></div>'+
      '<div class="row"><span class="label">Last event</span><span>'+(lastEvent ? fmtAgo(ps.last_event_epoch)+' — '+esc(lastEvent.camera_name)+' / <span class="pill kind-'+esc(lastEvent.kind)+'">'+esc(lastEvent.kind)+'</span>' : '—')+'</span></div>'+
      '<div class="row"><span class="label">Last webhook id</span><span><code>'+esc(lastOutcome && lastOutcome.webhook_id || '—')+'</code></span></div>'+
      '<div class="row"><span class="label">Last outcome</span><span class="'+(lastOutcome && lastOutcome.ok ? 'ok' : 'err')+'">'+(lastOutcome ? esc(lastOutcome.method)+' — '+esc(lastOutcome.message||(lastOutcome.ok?'OK':'failed')) : '—')+'</span></div>';
  } catch (e) { /* ignore */ }
}

async function refreshLogs(){
  var lines = document.getElementById('log-lines').value;
  try {
    var resp = await fetch('/api/logs?lines='+encodeURIComponent(lines));
    var text = await resp.text();
    document.getElementById('log-output').textContent = text || '(empty)';
  } catch (e) {
    document.getElementById('log-output').textContent = 'Failed to load logs: '+e;
  }
}

document.getElementById('log-refresh').addEventListener('click', refreshLogs);
var logTimer;
document.getElementById('log-auto').addEventListener('change', function(e){
  clearInterval(logTimer);
  if (e.target.checked) logTimer = setInterval(refreshLogs, 3000);
});

refreshStatus();
setInterval(refreshStatus, 3000);
</script></body></html>
"""

# RTSP password redaction in log lines (mirrors the full image's behaviour).
_RTSP_PWD_RE = re.compile(r"(rtsp://[^:]+:)([^@]+)(@)")


class BridgeWebTool:
    """Lightweight web app — Status + Logs."""

    def __init__(self, config: dict,
                 state_provider: Callable[[], dict]):
        self.config = config
        self._state_provider = state_provider
        self._start_time = time.monotonic()
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/status", self._status)
        self.app.router.add_get("/api/logs", self._logs)

    async def _index(self, _: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        subs = state.get("subscriptions") or {}
        # Project subscription state into JSON-safe shapes.
        sub_payload: Dict[str, dict] = {}
        for pid, sub in subs.items():
            ev = sub.last_event
            sub_payload[pid] = {
                "is_connected": sub.is_connected,
                "consecutive_failures": sub.consecutive_failures,
                "last_pull_epoch": sub.last_pull_epoch,
                "last_error": sub.last_error,
                "last_event": (
                    {
                        "topic": ev.topic, "kind": ev.kind,
                        "is_active": ev.is_active,
                        "timestamp_epoch": ev.timestamp_epoch,
                    } if ev else None
                ),
            }
        ps = state.get("pusher_stats")
        ps_payload = None
        if ps is not None:
            le = ps.last_event
            lo = ps.last_outcome
            ps_payload = {
                "pushes_ok": ps.pushes_ok,
                "pushes_failed": ps.pushes_failed,
                "last_event_epoch": ps.last_event_epoch,
                "last_event": (
                    {"camera_name": le.camera_name, "kind": le.kind,
                     "topic": le.topic} if le else None
                ),
                "last_outcome": (
                    {"ok": lo.ok, "method": lo.method,
                     "status": lo.status, "message": lo.message,
                     "webhook_id": lo.webhook_id}
                    if lo else None
                ),
            }
        return web.json_response({
            "build": get_build_info(),
            "variant": "onvif",
            "uptime_seconds": int(time.monotonic() - self._start_time),
            "cameras": state.get("discovered_cameras", []),
            "subscriptions": sub_payload,
            "pusher_stats": ps_payload,
            "bridge": {
                "last_discovery_error": state.get("last_discovery_error", ""),
                "last_discovery_epoch": state.get("last_discovery_epoch", 0),
            },
        })

    async def _logs(self, request: web.Request) -> web.Response:
        try:
            lines = int(request.query.get("lines", "500"))
        except ValueError:
            lines = 500
        lines = max(50, min(lines, 5000))
        log_path = Path("/config/camproxy.log")
        if not log_path.exists():
            return web.Response(text="(no log file yet)", content_type="text/plain")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-lines:]
        except OSError as exc:
            return web.Response(text=f"(could not read log: {exc})",
                                content_type="text/plain")
        redacted = "".join(_RTSP_PWD_RE.sub(r"\1***\3", line) for line in tail)
        return web.Response(text=redacted, content_type="text/plain")

    async def run(self, port: int) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web UI: http://0.0.0.0:%d/", port)
        # Hold the coroutine so the runner isn't garbage-collected.
        while True:
            await asyncio.sleep(3600)
