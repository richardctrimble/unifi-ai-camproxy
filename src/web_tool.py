"""
web_tool.py — embedded web UI for camera configuration and virtual line drawing.

Tabs:
    Status — live dashboard: connection info, inference device, CPU/GPU load, per-camera stats
    Setup  — add/edit/remove cameras + per-camera AI settings, save to config.yml
    Lines  — draw virtual crossing lines on live frames, save directly to config.yml

Endpoints:
    GET  /                       single-page HTML (tabbed UI)
    GET  /api/cameras            JSON list of configured cameras
    GET  /api/frame/<name>       current JPEG from that camera's AIEngine
    GET  /api/config             full camera + AI config (passwords stripped)
    POST /api/config             write cameras + AI settings back to config.yml
    GET  /api/lines/<name>       existing lines for a camera
    POST /api/lines/<name>       save a new line to a camera's config
    DELETE /api/lines/<name>/<idx>  remove a line by index
    POST /api/test-rtsp          test an RTSP URL (returns {"ok": bool, "message": str})
    GET  /api/status             live system status JSON (polled by the Status tab)

After saving, the UI shows "Restart the container to apply changes."
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import platform
import re
import subprocess
import threading
import time
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

import cv2
import numpy as np
import yaml
import aiohttp
from aiohttp import web

from ai_engine import list_supported_devices, probe_available_devices
from build_info import get_build_info

if TYPE_CHECKING:
    from unifi_client import AIPortCamera

logger = logging.getLogger("web_tool")
_RTSP_CAPTURE_OPTIONS_LOCK = threading.Lock()

_VALID_DIRECTIONS = frozenset(
    {"both", "left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"}
)

_VALID_DEVICES = frozenset(list_supported_devices())

_VALID_RTSP_TRANSPORTS = frozenset({"tcp", "udp"})

# ─── HTML (single file, no external resources) ──────────────────────────────


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>unifi-ai-camproxy</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
      margin: 0; padding: 16px;
      background: #1a1a1a; color: #e0e0e0;
    }
    h1 { font-size: 18px; margin: 0 0 12px 0; font-weight: 600; }
    h3 { font-size: 14px; margin: 16px 0 8px 0; color: #aaa; }

    /* Tabs */
    .tabs { display: flex; gap: 0; margin-bottom: 16px; border-bottom: 2px solid #333; }
    .tab {
      padding: 8px 20px; cursor: pointer; font-size: 14px; font-weight: 500;
      color: #888; border-bottom: 2px solid transparent; margin-bottom: -2px;
      background: none; border-top: none; border-left: none; border-right: none;
      font-family: inherit;
    }
    .tab:hover { color: #ccc; }
    .tab.active { color: #4af; border-bottom-color: #4af; }
    .pane { display: none; }
    .pane.active { display: block; }

    /* Forms */
    select, input, button, textarea {
      font-size: 14px; padding: 6px 10px;
      background: #2a2a2a; color: #e0e0e0;
      border: 1px solid #444; border-radius: 4px;
      font-family: inherit;
    }
    button { cursor: pointer; }
    button:hover { background: #3a3a3a; }
    button:active { background: #444; }
    label { font-size: 13px; color: #aaa; display: block; margin-bottom: 4px; }
    .hint { color: #888; font-size: 13px; margin: 8px 0 12px 0; }
    .example { font-size: 11px; color: #666; margin-top: 2px; }

    /* Cards */
    .card {
      background: #222; border: 1px solid #333; border-radius: 6px;
      padding: 16px; margin-bottom: 12px;
    }
    .card-header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 12px;
    }
    .card-header h3 { margin: 0; color: #e0e0e0; font-size: 15px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
    .grid.full { grid-template-columns: 1fr; }
    .field { margin-bottom: 8px; }
    .field input, .field select { width: 100%; }
    .field input[type="checkbox"] { width: auto; }
    .field-row { display: flex; align-items: center; gap: 8px; }

    /* Buttons */
    .btn-primary { background: #2563eb; border-color: #2563eb; color: #fff; }
    .btn-primary:hover { background: #1d4ed8; }
    .btn-danger { background: #dc2626; border-color: #dc2626; color: #fff; }
    .btn-danger:hover { background: #b91c1c; }
    .btn-sm { padding: 4px 10px; font-size: 13px; }

    /* Banner */
    .banner {
      background: #1e3a5f; border: 1px solid #2563eb; border-radius: 4px;
      padding: 10px 16px; margin-bottom: 12px; display: none; font-size: 14px;
    }
    .banner.warn { background: #5f3a1e; border-color: #eb8225; }

    /* Line tool */
    .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
    .stage {
      position: relative; display: inline-block; max-width: 100%;
      border-radius: 4px; overflow: hidden; background: #000; line-height: 0;
    }
    .stage img {
      display: block; max-width: 100%; max-height: 65vh;
      height: auto; cursor: crosshair;
      user-select: none; -webkit-user-select: none;
    }
    .stage svg {
      position: absolute; inset: 0; width: 100%; height: 100%;
      pointer-events: none;
    }
    .existing  { stroke: #888; stroke-width: 2; stroke-dasharray: 6 4; fill: none; }
    .draft     { stroke: #4af; stroke-width: 3; fill: none; }
    .handle    { fill: #4af; stroke: #fff; stroke-width: 1; }
    .empty-msg { color: #666; font-style: italic; padding: 20px; }
    .frame-msg {
      position: absolute; inset: 0; display: none; align-items: center;
      justify-content: center; color: #888; font-size: 13px; text-align: center;
      padding: 16px; background: rgba(0,0,0,0.55); pointer-events: none;
      border-radius: 4px;
    }

    /* Status tab */
    .status-grid {
      display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px;
    }
    .status-item { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #2a2a2a; }
    .status-label { color: #888; font-size: 13px; }
    .status-value { font-size: 14px; font-weight: 500; text-align: right; }
    .cam-row {
      display: grid; grid-template-columns: 2fr 1fr 1.4fr 1.4fr 1fr 1fr 1fr; gap: 8px;
      padding: 8px 0; border-bottom: 1px solid #2a2a2a; font-size: 13px; align-items: center;
    }
    .cam-row.header { color: #888; font-weight: 600; border-bottom: 1px solid #444; }
    .dev-tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 3px;
      background: #333; color: #bbd; }
    .dev-tag.cuda { background: #294; color: #fff; }
    .dev-tag.intel { background: #226; color: #cef; }
    .dev-tag.cpu { background: #533; color: #fda; }
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
    .dot.on  { background: #4c4; }
    .dot.off { background: #f44; }
    .dot.disabled { background: #666; }
    @media (max-width: 600px) {
      .status-grid { grid-template-columns: 1fr; }
      .cam-row { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <h1>unifi-ai-camproxy</h1>

  <div class="tabs">
    <button class="tab active" data-pane="status">Status</button>
    <button class="tab" data-pane="unifi">UniFi</button>
    <button class="tab" data-pane="setup">Setup</button>
    <button class="tab" data-pane="lines">Lines</button>
    <button class="tab" data-pane="logs">Logs</button>
  </div>

  <!-- ═══ STATUS TAB ═══ -->
  <div id="status" class="pane active">
    <div id="lockout-banner" class="banner warn">
      <!-- filled by refreshStatus when auth lockout is active -->
    </div>
    <div class="card">
      <div class="card-header">
        <h3>System</h3>
        <button id="restart-btn" class="btn-danger btn-sm">Restart Container</button>
      </div>
      <div class="status-grid">
        <div class="status-item"><span class="status-label">UniFi Host</span><span id="s-host" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Username</span><span id="s-user" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">API key</span><span id="s-apikey" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Uptime</span><span id="s-uptime" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Memory (RSS)</span><span id="s-mem" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Disk (/config)</span><span id="s-disk" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Last heartbeat</span><span id="s-heartbeat" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Python</span><span id="s-python" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Build</span><span id="s-build" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Built</span><span id="s-build-time" class="status-value">—</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><h3>Adoption</h3></div>
      <div class="status-grid">
        <div class="status-item"><span class="status-label">Auth state</span><span id="s-auth" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Token refreshes</span><span id="s-refresh-count" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Last refresh</span><span id="s-refresh-ok" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Last failure</span><span id="s-refresh-err" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">Current token</span><span id="s-token" class="status-value">—</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><h3>Inference</h3></div>
      <div class="status-grid">
        <div class="status-item"><span class="status-label">Device (first camera)</span><span id="s-device" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">CPU Load (1m)</span><span id="s-cpu" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">GPU</span><span id="s-gpu" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">GPU Utilization</span><span id="s-gpu-util" class="status-value">—</span></div>
        <div class="status-item"><span class="status-label">GPU Memory</span><span id="s-gpu-mem" class="status-value">—</span></div>
      </div>
      <h3 style="margin-top:12px;">Available backends</h3>
      <div id="s-devices" style="font-size:13px;">Loading…</div>
    </div>
    <div class="card">
      <div class="card-header"><h3>Cameras</h3></div>
      <div id="s-cameras"><span class="empty-msg">Loading…</span></div>
    </div>
  </div>

  <!-- ═══ UNIFI TAB ═══ -->
  <div id="unifi" class="pane">
    <div id="unifi-banner" class="banner">
      UniFi credentials saved to config.yml.
    </div>
    <div class="card">
      <div class="card-header"><h3>UniFi Protect connection</h3></div>
      <div class="hint" style="margin-bottom:10px;">
        Credentials for your UDM / UDM Pro / UNVR. Use <em>one</em> of:
        a Protect admin's <strong>API key</strong> (preferred on Protect 7.x —
        Settings → Control Plane → Integrations → Create API Key),
        a local Protect user (username + password),
        or a pre-generated adoption token. Changes are written to <code>config.yml</code>.
      </div>
      <div style="display:grid;grid-template-columns:140px 1fr;gap:8px;align-items:center;max-width:640px;">
        <label>Host / IP</label>
        <input id="u-host" placeholder="192.168.1.1">
        <label>API key</label>
        <input id="u-apikey" placeholder="recommended on Protect 7.x">
        <label>Username</label>
        <input id="u-user" placeholder="local Protect username" autocomplete="username">
        <label>Password</label>
        <input id="u-pass" type="password" placeholder="(unchanged if left blank)" autocomplete="new-password">
        <label>Adoption token</label>
        <input id="u-token" placeholder="optional — overrides everything when set">
      </div>
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;">
        <button id="u-test" class="btn-primary">Test login</button>
        <button id="u-save" class="btn-primary">Save &amp; retry adoption</button>
        <span id="u-result" style="align-self:center;font-size:13px;"></span>
      </div>
    </div>
  </div>

  <!-- ═══ SETUP TAB ═══ -->
  <div id="setup" class="pane">
    <div id="save-banner" class="banner">
      Configuration saved. <strong>Restart the container</strong> to apply changes.
    </div>
    <div class="hint">
      Add cameras and configure AI detection settings. Click <strong>Save All</strong> to write to config.yml.
    </div>
    <div id="cameras"></div>
    <div style="display:flex;gap:8px;margin-top:12px;">
      <button id="add-cam" class="btn-primary">+ Add Camera</button>
      <button id="save-all" class="btn-primary">Save All</button>
    </div>
  </div>

  <!-- ═══ LINES TAB ═══ -->
  <div id="lines" class="pane">
    <div id="line-banner" class="banner">
      Line saved to config. <strong>Restart the container</strong> to apply.
    </div>
    <div class="bar">
      <label>Camera:</label>
      <select id="cam"></select>
      <button id="refresh">Refresh frame</button>
      <button id="clear">Clear line</button>
      <label style="margin-bottom:0;"><input type="checkbox" id="auto"> Auto-refresh (2s)</label>
    </div>

    <div class="hint">
      Click two points on the frame to draw a line. Dashed grey = existing lines.
    </div>

    <div class="stage" id="stage">
      <img id="frame" alt="camera frame">
      <svg id="svg" viewBox="0 0 1 1" preserveAspectRatio="none"></svg>
      <div id="frame-msg" class="frame-msg"></div>
    </div>

    <h3>Line properties</h3>
    <div class="bar">
      <div class="field-row">
        <label style="margin:0;">Name:</label>
        <input id="line-name" value="EntryLine" style="width:160px;">
      </div>
      <div class="field-row">
        <label style="margin:0;">Direction:</label>
        <select id="dir">
          <option value="both">both</option>
          <option value="left_to_right">left_to_right</option>
          <option value="right_to_left">right_to_left</option>
          <option value="top_to_bottom">top_to_bottom</option>
          <option value="bottom_to_top">bottom_to_top</option>
        </select>
      </div>
      <button id="save-line" class="btn-primary btn-sm">Save Line</button>
    </div>

    <h3>Existing lines</h3>
    <div id="line-list"></div>
  </div>

  <!-- ═══ LOGS TAB ═══ -->
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
              <option value="5000">5000</option>
            </select>
          </label>
          <label style="margin:0;"><input type="checkbox" id="log-auto"> Auto-refresh (3s)</label>
          <button id="log-refresh" class="btn-sm">Refresh</button>
        </div>
      </div>
      <div id="log-path" style="font-size:12px;color:#666;margin-bottom:8px;"></div>
      <pre id="log-output" style="background:#111;border:1px solid #333;border-radius:4px;
        padding:10px;max-height:65vh;overflow:auto;font-size:12px;line-height:1.4;
        white-space:pre-wrap;word-break:break-all;margin:0;">Loading…</pre>
    </div>
  </div>

<script>
/* ── Tab switching ─────────────────────────────────────────────────────── */
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.pane).classList.add('active');
    if (t.dataset.pane === 'lines') loadLineCameras();
    if (t.dataset.pane === 'status') refreshStatus();
    if (t.dataset.pane === 'unifi') loadUnifiConfig();
    if (t.dataset.pane === 'logs') refreshLogs();
  });
});

/* ── Status tab ────────────────────────────────────────────────────────── */
let statusTimer = null;

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm ' + (s % 60) + 's';
}

function fmtAgo(epoch) {
  if (!epoch) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  return Math.floor(secs / 86400) + 'd ago';
}

document.getElementById('restart-btn').addEventListener('click', async () => {
  if (!confirm('Restart the container? All camera connections will be dropped temporarily.')) return;
  try {
    const resp = await fetch('/api/restart', { method: 'POST' });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('restart-btn').textContent = 'Restarting…';
      document.getElementById('restart-btn').disabled = true;
    } else {
      alert('Restart failed: ' + (data.message || 'unknown error'));
    }
  } catch (e) {
    alert('Restart request failed — the container may already be stopping.');
  }
});

async function refreshStatus() {
  try {
    const data = await (await fetch('/api/status')).json();

    document.getElementById('s-host').textContent = data.unifi_host || '—';
    document.getElementById('s-user').textContent = data.unifi_username || '—';
    document.getElementById('s-apikey').textContent = data.unifi_has_api_key ? 'set' : 'not set';
    document.getElementById('s-uptime').textContent = fmtUptime(data.uptime_seconds || 0);
    document.getElementById('s-mem').textContent =
      data.memory_rss_mb != null ? data.memory_rss_mb.toFixed(1) + ' MB' : '—';

    // Disk — warn under 256 MB free
    const disk = data.disk_config || {};
    const diskEl = document.getElementById('s-disk');
    if (disk.free_mb != null && disk.total_mb != null) {
      diskEl.textContent = disk.free_mb + ' / ' + disk.total_mb + ' MB free';
      diskEl.style.color = disk.free_mb < 256 ? '#f87' : '';
    } else {
      diskEl.textContent = '—';
      diskEl.style.color = '';
    }

    // Heartbeat — warn if it hasn't advanced in ~10 min (interval is 5m)
    const hb = data.heartbeat || {};
    const hbEl = document.getElementById('s-heartbeat');
    if (hb.last_epoch) {
      const age = Math.floor(Date.now() / 1000 - hb.last_epoch);
      hbEl.textContent = fmtAgo(hb.last_epoch);
      hbEl.style.color = age > 600 ? '#f87' : '';
    } else {
      hbEl.textContent = 'not yet';
      hbEl.style.color = '';
    }

    document.getElementById('s-python').textContent = data.python_version || '—';

    // Auth-lockout banner — only visible when Protect has rejected us
    // (bad creds) or rate-limited (429).
    const lockout = data.auth_lockout || {};
    const lockBanner = document.getElementById('lockout-banner');
    if (lockout.active) {
      const m = Math.ceil(lockout.remaining_seconds / 60);
      lockBanner.innerHTML = '<strong>⚠ Auth paused:</strong> ' +
        esc(lockout.reason) + ' — retrying in ~' + m + ' min. ' +
        'Fix credentials in the UniFi tab and click Test login.';
      lockBanner.style.display = 'block';
    } else {
      lockBanner.style.display = 'none';
    }

    // Adoption card — proves token rotation is actually happening.
    const ad = data.adoption || {};
    document.getElementById('s-auth').textContent =
      lockout.active ? 'Paused (' + Math.ceil(lockout.remaining_seconds / 60) + ' min)' : 'OK';
    document.getElementById('s-refresh-count').textContent = (ad.refresh_ok_count != null ? ad.refresh_ok_count : 0);
    document.getElementById('s-refresh-ok').textContent = fmtAgo(ad.last_ok_epoch);
    const errEl = document.getElementById('s-refresh-err');
    if (ad.last_err_epoch) {
      const st = ad.last_err_status ? ' (HTTP ' + ad.last_err_status + ')' : '';
      errEl.textContent = fmtAgo(ad.last_err_epoch) + st;
      errEl.title = ad.last_err_msg || '';
      errEl.style.color = '#f87';
    } else {
      errEl.textContent = 'none';
      errEl.title = '';
      errEl.style.color = '';
    }
    document.getElementById('s-token').textContent = ad.current_token_masked || '—';

    // Build info — so users can tell which image is actually running
    const build = data.build || {};
    const buildEl = document.getElementById('s-build');
    const sha = build.git_sha_short || 'unknown';
    const ref = build.git_ref && build.git_ref !== 'unknown' ? ` (${build.git_ref})` : '';
    if (build.git_sha && build.git_sha !== 'unknown') {
      buildEl.innerHTML = `<a href="https://github.com/richardctrimble/unifi-ai-camproxy/commit/${build.git_sha}" target="_blank" rel="noopener" style="color:inherit;">${sha}${ref}</a>`;
    } else {
      buildEl.textContent = sha + ref;
    }
    document.getElementById('s-build-time').textContent = build.build_time || '—';

    // Inference
    const dev = data.inference_device || 'N/A';
    const devEl = document.getElementById('s-device');
    devEl.textContent = dev;
    if (dev.startsWith('cuda')) devEl.style.color = '#4c4';
    else if (dev.startsWith('intel:gpu')) devEl.style.color = '#4af';
    else if (dev === 'cpu') devEl.style.color = '#fa4';
    else devEl.style.color = '';

    document.getElementById('s-cpu').textContent = data.cpu_load != null ? data.cpu_load.toFixed(2) : '—';

    // Available backends
    const catalog = window._deviceCatalog || [];
    if (catalog.length) {
      const rows = catalog.filter(d => d.id !== 'auto').map(d => {
        const color = d.available ? '#4c4' : '#888';
        const check = d.available ? '✓' : '✗';
        return '<div style="padding:3px 0;"><span style="color:' + color + ';display:inline-block;width:16px;">'
          + check + '</span><strong>' + esc(d.label) + '</strong> <span style="color:#888;">' + esc(d.detail || '') + '</span></div>';
      }).join('');
      document.getElementById('s-devices').innerHTML = rows;
    }

    // GPU
    const gpu = data.gpu || {};
    if (gpu.name) {
      document.getElementById('s-gpu').textContent = gpu.name;
      document.getElementById('s-gpu-util').textContent = gpu.utilization_pct != null ? gpu.utilization_pct + '%' : '—';
      document.getElementById('s-gpu-mem').textContent = (gpu.memory_used_mb != null && gpu.memory_total_mb != null)
        ? gpu.memory_used_mb + ' / ' + gpu.memory_total_mb + ' MB' : '—';
    } else if (gpu.devices) {
      document.getElementById('s-gpu').textContent = 'OpenVINO: ' + gpu.devices.join(', ');
      document.getElementById('s-gpu-util').textContent = '—';
      document.getElementById('s-gpu-mem').textContent = '—';
    } else {
      document.getElementById('s-gpu').textContent = 'None detected';
      document.getElementById('s-gpu-util').textContent = '—';
      document.getElementById('s-gpu-mem').textContent = '—';
    }

    // Cameras
    const camDiv = document.getElementById('s-cameras');
    const cams = data.cameras || [];
    if (!cams.length) {
      camDiv.innerHTML = '<span class="empty-msg">No cameras configured.</span>';
    } else {
      let html = '<div class="cam-row header"><span>Name</span><span>Status</span><span>Device</span><span>Inference</span><span>Frames</span><span>Persons</span><span>Vehicles</span></div>';
      for (const c of cams) {
        const hasError = !!c.error;
        const dotClass = c.disabled ? 'disabled' : (hasError ? 'off' : (c.connected ? 'on' : 'off'));
        let label = c.disabled ? 'Disabled' : (c.connected ? 'Connected' : 'Disconnected');
        if (hasError && !c.disabled) label = 'Error';
        // Show reconnect count — flapping cameras stand out.
        if (c.reconnects && !c.disabled) {
          label += ' <span style="color:#fa4;font-size:12px;">×' + c.reconnects + '</span>';
        }

        const dev = c.device_active || c.device_requested || '—';
        let devCls = 'dev-tag';
        if (dev.startsWith('cuda')) devCls += ' cuda';
        else if (dev.startsWith('intel')) devCls += ' intel';
        else if (dev === 'cpu') devCls += ' cpu';
        const devTag = '<span class="' + devCls + '">' + esc(dev) + '</span>';

        const inferMs = c.last_inference_ms != null ? (c.last_inference_ms + ' ms') : '—';

        html += '<div class="cam-row">' +
          '<span><span class="dot ' + dotClass + '"></span>' + esc(c.name) + '</span>' +
          '<span>' + label + '</span>' +
          '<span>' + devTag + '</span>' +
          '<span>' + inferMs + '</span>' +
          '<span>' + (c.frames_captured || 0).toLocaleString() + ' / ' + (c.frames_analysed || 0).toLocaleString() + '</span>' +
          '<span>' + (c.detections_person || 0).toLocaleString() + '</span>' +
          '<span>' + (c.detections_vehicle || 0).toLocaleString() + '</span>' +
          '</div>';
        if (hasError) {
          html += '<div style="grid-column:1/-1;padding:4px 0 8px 14px;font-size:12px;color:#f87;">⚠ ' + esc(c.error) + '</div>';
        }
      }
      camDiv.innerHTML = html;
    }
  } catch (e) {
    console.error('Status fetch failed', e);
  }
}

// Auto-refresh status every 3 seconds when tab is active
(function initStatusPoll() {
  statusTimer = setInterval(() => {
    if (document.querySelector('[data-pane="status"].active')) refreshStatus();
  }, 3000);
})();

// Initial load
refreshStatus();

/* ── Setup tab ─────────────────────────────────────────────────────────── */
const camerasDiv = document.getElementById('cameras');
let cameraData = [];

function deviceOptions(selected) {
  // deviceCatalog is populated from /api/devices on first load; fall back
  // to a fixed list until it arrives so the UI is never blank.
  const catalog = window._deviceCatalog || [
    {id: 'auto', label: 'Auto (recommended)', available: true},
    {id: 'cpu', label: 'CPU', available: true},
    {id: 'cuda', label: 'NVIDIA GPU (CUDA)', available: false},
    {id: 'mps', label: 'Apple Silicon (MPS)', available: false},
    {id: 'intel:gpu', label: 'Intel iGPU/dGPU (OpenVINO)', available: false},
    {id: 'intel:cpu', label: 'Intel CPU (OpenVINO)', available: false},
    {id: 'intel:npu', label: 'Intel NPU (OpenVINO)', available: false},
  ];
  const chosen = selected || 'auto';
  return catalog.map(d => {
    const isChosen = d.id === chosen;
    const tag = d.available ? '' : ' — unavailable';
    const sel = isChosen ? 'selected' : '';
    // Disable unavailable options unless they are already the saved value,
    // so users can see what is configured but cannot pick an unreachable backend.
    const dis = (!d.available && !isChosen) ? 'disabled' : '';
    return `<option value="${esc(d.id)}" ${sel} ${dis}>${esc(d.label)}${tag}</option>`;
  }).join('');
}

function createCameraCard(cam, idx) {
  const ai = cam.ai || {};
  const card = document.createElement('div');
  card.className = 'card';
  const device = ai.device || 'auto';
  const rtspTransport = cam.rtsp_transport || 'tcp';
  card.innerHTML = `
    <div class="card-header">
      <h3>${esc(cam.name || 'Camera ' + (idx + 1))}</h3>
      <button class="btn-danger btn-sm remove-cam" data-idx="${idx}">Remove</button>
    </div>
    <div class="grid">
      <div class="field">
        <label>Name</label>
        <input data-key="name" value="${esc(cam.name || '')}" placeholder="e.g. Front Door">
        <div class="example">Display name used in UniFi Protect</div>
      </div>
      <div class="field">
        <label>RTSP URL</label>
        <div class="field-row">
          <input data-key="rtsp_url" value="${esc(cam.rtsp_url || '')}" placeholder="rtsp://user:pass@192.168.1.50:554/stream1" style="flex:1;">
          <button class="btn-sm test-rtsp-btn" type="button">Test</button>
        </div>
        <div class="example">e.g. rtsp://admin:password@192.168.1.50:554/stream1</div>
        <div class="rtsp-status" style="font-size:12px;margin-top:3px;"></div>
      </div>
      <div class="field">
        <label>Snapshot URL (optional)</label>
        <input data-key="snapshot_url" value="${esc(cam.snapshot_url || '')}" placeholder="http://192.168.1.50/snap.jpeg">
        <div class="example">HTTP still — fallback when no frame is available</div>
      </div>
      <div class="field">
        <label>RTSP Transport</label>
        <select data-key="rtsp_transport">
          <option value="tcp" ${rtspTransport === 'tcp' ? 'selected' : ''}>tcp (reliable, default)</option>
          <option value="udp" ${rtspTransport === 'udp' ? 'selected' : ''}>udp (lower latency, may drop)</option>
        </select>
        <div class="example">Use udp only if your camera/network prefers it</div>
      </div>
    </div>

    <h3 style="margin-top:16px;">AI detection</h3>
    <div class="grid">
      <div class="field">
        <label>Inference Device</label>
        <select data-key="ai.device">${deviceOptions(device)}</select>
        <div class="example">Per-camera override. "Auto" picks the fastest reachable.</div>
      </div>
      <div class="field">
        <label>Model</label>
        <select data-key="ai.model">
          <option value="yolov8n.pt" ${ai.model === 'yolov8s.pt' || ai.model === 'yolov8m.pt' ? '' : 'selected'}>yolov8n.pt — fastest</option>
          <option value="yolov8s.pt" ${ai.model === 'yolov8s.pt' ? 'selected' : ''}>yolov8s.pt — balanced</option>
          <option value="yolov8m.pt" ${ai.model === 'yolov8m.pt' ? 'selected' : ''}>yolov8m.pt — most accurate</option>
        </select>
      </div>
      <div class="field">
        <label>Frame Skip</label>
        <input data-key="ai.frame_skip" type="number" min="1" max="30" value="${ai.frame_skip ?? 3}" placeholder="3">
        <div class="example">Analyse every Nth frame. Higher = less CPU.</div>
      </div>
      <div class="field">
        <label>Confidence (fallback)</label>
        <input data-key="ai.confidence" type="number" min="0" max="1" step="0.05" value="${ai.confidence ?? 0.45}" placeholder="0.45">
        <div class="example">0.0–1.0, used when per-class not set</div>
      </div>
      <div class="field">
        <label>Confidence — Persons</label>
        <input data-key="ai.confidence_person" type="number" min="0" max="1" step="0.05" value="${ai.confidence_person ?? ''}" placeholder="e.g. 0.45">
        <div class="example">Leave blank to use fallback</div>
      </div>
      <div class="field">
        <label>Confidence — Vehicles</label>
        <input data-key="ai.confidence_vehicle" type="number" min="0" max="1" step="0.05" value="${ai.confidence_vehicle ?? ''}" placeholder="e.g. 0.60">
        <div class="example">Vehicles often want stricter threshold</div>
      </div>
      <div class="field">
        <label class="field-row">
          <input type="checkbox" data-key="ai.detect_persons" ${(ai.detect_persons ?? true) ? 'checked' : ''}>
          Detect Persons
        </label>
      </div>
      <div class="field">
        <label class="field-row">
          <input type="checkbox" data-key="ai.detect_vehicles" ${(ai.detect_vehicles ?? true) ? 'checked' : ''}>
          Detect Vehicles
        </label>
      </div>
      <div class="field">
        <label class="field-row">
          <input type="checkbox" data-key="disabled" ${cam.disabled ? 'checked' : ''}>
          Disabled (skip on startup)
        </label>
      </div>
    </div>`;
  card.querySelector('.remove-cam').addEventListener('click', () => {
    cameraData = readCamerasFromDOM();
    cameraData.splice(idx, 1);
    renderCameras();
  });
  card.querySelector('.test-rtsp-btn').addEventListener('click', async () => {
    const rtspInput = card.querySelector('[data-key="rtsp_url"]');
    const transportSel = card.querySelector('[data-key="rtsp_transport"]');
    const statusDiv = card.querySelector('.rtsp-status');
    const rtspUrl = rtspInput.value.trim();
    if (!rtspUrl) { statusDiv.textContent = 'Enter an RTSP URL first.'; statusDiv.style.color = '#f87'; return; }
    statusDiv.textContent = 'Testing…';
    statusDiv.style.color = '#aaa';
    try {
      const resp = await fetch('/api/test-rtsp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rtsp_url: rtspUrl, rtsp_transport: transportSel ? transportSel.value : 'tcp' }),
      });
      const data = await resp.json();
      if (data.ok) {
        statusDiv.textContent = '✓ ' + (data.message || 'Stream reachable');
        statusDiv.style.color = '#4c4';
      } else {
        statusDiv.textContent = '✗ ' + (data.message || 'Stream unreachable');
        statusDiv.style.color = '#f87';
      }
    } catch (e) {
      statusDiv.textContent = '✗ Request failed';
      statusDiv.style.color = '#f87';
    }
  });
  return card;
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/"/g,'&quot;')
    .replace(/</g,'&lt;');
}

function renderCameras() {
  camerasDiv.innerHTML = '';
  if (cameraData.length === 0) {
    camerasDiv.innerHTML = '<div class="empty-msg">No cameras configured. Click "+ Add Camera" to get started.</div>';
    return;
  }
  cameraData.forEach((cam, i) => camerasDiv.appendChild(createCameraCard(cam, i)));
}

function readCamerasFromDOM() {
  const cards = camerasDiv.querySelectorAll('.card');
  const result = [];
  const INTEGER_KEYS = new Set(['ai.frame_skip']);
  cards.forEach(card => {
    const cam = {};
    const ai = {};
    card.querySelectorAll('[data-key]').forEach(el => {
      const key = el.dataset.key;
      let val;
      if (el.type === 'checkbox') val = el.checked;
      else if (el.type === 'number') {
        if (el.value === '' || el.value == null) return; // leave empty numbers unset
        val = INTEGER_KEYS.has(key) ? parseInt(el.value, 10) : parseFloat(el.value);
        if (isNaN(val)) return;
      }
      else val = (typeof el.value === 'string') ? el.value.trim() : el.value;

      // Skip empty string values for optional top-level fields like snapshot_url
      if (val === '' && !key.startsWith('ai.')) return;

      if (key.startsWith('ai.')) {
        ai[key.slice(3)] = val;
      } else {
        cam[key] = val;
      }
    });
    if (Object.keys(ai).length) cam.ai = ai;
    // preserve existing lines
    const orig = cameraData[result.length];
    if (orig && orig.ai && orig.ai.lines && orig.ai.lines.length) {
      if (!cam.ai) cam.ai = {};
      cam.ai.lines = orig.ai.lines;
    }
    result.push(cam);
  });
  return result;
}

document.getElementById('add-cam').addEventListener('click', () => {
  cameraData = readCamerasFromDOM();
  cameraData.push({ name: '', rtsp_url: '', ai: {} });
  renderCameras();
});

document.getElementById('save-all').addEventListener('click', async () => {
  const cameras = readCamerasFromDOM();
  const resp = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cameras }),
  });
  if (resp.ok) {
    cameraData = cameras;
    document.getElementById('save-banner').style.display = 'block';
  } else {
    alert('Save failed: ' + await resp.text());
  }
});

async function loadDeviceCatalog() {
  try {
    const resp = await fetch('/api/devices');
    const data = await resp.json();
    window._deviceCatalog = data.devices || null;
  } catch (_) { /* fall back to built-in list */ }
}

async function loadConfig() {
  try {
    const resp = await fetch('/api/config');
    const data = await resp.json();
    cameraData = data.cameras || [];
  } catch (_) { cameraData = []; }
  renderCameras();
}

/* ── Lines tab ─────────────────────────────────────────────────────────── */
const camSel = document.getElementById('cam');
const frame = document.getElementById('frame');
const svg = document.getElementById('svg');
const frameMsg = document.getElementById('frame-msg');
const lineList = document.getElementById('line-list');
const lineNameInp = document.getElementById('line-name');
const dirSel = document.getElementById('dir');

let pts = [];
let existingLines = [];
let autoTimer = null;
let frameRetryTimer = null;

function showFrameMsg(text) {
  frameMsg.textContent = text;
  frameMsg.style.display = 'flex';
}
function hideFrameMsg() {
  frameMsg.style.display = 'none';
}

frame.addEventListener('load', () => {
  hideFrameMsg();
});
frame.addEventListener('error', () => {
  // Re-fetch via XHR so we can surface the server's actual error text
  // instead of a generic "Waiting for first frame…" when the real issue
  // is e.g. "camera has no rtsp_url" or "RTSP unreachable".
  if (camSel.value) fetchFrameWithDiagnostic(camSel.value);
  clearTimeout(frameRetryTimer);
  if (!autoTimer) {
    frameRetryTimer = setTimeout(() => {
      if (camSel.value && !autoTimer)
        frame.src = `/api/frame/${encodeURIComponent(camSel.value)}?t=${Date.now()}`;
    }, 5000);
  }
});

async function fetchFrameWithDiagnostic(name) {
  // When <img> fires error we don't get the HTTP body — do a parallel
  // fetch() purely to read the diagnostic text the server returned.
  try {
    const resp = await fetch(`/api/frame/${encodeURIComponent(name)}?diag=1&t=${Date.now()}`);
    if (resp.ok) return; // a retry succeeded; the <img> reload will pick it up
    const text = (await resp.text()) || `HTTP ${resp.status}`;
    showFrameMsg(text);
  } catch (_) {
    showFrameMsg('Waiting for first frame… click "Refresh frame" or enable Auto-refresh.');
  }
}

async function loadLineCameras() {
  try {
    const cams = await (await fetch('/api/cameras')).json();
    camSel.innerHTML = cams.map(c => `<option>${c.name}</option>`).join('');
    if (cams.length) await loadLineCamera();
    else {
      frame.src = '';
      hideFrameMsg();
      lineList.innerHTML = '<div class="empty-msg">No cameras configured yet.</div>';
    }
  } catch (_) {}
}

async function loadLineCamera() {
  const name = camSel.value;
  if (!name) return;
  showFrameMsg('Loading frame…');
  frame.src = `/api/frame/${encodeURIComponent(name)}?t=${Date.now()}`;
  try {
    existingLines = await (await fetch(`/api/lines/${encodeURIComponent(name)}`)).json();
  } catch (_) { existingLines = []; }
  pts = [];
  redraw();
  renderLineList();
}

function refreshFrame() {
  if (camSel.value) {
    showFrameMsg('Loading frame…');
    frame.src = `/api/frame/${encodeURIComponent(camSel.value)}?t=${Date.now()}`;
  }
}

function redraw() {
  let out = '';
  for (const l of existingLines) {
    out += `<line class="existing" x1="${l.x1}" y1="${l.y1}" x2="${l.x2}" y2="${l.y2}" vector-effect="non-scaling-stroke"/>`;
  }
  if (pts.length >= 1) {
    out += `<circle class="handle" cx="${pts[0].x}" cy="${pts[0].y}" r="0.01" vector-effect="non-scaling-stroke"/>`;
  }
  if (pts.length === 2) {
    out += `<line class="draft" x1="${pts[0].x}" y1="${pts[0].y}" x2="${pts[1].x}" y2="${pts[1].y}" vector-effect="non-scaling-stroke"/>`;
    out += `<circle class="handle" cx="${pts[1].x}" cy="${pts[1].y}" r="0.01" vector-effect="non-scaling-stroke"/>`;
  }
  svg.innerHTML = out;
}

function renderLineList() {
  if (!existingLines.length) {
    lineList.innerHTML = '<div class="empty-msg">No lines configured for this camera.</div>';
    return;
  }
  lineList.innerHTML = existingLines.map((l, i) => `
    <div class="card" style="padding:8px 12px;margin-bottom:6px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span><strong>${esc(l.name || 'Line ' + i)}</strong> — ${l.direction || 'both'}
          (${Number(l.x1).toFixed(2)},${Number(l.y1).toFixed(2)}) → (${Number(l.x2).toFixed(2)},${Number(l.y2).toFixed(2)})</span>
        <button class="btn-danger btn-sm" onclick="deleteLine(${i})">Delete</button>
      </div>
    </div>`).join('');
}

frame.addEventListener('click', (e) => {
  const rect = frame.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const y = (e.clientY - rect.top) / rect.height;
  if (pts.length >= 2) pts = [];
  pts.push({ x, y });
  redraw();
});

camSel.addEventListener('change', loadLineCamera);
document.getElementById('refresh').addEventListener('click', refreshFrame);
document.getElementById('clear').addEventListener('click', () => { pts = []; redraw(); });
document.getElementById('auto').addEventListener('change', (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) autoTimer = setInterval(refreshFrame, 2000);
});

document.getElementById('save-line').addEventListener('click', async () => {
  if (pts.length !== 2) { alert('Click two points on the frame first.'); return; }
  const name = camSel.value;
  const line = {
    name: lineNameInp.value || 'Line',
    x1: parseFloat(pts[0].x.toFixed(4)),
    y1: parseFloat(pts[0].y.toFixed(4)),
    x2: parseFloat(pts[1].x.toFixed(4)),
    y2: parseFloat(pts[1].y.toFixed(4)),
    direction: dirSel.value,
  };
  const resp = await fetch(`/api/lines/${encodeURIComponent(name)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(line),
  });
  if (resp.ok) {
    pts = [];
    document.getElementById('line-banner').style.display = 'block';
    await loadLineCamera();
  } else {
    alert('Save failed: ' + await resp.text());
  }
});

window.deleteLine = async function(idx) {
  const name = camSel.value;
  const resp = await fetch(`/api/lines/${encodeURIComponent(name)}/${idx}`, { method: 'DELETE' });
  if (resp.ok) {
    document.getElementById('line-banner').style.display = 'block';
    await loadLineCamera();
  } else {
    alert('Delete failed: ' + await resp.text());
  }
};

/* ── UniFi tab ─────────────────────────────────────────────────────────── */
const uHost   = document.getElementById('u-host');
const uApiKey = document.getElementById('u-apikey');
const uUser   = document.getElementById('u-user');
const uPass   = document.getElementById('u-pass');
const uToken  = document.getElementById('u-token');
const uResult = document.getElementById('u-result');
const uBanner = document.getElementById('unifi-banner');

function setUnifiResult(ok, msg) {
  uResult.textContent = (ok ? '✓ ' : '✗ ') + msg;
  uResult.style.color = ok ? '#4c4' : '#f87';
}

async function loadUnifiConfig() {
  try {
    const data = await (await fetch('/api/unifi')).json();
    uHost.value = data.host || '';
    uUser.value = data.username || '';
    uPass.value = '';
    uPass.placeholder = data.has_password ? '(unchanged — leave blank to keep)' : 'Protect password';
    uApiKey.value = '';
    uApiKey.placeholder = data.has_api_key
      ? '(unchanged — leave blank to keep)'
      : 'recommended on Protect 7.x';
    uToken.value = '';
    uToken.placeholder = data.has_token
      ? '(unchanged — leave blank to keep)'
      : 'optional — overrides everything when set';
  } catch (e) {
    setUnifiResult(false, 'Could not load current UniFi config');
  }
}

document.getElementById('u-test').addEventListener('click', async () => {
  setUnifiResult(true, 'Testing…');
  uResult.style.color = '#aaa';
  try {
    const resp = await fetch('/api/test-unifi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: uHost.value.trim(),
        api_key: uApiKey.value.trim(),
        username: uUser.value.trim(),
        password: uPass.value,
      }),
    });
    const data = await resp.json();
    setUnifiResult(!!data.ok, data.message || (data.ok ? 'Login succeeded' : 'Login failed'));
  } catch (e) {
    setUnifiResult(false, 'Request failed');
  }
});

document.getElementById('u-save').addEventListener('click', async () => {
  setUnifiResult(true, 'Saving…');
  uResult.style.color = '#aaa';
  try {
    const resp = await fetch('/api/unifi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: uHost.value.trim(),
        api_key: uApiKey.value.trim(),
        username: uUser.value.trim(),
        password: uPass.value,
        token: uToken.value.trim(),
      }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      setUnifiResult(false, data.message || ('HTTP ' + resp.status));
      return;
    }
    setUnifiResult(true, 'Saved to config.yml');
    uBanner.textContent = data.message || 'Saved. Restart required to re-run adoption.';
    uBanner.style.display = 'block';
    await loadUnifiConfig();
  } catch (e) {
    setUnifiResult(false, 'Request failed');
  }
});

/* ── Logs tab ──────────────────────────────────────────────────────────── */
const logOutput = document.getElementById('log-output');
const logLines = document.getElementById('log-lines');
const logAuto = document.getElementById('log-auto');
const logPath = document.getElementById('log-path');
let logTimer = null;

async function refreshLogs() {
  try {
    const n = logLines.value || '500';
    const resp = await fetch('/api/logs?lines=' + encodeURIComponent(n));
    const data = await resp.json();
    if (!data.ok) {
      logOutput.textContent = data.message || 'Could not read logs.';
      logPath.textContent = '';
      return;
    }
    logPath.textContent = data.path ? ('Source: ' + data.path) : '';
    // Render newest at bottom and auto-scroll to the tail.
    logOutput.textContent = (data.lines || []).join('\n');
    logOutput.scrollTop = logOutput.scrollHeight;
  } catch (e) {
    logOutput.textContent = 'Request failed: ' + e;
  }
}

document.getElementById('log-refresh').addEventListener('click', refreshLogs);
logLines.addEventListener('change', refreshLogs);
logAuto.addEventListener('change', (e) => {
  clearInterval(logTimer);
  if (e.target.checked) logTimer = setInterval(() => {
    if (document.querySelector('[data-pane="logs"].active')) refreshLogs();
  }, 3000);
});

/* ── Init ──────────────────────────────────────────────────────────────── */
(async () => {
  await loadDeviceCatalog();
  await loadConfig();
})();
</script>
</body>
</html>
"""


