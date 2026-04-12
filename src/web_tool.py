"""
web_tool.py — embedded web UI for camera configuration and virtual line drawing.

Tabs:
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

After saving, the UI shows "Restart the container to apply changes."
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

import cv2
import numpy as np
import yaml
from aiohttp import web

if TYPE_CHECKING:
    from unifi_client import AIPortCamera

logger = logging.getLogger("web_tool")


# ─── HTML (single file, no external resources) ──────────────────────────────


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>happy-ai-port</title>
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
  </style>
</head>
<body>
  <h1>happy-ai-port</h1>

  <div class="tabs">
    <button class="tab active" data-pane="setup">Setup</button>
    <button class="tab" data-pane="lines">Lines</button>
  </div>

  <!-- ═══ SETUP TAB ═══ -->
  <div id="setup" class="pane active">
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

<script>
/* ── Tab switching ─────────────────────────────────────────────────────── */
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.pane).classList.add('active');
    if (t.dataset.pane === 'lines') loadLineCameras();
  });
});

/* ── Setup tab ─────────────────────────────────────────────────────────── */
const camerasDiv = document.getElementById('cameras');
let cameraData = [];

function createCameraCard(cam, idx) {
  const ai = cam.ai || {};
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <div class="card-header">
      <h3>Camera ${idx + 1}</h3>
      <button class="btn-danger btn-sm remove-cam" data-idx="${idx}">Remove</button>
    </div>
    <div class="grid">
      <div class="field">
        <label>Name</label>
        <input data-key="name" value="${esc(cam.name || '')}">
      </div>
      <div class="field">
        <label>RTSP URL</label>
        <input data-key="rtsp_url" value="${esc(cam.rtsp_url || '')}">
      </div>
      <div class="field">
        <label>Snapshot URL (optional)</label>
        <input data-key="snapshot_url" value="${esc(cam.snapshot_url || '')}">
      </div>
      <div class="field">
        <label>Model</label>
        <select data-key="ai.model">
          <option value="yolov8n.pt" ${ai.model === 'yolov8s.pt' || ai.model === 'yolov8m.pt' ? '' : 'selected'}>yolov8n.pt (fast)</option>
          <option value="yolov8s.pt" ${ai.model === 'yolov8s.pt' ? 'selected' : ''}>yolov8s.pt (balanced)</option>
          <option value="yolov8m.pt" ${ai.model === 'yolov8m.pt' ? 'selected' : ''}>yolov8m.pt (accurate)</option>
        </select>
      </div>
      <div class="field">
        <label>Confidence</label>
        <input data-key="ai.confidence" type="number" min="0" max="1" step="0.05" value="${ai.confidence ?? 0.45}">
      </div>
      <div class="field">
        <label>Frame Skip</label>
        <input data-key="ai.frame_skip" type="number" min="1" max="30" value="${ai.frame_skip ?? 3}">
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
    </div>`;
  card.querySelector('.remove-cam').addEventListener('click', () => {
    cameraData.splice(idx, 1);
    renderCameras();
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
        val = INTEGER_KEYS.has(key) ? parseInt(el.value, 10) : parseFloat(el.value);
        if (isNaN(val)) return; // skip empty/invalid number fields
      }
      else val = el.value.trim();

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
const lineList = document.getElementById('line-list');
const lineNameInp = document.getElementById('line-name');
const dirSel = document.getElementById('dir');

let pts = [];
let existingLines = [];
let autoTimer = null;

async function loadLineCameras() {
  try {
    const cams = await (await fetch('/api/cameras')).json();
    camSel.innerHTML = cams.map(c => `<option>${c.name}</option>`).join('');
    if (cams.length) await loadLineCamera();
    else {
      frame.src = '';
      lineList.innerHTML = '<div class="empty-msg">No cameras configured yet.</div>';
    }
  } catch (_) {}
}

async function loadLineCamera() {
  const name = camSel.value;
  if (!name) return;
  frame.src = `/api/frame/${encodeURIComponent(name)}?t=${Date.now()}`;
  try {
    existingLines = await (await fetch(`/api/lines/${encodeURIComponent(name)}`)).json();
  } catch (_) { existingLines = []; }
  pts = [];
  redraw();
  renderLineList();
}

function refreshFrame() {
  if (camSel.value)
    frame.src = `/api/frame/${encodeURIComponent(camSel.value)}?t=${Date.now()}`;
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

/* ── Init ──────────────────────────────────────────────────────────────── */
loadConfig();
</script>
</body>
</html>
"""


# ─── Server ─────────────────────────────────────────────────────────────────


def _encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else b""


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
    ):
        self.registry = registry
        self.config = config
        self.config_path = Path(config_path) if config_path else Path("/config/config.yml")
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/cameras", self._list_cameras)
        self.app.router.add_get("/api/frame/{name}", self._get_frame)
        self.app.router.add_get("/api/config", self._get_config)
        self.app.router.add_post("/api/config", self._save_config)
        self.app.router.add_get("/api/lines/{name}", self._get_lines)
        self.app.router.add_post("/api/lines/{name}", self._save_line)
        self.app.router.add_delete("/api/lines/{name}/{idx}", self._delete_line)

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
            os.unlink(tmp_path)
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

    async def _get_frame(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        cam = self.registry.get(name)
        if cam is None:
            return web.Response(status=404, text="camera not registered yet")

        frame = cam.ai_engine.get_latest_frame()
        if frame is None:
            return web.Response(status=503, text="no frame yet — stream still warming up")

        jpeg = _encode_jpeg(frame)
        if not jpeg:
            return web.Response(status=500, text="jpeg encode failed")

        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

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

        # Clean up: remove empty optional fields
        for idx, cam in enumerate(new_cameras):
            if not isinstance(cam, dict):
                return web.Response(
                    status=400,
                    text=f"invalid payload: cameras[{idx}] must be an object",
                )
            if not cam.get("snapshot_url"):
                cam.pop("snapshot_url", None)
            ai = cam.get("ai", {})
            if ai is not None and not isinstance(ai, dict):
                return web.Response(
                    status=400,
                    text=f"invalid payload: cameras[{idx}].ai must be an object",
                )
            if ai:
                # Remove defaults so config stays clean
                for key, default in [
                    ("model", "yolov8n.pt"),
                    ("confidence", 0.45),
                    ("frame_skip", 3),
                    ("detect_persons", True),
                    ("detect_vehicles", True),
                ]:
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

        _VALID_DIRECTIONS = {"both", "left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"}
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

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def run(self, port: int = 8091) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web UI: http://<host>:%d/", port)
        try:
            await asyncio.Event().wait()  # block forever
        finally:
            await runner.cleanup()
