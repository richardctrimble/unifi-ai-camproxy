"""
web_tool.py — tiny embedded web UI for drawing virtual lines on camera
frames. Served from the running container itself, all local, no external
services.

Endpoints:
    GET /                       single-page HTML drawing tool
    GET /api/cameras            JSON list of configured cameras
    GET /api/frame/<name>       current JPEG from that camera's AIEngine
    GET /api/lines/<name>       existing lines from config.yml (for reference)

The HTML page lets you pick a camera, click two points on the live frame,
and copy-paste the resulting YAML into config.yml. Existing lines are
drawn in grey as a reference.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict

import cv2
import numpy as np
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
  <title>happy-ai-port — line tool</title>
  <style>
    :root { color-scheme: dark; }
    body {
      font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
      margin: 0; padding: 16px;
      background: #1a1a1a; color: #e0e0e0;
    }
    h1 { font-size: 18px; margin: 0 0 12px 0; font-weight: 600; }
    h3 { font-size: 14px; margin: 20px 0 8px 0; color: #aaa; }
    .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
    select, input, button {
      font-size: 14px; padding: 6px 10px;
      background: #2a2a2a; color: #e0e0e0;
      border: 1px solid #444; border-radius: 4px;
      font-family: inherit;
    }
    button { cursor: pointer; }
    button:hover { background: #3a3a3a; }
    button:active { background: #444; }
    label { font-size: 13px; color: #aaa; }
    .hint { color: #888; font-size: 13px; margin: 8px 0 12px 0; }
    .stage {
      position: relative; display: inline-block; max-width: 100%;
      border-radius: 4px; overflow: hidden; background: #000;
      line-height: 0;
    }
    .stage img {
      display: block; max-width: 100%; max-height: 75vh;
      height: auto; cursor: crosshair;
      user-select: none; -webkit-user-select: none;
    }
    .stage svg {
      position: absolute; inset: 0;
      width: 100%; height: 100%;
      pointer-events: none;
    }
    .existing  { stroke: #888; stroke-width: 2; stroke-dasharray: 6 4; fill: none; }
    .draft     { stroke: #4af; stroke-width: 3; fill: none; }
    .handle    { fill: #4af; stroke: #fff; stroke-width: 1; }
    .row { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
    pre {
      background: #0d0d0d; border: 1px solid #333; border-radius: 4px;
      padding: 12px; overflow-x: auto; font-size: 13px;
      white-space: pre-wrap; margin: 0 0 8px 0;
    }
    .empty { color: #666; font-style: italic; }
  </style>
</head>
<body>
  <h1>happy-ai-port — virtual line tool</h1>

  <div class="bar">
    <label>Camera:</label>
    <select id="cam"></select>
    <button id="refresh">Refresh frame</button>
    <button id="clear">Clear line</button>
    <label><input type="checkbox" id="auto"> Auto-refresh (2s)</label>
  </div>

  <div class="hint">
    Click two points on the frame to draw a line.
    Dashed grey lines are already in <code>config.yml</code>.
  </div>

  <div class="stage" id="stage">
    <img id="frame" alt="camera frame">
    <svg id="svg" viewBox="0 0 1 1" preserveAspectRatio="none"></svg>
  </div>

  <h3>Line properties</h3>
  <div class="row">
    <div>
      <label>Name:</label>
      <input id="name" value="EntryLine" style="width: 160px;">
    </div>
    <div>
      <label>Direction:</label>
      <select id="dir">
        <option value="both">both</option>
        <option value="left_to_right">left_to_right</option>
        <option value="right_to_left">right_to_left</option>
        <option value="top_to_bottom">top_to_bottom</option>
        <option value="bottom_to_top">bottom_to_top</option>
      </select>
    </div>
  </div>

  <h3>YAML snippet — paste under this camera's <code>ai.lines:</code></h3>
  <pre id="yaml" class="empty">(click two points on the frame)</pre>
  <button id="copy">Copy to clipboard</button>

<script>
const $ = (id) => document.getElementById(id);
const camSel = $('cam'), frame = $('frame'), svg = $('svg');
const yamlBox = $('yaml'), nameInp = $('name'), dirSel = $('dir');

let pts = [];
let existing = [];
let autoTimer = null;

async function loadCameras() {
  const cams = await (await fetch('/api/cameras')).json();
  camSel.innerHTML = cams.map(c => `<option>${c.name}</option>`).join('');
  if (cams.length) await loadCamera();
}

async function loadCamera() {
  const name = camSel.value;
  frame.src = `/api/frame/${encodeURIComponent(name)}?t=${Date.now()}`;
  try {
    existing = await (await fetch(`/api/lines/${encodeURIComponent(name)}`)).json();
  } catch (_) { existing = []; }
  pts = [];
  redraw();
}

function refreshFrame() {
  frame.src = `/api/frame/${encodeURIComponent(camSel.value)}?t=${Date.now()}`;
}

function redraw() {
  let out = '';
  for (const l of existing) {
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
  updateYaml();
}

function updateYaml() {
  if (pts.length !== 2) {
    yamlBox.textContent = '(click two points on the frame)';
    yamlBox.classList.add('empty');
    return;
  }
  const r = v => Number(v).toFixed(3);
  yamlBox.classList.remove('empty');
  yamlBox.textContent =
`- name: "${nameInp.value}"
  x1: ${r(pts[0].x)}
  y1: ${r(pts[0].y)}
  x2: ${r(pts[1].x)}
  y2: ${r(pts[1].y)}
  direction: "${dirSel.value}"`;
}

frame.addEventListener('click', (e) => {
  const rect = frame.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const y = (e.clientY - rect.top)  / rect.height;
  if (pts.length >= 2) pts = [];
  pts.push({x, y});
  redraw();
});

camSel.addEventListener('change', loadCamera);
$('refresh').addEventListener('click', refreshFrame);
$('clear').addEventListener('click', () => { pts = []; redraw(); });
nameInp.addEventListener('input', updateYaml);
dirSel.addEventListener('change', updateYaml);
$('auto').addEventListener('change', (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) autoTimer = setInterval(refreshFrame, 2000);
});
$('copy').addEventListener('click', async () => {
  if (pts.length !== 2) return;
  try {
    await navigator.clipboard.writeText(yamlBox.textContent);
    const btn = $('copy');
    const prev = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = prev, 1500);
  } catch (_) {}
});

loadCameras();
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
    Lightweight aiohttp app. Holds a reference to the live camera registry
    (populated by main.py) and the parsed config, then serves both to a
    single-page HTML tool.
    """

    def __init__(
        self,
        registry: Dict[str, "AIPortCamera"],
        config: dict,
    ):
        self.registry = registry
        self.config = config
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/cameras", self._list_cameras)
        self.app.router.add_get("/api/frame/{name}", self._get_frame)
        self.app.router.add_get("/api/lines/{name}", self._get_lines)

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

    async def _get_lines(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        for cam in self.config.get("cameras", []):
            if cam.get("name") == name:
                lines = (cam.get("ai") or {}).get("lines") or []
                return web.json_response(lines)
        return web.json_response([])

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def run(self, port: int = 8091) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Line tool UI: http://<docker-host>:%d/", port)
        try:
            await asyncio.Event().wait()  # block forever
        finally:
            await runner.cleanup()
