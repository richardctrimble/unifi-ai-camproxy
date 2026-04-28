"""
web_tool — aiohttp UI for the ONVIF bridge.

Five tabs:

  * **Status** — discovered cameras, per-camera subscription state,
    last event, push counters, discovery error banner.
  * **UniFi Creds** — Protect host, username + password (with test button),
    API key (with test button), save to config.yml.
  * **ONVIF Creds** — fleet ONVIF username + password + port, per-camera
    supported topics (live from subscriptions).
  * **Alarm Setup** — per-(camera, kind) webhook IDs with copy buttons
    and live firing status. Guides user through creating Alarm Manager
    rules in Protect (the only manual part).
  * **Logs** — tail of /config/camproxy.log with password redaction.

Config writes use mutate-in-place (self.config.clear(); self.config.update(...))
so the dict shared with main.py's discovery loop picks up changes immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict

import aiohttp
import yaml
from aiohttp import web

from build_info import get_build_info
from onvif_bridge.onvif_subscriber import classify_topic

logger = logging.getLogger("onvif_bridge.web")

CONFIG_PATH = Path("/config/config.yml")

SUPPORTED_KINDS = ["person", "vehicle", "line_crossing", "motion", "audio", "face"]
DEFAULT_WEBHOOK_TEMPLATE = "onvif-bridge:{protect_id}:{kind}"

_RTSP_PWD_RE = re.compile(r"(rtsp://[^:]+:)([^@]+)(@)")


def _format_webhook_id(template: str, protect_id: str, kind: str,
                       name: str) -> str:
    """Apply the user's template; fall back to the default on KeyError."""
    try:
        return template.format(protect_id=protect_id, kind=kind, name=name)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_WEBHOOK_TEMPLATE.format(
            protect_id=protect_id, kind=kind, name=name,
        )


