#!/usr/bin/env python3

"""Build a standalone score-based sheet matching dashboard.

This stage is additive and does not modify the existing overlap Sankey
pipeline. It reads the cached match scores from compareSheetShapes and
generates a separate browser viewer that supports:

- timestep range selection
- per-panel score mode selection
- per-panel score thresholding
- add/remove score panels
- node sizing by sheet area
- global link normalization by score
- hover tooltips with sheet images

The viewer is intentionally separate from the existing Sankey stage so the
current dashboard behavior remains isolated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from common import BASE_DIR, OUTPUT_DIR, SHEET_IMAGE_DIR
from viewer_common import (
    shared_viewer_css,
    shared_viewer_script_tags,
    write_viewer_common_js,
)

STORAGE_ROOT = BASE_DIR / "compareSheetShapesCache"
TIMESTEP_CACHE_DIR = STORAGE_ROOT / "cache" / "timesteps"
MATCHES_FILE = STORAGE_ROOT / "results" / "sheet_shape_matches.json"

MATCH_SUMMARY_VIEWER_DIR = OUTPUT_DIR / "match_summary_viewer"

SCORE_MODES = [
    {"id": "combined", "label": "combined", "field": "final_score"},
    {"id": "shape_iou", "label": "shape IoU", "field": "shape_iou"},
    {"id": "support_jaccard", "label": "vertex Jaccard", "field": "support_jaccard"},
    {"id": "area_ratio", "label": "area ratio", "field": "area_ratio"},
    {"id": "bbox_iou", "label": "bbox IoU", "field": "bbox_iou"},
    {"id": "centroid_similarity", "label": "centroid similarity", "field": "centroid_similarity"},
]

DEFAULT_RANGES = [{"start": 0, "end": 20}]


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def link_sheet_images(viewer_dir: Path) -> None:
    if not SHEET_IMAGE_DIR.exists():
        return

    target = viewer_dir / "sheet_images"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    try:
        target.symlink_to(SHEET_IMAGE_DIR.resolve(), target_is_directory=True)
    except OSError:
        shutil.copytree(SHEET_IMAGE_DIR, target)


def find_sheet_image(stem: str, sheet_id: int, viewer_dir: Path) -> str | None:
    folder = SHEET_IMAGE_DIR / stem
    if not folder.exists():
        return None

    matches = sorted(folder.glob(f"{sheet_id}_*.png"))
    if not matches:
        return None

    linked = viewer_dir / "sheet_images" / stem / matches[0].name
    return linked.relative_to(viewer_dir).as_posix()


def load_timestep_cache(viewer_dir: Path) -> tuple[list[dict], float, int]:
    if not TIMESTEP_CACHE_DIR.exists():
        raise FileNotFoundError(f"Timestep cache directory missing: {TIMESTEP_CACHE_DIR}")

    timesteps: list[dict] = []
    max_area = 0.0
    max_vertices = 0

    for path in sorted(TIMESTEP_CACHE_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        sheets = []
        for sheet in data.get("sheets", []):
            area = safe_float(sheet.get("area"))
            vertices = safe_int(sheet.get("num_vertices"))
            max_area = max(max_area, area)
            max_vertices = max(max_vertices, vertices)
            sheets.append(
                {
                    "sheet_id": safe_int(sheet.get("sheet_id")),
                    "rank": safe_int(sheet.get("rank")),
                    "area": area,
                    "num_vertices": vertices,
                    "bbox": sheet.get("bbox", []),
                    "centroid": sheet.get("centroid", []),
                    "thumbnail": find_sheet_image(data.get("stem", ""), safe_int(sheet.get("sheet_id")), viewer_dir),
                }
            )

        timesteps.append(
            {
                "timestep_index": safe_int(data.get("timestep_index")),
                "label": str(data.get("label", "")),
                "stem": str(data.get("stem", "")),
                "sheets": sheets,
            }
        )

    timesteps.sort(key=lambda item: item["timestep_index"])
    return timesteps, max_area, max_vertices


def load_match_data() -> dict:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Match file does not exist: {MATCHES_FILE}")
    return json.loads(MATCHES_FILE.read_text())


def prepare_data(viewer_dir: Path) -> dict:
    timesteps, max_area, max_vertices = load_timestep_cache(viewer_dir)
    match_data = load_match_data()

    score_maxima = {mode["id"]: 0.0 for mode in SCORE_MODES}
    score_fields = {mode["id"]: mode["field"] for mode in SCORE_MODES}

    pairs = []
    for pair in match_data.get("pairwise_matches", []):
        matches = []
        for match in pair.get("matches", []):
            scores = {}
            for mode in SCORE_MODES:
                value = safe_float(match.get(mode["field"]))
                scores[mode["id"]] = value
                score_maxima[mode["id"]] = max(score_maxima[mode["id"]], value)

            matches.append(
                {
                    "source_sheet_id": safe_int(match.get("source_sheet_id")),
                    "target_sheet_id": safe_int(match.get("target_sheet_id")),
                    "source_rank": safe_int(match.get("source_rank")),
                    "target_rank": safe_int(match.get("target_rank")),
                    "source_area": safe_float(match.get("source_area")),
                    "target_area": safe_float(match.get("target_area")),
                    "source_num_vertices": safe_int(match.get("source_num_vertices")),
                    "target_num_vertices": safe_int(match.get("target_num_vertices")),
                    "scores": scores,
                }
            )

        pairs.append(
            {
                "source_timestep_index": safe_int(pair.get("source_timestep_index")),
                "source_label": str(pair.get("source_label", "")),
                "source_stem": str(pair.get("source_stem", "")),
                "target_timestep_index": safe_int(pair.get("target_timestep_index")),
                "target_label": str(pair.get("target_label", "")),
                "target_stem": str(pair.get("target_stem", "")),
                "pair_count": safe_int(pair.get("pair_count")),
                "matches": matches,
            }
        )

    return {
        "meta": {
            "generated_from": str(MATCHES_FILE),
            "timesteps": len(timesteps),
            "score_modes": SCORE_MODES,
            "score_fields": score_fields,
            "score_maxima": score_maxima,
            "global_area_max": max_area,
            "global_vertex_max": max_vertices,
            "default_ranges": DEFAULT_RANGES,
            "node_height_min": 8,
            "node_height_max": 32,
            "link_thickness_min": 1.4,
            "link_thickness_max": 16,
        },
        "timesteps": timesteps,
        "pairs": pairs,
        "viewer": {
            "generated_from": str(MATCHES_FILE),
            "sheet_image_dir": str(SHEET_IMAGE_DIR),
        },
    }


def write_data_json(data: dict) -> Path:
    path = MATCH_SUMMARY_VIEWER_DIR / "data.json"
    path.write_text(json.dumps(data, indent=2, allow_nan=False))
    return path


def write_index_html() -> Path:
    path = MATCH_SUMMARY_VIEWER_DIR / "index.html"
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Range based Sheet Match Summary Viewer</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <div class="title-block">
      <h1>Range based Sheet Match Summary</h1>
      <p>Score-based correspondence views across adjacent timesteps.</p>
    </div>
    <div class="header-actions">
      <button id="zoomOut">Zoom out</button>
      <button id="zoomIn">Zoom in</button>
      <button id="centerView">Center sankey</button>
      <button id="addPanel">+ Add score view</button>
    </div>
  </header>

  <main>
    <aside id="controls">
      <section>
        <h2>Timestep ranges</h2>
        <div id="rangeRows"></div>
        <div class="row-actions">
          <button id="addRange">+ Add range</button>
          <button id="deleteRange">Delete selected range</button>
        </div>
        <p class="hint">Drag on the bar to create a range. Click a range to select it. Delete removes the selected range.</p>
      </section>

      <section>
        <h2>Summary</h2>
        <dl id="stats"></dl>
      </section>
    </aside>

    <section id="viewer">
      <div id="rangeBarWrap">
        <svg id="rangeBar" aria-label="Timestep range selector"></svg>
      </div>
      <div id="panelList"></div>
    </section>
  </main>

  <div id="tooltip"></div>

  {shared_viewer_script_tags(include_sankey=False)}
</body>
</html>
"""
    )
    return path