# ─── Server ─────────────────────────────────────────────────────────────────


def _encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else b""


def _get_config_disk(path: str = "/config") -> dict:
    """Free / total MB for the config volume. Empty dict if unavailable.

    Useful when log rotation falls behind or the user stashes large
    test videos under /config and doesn't realise disk is full.
    """
    import shutil
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "free_mb": usage.free // (1024 * 1024),
            "total_mb": usage.total // (1024 * 1024),
        }
    except OSError:
        return {}


def _get_process_rss_mb() -> Optional[float]:
    """Resident set size for this process, in MB. Linux-first (reads
    /proc/self/status), falls back to resource.getrusage. Returns None
    if we can't tell — keeps the UI graceful on odd platforms."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except (OSError, ValueError):
        pass
    try:
        import resource
        # ru_maxrss is KB on Linux, bytes on macOS — container is Linux.
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (ImportError, OSError):
        return None


def _grab_rtsp_frame(
    rtsp_url: str,
    timeout_s: float = 10.0,
    transport: str = "tcp",
) -> bytes:
    """Open the camera RTSP URL directly and grab one frame as JPEG bytes.

    Runs in a thread-pool executor (blocking).  Returns empty bytes on failure.
    Used as a fallback for the Lines-tab frame endpoint when the Protect
    stream hasn't started yet (e.g. adoption failed, or the user hasn't
    approved the camera in Protect) — we still want them to be able to draw
    lines on a real frame.

    `transport` is "tcp" or "udp"; it's applied via the only knob OpenCV's
    Python bindings expose for per-capture ffmpeg options — the
    `OPENCV_FFMPEG_CAPTURE_OPTIONS` env var — so we guard it with a module
    lock so concurrent callers don't stomp on each other.
    """
    if transport not in _VALID_RTSP_TRANSPORTS:
        transport = "tcp"
    with _RTSP_CAPTURE_OPTIONS_LOCK:
        prev = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_s * 1000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_s * 1000)
        try:
            if not cap.open(rtsp_url):
                return b""
            ret, frame = cap.read()
            if ret and frame is not None:
                return _encode_jpeg(frame)
        except Exception as exc:
            logger.debug("RTSP one-shot grab failed for %s: %s", rtsp_url, exc)
        finally:
            cap.release()
            if prev is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = prev
    return b""


class LineTool:
    """
    Lightweight aiohttp app serving the config + line-drawing UI.
    Holds a reference to the live camera registry (populated by main.py)
    and a path to config.yml for reading/writing.
    """

    def __init__(
        self,
        registry: Dict[str, "AIPortCamera"],
        config: dict,
        config_path: Optional[str] = None,
        error_registry: Optional[Dict[str, str]] = None,
        reconnect_registry: Optional[Dict[str, int]] = None,
        adoption_probe: Optional[Callable[[], dict]] = None,
        lockout_probe: Optional[Callable[[], dict]] = None,
        heartbeat_probe: Optional[Callable[[], dict]] = None,
    ):
        self.registry = registry
        self.config = config
        self.config_path = Path(config_path) if config_path else Path("/config/config.yml")
        self._error_registry = error_registry or {}
        self._reconnect_registry = reconnect_registry or {}
        # Callables injected by main.py that return snapshots of adoption-
        # token refresh stats, the auth cooldown and the heartbeat tick.
        # Kept as callables (not dicts) so the Status tab always sees
        # fresh values on each poll.
        self._adoption_probe = adoption_probe
        self._lockout_probe = lockout_probe
        self._heartbeat_probe = heartbeat_probe
        self._start_time = time.monotonic()
        # Short-lived cache of last-good JPEG per camera so repeated Refresh
        # clicks (and the Lines-tab auto-refresh poller) don't re-open RTSP
        # for every request. Entry: name -> (monotonic_ts, jpeg_bytes).
        self._frame_cache: Dict[str, tuple[float, bytes]] = {}
        self._frame_cache_ttl_s = 3.0
        # Coalesce concurrent grabs for the same camera behind a per-camera
        # lock so a burst of requests only triggers one RTSP open.
        self._frame_locks: Dict[str, asyncio.Lock] = {}
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/cameras", self._list_cameras)
        self.app.router.add_get("/api/frame/{name}", self._get_frame)
        self.app.router.add_get("/api/config", self._get_config)
        self.app.router.add_post("/api/config", self._save_config)
        self.app.router.add_get("/api/lines/{name}", self._get_lines)
        self.app.router.add_post("/api/lines/{name}", self._save_line)
        self.app.router.add_delete("/api/lines/{name}/{idx}", self._delete_line)
        self.app.router.add_post("/api/test-rtsp", self._test_rtsp)
        self.app.router.add_get("/api/status", self._get_status)
        self.app.router.add_get("/api/devices", self._get_devices)
        self.app.router.add_get("/api/logs", self._get_logs)
        self.app.router.add_get("/api/unifi", self._get_unifi)
        self.app.router.add_post("/api/unifi", self._save_unifi)
        self.app.router.add_post("/api/test-unifi", self._test_unifi)
        self.app.router.add_post("/api/restart", self._restart_container)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _reload_config(self) -> dict:
        """Re-read config.yml from disk, keeping the last-good config on error."""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    loaded = yaml.safe_load(f) or {}
                self.config = loaded
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("Failed to reload config from %s: %s", self.config_path, exc)
        return self.config

    def _write_config(self) -> None:
        """Write current self.config back to config.yml atomically."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(self.config, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, self.config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _find_camera_cfg(self, name: str) -> Optional[dict]:
        for cam in self.config.get("cameras", []):
            if cam.get("name") == name:
                return cam
        return None

    # ── handlers ────────────────────────────────────────────────────────────

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def _list_cameras(self, request: web.Request) -> web.Response:
        cams = [{"name": c["name"]} for c in self.config.get("cameras", [])]
        return web.json_response(cams)

    def _find_camera_config(self, name: str) -> Optional[dict]:
        """Return the config.yml entry for `name`, or None."""
        for cam in self.config.get("cameras", []):
            if cam.get("name") == name:
                return cam
        return None

    def _jpeg_response(self, jpeg: bytes) -> web.Response:
        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _get_frame(self, request: web.Request) -> web.Response:
        """Serve a JPEG for the Lines tab.

        Resolution order — we try each source in turn and stop at the first
        one that gives us bytes:

          1. Live frame from AIEngine (if the camera is registered AND its
             capture loop has produced at least one frame — the cheapest
             path, no network IO).
          2. Last snapshot written to disk by the AI capture loop.
          3. The camera's HTTP snapshot_url (the camera's own MJPEG/JPEG
             endpoint — works without Protect being involved at all).
          4. A one-shot direct RTSP grab against the configured rtsp_url
             with the camera's configured rtsp_transport — works even if
             Protect adoption is broken, because we're just reading the
             camera's RTSP stream directly.

        Result is cached for a few seconds so that Refresh spam / the
        auto-refresh poller don't re-open RTSP on every tick.
        """
        name = request.match_info["name"]

        cached = self._frame_cache.get(name)
        if cached and (time.monotonic() - cached[0]) < self._frame_cache_ttl_s:
            return self._jpeg_response(cached[1])

        lock = self._frame_locks.setdefault(name, asyncio.Lock())
        async with lock:
            # Re-check the cache — another request may have filled it while
            # we were waiting for the lock.
            cached = self._frame_cache.get(name)
            if cached and (time.monotonic() - cached[0]) < self._frame_cache_ttl_s:
                return self._jpeg_response(cached[1])

            jpeg = await self._fetch_frame(name)
            if jpeg:
                self._frame_cache[name] = (time.monotonic(), jpeg)
                return self._jpeg_response(jpeg)

        # Nothing worked — tell the user *why* so they can act, rather than
        # the generic "warming up" message which is only true sometimes.
        cam_cfg = self._find_camera_config(name)
        if cam_cfg is None:
            return web.Response(status=404, text=f"no camera named '{name}' in config.yml")
        if not cam_cfg.get("rtsp_url") and not cam_cfg.get("snapshot_url"):
            return web.Response(
                status=503,
                text="camera has no rtsp_url or snapshot_url — add one in Setup",
            )
        return web.Response(
            status=503,
            text=(
                "could not grab a frame from this camera — check that the RTSP URL "
                "is reachable from the container, the credentials are correct, and "
                "the camera is online. Try the 'Test' button on the Setup tab."
            ),
        )

    async def _fetch_frame(self, name: str) -> bytes:
        """Try each frame source in order, return raw JPEG bytes or b''."""
        cam = self.registry.get(name)
        cam_cfg = self._find_camera_config(name) or {}

        # Source 1: live in-memory frame from the AI engine (best quality,
        # zero IO), but only if the camera is registered AND its capture
        # loop has produced at least one frame.
        if cam is not None:
            frame = cam.ai_engine.get_latest_frame()
            if frame is not None:
                jpeg = _encode_jpeg(frame)
                if jpeg:
                    return jpeg

            # Source 2: last snapshot the AI loop wrote to disk.
            try:
                snap_path = await cam.ai_engine.get_snapshot()
            except Exception as exc:
                logger.debug("get_snapshot() failed for %s: %s", name, exc)
                snap_path = None
            if snap_path and snap_path.exists():
                try:
                    data = snap_path.read_bytes()
                    if data:
                        return data
                except OSError as exc:
                    logger.debug("reading snapshot %s failed: %s", snap_path, exc)

        # Source 3: camera's HTTP snapshot URL. Works even before Protect
        # adoption because we're talking to the camera directly.
        snapshot_url = (
            getattr(cam, "snapshot_url", None) if cam is not None else None
        ) or cam_cfg.get("snapshot_url")
        if snapshot_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        snapshot_url,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
                                return data
                        else:
                            logger.debug(
                                "snapshot_url for %s returned HTTP %s",
                                name, resp.status,
                            )
            except Exception as exc:
                logger.debug("snapshot_url GET failed for %s: %s", name, exc)

        # Source 4: one-shot direct RTSP grab. This is the fallback that
        # "always works as long as the camera is reachable" — including
        # when Protect adoption is broken entirely.
        rtsp_url = (
            getattr(cam, "rtsp_url", None) if cam is not None else None
        ) or cam_cfg.get("rtsp_url")
        if rtsp_url:
            transport = cam_cfg.get("rtsp_transport", "tcp")
            if transport not in _VALID_RTSP_TRANSPORTS:
                transport = "tcp"
            loop = asyncio.get_running_loop()
            try:
                jpeg = await loop.run_in_executor(
                    None, _grab_rtsp_frame, rtsp_url, 10.0, transport,
                )
            except Exception as exc:
                logger.debug("RTSP grab executor raised for %s: %s", name, exc)
                jpeg = b""
            if jpeg:
                return jpeg

        return b""

    async def _get_config(self, request: web.Request) -> web.Response:
        """Return camera config without sensitive fields."""
        self._reload_config()
        cameras = []
        for cam in self.config.get("cameras", []):
            safe = copy.deepcopy(cam)
            cameras.append(safe)
        return web.json_response({"cameras": cameras})

    async def _save_config(self, request: web.Request) -> web.Response:
        """Write cameras list back to config.yml, preserving unifi + web_tool."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")

        if not isinstance(body, dict):
            return web.Response(status=400, text="invalid payload: expected a JSON object")

        new_cameras = body.get("cameras", [])
        if not isinstance(new_cameras, list):
            return web.Response(status=400, text="invalid payload: 'cameras' must be a list")

        seen_names: set[str] = set()

        # Clean up + validate per-camera config
        for idx, cam in enumerate(new_cameras):
            if not isinstance(cam, dict):
                return web.Response(
                    status=400,
                    text=f"invalid payload: cameras[{idx}] must be an object",
                )
            name = (cam.get("name") or "").strip()
            if not name:
                return web.Response(
                    status=400,
                    text=f"camera {idx + 1}: name is required",
                )
            if name in seen_names:
                return web.Response(
                    status=400,
                    text=(f"camera {idx + 1}: duplicate name '{name}'. "
                          "Each camera must have a unique name."),
                )
            seen_names.add(name)
            cam["name"] = name

            rtsp_url = (cam.get("rtsp_url") or "").strip()
            if not rtsp_url:
                return web.Response(
                    status=400,
                    text=f"camera '{name}': rtsp_url is required",
                )
            cam["rtsp_url"] = rtsp_url

            if not cam.get("snapshot_url"):
                cam.pop("snapshot_url", None)
            # Remove disabled: false to keep config clean (only store disabled: true)
            if not cam.get("disabled"):
                cam.pop("disabled", None)

            transport = cam.get("rtsp_transport")
            if transport:
                if transport not in _VALID_RTSP_TRANSPORTS:
                    return web.Response(
                        status=400,
                        text=f"invalid rtsp_transport '{transport}' — must be tcp or udp",
                    )
                if transport == "tcp":
                    # tcp is the default — keep config minimal
                    cam.pop("rtsp_transport", None)

            ai = cam.get("ai", {})
            if ai is not None and not isinstance(ai, dict):
                return web.Response(
                    status=400,
                    text=f"invalid payload: cameras[{idx}].ai must be an object",
                )
            if ai:
                device = ai.get("device")
                if device and device not in _VALID_DEVICES:
                    return web.Response(
                        status=400,
                        text=(f"invalid ai.device '{device}' — must be one of "
                              f"{sorted(_VALID_DEVICES)}"),
                    )
                # Remove defaults so the stored config stays short and readable.
                defaults = {
                    "model": "yolov8n.pt",
                    "confidence": 0.45,
                    "frame_skip": 3,
                    "detect_persons": True,
                    "detect_vehicles": True,
                    "device": "auto",
                }
                for key, default in defaults.items():
                    if key in ai and ai[key] == default:
                        del ai[key]
                if not ai or ai == {}:
                    cam.pop("ai", None)

        self._reload_config()
        self.config["cameras"] = new_cameras
        self._write_config()
        logger.info("Config saved via web UI (%d cameras)", len(new_cameras))
        return web.json_response({"ok": True})

    async def _get_lines(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        self._reload_config()
        cam = self._find_camera_cfg(name)
        if cam is None:
            return web.json_response([])
        lines = (cam.get("ai") or {}).get("lines") or []
        return web.json_response(lines)

    async def _save_line(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            line = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")

        if not isinstance(line, dict):
            return web.Response(status=400, text="invalid payload: expected a JSON object")

        for coord in ("x1", "y1", "x2", "y2"):
            val = line.get(coord)
            if val is None:
                return web.Response(status=400, text=f"missing required field: {coord}")
            try:
                fval = float(val)
            except (TypeError, ValueError):
                return web.Response(status=400, text=f"invalid value for {coord}: must be a number")
            if not (0.0 <= fval <= 1.0):
                return web.Response(status=400, text=f"invalid value for {coord}: must be in [0, 1]")
            line[coord] = fval

        direction = line.get("direction", "both")
        if direction not in _VALID_DIRECTIONS:
            return web.Response(
                status=400,
                text=f"invalid direction '{direction}': must be one of {sorted(_VALID_DIRECTIONS)}",
            )

        if not line.get("name"):
            return web.Response(status=400, text="missing required field: name")

        self._reload_config()
        cam = self._find_camera_cfg(name)
        if cam is None:
            return web.Response(status=404, text="camera not found in config")

        if "ai" not in cam:
            cam["ai"] = {}
        if "lines" not in cam["ai"]:
            cam["ai"]["lines"] = []
        cam["ai"]["lines"].append(line)
        self._write_config()
        logger.info("Saved line '%s' for camera '%s'", line.get("name"), name)
        return web.json_response({"ok": True})

    async def _delete_line(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            idx = int(request.match_info["idx"])
        except ValueError:
            return web.Response(status=400, text="invalid index")

        self._reload_config()
        cam = self._find_camera_cfg(name)
        if cam is None:
            return web.Response(status=404, text="camera not found in config")

        lines = (cam.get("ai") or {}).get("lines") or []
        if idx < 0 or idx >= len(lines):
            return web.Response(status=404, text="line index out of range")

        lines.pop(idx)
        self._write_config()
        logger.info("Deleted line %d from camera '%s'", idx, name)
        return web.json_response({"ok": True})

    async def _get_status(self, request: web.Request) -> web.Response:
        """Return live system status for the Status dashboard tab."""
        self._reload_config()
        unifi_cfg = self.config.get("unifi", {}) or {}

        # ── Uptime ────────────────────────────────────────────────────────
        uptime_secs = int(time.monotonic() - self._start_time)

        # ── CPU load (1-min average) ──────────────────────────────────────
        try:
            cpu_load = os.getloadavg()[0]
        except (OSError, AttributeError):
            cpu_load = None

        # ── Inference device (from first live camera's AIEngine) ──────────
        inference_device = "N/A"
        for cam in self.registry.values():
            if hasattr(cam, "ai_engine"):
                inference_device = getattr(cam.ai_engine, "device", "N/A")
                break

        # If no cameras are running yet, try the global AI config hint
        if inference_device == "N/A":
            for cam_cfg in self.config.get("cameras", []):
                ai_cfg = cam_cfg.get("ai", {}) or {}
                requested = ai_cfg.get("device", "auto")
                inference_device = f"{requested} (not started)"
                break

        # ── GPU utilisation (best-effort) ─────────────────────────────────
        gpu_info = self._probe_gpu()

        # ── Per-camera status ─────────────────────────────────────────────
        cam_statuses = []
        now = time.time()
        for cam_cfg in self.config.get("cameras", []):
            name = cam_cfg.get("name", "<unnamed>")
            ai_cfg = cam_cfg.get("ai", {}) or {}
            entry: dict = {
                "name": name,
                "disabled": bool(cam_cfg.get("disabled")),
                "connected": False,
                "frames_captured": 0,
                "frames_analysed": 0,
                "detections_person": 0,
                "detections_vehicle": 0,
                "device_requested": ai_cfg.get("device", "auto"),
                "device_active": None,
                "last_inference_ms": None,
                "last_detection_age_s": None,
                "error": self._error_registry.get(name),
                "reconnects": self._reconnect_registry.get(name, 0),
            }
            live = self.registry.get(name)
            if live and hasattr(live, "ai_engine"):
                eng = live.ai_engine
                entry["connected"] = getattr(eng, "_stream_connected", False)
                entry["frames_captured"] = getattr(eng, "_frames_captured", 0)
                entry["frames_analysed"] = getattr(eng, "_frames_analysed", 0)
                entry["detections_person"] = getattr(eng, "_detections_person", 0)
                entry["detections_vehicle"] = getattr(eng, "_detections_vehicle", 0)
                entry["device_active"] = getattr(eng, "device", None)
                last_ms = getattr(eng, "_last_inference_ms", None)
                if last_ms:
                    entry["last_inference_ms"] = round(float(last_ms), 1)
                last_ts = getattr(eng, "_last_detection_ts", None)
                if last_ts:
                    entry["last_detection_age_s"] = int(now - last_ts)
            cam_statuses.append(entry)

        payload = {
            "unifi_host": unifi_cfg.get("host", ""),
            "unifi_username": unifi_cfg.get("username", ""),
            "unifi_has_api_key": bool(unifi_cfg.get("api_key")),
            "cameras_configured": len(self.config.get("cameras", [])),
            "cameras_running": len(self.registry),
            "inference_device": inference_device,
            "cpu_load": round(cpu_load, 2) if cpu_load is not None else None,
            "memory_rss_mb": _get_process_rss_mb(),
            "disk_config": _get_config_disk(),
            "gpu": gpu_info,
            "uptime_seconds": uptime_secs,
            "python_version": platform.python_version(),
            "cameras": cam_statuses,
            "build": get_build_info(),
            "adoption": self._adoption_probe() if self._adoption_probe else {},
            "auth_lockout": self._lockout_probe() if self._lockout_probe else {},
            "heartbeat": self._heartbeat_probe() if self._heartbeat_probe else {},
        }
        return web.json_response(payload)

    @staticmethod
    def _probe_gpu() -> dict:
        """Best-effort GPU info. Returns empty dict if nothing detected."""
        # Try NVIDIA first (nvidia-smi)
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            ).strip()
            if out:
                parts = [p.strip() for p in out.split(",")]

                def _safe_int(s: str):
                    try:
                        return int(float(s))
                    except (ValueError, TypeError):
                        return None

                return {
                    "type": "nvidia",
                    "name": parts[0] if len(parts) > 0 else "",
                    "utilization_pct": _safe_int(parts[1]) if len(parts) > 1 else None,
                    "memory_used_mb": _safe_int(parts[2]) if len(parts) > 2 else None,
                    "memory_total_mb": _safe_int(parts[3]) if len(parts) > 3 else None,
                }
        except Exception:
            pass

        # Try Intel OpenVINO
        try:
            import openvino as ov
            devices = ov.Core().available_devices
            if devices:
                return {"type": "intel_openvino", "devices": sorted(devices)}
        except Exception:
            pass

        return {}

    async def _test_rtsp(self, request: web.Request) -> web.Response:
        """Try to open an RTSP stream and read one frame; return ok/message."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")

        rtsp_url = body.get("rtsp_url", "")
        transport = (body.get("rtsp_transport") or "tcp").lower()
        if transport not in _VALID_RTSP_TRANSPORTS:
            transport = "tcp"
        if not rtsp_url:
            return web.json_response({"ok": False, "message": "rtsp_url is required"})

        loop = asyncio.get_event_loop()

        def _check_stream(url: str) -> tuple[bool, str]:
            # Hint OpenCV at the desired transport via FFmpeg env var —
            # the OpenCV Python bindings don't take per-capture options,
            # so this is the only portable knob we have.
            with _RTSP_CAPTURE_OPTIONS_LOCK:
                prev = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"
                cap = cv2.VideoCapture(url)
                try:
                    if not cap.isOpened():
                        return False, "Could not open stream"
                    ok, _frame = cap.read()
                    if ok:
                        return True, f"Stream reachable via {transport}"
                    return False, "Stream opened but could not read a frame"
                finally:
                    cap.release()
                    if prev is None:
                        os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
                    else:
                        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = prev

        try:
            ok, message = await asyncio.wait_for(
                loop.run_in_executor(None, _check_stream, rtsp_url),
                timeout=10,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "message": "Timed out after 10 s"})
        except Exception as exc:
            return web.json_response({"ok": False, "message": str(exc)})

        return web.json_response({"ok": ok, "message": message})

    async def _get_devices(self, request: web.Request) -> web.Response:
        """Return the list of inference devices this image can reach.

        The UI uses this to render the per-camera "Inference Device"
        dropdown and flag unreachable devices so users don't pick
        something that's only going to fall back to CPU at runtime.
        """
        probe = probe_available_devices()

        # Label map — we explicitly pin the order so the dropdown is stable.
        labels = [
            ("auto",      "Auto (recommended)"),
            ("cpu",       "CPU"),
            ("cuda",      "NVIDIA GPU (CUDA)"),
            ("intel:gpu", "Intel iGPU/dGPU (OpenVINO)"),
            ("intel:npu", "Intel NPU (OpenVINO)"),
            ("intel:cpu", "Intel CPU (OpenVINO)"),
            ("mps",       "Apple Silicon (MPS)"),
        ]

        devices = []
        for key, label in labels:
            if key == "auto":
                devices.append({"id": "auto", "label": label, "available": True,
                                "detail": "probes at startup"})
                continue
            info = probe.get(key, {"available": False, "detail": "unknown"})
            devices.append({
                "id": key,
                "label": label,
                "available": bool(info.get("available")),
                "detail": info.get("detail", ""),
            })
        return web.json_response({"devices": devices})

    def _redact_log_line(self, line: str) -> str:
        """Redact sensitive values before returning log content to the UI."""
        return re.sub(r"(?i)(rtsp://)([^/\s:@]+):([^@\s]+)@", r"\1***:***@", line)

    async def _get_logs(self, request: web.Request) -> web.Response:
        """Return the last ~500 lines of logs from the rotating log file.

        main._configure_logging() writes to /config/camproxy.log by default
        so this endpoint works out of the box. Values like RTSP credentials
        are redacted before returning to the UI.

        Query params:
          lines=N   — override tail length (default 500, max 5000)
        """
        try:
            n = int(request.query.get("lines", "500"))
        except ValueError:
            n = 500
        n = max(1, min(n, 5000))

        log_path = os.environ.get("LOG_FILE") or "/config/camproxy.log"
        try:
            if not Path(log_path).exists():
                return web.json_response({
                    "ok": False,
                    "message": (f"No log file at {log_path}. Set LOG_FILE to a "
                                "writable path or check /config is mounted."),
                    "lines": [],
                })
            # Read the tail by seeking backwards — cheap even on large files.
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # ~200 bytes/line average × requested tail, capped at 1 MB
                f.seek(max(0, size - min(1_000_000, 200 * n)))
                tail = f.read().decode("utf-8", errors="replace")
            lines = [self._redact_log_line(line) for line in tail.splitlines()[-n:]]
            return web.json_response({"ok": True, "lines": lines, "path": log_path})
        except OSError as exc:
            return web.json_response({"ok": False, "message": str(exc), "lines": []})

    async def _get_unifi(self, request: web.Request) -> web.Response:
        """Return the current UniFi config, with secrets redacted.

        We never send the raw password / token back to the browser — the
        Status tab would then leak them in anyone's browser DevTools.
        Instead the UI gets booleans so it can show "unchanged" placeholders.
        """
        self._reload_config()
        unifi = self.config.get("unifi", {}) or {}
        return web.json_response({
            "host": unifi.get("host", ""),
            "username": unifi.get("username", ""),
            "has_password": bool(unifi.get("password")),
            "has_token": bool(unifi.get("token")),
            "has_api_key": bool(unifi.get("api_key")),
        })

    async def _save_unifi(self, request: web.Request) -> web.Response:
        """Persist UniFi credentials to config.yml.

        Blank password / token fields are treated as "leave unchanged" so
        the user doesn't have to retype the password every time they edit
        the host. Adoption isn't re-run from inside this handler — we tell
        the user to restart via the banner, which is the only reliable way
        to rebuild the unifi-cam-proxy camera processes.
        """
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        if not isinstance(body, dict):
            return web.Response(status=400, text="expected a JSON object")

        host = (body.get("host") or "").strip()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        token = (body.get("token") or "").strip()
        api_key = (body.get("api_key") or "").strip()

        if not host:
            return web.json_response({"ok": False, "message": "Host is required"})

        self._reload_config()
        unifi = self.config.get("unifi", {}) or {}
        unifi["host"] = host
        if username:
            unifi["username"] = username
        else:
            unifi.pop("username", None)
        # Blank = keep existing value; explicit value = overwrite.
        if password:
            unifi["password"] = password
        if token:
            unifi["token"] = token
        if api_key:
            unifi["api_key"] = api_key

        self.config["unifi"] = unifi
        try:
            self._write_config()
        except OSError as exc:
            return web.json_response(
                {"ok": False, "message": f"Could not write config.yml: {exc}"},
                status=500,
            )
        logger.info("UniFi credentials updated via web UI (host=%s)", host)
        return web.json_response({
            "ok": True,
            "message": "Saved to config.yml. Restart the container to re-run adoption.",
        })

    async def _test_unifi(self, request: web.Request) -> web.Response:
        """Try the supplied credentials against the Protect controller.

        Prefers the API-key path (``GET /proxy/protect/integration/v1/nvrs``
        with ``X-API-KEY``) when a key is supplied, since on Protect 7.x the
        legacy username/password flow works but still can't fetch the
        adoption token — and the API-key path is the only way we can
        verify against the new integration API at all.

        Blank password / api_key means "use the stored value" so the user
        can re-test after only changing the host.
        """
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")

        host = (body.get("host") or "").strip()
        if not host:
            return web.json_response({"ok": False, "message": "Host is required"})

        stored = self.config.get("unifi", {}) or {}
        api_key = (body.get("api_key") or "").strip() or (stored.get("api_key") or "")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not password:
            password = stored.get("password") or ""

        # Prefer API key — it's the new canonical auth on Protect 7.x.
        if api_key:
            base = host if host.startswith("http") else f"https://{host}"
            url = f"{base}/proxy/protect/integration/v1/nvrs"
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                    async with s.get(url, headers={"X-API-KEY": api_key}) as r:
                        if r.status == 200:
                            return web.json_response(
                                {"ok": True, "message": "API key accepted — integration API reachable"}
                            )
                        if r.status in (401, 403):
                            return web.json_response(
                                {"ok": False, "message": f"API key rejected (HTTP {r.status})"}
                            )
                        if r.status == 404:
                            return web.json_response(
                                {"ok": False, "message": (
                                    "Integration API not found (HTTP 404). Upgrade Protect to "
                                    "7.x+, or create the API key under Settings → Control Plane → "
                                    "Integrations.")}
                            )
                        body_text = (await r.text())[:200]
                        return web.json_response(
                            {"ok": False, "message": f"HTTP {r.status}: {body_text}"}
                        )
            except aiohttp.ClientError as exc:
                return web.json_response({"ok": False, "message": f"Could not reach {host}: {exc}"})
            except Exception as exc:
                return web.json_response({"ok": False, "message": f"Unexpected error: {exc}"})

        # Fall back to username/password.
        if not username:
            return web.json_response(
                {"ok": False, "message": "Provide an API key, or username + password"}
            )
        if not password:
            return web.json_response(
                {"ok": False, "message": "Password required (no stored value)"}
            )

        # Import here to avoid a circular import on module load.
        from unifi_auth import UniFiProtectClient, UniFiAuthError

        try:
            async with UniFiProtectClient(host, username, password):
                pass
        except UniFiAuthError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "message": f"Unexpected error: {exc}"}
            )
        return web.json_response({"ok": True, "message": "Login succeeded (username/password)"})

    async def _restart_container(self, request: web.Request) -> web.Response:
        """Trigger a graceful container restart by exiting the process.

        Docker (or any orchestrator with restart policy) will restart the
        container automatically. We respond first, then schedule the exit.
        """
        logger.info("Restart requested via web UI — shutting down process")

        async def _delayed_exit():
            await asyncio.sleep(0.5)  # give the HTTP response time to flush
            # os._exit is intentional: sys.exit raises SystemExit which asyncio
            # catches and suppresses inside a running event loop. We need the
            # process to actually terminate so the orchestrator restarts it.
            os._exit(0)

        asyncio.ensure_future(_delayed_exit())
        return web.json_response({"ok": True, "message": "Restarting…"})

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def run(self, port: int = 8091) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web UI: http://0.0.0.0:%d/", port)
        try:
            await asyncio.Event().wait()  # block forever
        finally:
            await runner.cleanup()