_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>unifi-ai-camproxy / ONVIF bridge</title>
<style>
 * { box-sizing: border-box; }
 html, body { overflow-x:hidden; }
 body { font-family: -apple-system, system-ui, sans-serif; background:#1a1a1a; color:#ddd; margin:0; padding:18px; }
 h1 { font-size:18px; margin:0 0 12px; }
 h4 { margin:14px 0 6px; font-size:13px; color:#bbb; }
 .tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; border-bottom:1px solid #333; }
 .tab { background:none; border:0; color:#888; padding:8px 14px; cursor:pointer; font-size:14px; border-bottom:2px solid transparent; }
 .tab.active { color:#ddd; border-bottom-color:#3b82f6; }
 .pane { display:none; }
 .pane.active { display:block; }
 .card { background:#222; border:1px solid #333; border-radius:5px; padding:14px 18px; margin-bottom:14px; }
 .card-header { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; align-items:center; margin-bottom:10px; }
 .card h3 { margin:0; font-size:15px; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:6px 18px; }
 .row { display:flex; flex-wrap:wrap; gap:6px; justify-content:space-between; padding:5px 0; border-bottom:1px solid #2a2a2a; font-size:13px; }
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
 .pill.kind-face { background:#3b2e1e; color:#f9c; }
 code { background:#111; padding:1px 5px; border-radius:3px; font-size:12px; word-break:break-all; }
 .copy-row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-width:0; }
 .copy-row code { flex:1 1 200px; min-width:0; padding:4px 8px; font-size:12px; overflow:hidden; word-break:break-all; }
 .copy-btn { background:#333; border:0; color:#ddd; padding:4px 10px; border-radius:3px; cursor:pointer; font-size:12px; }
 .copy-btn:hover { background:#444; }
 .copy-btn.copied { background:#0d3b2c; color:#6f6; }
 ol.steps { margin:0; padding-left:22px; line-height:1.55; font-size:13px; }
 ol.steps li { margin-bottom:6px; }
 .cam-row { display:grid; grid-template-columns:2fr 1.4fr 1fr 1fr 1fr 1.2fr; gap:8px; padding:8px 0; border-bottom:1px solid #2a2a2a; font-size:13px; align-items:center; word-break:break-word; }
 .cam-row.header { color:#888; font-weight:600; border-bottom:1px solid #444; }
 .setup-row { display:grid; grid-template-columns:0.4fr 1.4fr 1fr 0.6fr 2.4fr 1fr; gap:10px; padding:6px 0; border-bottom:1px solid #2a2a2a; font-size:13px; align-items:center; word-break:break-word; }
 .setup-row input[type="checkbox"] { width:16px; height:16px; cursor:pointer; accent-color:#4a90e2; }
 .setup-row.disabled { opacity:0.55; }
 .setup-row.header { color:#888; font-weight:600; border-bottom:1px solid #444; }
 pre#log-output { background:#111; border:1px solid #333; border-radius:4px; padding:10px; max-height:65vh; overflow:auto; font-size:12px; line-height:1.4; white-space:pre-wrap; word-break:break-all; margin:0; }
 .form-group { display:grid; grid-template-columns:140px 1fr; gap:8px; align-items:center; margin-bottom:10px; font-size:13px; }
 .form-group label { color:#bbb; text-align:right; }
 .form-group input { background:#111; border:1px solid #444; color:#ddd; padding:6px 10px; border-radius:4px; font-size:13px; min-width:0; width:100%; }
 .form-group input:focus { outline:none; border-color:#3b82f6; }
 .btn { background:#3b82f6; border:0; color:#fff; padding:7px 14px; border-radius:4px; cursor:pointer; font-size:13px; }
 .btn:hover { background:#2563eb; }
 .btn-ghost { background:#333; color:#ddd; }
 .btn-ghost:hover { background:#444; }
 .btn-sm { padding:4px 10px; font-size:12px; }
 .btn-group { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
 .alert { padding:10px 14px; border-radius:4px; font-size:13px; margin-bottom:12px; }
 .alert-err { background:#3b0000; border:1px solid #7f1d1d; color:#fca5a5; }
 .topic-list { display:flex; flex-wrap:wrap; gap:4px; }
 .topic-pill { background:#1e293b; color:#94a3b8; padding:2px 8px; border-radius:9px; font-size:11px; font-family:monospace; word-break:break-all; }
 .topic-pill.kind-person { background:#1e3a5f; color:#9bf; }
 .topic-pill.kind-vehicle { background:#0d3b2c; color:#6f6; }
 .topic-pill.kind-line_crossing { background:#5f3a1e; color:#fa4; }
 .topic-pill.kind-motion { background:#2a2a2a; color:#ddd; }
 .topic-pill.kind-audio { background:#3b1e5f; color:#c8a; }
 .topic-pill.kind-face { background:#3b2e1e; color:#f9c; }
 .topic-pill.kind-unknown { background:#1e293b; color:#64748b; opacity:0.7; }
 .topic-legend { font-size:11px; color:#94a3b8; margin-bottom:8px; }
 .topic-legend .pill { font-size:10px; }
 .onvif-row { display:grid; grid-template-columns:1.5fr 1fr 1fr 3fr; gap:8px; padding:8px 0; border-bottom:1px solid #2a2a2a; font-size:13px; align-items:start; word-break:break-word; }
 .onvif-row.header { color:#888; font-weight:600; border-bottom:1px solid #444; align-items:center; }
 .cam-onvif-row { padding:10px 0; border-bottom:1px solid #2a2a2a; }
 .cam-onvif-row:last-child { border-bottom:0; }
 .cam-onvif-head { display:flex; flex-wrap:wrap; gap:10px; align-items:baseline; margin-bottom:6px; }
 .cam-onvif-head .cam-name { font-weight:600; font-size:14px; }
 .cam-onvif-head .cam-host { color:#888; font-size:12px; }
 .cam-onvif-head .cam-status { font-size:11px; padding:2px 8px; border-radius:9px; }
 .cam-onvif-head .cam-status.ok { background:#0d3b2c; color:#6f6; }
 .cam-onvif-head .cam-status.warn { background:#3b2a0d; color:#fa4; }
 .cam-onvif-fields { display:grid; grid-template-columns:1.5fr 1.5fr 0.7fr auto; gap:8px; align-items:center; }
 .cam-onvif-fields input { background:#111; border:1px solid #444; color:#ddd; padding:5px 8px; border-radius:3px; font-size:12px; min-width:0; width:100%; }
 .cam-onvif-fields input:focus { outline:none; border-color:#3b82f6; }
 .cam-onvif-fields input::placeholder { color:#555; font-style:italic; }
 .cam-onvif-topics { margin-top:6px; font-size:11px; }
 @media (max-width: 560px) {
   .cam-onvif-fields { grid-template-columns:1fr; }
 }
 .status-msg { font-size:12px; }
 .status-msg.ok { color:#4c4; }
 .status-msg.err { color:#f87; }
 .table-scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
 .table-scroll > .cam-row, .table-scroll > .setup-row, .table-scroll > .onvif-row { min-width:640px; }

 /* Tablet: tighter padding, allow form labels to stack */
 @media (max-width: 768px) {
   body { padding:12px; }
   .card { padding:12px; }
   .form-group { grid-template-columns:1fr; gap:4px; }
   .form-group label { text-align:left; }
   .form-group input { max-width:none !important; }
 }

 /* Phones: stack table rows into label/value pairs */
 @media (max-width: 560px) {
   body { padding:10px; }
   h1 { font-size:16px; }
   .tab { padding:6px 10px; font-size:13px; }
   .card { padding:10px; }
   .card-header { flex-direction:column; align-items:flex-start; }
   .table-scroll > .cam-row, .table-scroll > .setup-row, .table-scroll > .onvif-row { min-width:0; }
   .cam-row, .setup-row, .onvif-row { grid-template-columns:1fr; gap:2px; padding:8px 0; }
   .cam-row.header, .setup-row.header, .onvif-row.header { display:none; }
   .cam-row > span, .setup-row > span, .onvif-row > span { padding:1px 0; }
   .cam-row > span::before, .setup-row > span::before, .onvif-row > span::before { content: attr(data-label); color:#888; font-size:11px; display:block; text-transform:uppercase; letter-spacing:.5px; }
   .copy-row { flex-wrap:wrap; }
   .copy-row code { flex-basis:100%; }
 }
</style></head><body>
<h1>unifi-ai-camproxy / ONVIF bridge</h1>
<div class="tabs">
  <button class="tab active" data-pane="status">Status</button>
  <button class="tab" data-pane="unifi">UniFi Creds</button>
  <button class="tab" data-pane="onvif">ONVIF Creds</button>
  <button class="tab" data-pane="setup">Alarm Setup</button>
  <button class="tab" data-pane="logs">Logs</button>
</div>

<div id="status" class="pane active">
  <div id="status-error-banner"></div>
  <div class="card">
    <div class="card-header"><h3>Bridge</h3></div>
    <div class="grid" id="bridge-grid"><span class="empty">Loading…</span></div>
  </div>
  <div class="card">
    <div class="card-header">
      <h3>Cameras discovered in Protect</h3>
      <div class="btn-group">
        <button class="btn btn-ghost btn-sm" id="btn-discover" onclick="triggerDiscover()">Get cameras from Protect</button>
        <span class="status-msg" id="discover-msg"></span>
      </div>
    </div>
    <div id="cams-block"><span class="empty">Loading…</span></div>
  </div>
  <div class="card">
    <div class="card-header"><h3>Push activity</h3></div>
    <div class="grid" id="push-grid"><span class="empty">Loading…</span></div>
  </div>
</div>

<div id="unifi" class="pane">
  <div class="card">
    <div class="card-header"><h3>UniFi Protect — connection settings</h3></div>
    <p style="font-size:13px;color:#bbb;margin:0 0 12px;">
      The bridge needs <strong>two separate credentials</strong> against your Protect controller:
    </p>
    <ul style="font-size:13px;color:#bbb;margin:0 0 16px;padding-left:20px;line-height:1.6;">
      <li><strong style="color:#9bf;">Username + Password</strong> → used to <em>discover</em> which ONVIF cameras are adopted in Protect (the "Get cameras from Protect" button)</li>
      <li><strong style="color:#9bf;">API key</strong> → used to <em>fire webhooks</em> into Alarm Manager when ONVIF events arrive</li>
    </ul>
    <div id="unifi-msg"></div>
    <h4 style="margin:0 0 6px;color:#bbb;">Protect host</h4>
    <p style="font-size:12px;color:#888;margin:0 0 8px;">Used by both auth methods below.</p>
    <div class="form-group">
      <label>Host / IP</label>
      <input type="text" id="unifi-host" placeholder="192.168.1.1 or https://unifi.local" autocomplete="off">
    </div>

    <div style="border-top:1px solid #333;margin-top:18px;padding-top:14px;">
      <h4 style="margin:0 0 4px;color:#9bf;">① Camera discovery login</h4>
      <p style="font-size:12px;color:#888;margin:0 0 10px;">
        Used by the <strong>Get cameras from Protect</strong> button to fetch the list of adopted ONVIF cameras.
        Must be a <strong style="color:#bbb;">UniFi OS account</strong> — not a Protect-app-only account.
        Leave password blank to keep the existing saved value.
      </p>
      <div class="form-group">
        <label>Username</label>
        <input type="text" id="unifi-username" placeholder="admin" autocomplete="off">
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="unifi-password" placeholder="(leave blank to keep existing)" autocomplete="new-password">
      </div>
      <div class="btn-group" style="margin-top:8px;">
        <button class="btn btn-ghost btn-sm" id="btn-test-userpass">Test login</button>
        <span class="status-msg" id="test-userpass-result"></span>
      </div>
    </div>

    <div style="border-top:1px solid #333;margin-top:18px;padding-top:14px;">
      <h4 style="margin:0 0 4px;color:#9bf;">② Alarm Manager API key</h4>
      <p style="font-size:12px;color:#888;margin:0 0 10px;">
        Used to fire <strong>webhooks into Alarm Manager</strong> when an ONVIF event arrives.
        Generate in Protect → Settings → Control Plane → Integrations → Create API Key.
        Leave blank to keep the existing saved value.
      </p>
      <div class="form-group">
        <label>API key</label>
        <input type="password" id="unifi-apikey" placeholder="(leave blank to keep existing)" autocomplete="new-password">
      </div>
      <div class="btn-group" style="margin-top:8px;">
        <button class="btn btn-ghost btn-sm" id="btn-test-apikey">Test API key</button>
        <span class="status-msg" id="test-apikey-result"></span>
      </div>
    </div>

    <div class="btn-group" style="margin-top:18px;border-top:1px solid #333;padding-top:14px;">
      <button class="btn" id="btn-save-unifi">Save</button>
      <span class="status-msg" id="save-unifi-result"></span>
    </div>
  </div>
</div>

<div id="onvif" class="pane">
  <div class="card">
    <div class="card-header"><h3>ONVIF — fleet credentials</h3></div>
    <p style="font-size:13px;color:#bbb;margin:0 0 14px;">
      Applied to every camera discovered in Protect unless overridden per-camera.
    </p>
    <div id="onvif-msg"></div>
    <div class="form-group">
      <label>Username</label>
      <input type="text" id="onvif-username" placeholder="admin" autocomplete="off">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" id="onvif-password" placeholder="password" autocomplete="off">
    </div>
    <div class="form-group">
      <label>Port</label>
      <input type="text" id="onvif-port" placeholder="80" style="max-width:100px;">
    </div>
    <div class="btn-group" style="margin-top:14px;">
      <button class="btn" id="btn-save-onvif">Save to config.yml</button>
      <span class="status-msg" id="save-onvif-result"></span>
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h3>Per-camera ONVIF credentials</h3></div>
    <p style="font-size:12px;color:#888;margin:0 0 10px;">
      Override fleet creds for individual cameras. Leave a row blank to inherit
      the fleet defaults above. Saving cancels the live subscription so it
      reconnects with the new credentials within ~60s.
    </p>
    <div class="topic-legend">
      Topic colours show how each ONVIF topic maps to an alarm kind:
      <span class="pill kind-motion">motion</span>
      <span class="pill kind-person">person</span>
      <span class="pill kind-face">face</span>
      <span class="pill kind-vehicle">vehicle</span>
      <span class="pill kind-line_crossing">line_crossing</span>
      <span class="pill kind-audio">audio</span>
      <span class="pill kind-unknown">unknown</span>
      (unknown topics are not pushed to Protect.)
    </div>
    <div id="cam-onvif-block"><span class="empty">Loading…</span></div>
  </div>
</div>

<div id="setup" class="pane">
  <div class="card">
    <div class="card-header"><h3>Configure Protect Alarm Manager rules</h3></div>
    <div style="font-size:13px;color:#bbb;margin-bottom:10px;">
      Protect's integration API doesn't expose alarm-rule CRUD, so each rule must be created
      in the Protect UI. The bridge fires the webhook IDs listed below; create one matching
      alarm rule per row you care about. Rows are filtered to event kinds the camera actually
      advertises via ONVIF — if a camera hasn't enumerated its topics yet, all kinds are shown
      as a fallback. Use the <strong>Fire</strong> checkbox to enable or disable each webhook
      (unchecked = bridge skips it, even if the camera fires events). The <strong>Events</strong>
      column shows how many times each kind has been seen since the bridge started. Active
      template: <code id="setup-template">—</code>.
    </div>
    <h4>One-time setup per row</h4>
    <ol class="steps">
      <li>Open <strong>UniFi Protect</strong> → <strong>Alarm Manager</strong> → <strong>Create Alarm</strong>.</li>
      <li>Set the trigger to <strong>Custom Webhook</strong>.</li>
      <li>Paste the row's webhook ID (Copy button →) into the <em>Trigger ID</em> field.</li>
      <li>Configure actions: push notification, recording extension, etc.</li>
      <li>Save. The status column flips to <span class="ok">firing</span> the next time the bridge sees that kind of event.</li>
    </ol>
  </div>
  <div class="card">
    <div class="card-header"><h3>Webhook IDs</h3></div>
    <div id="setup-table"><span class="empty">Waiting for camera discovery…</span></div>
  </div>
</div>

<div id="logs" class="pane">
  <div class="card">
    <div class="card-header">
      <h3>Container logs</h3>
      <div class="btn-group">
        <label style="margin:0;">Lines:
          <select id="log-lines" style="margin-left:4px;">
            <option value="200">200</option>
            <option value="500" selected>500</option>
            <option value="1000">1000</option>
          </select>
        </label>
        <label style="margin:0;"><input type="checkbox" id="log-auto"> Auto-refresh (3s)</label>
        <button class="btn btn-ghost btn-sm" id="log-refresh">Refresh</button>
        <button class="btn btn-ghost btn-sm" id="log-clear" style="color:#fca5a5;">Clear logs</button>
        <span class="status-msg" id="log-clear-msg"></span>
      </div>
    </div>
    <pre id="log-output">Loading…</pre>
  </div>
</div>

<script>
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function fmtAgo(epoch){if(!epoch)return '—';var s=Math.max(0,Math.floor(Date.now()/1000-epoch));if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function setMsg(id,ok,msg){var el=document.getElementById(id);el.className='status-msg '+(ok?'ok':'err');el.textContent=msg;}
function switchTab(name){var t=document.querySelector('[data-pane="'+name+'"]');if(t)t.click();}

document.querySelectorAll('.tab').forEach(function(t){t.addEventListener('click',function(){
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');});
  document.querySelectorAll('.pane').forEach(function(x){x.classList.remove('active');});
  t.classList.add('active');
  document.getElementById(t.dataset.pane).classList.add('active');
  if(t.dataset.pane==='logs')refreshLogs();
  if(t.dataset.pane==='setup')refreshSetup();
  if(t.dataset.pane==='onvif'){loadOnvif();loadCamOnvif();}
  if(t.dataset.pane==='unifi')loadUnifi();
});});

async function refreshStatus(){
  var banner=document.getElementById('status-error-banner');
  try{
    var resp=await fetch('/api/status');
    if(!resp.ok){
      var errBody='';try{errBody=await resp.text();}catch(_){}
      banner.innerHTML='<div class="alert alert-err">Status endpoint returned HTTP '+resp.status+(errBody?' — <code>'+esc(errBody.slice(0,200))+'</code>':'')+'.<br>Check container logs for details.</div>';
      return;
    }
    var data=await resp.json();
    var b=data.build||{};var bridge=data.bridge||{};
    var btn=document.getElementById('btn-discover');
    var dmsg=document.getElementById('discover-msg');
    if(data.is_discovering){
      btn.disabled=true;btn.textContent='Discovering…';
      window.__wasDiscovering=true;
    }else{
      btn.disabled=false;btn.textContent='Get cameras from Protect';
      // Just-completed: replace "Querying…" with the actual result.
      if(window.__wasDiscovering){
        window.__wasDiscovering=false;
        if(bridge.last_discovery_error){
          dmsg.className='status-msg err';
          dmsg.textContent='Failed: '+bridge.last_discovery_error;
        }else{
          var n=(data.cameras||[]).length;
          dmsg.className='status-msg ok';
          dmsg.textContent='Found '+n+' camera'+(n===1?'':'s')+'.';
          setTimeout(function(){if(dmsg.textContent.indexOf('Found ')===0)dmsg.textContent='';},5000);
        }
      }
    }
    if(bridge.last_discovery_error){
      banner.innerHTML='<div class="alert alert-err">Discovery error: '+esc(bridge.last_discovery_error)+' — <a href="#" onclick="switchTab(&apos;unifi&apos;);return false;" style="color:#fca5a5;">Fix in UniFi tab →</a></div>';
    }else{banner.innerHTML='';}
    document.getElementById('bridge-grid').innerHTML=
      '<div class="row"><span class="label">Image variant</span><span><code>'+esc(data.variant||'onvif')+'</code></span></div>'+
      '<div class="row"><span class="label">Build</span><span>'+esc(b.git_sha_short||'?')+' ('+esc(b.git_ref||'?')+')</span></div>'+
      '<div class="row"><span class="label">Built</span><span>'+esc(b.build_time||'?')+'</span></div>'+
      '<div class="row"><span class="label">Uptime</span><span>'+(data.uptime_seconds||0)+'s</span></div>'+
      '<div class="row"><span class="label">Last discovery</span><span>'+fmtAgo(bridge.last_discovery_epoch)+'</span></div>'+
      '<div class="row"><span class="label">Discovery error</span><span class="'+(bridge.last_discovery_error?'err':'ok')+'">'+esc(bridge.last_discovery_error||'none')+'</span></div>';
    var cams=data.cameras||[];var camsEl=document.getElementById('cams-block');
    if(!cams.length){
      camsEl.innerHTML='<span class="empty">No ONVIF cameras discovered yet. Check <a href="#" onclick="switchTab(&apos;unifi&apos;);return false;" style="color:#888;">UniFi credentials</a>, then click <strong>Get cameras from Protect</strong> above.</span>';
    }else{
      var nConn=0,nAuth=0,nNoCred=0,nConnecting=0;
      cams.forEach(function(c){
        var sub=(data.subscriptions||{})[c.protect_id];
        if(!sub){nNoCred++;}
        else if(sub.auth_locked){nAuth++;}
        else if(sub.is_connected){nConn++;}
        else{nConnecting++;}
      });
      var summary='<div style="font-size:12px;color:#bbb;margin-bottom:10px;">'
        +'<strong>'+cams.length+'</strong> discovered'
        +' · <span class="ok">'+nConn+' connected</span>'
        +(nConnecting?' · <span class="warn">'+nConnecting+' connecting</span>':'')
        +(nAuth?' · <span class="err">'+nAuth+' auth failed</span>':'')
        +(nNoCred?' · <span class="err">'+nNoCred+' no creds</span>':'')
        +'</div>';
      var html=summary+'<div class="table-scroll"><div class="cam-row header"><span>Name</span><span>IP</span><span>Protect state</span><span>ONVIF sub</span><span>Last event</span><span>Kind / topic</span></div>';
      cams.forEach(function(c){
        var sub=(data.subscriptions||{})[c.protect_id];
        var subStat,subCls,subExtra='';
        if(!sub){subStat='no creds';subCls='err';}
        else if(sub.auth_locked){
          subStat='auth failed';subCls='err';
          subExtra=' <a href="#" onclick="switchTab(&apos;onvif&apos;);return false;" style="color:#fca5a5;font-size:11px;">fix →</a>';
        }
        else if(sub.is_connected){subStat='connected';subCls='ok';}
        else{subStat='connecting…';subCls='warn';}
        var errIndicator=(sub&&sub.last_error&&!sub.auth_locked)?'<span class="err" title="'+esc(sub.last_error)+'"> !</span>':'';
        var ev=sub&&sub.last_event;
        var counts=sub&&sub.event_counts||{};
        var totalEvs=Object.values(counts).reduce(function(a,b){return a+b;},0);
        var unknownEvs=counts.unknown||0;
        var lastEv=ev?fmtAgo(ev.timestamp_epoch):(totalEvs?'<span class="warn" title="Events are arriving but none match a known topic — check ONVIF Creds tab for topic strings">'+totalEvs+' event'+(totalEvs===1?'':'s')+', all unclassified</span>':'—');
        var kind=ev?'<span class="pill kind-'+esc(ev.kind)+'">'+esc(ev.kind)+'</span> '+esc((ev.topic||'').slice(0,50)):'';
        html+='<div class="cam-row"><span data-label="Name">'+esc(c.name)+'</span><span data-label="IP"><code>'+esc(c.host||'?')+'</code></span><span data-label="Protect state">'+esc(c.state||'')+'</span><span data-label="ONVIF sub" class="'+subCls+'">'+esc(subStat)+errIndicator+subExtra+'</span><span data-label="Last event">'+lastEv+'</span><span data-label="Kind / topic">'+kind+'</span></div>';
      });html+='</div>';camsEl.innerHTML=html;
    }
    var ps=data.pusher_stats||{};var le=ps.last_event;var lo=ps.last_outcome;
    document.getElementById('push-grid').innerHTML=
      '<div class="row"><span class="label">Alarm triggers OK / failed</span><span><span class="ok">'+(ps.pushes_ok||0)+'</span> / <span class="err">'+(ps.pushes_failed||0)+'</span></span></div>'+
      '<div class="row"><span class="label">Last event</span><span>'+(le?fmtAgo(ps.last_event_epoch)+' — '+esc(le.camera_name)+' / <span class="pill kind-'+esc(le.kind)+'">'+esc(le.kind)+'</span>':'—')+'</span></div>'+
      '<div class="row"><span class="label">Last webhook id</span><span><code>'+esc(lo&&lo.webhook_id||'—')+'</code></span></div>'+
      '<div class="row"><span class="label">Last outcome</span><span class="'+(lo&&lo.ok?'ok':'err')+'">'+(lo?esc(lo.method)+' — '+esc(lo.message||(lo.ok?'OK':'failed')):'—')+'</span></div>';
  }catch(e){
    banner.innerHTML='<div class="alert alert-err">Failed to reach /api/status: '+esc(String(e))+'<br>Is the bridge container running? Check <code>docker compose logs</code>.</div>';
  }
}

async function loadUnifi(){
  try{
    var d=await(await fetch('/api/config/unifi')).json();
    document.getElementById('unifi-host').value=d.host||'';
    document.getElementById('unifi-username').value=d.username||'';
    // Passwords are never pre-filled — leave blank to keep existing saved value.
    // Show a hint when a password is already saved.
    var pwdEl=document.getElementById('unifi-password');
    pwdEl.placeholder=d.has_password?'(saved — leave blank to keep)':'(not set)';
    var keyEl=document.getElementById('unifi-apikey');
    keyEl.placeholder=d.has_api_key?'(saved — leave blank to keep)':'(not set)';
  }catch(e){setMsg('save-unifi-result',false,'Could not load config: '+e);}
}

document.getElementById('btn-test-userpass').addEventListener('click',async function(){
  var host=document.getElementById('unifi-host').value.trim();
  var user=document.getElementById('unifi-username').value.trim();
  var pass=document.getElementById('unifi-password').value;
  setMsg('test-userpass-result',true,'Testing…');
  try{var r=await(await fetch('/api/test/userpass',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({host,username:user,password:pass})})).json();setMsg('test-userpass-result',r.ok,r.message);}catch(e){setMsg('test-userpass-result',false,'Request failed: '+e);}
});

document.getElementById('btn-test-apikey').addEventListener('click',async function(){
  var host=document.getElementById('unifi-host').value.trim();
  var apikey=document.getElementById('unifi-apikey').value.trim();
  setMsg('test-apikey-result',true,'Testing…');
  try{var r=await(await fetch('/api/test/apikey',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({host,api_key:apikey})})).json();setMsg('test-apikey-result',r.ok,r.message);}catch(e){setMsg('test-apikey-result',false,'Request failed: '+e);}
});

document.getElementById('btn-save-unifi').addEventListener('click',async function(){
  var host=document.getElementById('unifi-host').value.trim();
  var user=document.getElementById('unifi-username').value.trim();
  var pass=document.getElementById('unifi-password').value;
  var apikey=document.getElementById('unifi-apikey').value.trim();
  setMsg('save-unifi-result',true,'Saving…');
  try{var r=await(await fetch('/api/config/unifi',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({host,username:user,password:pass,api_key:apikey})})).json();setMsg('save-unifi-result',r.ok,r.message);}catch(e){setMsg('save-unifi-result',false,'Save failed: '+e);}
});

async function loadOnvif(){
  try{
    var d=await(await fetch('/api/config/onvif')).json();
    document.getElementById('onvif-username').value=d.username||'';
    document.getElementById('onvif-password').value=d.password||'';
    document.getElementById('onvif-port').value=d.port||'80';
  }catch(e){setMsg('save-onvif-result',false,'Could not load config: '+e);}
}

document.getElementById('btn-save-onvif').addEventListener('click',async function(){
  var user=document.getElementById('onvif-username').value.trim();
  var pass=document.getElementById('onvif-password').value;
  var port=parseInt(document.getElementById('onvif-port').value)||80;
  setMsg('save-onvif-result',true,'Saving…');
  try{var r=await(await fetch('/api/config/onvif',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:user,password:pass,port})})).json();setMsg('save-onvif-result',r.ok,r.message);}catch(e){setMsg('save-onvif-result',false,'Save failed: '+e);}
});

async function loadCamOnvif(){
  var el=document.getElementById('cam-onvif-block');
  try{
    var cams=await(await fetch('/api/cameras/onvif')).json();
    if(!cams.length){el.innerHTML='<span class="empty">No cameras discovered yet — populates after the first successful Protect discovery.</span>';return;}
    var html='';
    cams.forEach(function(c){
      var statCls=c.auth_locked?'err':(c.is_connected?'ok':'warn');
      var statTxt=c.auth_locked?'auth failed':(c.is_connected?'connected':(c.last_error?'error':'connecting…'));
      var classified=c.supported_topics_classified||c.supported_topics.map(function(t){return {topic:t,kind:'unknown'};});
      var topics=classified.length?classified.map(function(o){return '<span class="topic-pill kind-'+esc(o.kind)+'" title="maps to '+esc(o.kind)+'">'+esc(o.topic)+'</span>';}).join(' '):'<span class="empty">no topics advertised yet</span>';
      var userPh=c.fleet_username?'fleet: '+esc(c.fleet_username):'(fleet creds unset)';
      var portPh=c.fleet_port||80;
      var retryBtn=c.auth_locked?'<button class="btn btn-sm cam-retry" style="background:#7f1d1d;color:#fca5a5;">Retry auth</button>':'';
      html+='<div class="cam-onvif-row" data-pid="'+esc(c.protect_id)+'">'
        +'<div class="cam-onvif-head">'
        +'<span class="cam-name">'+esc(c.name)+'</span>'
        +'<span class="cam-host"><code>'+esc(c.host||'?')+'</code></span>'
        +'<span class="cam-status '+statCls+'">'+esc(statTxt)+'</span>'
        +(c.auth_locked&&c.last_error?'<span class="err" style="font-size:11px;">'+esc(c.last_error.slice(0,80))+'</span>':'')
        +'</div>'
        +'<div class="cam-onvif-fields">'
        +'<input type="text" class="cam-user" value="'+esc(c.override_username||'')+'" placeholder="'+userPh+'" autocomplete="off">'
        +'<input type="password" class="cam-pass" value="'+esc(c.override_password||'')+'" placeholder="(inherit fleet password)" autocomplete="new-password">'
        +'<input type="text" class="cam-port" value="'+esc(c.override_port||'')+'" placeholder="'+portPh+'">'
        +'<button class="btn btn-sm cam-save">Save</button>'
        +retryBtn
        +'</div>'
        +'<div class="cam-onvif-topics"><div class="topic-list">'+topics+'</div></div>'
        +'<div class="status-msg cam-msg" style="margin-top:4px;"></div>'
        +'</div>';
    });el.innerHTML=html;
    el.querySelectorAll('.cam-onvif-row').forEach(function(row){
      var saveBtn=row.querySelector('.cam-save');
      saveBtn.addEventListener('click',async function(){
        var pid=row.dataset.pid;
        var user=row.querySelector('.cam-user').value.trim();
        var pass=row.querySelector('.cam-pass').value;
        var portRaw=row.querySelector('.cam-port').value.trim();
        var port=portRaw?parseInt(portRaw):0;
        var msg=row.querySelector('.cam-msg');
        msg.className='status-msg cam-msg ok';msg.textContent='Saving…';
        try{
          var r=await(await fetch('/api/cameras/onvif',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({protect_id:pid,username:user,password:pass,port:port})})).json();
          msg.className='status-msg cam-msg '+(r.ok?'ok':'err');
          msg.textContent=r.message||(r.ok?'Saved':'Failed');
        }catch(e){msg.className='status-msg cam-msg err';msg.textContent='Save failed: '+e;}
      });
      var retryBtn=row.querySelector('.cam-retry');
      if(retryBtn){retryBtn.addEventListener('click',async function(){
        var pid=row.dataset.pid;
        var msg=row.querySelector('.cam-msg');
        retryBtn.disabled=true;retryBtn.textContent='Retrying…';
        msg.className='status-msg cam-msg ok';msg.textContent='';
        try{
          var r=await(await fetch('/api/cameras/onvif/retry',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({protect_id:pid})})).json();
          msg.className='status-msg cam-msg '+(r.ok?'ok':'err');
          msg.textContent=r.message||(r.ok?'Retrying…':'Failed');
          if(r.ok)setTimeout(loadCamOnvif,2000);
        }catch(e){msg.className='status-msg cam-msg err';msg.textContent='Retry failed: '+e;retryBtn.disabled=false;retryBtn.textContent='Retry auth';}
      });}
    });
  }catch(e){
    el.innerHTML='<div class="alert alert-err">Failed to load camera ONVIF data: '+esc(String(e))+'</div>';
  }
}

async function refreshSetup(){
  var el=document.getElementById('setup-table');
  try{
    var data=await(await fetch('/api/setup')).json();
    document.getElementById('setup-template').textContent=data.webhook_id_template||'—';
    var rows=data.rows||[];
    if(!rows.length){el.innerHTML='<span class="empty">No cameras yet — Setup populates after the first successful discovery.</span>';return;}
    var html='<div class="table-scroll"><div class="setup-row header"><span title="Tick to fire this webhook to Protect">Fire</span><span>Camera</span><span>Kind</span><span>Events</span><span>Webhook ID</span><span>Status</span></div>';
    rows.forEach(function(r){
      var status,statusCls;
      if(r.fires_ok>0){status='firing — last '+fmtAgo(r.last_fire_epoch)+' ('+r.fires_ok+' total)';statusCls='ok';}
      else if(r.fires_failed>0){status='failing — HTTP '+(r.last_status||'?')+' ('+r.fires_failed+' failures)';statusCls='err';}
      else{status='not yet fired';statusCls='label';}
      var evCount=r.events_seen||0;
      var enabled=r.enabled!==false;
      var rowCls=enabled?'setup-row':'setup-row disabled';
      var chk='<input type="checkbox" class="alarm-toggle" data-wid="'+esc(r.webhook_id)+'"'+(enabled?' checked':'')+' title="Tick to fire this webhook to Protect">';
      html+='<div class="'+rowCls+'"><span data-label="Fire">'+chk+'</span><span data-label="Camera">'+esc(r.camera_name)+'</span><span data-label="Kind"><span class="pill kind-'+esc(r.kind)+'">'+esc(r.kind)+'</span></span><span data-label="Events" title="Events of this kind seen since the bridge started">'+evCount+'</span><span data-label="Webhook ID" class="copy-row"><code>'+esc(r.webhook_id)+'</code><button class="copy-btn" data-copy="'+esc(r.webhook_id)+'">Copy</button></span><span data-label="Status" class="'+statusCls+'">'+esc(status)+'</span></div>';
    });html+='</div>';el.innerHTML=html;
    el.querySelectorAll('.copy-btn').forEach(function(b){b.addEventListener('click',async function(){try{await navigator.clipboard.writeText(b.dataset.copy);b.classList.add('copied');var prev=b.textContent;b.textContent='Copied!';setTimeout(function(){b.classList.remove('copied');b.textContent=prev;},1200);}catch(e){var range=document.createRange();range.selectNode(b.previousElementSibling);window.getSelection().removeAllRanges();window.getSelection().addRange(range);}});});
    el.querySelectorAll('.alarm-toggle').forEach(function(cb){cb.addEventListener('change',async function(){
      var wid=cb.dataset.wid;var en=cb.checked;cb.disabled=true;
      try{var resp=await fetch('/api/alarms/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({webhook_id:wid,enabled:en})});var j=await resp.json();
        if(!j.ok){cb.checked=!en;alert('Toggle failed: '+(j.message||'unknown'));}
        else{var row=cb.closest('.setup-row');if(row){row.classList.toggle('disabled',!en);}}
      }catch(e){cb.checked=!en;alert('Toggle failed: '+e);}
      finally{cb.disabled=false;}
    });});
  }catch(e){
    el.innerHTML='<div class="alert alert-err">Failed to load setup data: '+esc(String(e))+'</div>';
  }
}

async function refreshLogs(){var lines=document.getElementById('log-lines').value;try{var resp=await fetch('/api/logs?lines='+encodeURIComponent(lines));var text=await resp.text();document.getElementById('log-output').textContent=text||'(empty)';}catch(e){document.getElementById('log-output').textContent='Failed to load logs: '+e;}}

document.getElementById('log-refresh').addEventListener('click',refreshLogs);
var logTimer;
document.getElementById('log-auto').addEventListener('change',function(e){clearInterval(logTimer);if(e.target.checked)logTimer=setInterval(refreshLogs,3000);});

document.getElementById('log-clear').addEventListener('click',async function(){
  if(!confirm('Truncate the bridge log file? This deletes all log history.'))return;
  var msg=document.getElementById('log-clear-msg');
  msg.className='status-msg ok';msg.textContent='Clearing…';
  try{
    var r=await(await fetch('/api/logs',{method:'DELETE'})).json();
    msg.className='status-msg '+(r.ok?'ok':'err');
    msg.textContent=r.message||'';
    if(r.ok)refreshLogs();
    setTimeout(function(){msg.textContent='';},3000);
  }catch(e){msg.className='status-msg err';msg.textContent='Failed: '+e;}
});

async function triggerDiscover(){
  var btn=document.getElementById('btn-discover');
  var msg=document.getElementById('discover-msg');
  btn.disabled=true;btn.textContent='Querying…';
  msg.className='status-msg ok';msg.textContent='';
  try{
    var r=await(await fetch('/api/discover',{method:'POST'})).json();
    msg.className='status-msg '+(r.ok?'ok':'err');
    msg.textContent=r.message||'';
  }catch(e){
    msg.className='status-msg err';msg.textContent='Request failed: '+e;
    btn.disabled=false;btn.textContent='Get cameras from Protect';
  }
}

refreshStatus();
setInterval(refreshStatus,3000);
setInterval(function(){if(document.querySelector('[data-pane="setup"].active'))refreshSetup();},5000);
setInterval(function(){
  if(!document.querySelector('[data-pane="onvif"].active'))return;
  var ae=document.activeElement;
  if(ae&&document.getElementById('cam-onvif-block').contains(ae))return;
  loadCamOnvif();
},5000);
</script></body></html>"""


class BridgeWebTool:
    """Five-tab web app: Status, UniFi Creds, ONVIF Creds, Setup, Logs."""

    def __init__(self, config: dict, state_provider: Callable[[], dict],
                 trigger_discovery: Callable[[], None] | None = None,
                 pusher=None):
        self.config = config
        self._state_provider = state_provider
        self._trigger_discovery = trigger_discovery or (lambda: None)
        self._pusher = pusher  # used by /api/alarms/toggle
        self._start_time = time.monotonic()
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/status", self._status)
        self.app.router.add_get("/api/setup", self._setup)
        self.app.router.add_post("/api/alarms/toggle", self._post_alarm_toggle)
        self.app.router.add_get("/api/logs", self._logs)
        self.app.router.add_delete("/api/logs", self._clear_logs)
        self.app.router.add_post("/api/discover", self._post_discover)
        self.app.router.add_post("/api/cameras/onvif/retry", self._post_camera_retry)
        self.app.router.add_get("/api/cameras/topics", self._camera_topics)
        self.app.router.add_get("/api/cameras/onvif", self._get_camera_onvif)
        self.app.router.add_post("/api/cameras/onvif", self._post_camera_onvif)
        self.app.router.add_get("/api/config/unifi", self._get_unifi)
        self.app.router.add_post("/api/config/unifi", self._post_unifi)
        self.app.router.add_get("/api/config/onvif", self._get_onvif)
        self.app.router.add_post("/api/config/onvif", self._post_onvif)
        self.app.router.add_post("/api/test/userpass", self._test_userpass)
        self.app.router.add_post("/api/test/apikey", self._test_apikey)

    async def _index(self, _: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(dict(self.config), f, default_flow_style=False,
                         allow_unicode=True)
        except OSError as exc:
            raise RuntimeError(f"Could not write config: {exc}") from exc

    async def _get_unifi(self, _: web.Request) -> web.Response:
        cfg = self.config.get("unifi") or {}
        return web.json_response({
            "host": cfg.get("host", ""),
            "username": cfg.get("username", ""),
            # Never send secrets back to the browser — just tell it whether
            # a value is saved so the placeholder can say "(saved)".
            "has_password": bool(cfg.get("password", "")),
            "has_api_key": bool(cfg.get("api_key", "")),
        })

    async def _post_unifi(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        cfg = self.config.setdefault("unifi", {})
        if not isinstance(cfg, dict):
            self.config["unifi"] = cfg = {}
        # Always update host and username (they're not secret).
        cfg["host"] = str(body.get("host", "")).strip()
        cfg["username"] = str(body.get("username", "")).strip()
        # Only overwrite password / api_key when the field was actually filled in —
        # blank means "keep existing saved value".
        password = str(body.get("password", ""))
        if password:
            cfg["password"] = password
        api_key = str(body.get("api_key", "")).strip()
        if api_key:
            cfg["api_key"] = api_key
        try:
            self._save_config()
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        return web.json_response({
            "ok": True,
            "message": "Saved. Use 'Get cameras from Protect' on the Status tab to discover cameras.",
        })

    async def _get_onvif(self, _: web.Request) -> web.Response:
        cfg = self.config.get("onvif") or {}
        return web.json_response({
            "username": cfg.get("username", ""),
            "password": cfg.get("password", ""),
            "port": cfg.get("port", 80),
        })

    async def _post_onvif(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        cfg = self.config.setdefault("onvif", {})
        if not isinstance(cfg, dict):
            self.config["onvif"] = cfg = {}
        cfg["username"] = str(body.get("username", "")).strip()
        cfg["password"] = str(body.get("password", ""))
        cfg["port"] = int(body.get("port", 80))
        try:
            self._save_config()
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": str(exc)})

        # Cancel subscriptions for cameras that don't have their own
        # override — they're using the fleet creds we just changed.
        overrides = self._camera_overrides_map()
        affected = [
            pid for pid in (self._state_provider().get("subscription_tasks") or {})
            if not (
                overrides.get(pid, {}).get("onvif_username")
                and overrides.get(pid, {}).get("onvif_password")
            )
        ]
        self._cancel_subscriptions(affected)

        return web.json_response({
            "ok": True,
            "message": (
                f"Saved. Resubscribing {len(affected)} camera(s) with new fleet creds…"
                if affected else
                "Saved. Will apply to newly discovered cameras."
            ),
        })

    async def _test_userpass(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        # Fall back to saved values when the form fields are blank (the form
        # never pre-fills passwords, so "Test login" with no edits should
        # test the saved credentials).
        saved = self.config.get("unifi") or {}
        host = str(body.get("host", "")).strip() or str(saved.get("host", "")).strip()
        username = str(body.get("username", "")).strip() or str(saved.get("username", "")).strip()
        password = str(body.get("password", "")) or str(saved.get("password", ""))
        if not host or not username or not password:
            return web.json_response({
                "ok": False,
                "message": "host, username, and password are required (and none saved)",
            })
        base = host if host.startswith("http") else f"https://{host}"
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(connector=connector,
                                           timeout=timeout) as session:
                async with session.post(
                    f"{base}/api/auth/login",
                    json={"username": username, "password": password},
                ) as r:
                    if r.status in (200, 201):
                        return web.json_response({
                            "ok": True,
                            "message": f"Login successful (HTTP {r.status})",
                        })
                    text = (await r.text())[:200]
                    return web.json_response({
                        "ok": False,
                        "message": f"Login failed: HTTP {r.status}",
                    })
        except Exception as exc:
            return web.json_response({
                "ok": False,
                "message": f"Connection error: {exc}",
            })

    async def _test_apikey(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        saved = self.config.get("unifi") or {}
        host = str(body.get("host", "")).strip() or str(saved.get("host", "")).strip()
        api_key = str(body.get("api_key", "")).strip() or str(saved.get("api_key", "")).strip()
        if not host or not api_key:
            return web.json_response({
                "ok": False,
                "message": "host and api_key are required (and none saved)",
            })
        base = host if host.startswith("http") else f"https://{host}"
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(connector=connector,
                                           timeout=timeout) as session:
                async with session.get(
                    f"{base}/proxy/protect/integration/v1/cameras",
                    headers={"X-API-Key": api_key},
                ) as r:
                    if r.status in (200, 201):
                        return web.json_response({
                            "ok": True,
                            "message": f"API key valid (HTTP {r.status})",
                        })
                    if r.status in (401, 403):
                        return web.json_response({
                            "ok": False,
                            "message": f"API key rejected (HTTP {r.status})",
                        })
                    text = (await r.text())[:200]
                    return web.json_response({
                        "ok": False,
                        "message": f"Unexpected: HTTP {r.status}",
                    })
        except Exception as exc:
            return web.json_response({
                "ok": False,
                "message": f"Connection error: {exc}",
            })

    async def _camera_topics(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        subs = state.get("subscriptions") or {}
        result = []
        for pid, sub in subs.items():
            result.append({
                "protect_id": pid,
                "name": sub.name,
                "host": sub.onvif_host,
                "is_connected": sub.is_connected,
                "supported_topics": sub.supported_topics or [],
            })
        return web.json_response(result)

    def _camera_overrides_map(self) -> Dict[str, dict]:
        """Index cfg['cameras'] entries by protect_id."""
        out: Dict[str, dict] = {}
        for entry in self.config.get("cameras") or []:
            if isinstance(entry, dict) and entry.get("protect_id"):
                out[entry["protect_id"]] = entry
        return out

    def _cancel_subscriptions(self, protect_ids) -> None:
        """Cancel running subscriptions so _reconcile will re-create them
        on the next discovery cycle with the new credentials.

        Pass None to cancel all (used when fleet creds change).
        """
        state = self._state_provider()
        tasks = state.get("subscription_tasks") or {}
        subs = state.get("subscriptions") or {}
        targets = list(tasks.keys()) if protect_ids is None else list(protect_ids)
        for pid in targets:
            task = tasks.get(pid)
            if task is not None:
                task.cancel()
                tasks.pop(pid, None)
            subs.pop(pid, None)

    async def _get_camera_onvif(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        cams = state.get("discovered_cameras") or []
        subs = state.get("subscriptions") or {}
        overrides = self._camera_overrides_map()
        fleet = self.config.get("onvif") or {}
        result = []
        for cam in cams:
            pid = cam.get("protect_id", "")
            entry = overrides.get(pid, {})
            sub = subs.get(pid)
            topics = (sub.supported_topics if sub else []) or []
            # Pair each topic with the kind it classifies to so the UI
            # can colour it. Helps users see at a glance which topics map
            # to which alarm kind ("motion", "face", etc.).
            classified = [
                {"topic": t, "kind": classify_topic(t)} for t in topics
            ]
            result.append({
                "protect_id": pid,
                "name": cam.get("name", ""),
                "host": cam.get("host", ""),
                "override_username": entry.get("onvif_username", "") or "",
                "override_password": entry.get("onvif_password", "") or "",
                "override_port": entry.get("onvif_port") if entry.get("onvif_port") else None,
                "fleet_username": fleet.get("username", ""),
                "fleet_port": int(fleet.get("port", 80)),
                "is_connected": bool(sub and sub.is_connected),
                "auth_locked": bool(sub and sub.auth_locked),
                "last_error": sub.last_error if sub else "",
                "supported_topics": topics,
                "supported_topics_classified": classified,
            })
        return web.json_response(result)

    async def _post_camera_onvif(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        pid = str(body.get("protect_id", "")).strip()
        if not pid:
            return web.json_response({"ok": False, "message": "protect_id required"})
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        port_raw = body.get("port")

        cams = self.config.setdefault("cameras", [])
        if not isinstance(cams, list):
            self.config["cameras"] = cams = []

        entry = None
        for e in cams:
            if isinstance(e, dict) and e.get("protect_id") == pid:
                entry = e
                break
        if entry is None:
            entry = {"protect_id": pid}
            cams.append(entry)

        # Empty username + password = clear override (revert to fleet).
        if username:
            entry["onvif_username"] = username
        else:
            entry.pop("onvif_username", None)
        if password:
            entry["onvif_password"] = password
        else:
            entry.pop("onvif_password", None)
        try:
            port_int = int(port_raw) if port_raw not in (None, "", 0) else 0
        except (TypeError, ValueError):
            port_int = 0
        if port_int > 0:
            entry["onvif_port"] = port_int
        else:
            entry.pop("onvif_port", None)

        # If the entry has nothing left except protect_id, drop it so
        # the file stays tidy.
        if list(entry.keys()) == ["protect_id"]:
            cams.remove(entry)

        try:
            self._save_config()
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": str(exc)})

        # Cancel the live subscription for this camera and immediately
        # trigger discovery so it re-creates with the new credentials.
        self._cancel_subscriptions([pid])
        self._trigger_discovery()

        return web.json_response({
            "ok": True,
            "message": "Saved. Resubscribing with new credentials…",
        })

    async def _post_camera_retry(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        pid = str(body.get("protect_id", "")).strip()
        if not pid:
            return web.json_response({"ok": False, "message": "protect_id required"})
        self._cancel_subscriptions([pid])
        self._trigger_discovery()
        return web.json_response({
            "ok": True,
            "message": "Retrying connection…",
        })

    async def _post_discover(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        if state.get("is_discovering"):
            return web.json_response({
                "ok": True,
                "message": "Discovery already in progress…",
            })
        self._trigger_discovery()
        return web.json_response({
            "ok": True,
            "message": "Querying Protect for cameras…",
        })

    async def _status(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        subs = state.get("subscriptions") or {}
        sub_payload: Dict[str, dict] = {}
        for pid, sub in subs.items():
            ev = sub.last_event
            sub_payload[pid] = {
                "is_connected": sub.is_connected,
                "auth_locked": sub.auth_locked,
                "consecutive_failures": sub.consecutive_failures,
                "last_pull_epoch": sub.last_pull_epoch,
                "last_error": sub.last_error,
                "event_counts": dict(sub.event_counts),
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
            "is_discovering": bool(state.get("is_discovering")),
            "cameras": state.get("discovered_cameras", []),
            "subscriptions": sub_payload,
            "pusher_stats": ps_payload,
            "bridge": {
                "last_discovery_error": state.get("last_discovery_error", ""),
                "last_discovery_epoch": state.get("last_discovery_epoch", 0),
            },
        })

    async def _setup(self, _: web.Request) -> web.Response:
        state = self._state_provider()
        template = state.get("webhook_id_template") or DEFAULT_WEBHOOK_TEMPLATE
        cams = state.get("discovered_cameras", []) or []
        subs = state.get("subscriptions") or {}
        ps = state.get("pusher_stats")
        wstats: dict = getattr(ps, "webhook_stats", {}) if ps is not None else {}
        rows = []
        for cam in cams:
            protect_id = cam.get("protect_id", "")
            name = cam.get("name", "")
            # Filter the kind list to what this camera actually advertises
            # via ONVIF GetEventProperties. If the subscription hasn't yet
            # enumerated topics (still connecting, restricted to admins, or
            # the camera doesn't implement GetEventProperties), fall back to
            # showing every kind so the user can still wire something up.
            sub = subs.get(protect_id)
            topics = (sub.supported_topics if sub else None) or []
            if topics:
                kinds_for_cam = {classify_topic(t) for t in topics}
                kinds_for_cam.discard("unknown")
                rendered_kinds = [k for k in SUPPORTED_KINDS if k in kinds_for_cam]
                # If the camera's topics classify to nothing we recognise,
                # fall through to all kinds rather than hiding the camera
                # entirely — better to show too much than too little.
                if not rendered_kinds:
                    rendered_kinds = list(SUPPORTED_KINDS)
            else:
                rendered_kinds = list(SUPPORTED_KINDS)
            event_counts = sub.event_counts if sub else {}
            disabled = self._disabled_webhooks_set()
            for kind in rendered_kinds:
                wid = _format_webhook_id(template, protect_id, kind, name)
                ws = wstats.get(wid)
                rows.append({
                    "camera_name": name,
                    "camera_protect_id": protect_id,
                    "kind": kind,
                    "webhook_id": wid,
                    "enabled": wid not in disabled,
                    "events_seen": int(event_counts.get(kind, 0)),
                    "fires_ok": ws.fires_ok if ws else 0,
                    "fires_failed": ws.fires_failed if ws else 0,
                    "last_fire_epoch": ws.last_fire_epoch if ws else 0,
                    "last_status": ws.last_status if ws else 0,
                })
        return web.json_response({
            "webhook_id_template": template,
            "supported_kinds": SUPPORTED_KINDS,
            "rows": rows,
        })

    def _disabled_webhooks_set(self) -> set:
        """Return the set of webhook IDs the user has disabled in the
        Alarm Setup tab. Source of truth is config['alarms']['disabled_webhooks'];
        the pusher mirrors it for the runtime check."""
        alarms = self.config.get("alarms") or {}
        return set(alarms.get("disabled_webhooks") or [])

    async def _post_alarm_toggle(self, request: web.Request) -> web.Response:
        """Enable or disable an individual webhook ID.

        Body: ``{"webhook_id": "...", "enabled": true|false}``.
        Mirrors the change into the running pusher so the next event is
        either fired or skipped immediately, and persists to config.yml.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "invalid JSON"},
                                    status=400)
        wid = str(body.get("webhook_id", "")).strip()
        if not wid:
            return web.json_response({"ok": False,
                                      "message": "webhook_id is required"})
        enabled = bool(body.get("enabled", True))

        alarms = self.config.setdefault("alarms", {})
        if not isinstance(alarms, dict):
            self.config["alarms"] = alarms = {}
        disabled = set(alarms.get("disabled_webhooks") or [])
        if enabled:
            disabled.discard(wid)
        else:
            disabled.add(wid)
        alarms["disabled_webhooks"] = sorted(disabled)
        if self._pusher is not None:
            self._pusher.disabled_webhooks = disabled
        try:
            self._save_config()
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        return web.json_response({"ok": True, "enabled": enabled})

    async def _logs(self, request: web.Request) -> web.Response:
        try:
            lines = int(request.query.get("lines", "500"))
        except ValueError:
            lines = 500
        lines = max(50, min(lines, 5000))
        log_path = Path("/config/camproxy.log")
        if not log_path.exists():
            return web.Response(text="(no log file yet)",
                              content_type="text/plain")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-lines:]
        except OSError as exc:
            return web.Response(text=f"(could not read log: {exc})",
                              content_type="text/plain")
        redacted = "".join(_RTSP_PWD_RE.sub(r"\1***\3", line) for line in tail)
        return web.Response(text=redacted, content_type="text/plain")

    async def _clear_logs(self, _: web.Request) -> web.Response:
        log_path = Path("/config/camproxy.log")
        if not log_path.exists():
            return web.json_response({"ok": True, "message": "No log file."})
        try:
            # Truncate in place so any open file handle (the running logger)
            # keeps writing into the same inode without errors.
            with open(log_path, "w", encoding="utf-8") as f:
                f.truncate(0)
        except OSError as exc:
            return web.json_response({"ok": False,
                                      "message": f"Could not clear log: {exc}"})
        logger.info("Log file cleared via web UI")
        return web.json_response({"ok": True, "message": "Logs cleared."})

    async def run(self, port: int) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web UI: http://0.0.0.0:%d/", port)
        while True:
            await asyncio.sleep(3600)