def write_style_css() -> Path:
    path = MATCH_SUMMARY_VIEWER_DIR / "style.css"
    path.write_text(
        """* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #1d252d;
  background: #f5f7fa;
}
header {
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  background: #fff;
  border-bottom: 1px solid #d9dee5;
}
.title-block h1 {
  margin: 0;
  font-size: 20px;
}
.title-block p {
  margin: 4px 0 0;
  font-size: 13px;
  color: #5a6572;
}
.header-actions {
  display: flex;
  gap: 8px;
}
button, select, input {
  font: inherit;
}
button {
  border: 1px solid #b8c1cc;
  background: #fff;
  border-radius: 5px;
  padding: 7px 10px;
  cursor: pointer;
}
button:hover {
  background: #f6f9fc;
}
main {
  height: calc(100vh - 72px);
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  min-height: 0;
}
aside {
  overflow: auto;
  background: #fff;
  border-right: 1px solid #d9dee5;
  padding: 14px;
}
section {
  margin-bottom: 18px;
}
h2 {
  margin: 0 0 10px;
  font-size: 14px;
}
.hint {
  margin: 8px 0 0;
  color: #6a7785;
  font-size: 12px;
  line-height: 1.45;
}
#rangeBarWrap {
  border: 1px solid #d9dee5;
  border-radius: 6px;
  background: #fafbfc;
  overflow: hidden;
}
#rangeBar {
  width: 100%;
  height: 78px;
  display: block;
}
#rangeBar,
#rangeBar * {
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
}
.range-bg {
  fill: #e8edf2;
}
.range-drag-surface {
  fill: transparent;
  cursor: crosshair;
  pointer-events: all;
}
.range-drag-preview {
  fill: rgba(47, 128, 201, 0.2);
  stroke: #2f80c9;
  stroke-width: 1;
  pointer-events: none;
}
.range-selected {
  fill: #6aa3d8;
  opacity: 0.55;
  cursor: pointer;
  pointer-events: all;
}
.range-selected:hover {
  opacity: 0.82;
  stroke: #15202b;
  stroke-width: 1;
}
.range-selected.selected {
  fill: #2f80c9;
  opacity: 0.9;
  stroke: #15202b;
  stroke-width: 1.1;
}
.viewport-window {
  fill: rgba(0, 0, 0, 0.22);
  stroke: #000;
  stroke-width: 1.2;
  pointer-events: all;
  cursor: grab;
}
.viewport-window.dragging {
  cursor: grabbing;
}
.range-item {
  fill: rgba(47, 128, 201, 0.18);
  stroke: #2f80c9;
  stroke-width: 1;
  cursor: pointer;
}
.range-item.selected {
  fill: rgba(47, 128, 201, 0.34);
  stroke-width: 1.5;
}
.range-label {
  font-size: 11px;
  fill: #425160;
  pointer-events: none;
}
.range-tick {
  stroke: #b6c0cb;
  stroke-width: 1;
}
.range-row {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 6px 8px;
  border-radius: 5px;
  border: 1px solid transparent;
  margin-top: 6px;
}
.range-row.selected {
  border-color: #2f80c9;
  background: #edf6ff;
}
.range-row input {
  width: 100%;
  cursor: text;
}
.row-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
#viewer {
  min-width: 0;
  min-height: 0;
  background: #fff;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  overflow: hidden;
}
#rangeBarWrap {
  border-bottom: 1px solid #d9dee5;
  background: #fafbfc;
  overflow: hidden;
}
#panelList {
  overflow: auto;
  min-width: 0;
  min-height: 0;
  display: block;
  padding: 12px;
}
.panel {
  border: 1px solid #d9dee5;
  border-radius: 8px;
  padding: 10px 10px 12px;
  background: #fff;
  margin-bottom: 12px;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.panel-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.panel-title strong {
  font-size: 14px;
}
.panel-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.panel-controls select,
.panel-controls input[type="number"] {
  height: 30px;
}
.panel-controls input[type="range"] {
  width: 150px;
}
.panel-canvas {
  border: 1px solid #d9dee5;
  border-radius: 6px;
  height: 49vh;
  min-height: 470px;
  max-height: 620px;
  overflow: hidden;
  background: #fff;
  cursor: grab;
  touch-action: none;
}
.panel-canvas.dragging {
  cursor: grabbing;
}
.timestep-label-layer text {
  font-size: 13px;
  font-weight: 600;
  fill: #465360;
}
svg.summary-chart {
  display: block;
  background: #fff;
}
.node rect {
  fill: #6f9ed4;
  stroke: rgba(20, 30, 40, 0.35);
  stroke-width: 0.6;
}
.node text {
  fill: #15202b;
  font-size: 12px;
  font-weight: 600;
  pointer-events: none;
}
.link {
  stroke: none;
  cursor: pointer;
}
.link.global-link {
  fill: rgba(80, 80, 80, 0.16);
}
.link:hover {
  fill: rgba(45, 65, 85, 0.42);
}
.panel-empty {
  padding: 18px;
  color: #6a7785;
}
.tooltip {
  position: fixed;
  z-index: 20;
  pointer-events: none;
  background: rgba(17, 24, 39, 0.96);
  color: #fff;
  border-radius: 8px;
  padding: 10px 12px;
  max-width: 380px;
  box-shadow: 0 12px 28px rgba(0,0,0,0.2);
  font-size: 12px;
  line-height: 1.45;
}
.tooltip.hidden {
  display: none;
}
.tooltip h3 {
  margin: 0 0 6px;
  font-size: 13px;
}
.tooltip-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.tooltip img {
  width: 100%;
  max-width: 150px;
  border-radius: 6px;
  display: block;
  background: #fff;
}
.meta-list {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 10px;
  align-items: start;
}
.stats-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 8px;
  font-size: 12px;
  color: #51606f;
}
""" + shared_viewer_css()
    )
    return path


def write_viewer_js() -> Path:
    path = MATCH_SUMMARY_VIEWER_DIR / "viewer.js"
    path.write_text(
        """const DATA = null;

d3.json("data.json").then(data => {
  const state = {
    ranges: (data.meta.default_ranges && data.meta.default_ranges.length ? data.meta.default_ranges : [{start: 0, end: 20}]).map(r => ({...r})),
    selectedRangeIndex: 0,
    panels: [{ id: 1, scoreMode: "combined", threshold: 0 }],
    nextPanelId: 2,
    rangeDrag: null,
    viewportDrag: null,
    tooltipLocked: false,
    activePanelId: 1,
    panelViews: new Map(),
    panelPan: null
  };

  let camera = null;
  let tooltipEngine = null;
  let renderAllPending = false;
  let thresholdSyncPending = false;

  const rangeBar = d3.select("#rangeBar");
  const panelList = d3.select("#panelList");
  const stats = d3.select("#stats");
  const tooltip = d3.select("#tooltip");

  const timestepByIndex = new Map(data.timesteps.map(t => [t.timestep_index, t]));
  const scoreMaxima = data.meta.score_maxima || {};
  const areaMax = data.meta.global_area_max || 1;
  const panelNodeHeightMin = data.meta.node_height_min || 8;
  const panelNodeHeightMax = data.meta.node_height_max || 32;
  const linkMin = data.meta.link_thickness_min || 1.4;
  const linkMax = data.meta.link_thickness_max || 16;
  const scoreModes = data.meta.score_modes || [];
  const timestepMax = Math.max(0, data.timesteps.length - 1);
  const VIEWPORT_ANCHOR_Y = 0.38;
  const ZOOM_MIN = 0.1;
  const ZOOM_MAX = 20;
  const ZOOM_STEP = 1.2;
  const PAN_DRAG_THRESHOLD = 4;

  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });

  function clamp(n, low, high) {
    return Math.min(high, Math.max(low, n));
  }

  function formatScore(value) {
    return numberFormat.format(Number(value || 0));
  }

  function applyRangeAction(action) {
    const next = window.ReebViewerCommon.rangeReducer(
      {
        ranges: state.ranges,
        selectedRangeIndex: state.selectedRangeIndex,
        rangeDrag: state.rangeDrag
      },
      action,
      {
        timestepMax,
        keepOne: true,
        fallbackRange: { start: 0, end: 0 },
        emptySelectedIndex: 0,
        defaultSpan: 20,
        minSpan: 0
      }
    );
    state.ranges = next.ranges;
    state.selectedRangeIndex = next.selectedRangeIndex;
    state.rangeDrag = next.rangeDrag;
    return next;
  }

  function normalizedRanges() {
    return applyRangeAction({ type: "normalize" }).ranges;
  }

  function inRanges(timestep) {
    const ranges = normalizedRanges();
    if (!ranges.length) return true;
    return ranges.some(r => timestep >= r.start && timestep <= r.end);
  }

  function visibleTimesteps() {
    const ranges = normalizedRanges();
    if (!ranges.length) return data.timesteps.slice();
    const visible = [];
    const seen = new Set();
    for (const range of ranges) {
      for (let t = range.start; t <= range.end; t += 1) {
        if (seen.has(t)) continue;
        const ts = timestepByIndex.get(t);
        if (ts) visible.push(ts);
        seen.add(t);
      }
    }
    return visible;
  }

  function scoreModeLabel(mode) {
    return (scoreModes.find(s => s.id === mode) || { label: mode }).label;
  }

  function currentPanel() {
    return state.panelViews.get(state.activePanelId) || state.panelViews.values().next().value || null;
  }

  function getPanelById(id) {
    return state.panels.find(panel => panel.id === id);
  }

  function nodeHeight(node) {
    const ratio = areaMax > 0 ? node.area / areaMax : 0;
    return panelNodeHeightMin + ratio * (panelNodeHeightMax - panelNodeHeightMin);
  }

  function scoreValue(link, mode) {
    return Number(link.scores?.[mode] ?? link.scores?.combined ?? 0);
  }

  function scoreOpacity(link, mode) {
    const maxScore = scoreMaxima[mode] || 1;
    const value = scoreValue(link, mode);
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return 0.18 + ratio * 0.64;
  }

  function linkWidth(link, mode) {
    const maxScore = scoreMaxima[mode] || 1;
    const value = scoreValue(link, mode);
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return linkMin + ratio * (linkMax - linkMin);
  }

  function rangeLabel(range) {
    return window.ReebViewerCommon.formatRangeLabel(range, {
      getLabel: value => {
        const ts = timestepByIndex.get(value);
        return ts ? ts.label : value;
      }
    });
  }

  function renderRangeRows() {
    window.ReebViewerCommon.renderRangeRows(document.getElementById("rangeRows"), {
      ranges: normalizedRanges(),
      selectedRangeIndex: state.selectedRangeIndex,
      timestepMax,
      onSelectRange: index => {
        applyRangeAction({ type: "select", index });
        renderAll();
      },
      onCommitRange: (index, startValue, endValue) => {
        applyRangeAction({ type: "commit", index, startValue, endValue });
        renderAll();
      },
      onDeleteRange: index => removeRange(index)
    });
  }

  function addRange() {
    applyRangeAction({ type: "add" });
    renderAll();
  }

  function removeRange(index) {
    applyRangeAction({ type: "delete", index });
    renderAll();
  }

  function updateTooltip(html, x, y) {
    tooltipEngine?.showAt(html, x, y);
  }

  function hideTooltip() {
    tooltipEngine?.hide();
  }

  function nodeTooltip(node) {
    const image = node.thumbnail ? `<img src="${node.thumbnail}" alt="Sheet ${node.sheet_id} image">` : "";
    return `
      <h3>Sheet ${node.sheet_id}</h3>
      <div class="meta-list">
        <div>Timestep</div><div>${node.timestep_label}</div>
        <div>Rank</div><div>${node.rank}</div>
        <div>Area</div><div>${formatScore(node.area)}</div>
        <div>Vertices</div><div>${node.num_vertices}</div>
      </div>
      ${image}
    `;
  }

  function linkTooltip(link, mode) {
    const sourceNode = timestepByIndex.get(link.source_timestep_index)?.sheets?.find(s => s.sheet_id === link.source_sheet_id);
    const targetNode = timestepByIndex.get(link.target_timestep_index)?.sheets?.find(s => s.sheet_id === link.target_sheet_id);
    const sourceImage = sourceNode?.thumbnail ? `<img src="${sourceNode.thumbnail}" alt="Source sheet image">` : "";
    const targetImage = targetNode?.thumbnail ? `<img src="${targetNode.thumbnail}" alt="Target sheet image">` : "";
    const pairScore = scoreValue(link, mode);
    const scores = scoreModes.map(s => `<div>${s.label}</div><div>${formatScore(link.scores?.[s.id] ?? 0)}</div>`).join("");
    return `
      <h3>Match ${link.source_sheet_id} → ${link.target_sheet_id}</h3>
      <div class="meta-list">
        <div>Source timestep</div><div>${link.source_label}</div>
        <div>Target timestep</div><div>${link.target_label}</div>
        <div>Current score</div><div>${formatScore(pairScore)}</div>
      </div>
      <div class="tooltip-grid" style="margin-top:10px;">
        <div>${sourceImage}</div>
        <div>${targetImage}</div>
      </div>
      <div class="meta-list" style="margin-top:10px;">${scores}</div>
    `;
  }

  function buildVisibleColumns() {
    const ranges = normalizedRanges();
    const columns = [];
    const active = (ranges.length ? ranges : [{ start: 0, end: timestepMax }])
      .slice()
      .sort((a, b) => a.start - b.start);
    const seen = new Set();
    for (let i = 0; i < active.length; i += 1) {
      const range = active[i];
      if (i > 0) {
        const prev = active[i - 1];
        const hiddenTimesteps = Math.max(0, range.start - prev.end - 1);
        if (hiddenTimesteps > 0) {
          columns.push({ type: "gap", span: hiddenTimesteps });
        }
      }
      for (let t = range.start; t <= range.end; t += 1) {
        if (seen.has(t)) continue;
        const ts = timestepByIndex.get(t);
        if (!ts) continue;
        columns.push({ type: "timestep", timestep: ts });
        seen.add(t);
      }
    }
    return columns;
  }

  function layoutForPanel(columns) {
    const xStart = 26;
    const colWidth = 112;
    const gapScale = 2.2;
    const nodeWidth = 18;
    const topPad = 54;
    const bottomPad = 22;
    const nodeGap = 6;
    const gapWidth = 34;

    const visibleNodes = [];
    const nodeByKey = new Map();
    let maxColumnHeight = 0;
    let xCursor = xStart;

    columns.forEach(column => {
      if (column.type === "gap") {
        xCursor += column.span * (colWidth + gapWidth) * gapScale;
        return;
      }
      const timestep = column.timestep;
      const nodes = [...(timestep.sheets || [])].sort((a, b) => a.rank - b.rank || a.sheet_id - b.sheet_id);
      let y = topPad;
      let columnHeight = 0;

      nodes.forEach(node => {
        const height = nodeHeight(node);
        const layoutNode = {
          ...node,
          timestep_index: timestep.timestep_index,
          timestep_label: timestep.label,
          stem: timestep.stem,
          x0: xCursor,
          x1: xCursor + nodeWidth,
          y0: y,
          y1: y + height,
          height
        };
        visibleNodes.push(layoutNode);
        nodeByKey.set(`${layoutNode.timestep_index}:${layoutNode.sheet_id}`, layoutNode);
        y += height + nodeGap;
        columnHeight = y;
      });

      maxColumnHeight = Math.max(maxColumnHeight, columnHeight);
      xCursor += colWidth;
    });

    const height = Math.max(260, maxColumnHeight + bottomPad);
    const width = Math.max(600, xCursor + 48);
    return {
      visibleNodes,
      nodeByKey,
      width,
      height,
      xStart,
      colWidth,
      gapWidth,
      nodeWidth,
      contentMinX: xStart,
      contentMaxX: xCursor
    };
  }

  function buildPanelState(layout) {
    const timeGroups = d3.groups(layout.visibleNodes || [], d => +d.timestep_index)
      .map(([t, ns]) => ({
        timestep: +t,
        x: d3.mean(ns, n => (n.x0 + n.x1) / 2)
      }))
      .sort((a, b) => a.timestep - b.timestep);

    if (!timeGroups.length) return null;

    const minTime = d3.min(timeGroups, d => d.timestep);
    const maxTime = d3.max(timeGroups, d => d.timestep);
    const xDomain = timeGroups.map(d => d.x);
    const tRange = timeGroups.map(d => d.timestep);
    const graphToTime = d3.scaleLinear()
      .domain(xDomain.length === 1 ? [xDomain[0] - 1, xDomain[0] + 1] : xDomain)
      .range(tRange.length === 1 ? [tRange[0] - 0.5, tRange[0] + 0.5] : tRange)
      .clamp(true);

    return {
      minTime,
      maxTime,
      graphToTime
    };
  }

  function clampZoom(scale) {
    return camera ? camera.clampZoom(scale) : Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, scale));
  }

  function isPanTarget(target) {
    if (!target || !(target instanceof Element)) return true;
    return Boolean(
      target.closest(".node, .link, .range-row, .range-selected, .range-hitbox, input, button, select, label, #rangeBar")
    ) === false;
  }

  function fitZoomForBounds(bounds, viewportNode) {
    const width = Math.max(1, viewportNode?.clientWidth || 1);
    const height = Math.max(1, viewportNode?.clientHeight || 1);
    const contentWidth = Math.max(1, (bounds.maxX - bounds.minX) + 220);
    const contentHeight = Math.max(1, (bounds.maxY - bounds.minY) + 130);
    const paddedWidth = Math.max(1, width - 100);
    const paddedHeight = Math.max(1, height - 100);
    return clampZoom(Math.min(paddedWidth / contentWidth, paddedHeight / contentHeight, 1));
  }

  function scheduleViewportUpdate() {
    if (!camera) return;
    camera.scheduleApply();
  }

  function scheduleRenderAll() {
    if (renderAllPending) return;
    renderAllPending = true;
    requestAnimationFrame(() => {
      renderAllPending = false;
      renderAll();
    });
  }

  function scheduleThresholdSync() {
    if (thresholdSyncPending) return;
    thresholdSyncPending = true;
    requestAnimationFrame(() => {
      thresholdSyncPending = false;
      syncThresholdVisibility();
    });
  }

  function syncThresholdVisibility() {
    state.panelViews.forEach((view, panelId) => {
      const panel = getPanelById(panelId);
      if (!panel || !view.linkSelection) return;
      const threshold = clamp(Number(panel.threshold) || 0, 0, 100) / 100;
      view.linkSelection
        .style("display", d => scoreValue(d, panel.scoreMode) >= threshold ? null : "none")
        .style("pointer-events", d => scoreValue(d, panel.scoreMode) >= threshold ? "all" : "none");
    });
    renderStats();
    renderRangeBar();
  }

  function visibleTimestepWindow() {
    const panel = currentPanel();
    const viewFocus = camera?.getViewFocus();
    const zoomScale = camera?.getZoomScale() ?? 1;
    if (!panel || !panel.graphToTime || !panel.canvasNode || !viewFocus) return null;

    const width = Math.max(1, panel.canvasNode.clientWidth || 1);
    const startX = viewFocus.x - width / (2 * zoomScale);
    const endX = viewFocus.x + width / (2 * zoomScale);
    return {
      start: panel.graphToTime(startX),
      end: panel.graphToTime(endX)
    };
  }

  function recenterViewportFromBarIndex(targetTime) {
    const panel = currentPanel();
    window.ReebViewerCommon.recenterViewportFromBarIndex(targetTime, {
      graphToTime: panel?.graphToTime,
      maxTime: timestepMax,
      visibleWindowFn: visibleTimestepWindow,
      getViewFocus: () => camera?.getViewFocus(),
      setViewFocus: nextFocus => camera?.setViewFocus(nextFocus),
      scheduleViewportUpdate
    });
  }

  function applyViewportTransform() {
    const viewFocus = camera?.getViewFocus();
    const zoomScale = camera?.getZoomScale() ?? 1;
    if (!viewFocus) return;

    state.panelViews.forEach(view => {
      const svg = d3.select(view.svgNode);
      const root = svg.select(".sankey-root");
      if (root.empty()) return;

      const width = Math.max(1, view.canvasNode?.clientWidth || 1);
      const height = Math.max(1, view.canvasNode?.clientHeight || 1);
      const translateX = width / 2 - viewFocus.x * zoomScale;
      const translateY = height * VIEWPORT_ANCHOR_Y - viewFocus.y * zoomScale;

      root.attr("transform", `translate(${translateX},${translateY}) scale(${zoomScale})`);

      svg.select(".timestep-label-layer")
        .selectAll("text")
        .attr("x", d => d.x * zoomScale + translateX)
        .each(function(d) {
          d3.select(this)
            .selectAll("tspan")
            .attr("x", d.x * zoomScale + translateX);
        });
    });

    renderRangeBar();
  }

  function setZoomScale(nextScale) {
    camera?.setZoomScale(nextScale);
  }

  function centerSankey() {
    const panel = currentPanel() || state.panelViews.values().next().value || null;
    if (!panel || !panel.layout?.visibleNodes?.length) return;

    const bounds = {
      minX: d3.min(panel.layout.visibleNodes, d => d.x0) ?? 0,
      maxX: d3.max(panel.layout.visibleNodes, d => d.x1) ?? 0,
      minY: d3.min(panel.layout.visibleNodes, d => d.y0) ?? 0,
      maxY: d3.max(panel.layout.visibleNodes, d => d.y1) ?? 0
    };

    camera?.centerOnBounds(bounds, b => fitZoomForBounds(b, panel.canvasNode), true);
  }

  function gatherVisiblePairs(mode, thresholdPercent, nodeByKey) {
    const links = [];
    const ranges = normalizedRanges();
    const threshold = clamp(Number(thresholdPercent) || 0, 0, 100) / 100;
    const visible = new Set();
    for (const range of ranges.length ? ranges : [{ start: 0, end: timestepMax }]) {
      for (let t = range.start; t <= range.end; t += 1) visible.add(t);
    }

    for (const pair of data.pairs) {
      if (!visible.has(pair.source_timestep_index) || !visible.has(pair.target_timestep_index)) continue;

      for (const match of pair.matches) {
        const score = scoreValue(match, mode);
        if (score < threshold) continue;
        const sourceNode = nodeByKey.get(`${pair.source_timestep_index}:${match.source_sheet_id}`);
        const targetNode = nodeByKey.get(`${pair.target_timestep_index}:${match.target_sheet_id}`);
        if (!sourceNode || !targetNode) continue;
        links.push({
          ...match,
          source_timestep_index: pair.source_timestep_index,
          source_label: pair.source_label,
          target_timestep_index: pair.target_timestep_index,
          target_label: pair.target_label,
          sourceNode,
          targetNode,
          width: linkWidth(match, mode),
          opacity: scoreOpacity(match, mode),
          score
        });
      }
    }

    return links;
  }

  function assignLinkOffsets(links) {
    const outgoing = new Map();
    const incoming = new Map();
    const sourceNodes = new Map();
    const targetNodes = new Map();

    for (const link of links) {
      const sourceKey = `${link.source_timestep_index}:${link.source_sheet_id}`;
      const targetKey = `${link.target_timestep_index}:${link.target_sheet_id}`;
      if (!outgoing.has(sourceKey)) outgoing.set(sourceKey, []);
      if (!incoming.has(targetKey)) incoming.set(targetKey, []);
      outgoing.get(sourceKey).push(link);
      incoming.get(targetKey).push(link);
      if (!sourceNodes.has(sourceKey)) sourceNodes.set(sourceKey, link.sourceNode);
      if (!targetNodes.has(targetKey)) targetNodes.set(targetKey, link.targetNode);
    }

    const byDescendingScore = (a, b) => b.score - a.score || a.target_rank - b.target_rank || a.target_sheet_id - b.target_sheet_id;

    for (const [key, list] of outgoing.entries()) {
      list.sort(byDescendingScore);
      const node = sourceNodes.get(key);
      if (!node) continue;
      const total = list.reduce((sum, link) => sum + link.width, 0);
      let offset = node.y0 + Math.max(0, (node.height - total) / 2);
      for (const link of list) {
        link.sourceY0 = offset;
        link.sourceY1 = offset + link.width;
        offset += link.width;
      }
    }

    for (const [key, list] of incoming.entries()) {
      list.sort(byDescendingScore);
      const node = targetNodes.get(key);
      if (!node) continue;
      const total = list.reduce((sum, link) => sum + link.width, 0);
      let offset = node.y0 + Math.max(0, (node.height - total) / 2);
      for (const link of list) {
        link.targetY0 = offset;
        link.targetY1 = offset + link.width;
        offset += link.width;
      }
    }

    return links;
  }

  function ribbonPath(link) {
    const x0 = link.sourceNode.x1;
    const x1 = link.targetNode.x0;
    const y0 = (link.sourceY0 + link.sourceY1) / 2;
    const y1 = (link.targetY0 + link.targetY1) / 2;
    const w0 = link.sourceY1 - link.sourceY0;
    const w1 = link.targetY1 - link.targetY0;
    const top0 = y0 - w0 / 2;
    const bottom0 = y0 + w0 / 2;
    const top1 = y1 - w1 / 2;
    const bottom1 = y1 + w1 / 2;
    const c = Math.max(20, Math.abs(x1 - x0) * 0.5);
    return `M ${x0} ${top0} C ${x0 + c} ${top0}, ${x1 - c} ${top1}, ${x1} ${top1}
L ${x1} ${bottom1} C ${x1 - c} ${bottom1}, ${x0 + c} ${bottom0}, ${x0} ${bottom0} Z`;
  }

  function renderRangeBar() {
    const ranges = normalizedRanges();
    const barNode = document.getElementById("rangeBar");
    const width = Math.max(600, barNode.clientWidth || 600);
    const height = 78;
    const svg = rangeBar.attr("width", width).attr("height", height);

    const tickStep = Math.max(1, Math.ceil(timestepMax / 12));
    window.ReebViewerCommon.renderRangeBar(svg, {
      width,
      height,
      timestepMax,
      tickValues: d3.range(0, timestepMax + 1, tickStep),
      ranges,
      selectedRangeIndex: state.selectedRangeIndex,
      rangeDrag: state.rangeDrag,
      viewportDrag: state.viewportDrag,
      visibleWindow: visibleTimestepWindow,
      rangeLabelFn: range => rangeLabel(range),
      tickLabelFn: value => {
        const ts = timestepByIndex.get(value);
        return ts ? ts.label : value;
      },
      onRangeSelected: index => {
        applyRangeAction({ type: "select", index });
        renderAll();
      },
      onRangeDragStart: idx => {
        applyRangeAction({ type: "drag-start", index: idx });
        renderRangeBar();
      },
      onRangeDragMove: idx => {
        applyRangeAction({ type: "drag-move", index: idx });
        renderRangeBar();
      },
      onRangeDragEnd: idx => {
        applyRangeAction({ type: "drag-move", index: idx });
        const previousCount = state.ranges.length;
        applyRangeAction({ type: "drag-commit" });
        if (state.ranges.length === previousCount) {
          renderRangeBar();
          return;
        }
        renderAll();
      },
      onViewportClick: idx => {
        recenterViewportFromBarIndex(idx);
      },
      onViewportDragStart: () => {
        state.viewportDrag = { active: true };
        renderRangeBar();
      },
      onViewportDragMove: idx => {
        recenterViewportFromBarIndex(idx);
      },
      onViewportDragEnd: () => {
        state.viewportDrag = null;
        renderRangeBar();
      }
    });
  }

  function renderStats() {
    const ranges = normalizedRanges();
    const visible = visibleTimesteps();
    const visibleNodes = visible.reduce((sum, t) => sum + (t.sheets?.length || 0), 0);
    const visiblePairs = data.pairs.filter(p => ranges.some(r => p.source_timestep_index >= r.start && p.target_timestep_index <= r.end)).length;

    const entries = [
      ["Timesteps", `${visible.length} / ${data.timesteps.length}`],
      ["Nodes", String(visibleNodes)],
      ["Pairs", String(visiblePairs)],
      ["Ranges", String(ranges.length)],
      ["Max area", formatScore(areaMax)],
    ];

    stats.html("");
    const grid = stats.append("div").attr("class", "stats-grid");
    entries.forEach(([label, value]) => {
      grid.append("div").text(label);
      grid.append("div").text(value);
    });
  }

  function renderPanel(panel) {
    const container = panel.container;
    container.html("");

    const header = container.append("div").attr("class", "panel-header");
    const title = header.append("div").attr("class", "panel-title");
    title.append("strong").text(`Score view ${panel.id}`);
    title.append("span").style("font-size", "12px").style("color", "#5b6673").text(scoreModeLabel(panel.scoreMode));

    const controls = header.append("div").attr("class", "panel-controls");
    const modeSelect = controls.append("select");
    scoreModes.forEach(mode => {
      modeSelect.append("option")
        .attr("value", mode.id)
        .property("selected", mode.id === panel.scoreMode)
        .text(mode.label);
    });
    modeSelect.on("change", event => {
      panel.scoreMode = event.target.value;
      renderAll();
    });

    const thresholdRange = controls.append("input")
      .attr("type", "range")
      .attr("min", 0)
      .attr("max", 100)
      .attr("step", 0.5)
      .property("value", panel.threshold);
    const thresholdBox = controls.append("input")
      .attr("type", "number")
      .attr("min", 0)
      .attr("max", 100)
      .attr("step", 0.5)
      .property("value", panel.threshold);
    const deleteButton = controls.append("button").text("Remove");

    window.ReebViewerCommon.bindThresholdControl({
      slider: thresholdRange.node(),
      box: thresholdBox.node(),
      min: 0,
      max: 100,
      step: 0.5,
      initialValue: panel.threshold,
      onPreview: value => {
        panel.threshold = clamp(Number(value) || 0, 0, 100);
        scheduleThresholdSync();
      },
      onCommit: value => {
        panel.threshold = clamp(Number(value) || 0, 0, 100);
        scheduleThresholdSync();
      }
    });

    deleteButton.on("click", () => {
      state.panels = state.panels.filter(item => item.id !== panel.id);
      if (!state.panels.length) {
        state.panels.push({ id: state.nextPanelId++, scoreMode: "combined", threshold: 0 });
      }
      renderAll();
    });

    const canvas = container.append("div").attr("class", "panel-canvas");
    const svg = canvas.append("svg").attr("class", "summary-chart");

    const columns = buildVisibleColumns();
    const layout = layoutForPanel(columns);
    const links = assignLinkOffsets(gatherVisiblePairs(panel.scoreMode, 0, layout.nodeByKey));
    const timestepLabels = d3.groups(layout.visibleNodes || [], d => +d.timestep_index)
      .map(([timestepIndex, nodes]) => ({
        x: d3.mean(nodes, n => (n.x0 + n.x1) / 2),
        index: +timestepIndex,
        label: nodes[0]?.timestep_label ?? timestepIndex
      }))
      .sort((a, b) => a.index - b.index);

    if (!layout.visibleNodes.length) {
      canvas.append("div").attr("class", "panel-empty").text("No timesteps in the selected range.");
      return;
    }

    const canvasWidth = Math.max(1, canvas.node().clientWidth || 1);
    const canvasHeight = Math.max(1, canvas.node().clientHeight || 1);
    const svgWidth = Math.max(layout.width, canvasWidth);
    const svgHeight = Math.max(layout.height, canvasHeight);
    svg.attr("width", svgWidth).attr("height", svgHeight);
    const barNode = svg.node();
    const root = svg.append("g").attr("class", "sankey-root");
    const labelLayer = svg.append("g").attr("class", "timestep-label-layer");

    const layoutBounds = {
      minX: d3.min(layout.visibleNodes, d => d.x0) ?? 0,
      maxX: d3.max(layout.visibleNodes, d => d.x1) ?? 0,
      minY: d3.min(layout.visibleNodes, d => d.y0) ?? 0,
      maxY: d3.max(layout.visibleNodes, d => d.y1) ?? 0
    };

    if (!camera.getViewFocus()) {
      camera.centerOnBounds(layoutBounds, b => fitZoomForBounds(b, canvas.node()), true);
    }

    state.panelViews.set(panel.id, {
      canvasNode: canvas.node(),
      svgNode: svg.node(),
      graphToTime: buildPanelState(layout)?.graphToTime || null,
      layout,
      linkSelection: null
    });

    labelLayer.selectAll("text")
      .data(timestepLabels)
      .join("text")
      .attr("class", "timestep-label")
      .attr("x", d => d.x)
      .attr("y", 20)
      .attr("text-anchor", "middle")
      .attr("dominant-baseline", "middle")
      .attr("font-size", 13)
      .each(function(d) {
        window.ReebViewerCommon.appendTimestepLabel(d3.select(this), d, {
          indexAccessor: item => item.index,
          labelAccessor: item => item.label,
          divisor: 41.341374575751,
          digits: 2
        });
      });

    const linkSelection = root.append("g")
      .selectAll("path")
      .data(links, d => `${d.source_timestep_index}:${d.source_sheet_id}->${d.target_timestep_index}:${d.target_sheet_id}`)
      .join("path")
      .attr("class", "link global-link")
      .attr("d", ribbonPath)
      .on("mousemove", (event, d) => updateTooltip(linkTooltip(d, panel.scoreMode), event.clientX, event.clientY))
      .on("mouseleave", hideTooltip);

    state.panelViews.get(panel.id).linkSelection = linkSelection;

    const node = root.append("g")
      .selectAll("g")
      .data(layout.visibleNodes)
      .join("g")
      .attr("class", "node")
      .on("mousemove", (event, d) => updateTooltip(nodeTooltip(d), event.clientX, event.clientY))
      .on("mouseleave", hideTooltip);

    node.append("rect")
      .attr("x", d => d.x0)
      .attr("y", d => d.y0)
      .attr("width", d => d.x1 - d.x0)
      .attr("height", d => Math.max(2, d.height));

    node.append("text")
      .attr("x", d => d.x1 + 5)
      .attr("y", d => d.y0 + Math.max(2, d.height) / 2)
      .attr("dominant-baseline", "middle")
      .text(d => `S${d.sheet_id} R${d.rank}`);

    const canvasNode = canvas.node();
    camera.bindPanAndWheel(canvasNode, {
      cursorTarget: canvasNode,
      isPanTarget: target => !target.closest(".node, .link, button, input, select, label"),
      ensureFocus: () => {
        state.activePanelId = panel.id;
        const focus = camera.getViewFocus();
        if (focus) return focus;
        return {
          x: (layoutBounds.minX + layoutBounds.maxX) / 2,
          y: (layoutBounds.minY + layoutBounds.maxY) / 2
        };
      },
      onActive: () => {
        state.activePanelId = panel.id;
      },
      onPanState: active => {
        canvas.classed("dragging", active);
      }
    });

    canvasNode.addEventListener("mouseenter", () => {
      state.activePanelId = panel.id;
      renderRangeBar();
    });

    syncThresholdVisibility();
  }

  function renderPanels() {
    const panels = panelList.selectAll(".panel")
      .data(state.panels, d => d.id)
      .join(enter => enter.append("div").attr("class", "panel"));

    panels.each(function(panel) {
      panel.container = d3.select(this);
      renderPanel(panel);
    });
  }

  function renderAll() {
    state.panelViews = new Map();
    state.ranges = normalizedRanges();
    state.selectedRangeIndex = applyRangeAction({ type: "normalize" }).selectedRangeIndex;
    renderRangeRows();
    renderStats();
    renderPanels();
    if (camera.getViewFocus()) {
      applyViewportTransform();
    } else {
      renderRangeBar();
    }
  }

  function addPanel() {
    state.panels.push({ id: state.nextPanelId++, scoreMode: "combined", threshold: 0 });
    renderAll();
  }

  document.getElementById("addRange").addEventListener("click", addRange);
  document.getElementById("deleteRange").addEventListener("click", () => removeRange(state.selectedRangeIndex));
  document.getElementById("addPanel").addEventListener("click", addPanel);
  document.getElementById("zoomOut").addEventListener("click", () => camera.zoomBy(1 / camera.zoomStep));
  document.getElementById("zoomIn").addEventListener("click", () => camera.zoomBy(camera.zoomStep));
  document.getElementById("centerView").addEventListener("click", () => centerSankey());

  window.ReebViewerCommon.bindKeyboardShortcuts({
    target: document,
    onDeleteRange: () => removeRange(state.selectedRangeIndex),
    onZoomIn: () => camera.zoomBy(camera.zoomStep),
    onZoomOut: () => camera.zoomBy(1 / camera.zoomStep)
  });

  window.addEventListener("resize", () => renderRangeBar());

  tooltipEngine = window.ReebViewerCommon.createTooltipEngine(tooltip, {
    hiddenClass: "hidden",
    edgePad: 12,
    offsetX: 14,
    offsetY: 14
  });
  camera = window.ReebViewerCommon.createCameraController({
    zoomMin: ZOOM_MIN,
    zoomMax: ZOOM_MAX,
    zoomStep: ZOOM_STEP,
    panDragThreshold: PAN_DRAG_THRESHOLD,
    applyTransform: () => applyViewportTransform()
  });
  camera.setZoomScale(1);
  camera.clearViewFocus();
  renderAll();
}).catch(error => {
  console.error(error);
  document.body.insertAdjacentHTML("beforeend", `<pre style="padding:16px;color:#b00020;">${error}</pre>`);
});
"""
    )
    return path


def build_match_summary_viewer_stage() -> None:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Expected match results at {MATCHES_FILE}")

    if MATCH_SUMMARY_VIEWER_DIR.exists():
        shutil.rmtree(MATCH_SUMMARY_VIEWER_DIR)
    MATCH_SUMMARY_VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    link_sheet_images(MATCH_SUMMARY_VIEWER_DIR)

    data = prepare_data(MATCH_SUMMARY_VIEWER_DIR)
    data_path = write_data_json(data)
    index_path = write_index_html()
    js_path = write_viewer_js()
    css_path = write_style_css()
    common_js_path = write_viewer_common_js(MATCH_SUMMARY_VIEWER_DIR)

    print(f"Wrote match summary viewer: {MATCH_SUMMARY_VIEWER_DIR}")
    for artifact in (data_path, index_path, js_path, css_path, common_js_path):
      print(f"  {artifact.name}")
    print("\nOpen with:")
    print(f"  cd {MATCH_SUMMARY_VIEWER_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


def main() -> int:
    build_match_summary_viewer_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
