#!/usr/bin/env python3

import json
import shutil
from pathlib import Path

from common import OVERLAP_FILE, SHEET_IMAGE_DIR, VIEWER_DIR


def load_data():
    if not OVERLAP_FILE.exists():
        raise FileNotFoundError(f"Overlap file does not exist: {OVERLAP_FILE}")
    return json.loads(OVERLAP_FILE.read_text())


def relative_path(path, base):
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def find_sheet_image(node, viewer_dir):
    rsi_file = node.get("rsi_file") or ""
    rsijson_file = node.get("rsijson_file") or ""
    timestep_stem = Path(rsi_file).stem or Path(rsijson_file).stem
    sheet_id = node.get("sheet_id")

    if not timestep_stem or sheet_id is None:
        return None

    folder = SHEET_IMAGE_DIR / timestep_stem
    if not folder.exists():
        return None

    matches = sorted(folder.glob(f"{sheet_id}_*.png"))
    if not matches:
        return None

    linked_image = viewer_dir / "sheet_images" / timestep_stem / matches[0].name
    return linked_image.relative_to(viewer_dir).as_posix()


def link_sheet_images(viewer_dir):
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
        # Fall back to copying only if symlinks are unavailable. This can be
        # larger, but keeps browser-relative image paths working.
        shutil.copytree(SHEET_IMAGE_DIR, target)


def color_from_thumbnail(thumbnail):
    if not thumbnail:
        return None
    stem = Path(thumbnail).stem
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    hex_color = parts[-1]
    if len(hex_color) == 6 and all(c in "0123456789abcdefABCDEF" for c in hex_color):
        return f"#{hex_color.lower()}"
    return None


def prepare_data(data, viewer_dir):
    nodes = []
    for node in data.get("nodes", []):
        item = dict(node)
        thumbnail = item.get("thumbnail") or item.get("image") or find_sheet_image(item, viewer_dir)
        if thumbnail:
            item["thumbnail"] = thumbnail
            item["image"] = thumbnail
        color = item.get("color") or color_from_thumbnail(thumbnail)
        if color:
            item["color"] = color
        nodes.append(item)

    return {
        **data,
        "nodes": nodes,
        "links": [dict(link) for link in data.get("links", [])],
        "viewer": {
            "generated_from": str(OVERLAP_FILE),
            "sheet_image_dir": str(SHEET_IMAGE_DIR),
        },
    }


def write_data_json(data):
    path = VIEWER_DIR / "data.json"
    path.write_text(json.dumps(data, indent=2, allow_nan=False))
    return path


def write_index_html():
    path = VIEWER_DIR / "index.html"
    path.write_text(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Interactive Reeb Sheet Sankey</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <div>
      <h1>Interactive Reeb Sheet Sankey</h1>
      <p>Filter overlaps, select timestep ranges, inspect sheet transitions.</p>
    </div>
    <div style="display:flex; gap:8px;">
      <button id="zoomOut">Zoom out</button>
      <button id="zoomIn">Zoom in</button>
      <button id="centerView">Center sankey</button>
    </div>
  </header>

  <main>
    <aside id="controls">
      <section>
        <h2>Overlap</h2>
        <label>
          Minimum percent
          <input id="threshold" type="range" min="0" max="100" step="0.5" value="0">
          <input id="thresholdBox" type="number" min="0" max="100" step="0.5" value="0" aria-label="Minimum percent">
          <span id="thresholdValue">0%</span>
        </label>
        <label>
          Percent mode
          <select id="percentMode">
            <option value="max" selected>max(source, target)</option>
            <option value="source">source_percent</option>
            <option value="target">target_percent</option>
          </select>
        </label>
        <label class="inline">
          <input id="hideIsolated" type="checkbox" checked>
          Hide nodes with no visible links
        </label>
      </section>

      <section>
        <h2>Timestep Ranges</h2>
        <div id="rangeRows"></div>
        <button id="addRange">+ Add range</button>
        <button id="deleteRange">Delete selected range</button>
        <p class="hint">Drag on the bar to create a range. Click a range to select it. Delete removes the selected range.</p>
      </section>

      <section>
        <h2>Ordering</h2>
        <label>
          Node ordering
          <select id="ordering">
            <option value="area" selected>decreasing area</option>
            <option value="rank">increasing rank</option>
            <option value="crossings">crossing-minimized</option>
          </select>
        </label>
        <label>
          Node size
          <select id="nodeSizeMode">
            <option value="area" selected>sheet area</option>
            <option value="sankey">vertex count</option>
          </select>
        </label>
        <label>
          Node scaling
          <select id="nodeSizeScaleMode">
            <option value="local" selected>local scaling</option>
            <option value="global">global scaling</option>
          </select>
        </label>
      </section>

      <section>
        <h2>Stats</h2>
        <dl id="stats"></dl>
      </section>
    </aside>

    <section id="viewer">
      <div id="minimap"></div>
      <div id="chartWrap">
        <svg id="chart"></svg>
      </div>
    </section>

    <aside id="details">
      <h2>Details</h2>
      <div id="detailsContent">Click a node or link.</div>
    </aside>
  </main>

  <div id="tooltip"></div>

  <script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
  <script src="https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/dist/d3-sankey.min.js"></script>
  <script src="viewer.js"></script>
</body>
</html>
"""
    )
    return path


def write_style_css():
    path = VIEWER_DIR / "style.css"
    path.write_text(
        """* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #1d252d;
  background: #f6f7f9;
}
header {
  height: 76px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  background: #fff;
  border-bottom: 1px solid #d9dee5;
}
h1 { margin: 0; font-size: 20px; }
h2 { margin: 0 0 10px; font-size: 14px; }
p { margin: 4px 0 0; color: #5b6673; }
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
main {
  height: calc(100vh - 76px);
  display: grid;
  grid-template-columns: 290px minmax(0, 1fr) 340px;
  min-height: 0;
}
aside {
  overflow: auto;
  background: #fff;
  border-right: 1px solid #d9dee5;
}
#details { border-right: 0; border-left: 1px solid #d9dee5; }
section { padding: 14px; border-bottom: 1px solid #edf0f3; }
label { display: grid; gap: 5px; margin: 10px 0; font-size: 13px; }
label.inline { display: flex; align-items: center; gap: 8px; }
.range-row {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 6px;
  margin-bottom: 6px;
  padding: 4px;
  border: 1px solid transparent;
  border-radius: 5px;
  cursor: pointer;
}
.range-row.selected { border-color: #2f80c9; background: #edf6ff; }
.range-row input { width: 100%; cursor: text; }
.hint { font-size: 12px; color: #71808f; }
#viewer { min-width: 0; min-height: 0; display: grid; grid-template-rows: 88px minmax(0, 1fr); }
#minimap { background: #fff; border-bottom: 1px solid #d9dee5; }
#chartWrap { overflow: hidden; position: relative; background: #fff; min-height: 0; }
#chart { display: block; background: #fff; }
.node rect { cursor: pointer; stroke: rgba(20, 30, 40, 0.4); stroke-width: 0.6; fill: #6f9ed4; }
.node text { font-size: 13px; font-weight: 600; pointer-events: none; fill: #15202b; }
.link { fill: rgba(80, 80, 80, 0.16); stroke: none; cursor: pointer; }
.link.global-link-mode { fill: rgba(48, 60, 74, 0.55); }
.link:hover { fill: rgba(45, 65, 85, 0.42); }
.link.global-link-mode:hover { fill: rgba(34, 46, 58, 0.65); }
.context-node { fill: #6f7d8b; opacity: 0.10; pointer-events: none; }
.context-link { fill: none; stroke: #6f7d8b; stroke-width: 0.7; opacity: 0.06; pointer-events: none; }
.context-range { fill: rgba(120, 130, 140, 0.12); opacity: 1; pointer-events: none; }
.timestep-label { font-size: 13px; font-weight: 600; fill: #465360; }
.range-bg { fill: #e9edf2; }
.range-drag-surface { fill: transparent; cursor: crosshair; pointer-events: all; }
.range-drag-preview { fill: rgba(47, 128, 201, 0.18); stroke: #2f80c9; stroke-width: 1; pointer-events: none; }
.range-selected { fill: #6aa3d8; opacity: 0.55; cursor: pointer; pointer-events: all; }
.range-selected:hover { opacity: 0.82; stroke: #15202b; stroke-width: 1; }
.range-selected.selected { fill: #2f80c9; opacity: 0.9; stroke: #15202b; stroke-width: 1.1; }
.range-hitbox { fill: transparent; cursor: pointer; pointer-events: all; }
.viewport-window { fill: rgba(0, 0, 0, 0.22); stroke: #000; stroke-width: 1.2; pointer-events: none; }
.overflow-indicator { fill: #d64a3a; opacity: 0.9; }
.overflow-label { font-size: 11px; fill: #8f2d22; font-weight: 700; }
.brush .selection { fill: #2f80c9; fill-opacity: 0.22; stroke: #2f80c9; }
#tooltip {
  position: fixed;
  display: none;
  pointer-events: none;
  background: rgba(22, 28, 34, 0.94);
  color: #fff;
  padding: 7px 9px;
  border-radius: 5px;
  font-size: 12px;
  max-width: 520px;
  z-index: 10;
}
.tooltip-thumb { max-width: 230px; max-height: 180px; border: 1px solid rgba(255,255,255,0.35); background: #fff; margin-top: 6px; }
.tooltip-thumb-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px; }
.tooltip-thumb-row img { width: 100%; max-height: 160px; object-fit: contain; border: 1px solid rgba(255,255,255,0.35); background: #fff; }
.tooltip-caption { color: #d8e0e8; font-size: 11px; margin-top: 3px; }
#detailsContent { padding: 14px; font-size: 13px; }
.meta { width: 100%; border-collapse: collapse; }
.meta td { border-bottom: 1px solid #edf0f3; padding: 5px 0; vertical-align: top; }
.meta td:first-child { color: #687583; width: 42%; padding-right: 8px; }
.thumb { max-width: 100%; border: 1px solid #d9dee5; background: #fff; margin: 8px 0 12px; }
.thumb-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
dl { display: grid; grid-template-columns: 1fr auto; gap: 6px 10px; margin: 0; }
dt { color: #687583; } dd { margin: 0; font-weight: 600; }
"""
    )
    return path


def write_viewer_js():
    path = VIEWER_DIR / "viewer.js"
    path.write_text(
r"""let fullData = null;
let globalAreaColumnMax = 0;
let globalAreaMaxNodesPerColumn = 0;
let globalAreaNodeMax = 0;
let globalAreaMinPositive = 0;
let globalLinkMax = 0;
let globalVertexScale = 1;
let ranges = [];
let selectedRangeIndex = 0;
let lastGraph = null;
let rangeDrag = null;
let minimapBarScale = null;
let minimapBarMaxIndex = 0;
let minimapState = null;
let zoomScale = 1;
let viewFocus = null;
let panDrag = null;
let viewportUpdatePending = false;

const BASE_COLUMN_SPACING = 190;
const BASE_MARGIN_X = 280;
const BASE_ROW_HEIGHT = 55;
const BASE_MARGIN_Y = 600;
const VIEWPORT_ANCHOR_Y = 0.38;
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 20;
const ZOOM_STEP = 1.2;
const PAN_DRAG_THRESHOLD = 4;

const chart = d3.select("#chart");
const tooltip = d3.select("#tooltip");
const chartWrap = document.getElementById("chartWrap");

const debounce = (fn, wait = 180) => {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
};

function getControls() {
  return {
    threshold: +document.getElementById("threshold").value,
    percentMode: document.getElementById("percentMode").value,
    hideIsolated: document.getElementById("hideIsolated").checked,
    ordering: document.getElementById("ordering").value,
    nodeSizeMode: document.getElementById("nodeSizeMode").value,
    nodeSizeScaleMode: document.getElementById("nodeSizeScaleMode").value,
    ranges: normalizedRanges()
  };
}

function setThresholdValue(value, triggerRender = false) {
  const slider = document.getElementById("threshold");
  const box = document.getElementById("thresholdBox");
  const label = document.getElementById("thresholdValue");
  const next = Math.max(+slider.min || 0, Math.min(+slider.max || 100, Number(value)));
  const safe = Number.isFinite(next) ? next : 0;

  slider.value = String(safe);
  box.value = String(safe);
  label.textContent = `${safe}%`;

  if (triggerRender) {
    renderSankey({ preserveFocus: true });
  }
}

function normalizedRanges() {
  return ranges
    .map(r => ({
      start: Math.min(+r.start, +r.end),
      end: Math.max(+r.start, +r.end)
    }))
    .filter(r => Number.isFinite(r.start) && Number.isFinite(r.end));
}

function percentValue(link, mode) {
  if (mode === "source") return +link.source_percent || 0;
  if (mode === "target") return +link.target_percent || 0;
  return Math.max(+link.source_percent || 0, +link.target_percent || 0);
}

function inRanges(timestep, selectedRanges) {
  if (!selectedRanges.length) return true;
  return selectedRanges.some(r => timestep >= r.start && timestep <= r.end);
}

function filterData() {
  const controls = getControls();

  let nodes = fullData.nodes.filter(n =>
    inRanges(+n.timestep_index, controls.ranges)
  );

  const allowedNodeIds = new Set(nodes.map(n => n.id));

  let links = fullData.links.filter(l =>
    allowedNodeIds.has(l.source) &&
    allowedNodeIds.has(l.target) &&
    percentValue(l, controls.percentMode) >= controls.threshold
  );

  if (controls.hideIsolated) {
    const used = new Set();
    links.forEach(l => {
      used.add(l.source);
      used.add(l.target);
    });
    nodes = nodes.filter(n => used.has(n.id));
  }

  const finalNodeIds = new Set(nodes.map(n => n.id));
  links = links.filter(l =>
    finalNodeIds.has(l.source) && finalNodeIds.has(l.target)
  );

  return {
    nodes: nodes.map(n => ({ ...n })),
    links: links.map(l => ({ ...l })),
    controls
  };
}

function buildDisplayLayout(timestepValues, selectedRanges) {
  const orderedRanges = selectedRanges.length
    ? selectedRanges.slice().sort((a, b) => a.start - b.start)
    : [{
        start: d3.min(timestepValues) ?? 0,
        end: d3.max(timestepValues) ?? 0
      }];

  const displayOrder = new Map();
  const visibleTimesteps = [];
  let displayIndex = 0;
  const gapColumns = 3;

  orderedRanges.forEach((range, rangeIndex) => {
    const values = timestepValues.filter(t => t >= range.start && t <= range.end);

    values.forEach(t => {
      if (!displayOrder.has(t)) {
        displayOrder.set(t, displayIndex++);
        visibleTimesteps.push(t);
      }
    });

    if (rangeIndex < orderedRanges.length - 1 && values.length) {
      displayIndex += gapColumns;
    }
  });

  return {
    orderedRanges,
    displayOrder,
    visibleTimesteps,
    displayColumnCount: Math.max(1, displayIndex),
    gapColumns
  };
}

function clampZoom(scale) {
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, scale));
}

function isPanTarget(target) {
  if (!target || !(target instanceof Element)) return true;
  return Boolean(
    target.closest(".node, .link, .range-row, .range-hitbox, .range-selected, input, button, select, label, #minimap")
  ) === false;
}

function setZoomScale(nextScale, focus = null) {
  const clamped = clampZoom(nextScale);
  if (Math.abs(clamped - zoomScale) < 1e-9) return;

  zoomScale = clamped;
  if (focus && Number.isFinite(focus.x) && Number.isFinite(focus.y)) {
    viewFocus = { x: focus.x, y: focus.y };
  }
  scheduleViewportUpdate();
}

function scheduleViewportUpdate() {
  if (viewportUpdatePending || !lastGraph) return;
  viewportUpdatePending = true;
  requestAnimationFrame(() => {
    viewportUpdatePending = false;
    applyViewportTransform();
  });
}

function orderNodes(nodes, links, mode) {
  const byTime = d3.group(nodes, d => +d.timestep_index);
  const columns = [...byTime.entries()]
    .sort((a, b) => d3.ascending(+a[0], +b[0]))
    .map(([t, column]) => ({ t: +t, column }));

  if (mode === "crossings") {
    const nodeById = new Map(nodes.map(node => [node.id, node]));
    const orderById = new Map();

    const rankComparator = (a, b) =>
      d3.ascending(a.rank ?? 999999, b.rank ?? 999999) ||
      d3.descending(+a.area || 0, +b.area || 0) ||
      d3.ascending(String(a.id), String(b.id));

    for (const { column } of columns) {
      column.sort(rankComparator);
      column.forEach((node, i) => orderById.set(node.id, i));
    }

    const barycenter = (node, direction) => {
      const neighbors = links
        .filter(link =>
          direction === "left"
            ? link.target === node.id
            : link.source === node.id
        )
        .map(link =>
          direction === "left"
            ? nodeById.get(link.source)
            : nodeById.get(link.target)
        )
        .filter(neighbor =>
          neighbor &&
          (direction === "left"
            ? +neighbor.timestep_index < +node.timestep_index
            : +neighbor.timestep_index > +node.timestep_index)
        );

      if (!neighbors.length) return null;
      return d3.mean(neighbors, neighbor => orderById.get(neighbor.id) ?? 0);
    };

    const compareByBarycenter = (a, b, direction) => {
      const baryA = barycenter(a, direction);
      const baryB = barycenter(b, direction);
      const aKey = baryA === null ? Infinity : baryA;
      const bKey = baryB === null ? Infinity : baryB;
      return (
        d3.ascending(aKey, bKey) ||
        rankComparator(a, b)
      );
    };

    for (let iter = 0; iter < 4; iter++) {
      for (let i = 1; i < columns.length; i++) {
        const column = columns[i].column;
        column.sort((a, b) => compareByBarycenter(a, b, "left"));
        column.forEach((node, order) => orderById.set(node.id, order));
      }

      for (let i = columns.length - 2; i >= 0; i--) {
        const column = columns[i].column;
        column.sort((a, b) => compareByBarycenter(a, b, "right"));
        column.forEach((node, order) => orderById.set(node.id, order));
      }
    }

    nodes.sort(
      (a, b) =>
        d3.ascending(+a.timestep_index, +b.timestep_index) ||
        d3.ascending(orderById.get(a.id) ?? 0, orderById.get(b.id) ?? 0)
    );
    return;
  }

  for (const [, column] of byTime) {
    column.sort((a, b) => {
      if (mode === "rank") {
        return d3.ascending(a.rank ?? 999999, b.rank ?? 999999);
      }

      return (
        d3.descending(+a.area || 0, +b.area || 0) ||
        d3.ascending(a.rank ?? 999999, b.rank ?? 999999)
      );
    });

    column.forEach((node, i) => {
      node._order = i;
    });
  }

  nodes.sort(
    (a, b) =>
      d3.ascending(+a.timestep_index, +b.timestep_index) ||
      d3.ascending(a._order, b._order)
  );
}

function applyTemporalXPositions(graph, layout) {
  const left = 70;
  const right = 110 + layout.displayColumnCount * BASE_COLUMN_SPACING;
  const nodeWidth = 12;
  const maxColumn = Math.max(0, layout.displayColumnCount - 1);

  for (const node of graph.nodes) {
    const col = layout.displayOrder.get(+node.timestep_index) ?? 0;
    const x = maxColumn === 0
      ? (left + right) / 2
      : left + (right - left) * (col / maxColumn);

    node.x0 = x;
    node.x1 = x + nodeWidth;
  }
}

function applyOrderedYPositions(graph, sizeMode, scaleMode) {
  const top = 50;
  const bottom = 1150;
  const minNodeHeight = 5;
  const nodeGap = 18;
  globalVertexScale = 1;

  const columns = [...d3.group(graph.nodes, d => +d.timestep_index).entries()]
    .sort((a, b) => d3.ascending(+a[0], +b[0]))
    .map(([t, column]) => ({ t: +t, column }));

  if (sizeMode === "sankey" && scaleMode === "global") {
    const columnScales = columns.map(({ column }) => {
      const total = d3.sum(column, node => Math.max(0, +node.num_vertices || 0));
      const available = Math.max(1, bottom - top - nodeGap * Math.max(0, column.length - 1));
      return total > 0 ? available / total : Infinity;
    }).filter(Number.isFinite);
    globalVertexScale = columnScales.length ? d3.min(columnScales) : 1;
  }

  for (const { column } of columns) {
    column.sort((a, b) =>
      d3.ascending(a._order ?? 999999, b._order ?? 999999)
    );

    const metrics = column.map(node => {
      if (sizeMode === "sankey") {
        if (scaleMode === "global") {
          return Math.max(0, +node.num_vertices || 0);
        }

        const sourceTotal = d3.sum(node.sourceLinks || [], link => Math.max(0, +link.value || +link.overlap_vertices || 0));
        const targetTotal = d3.sum(node.targetLinks || [], link => Math.max(0, +link.value || +link.overlap_vertices || 0));
        return Math.max(sourceTotal, targetTotal);
      }

      return Math.max(0, +node.area || 0);
    });

    const fallbackHeight = minNodeHeight;
    const available = Math.max(1, bottom - top - nodeGap * Math.max(0, column.length - 1));
    const heights = column.map((node, i) => {
      if (sizeMode === "sankey" && scaleMode === "global") {
        return Math.max(minNodeHeight, metrics[i] * globalVertexScale);
      }

      const totalMetric = d3.sum(metrics);
      return totalMetric > 0
        ? Math.max(minNodeHeight, available * metrics[i] / totalMetric)
        : fallbackHeight;
    });
    let y = top;

    column.forEach((node, i) => {
      const h = heights[i];

      node.y0 = y;
      node.y1 = y + h;
      y = node.y1 + nodeGap;
    });
  }

  for (const node of graph.nodes) {
    assignLinkOffsets(node, node.sourceLinks || [], "y0", sizeMode, scaleMode);
    assignLinkOffsets(node, node.targetLinks || [], "y1", sizeMode, scaleMode);
  }
  if (sizeMode === "sankey") {
    return;
  }
}

function graphBounds(graph) {
  if (!graph.nodes.length) {
    return { minX: 0, maxX: 0, minY: 0, maxY: 0 };
  }

  return {
    minX: d3.min(graph.nodes, d => d.x0) ?? 0,
    maxX: d3.max(graph.nodes, d => d.x1) ?? 0,
    minY: d3.min(graph.nodes, d => d.y0) ?? 0,
    maxY: d3.max(graph.nodes, d => d.y1) ?? 0
  };
}

function fitZoomForBounds(bounds) {
  const width = Math.max(1, chartWrap.clientWidth);
  const height = Math.max(1, chartWrap.clientHeight);
  const contentWidth = Math.max(1, (bounds.maxX - bounds.minX) + 260);
  const contentHeight = Math.max(1, (bounds.maxY - bounds.minY) + 140);
  const paddedWidth = Math.max(1, width - 120);
  const paddedHeight = Math.max(1, height - 120);
  const fit = Math.min(paddedWidth / contentWidth, paddedHeight / contentHeight, 1);
  return clampZoom(fit);
}

function assignLinkOffsets(node, links, key, sizeMode, scaleMode) {
  if (!links.length) return;

  links.sort((a, b) => {
    const ay = key === "y0"
      ? (a.target.y0 + a.target.y1) / 2
      : (a.source.y0 + a.source.y1) / 2;
    const by = key === "y0"
      ? (b.target.y0 + b.target.y1) / 2
      : (b.source.y0 + b.source.y1) / 2;
    return d3.ascending(ay, by);
  });

  const linkValue = d => Math.max(0, +d.overlap_vertices || +d.value || 0);

  let y = node.y0;
  const useGlobalVertexScale = sizeMode === "sankey" && scaleMode === "global";
  const useGlobalLinkScale = sizeMode === "area" && scaleMode === "global";
  const globalLinkScale = useGlobalLinkScale && globalLinkMax > 0
    ? 42 / globalLinkMax
    : null;

  if (useGlobalVertexScale) {
    for (const link of links) {
      const v = linkValue(link);
      const h = Math.max(1, v * globalVertexScale);
      const y0 = y;
      const y1 = y + h;

      link[key] = (y0 + y1) / 2;
      if (key === "y0") {
        link._sourceY0 = y0;
        link._sourceY1 = y1;
      } else {
        link._targetY0 = y0;
        link._targetY1 = y1;
      }

      y = y1;
    }
    return;
  }

  if (!useGlobalLinkScale) {
    const nodeHeight = Math.max(1, node.y1 - node.y0);
    const total = d3.sum(links, linkValue);
    if (total === 0) return;

    for (const link of links) {
      const v = linkValue(link);
      const h = nodeHeight * v / total;
      const y0 = y;
      const y1 = y + h;

      link[key] = (y0 + y1) / 2;
      if (key === "y0") {
        link._sourceY0 = y0;
        link._sourceY1 = y1;
      } else {
        link._targetY0 = y0;
        link._targetY1 = y1;
      }

      y = y1;
    }

    return;
  }

  for (const link of links) {
    const v = linkValue(link);
    const h = Math.max(1.8, v * globalLinkScale);
    const y0 = y;
    const y1 = y + h;

    link[key] = (y0 + y1) / 2;
    if (key === "y0") {
      link._sourceY0 = y0;
      link._sourceY1 = y1;
    } else {
      link._targetY0 = y0;
      link._targetY1 = y1;
    }

    y = y1;
  }
}

function sankeyRibbonPath(link) {
  const source = link.source;
  const target = link.target;
  const x0 = source.x1;
  const x1 = target.x0;
  const y0 = link._sourceY0 ?? source.y0;
  const y1 = link._sourceY1 ?? source.y1;
  const y2 = link._targetY0 ?? target.y0;
  const y3 = link._targetY1 ?? target.y1;
  const curvature = 0.5;
  const xi = x0 + (x1 - x0) * curvature;
  const xj = x1 - (x1 - x0) * curvature;

  return [
    `M${x0},${y0}`,
    `C${xi},${y0} ${xj},${y2} ${x1},${y2}`,
    `L${x1},${y3}`,
    `C${xj},${y3} ${xi},${y1} ${x0},${y1}`,
    "Z"
  ].join(" ");
}

function areaGlobalFill(node) {
  const maxArea = Math.max(0, globalAreaNodeMax || 0);
  const minArea = Math.max(0, globalAreaMinPositive || 0);
  const area = Math.max(0, +node.area || 0);
  if (!(maxArea > 0) || !(minArea > 0) || area <= 0 || maxArea <= minArea) {
    return "#eef5ff";
  }
  const t = Math.max(0, Math.min(1, (Math.log(area) - Math.log(minArea)) / (Math.log(maxArea) - Math.log(minArea))));
  const steps = [
    "#eff6ff",
    "#dbeafe",
    "#bfdbfe",
    "#93c5fd",
    "#60a5fa",
    "#3b82f6",
    "#2563eb",
    "#1d4ed8",
    "#1e40af",
    "#172554"
  ];
  const idx = Math.max(0, Math.min(steps.length - 1, Math.round(t * (steps.length - 1))));
  return steps[idx];
}

function applyViewportTransform() {
  if (!lastGraph) return;

  const width = Math.max(1, chartWrap.clientWidth);
  const height = Math.max(1, chartWrap.clientHeight);
  const root = chart.select(".sankey-root");
  if (root.empty() || !viewFocus) return;

  const translateX = width / 2 - viewFocus.x * zoomScale;
  const translateY = height * VIEWPORT_ANCHOR_Y - viewFocus.y * zoomScale;
  root.attr("transform", `translate(${translateX},${translateY}) scale(${zoomScale})`);

  chart.select(".timestep-label-layer")
    .selectAll("text")
    .attr("x", d => d.x * zoomScale + translateX);
  chart.select(".timestep-label-layer")
    .selectAll("text")
    .each(function(d) {
      d3.select(this)
        .selectAll("tspan")
        .attr("x", d.x * zoomScale + translateX);
    });

  renderMiniMap();
}

function renderSankey({ preserveFocus = true } = {}) {
  const filtered = filterData();
  orderNodes(filtered.nodes, filtered.links, filtered.controls.ordering);

  const timestepValues = [...new Set(filtered.nodes.map(n => +n.timestep_index))]
    .sort((a, b) => a - b);

  const layout = buildDisplayLayout(timestepValues, filtered.controls.ranges);

  const maxColumn = d3.max([
    ...d3.rollup(filtered.nodes, v => v.length, d => +d.timestep_index).values()
  ]) || 1;

  const width = Math.max(1, chartWrap.clientWidth);
  const height = Math.max(1, chartWrap.clientHeight);

  chart.attr("width", width).attr("height", height);
  chart.selectAll("*").remove();

  const root = chart.append("g")
    .attr("class", "sankey-root")
    .attr("transform", "translate(0,0)");

  if (!filtered.nodes.length) {
    lastGraph = { nodes: [], links: [] };
    viewFocus = null;
    root.append("text")
      .attr("x", 40)
      .attr("y", 60)
      .attr("fill", "#687583")
      .text("No visible nodes for the current range/filter. Try lowering the threshold or disabling 'Hide nodes with no visible links'.");
    updateStats(filtered);
    updateMiniMapState(lastGraph);
    renderMiniMap();
    return;
  }

  const graphNodes = filtered.nodes.map(node => ({
    ...node,
    sourceLinks: [],
    targetLinks: []
  }));
  const nodeById = new Map(graphNodes.map(node => [node.id, node]));
  const graphLinks = [];

  for (const link of filtered.links) {
    const source = nodeById.get(link.source);
    const target = nodeById.get(link.target);
    if (!source || !target) continue;

    const graphLink = {
      ...link,
      source,
      target,
      value: Math.max(1, +link.overlap_vertices || 1)
    };

    source.sourceLinks.push(graphLink);
    target.targetLinks.push(graphLink);
    graphLinks.push(graphLink);
  }

  const graph = {
    nodes: graphNodes,
    links: graphLinks
  };

  applyTemporalXPositions(graph, layout);
  applyOrderedYPositions(graph, filtered.controls.nodeSizeMode, filtered.controls.nodeSizeScaleMode);
  const bounds = graphBounds(graph);
  const graphCenterX = (bounds.minX + bounds.maxX) / 2;
  const graphCenterY = (bounds.minY + bounds.maxY) / 2;
  if (!viewFocus || !preserveFocus) {
    viewFocus = { x: graphCenterX, y: graphCenterY };
    if (!preserveFocus) {
      zoomScale = fitZoomForBounds(bounds);
    }
  } else if (!Number.isFinite(viewFocus.x) || !Number.isFinite(viewFocus.y)) {
    viewFocus = { x: graphCenterX, y: graphCenterY };
  }

  lastGraph = graph;
  updateMiniMapState(graph);

  const timestepLabels = d3.groups(graph.nodes, d => +d.timestep_index)
    .map(([t, ns]) => ({
      t,
      x: d3.mean(ns, n => (n.x0 + n.x1) / 2),
      label: ns[0].timestep_label
    }));
  const useGlobalLinkMode = filtered.controls.nodeSizeScaleMode === "global";

  root.append("g")
    .selectAll("path")
    .data(graph.links)
    .join("path")
    .attr("class", useGlobalLinkMode ? "link global-link-mode" : "link")
    .attr("d", sankeyRibbonPath)
    .on("mousemove", (event, d) => showTooltip(event, linkTooltip(d)))
    .on("mouseleave", hideTooltip)
    .on("click", (_, d) => showLinkDetails(d));

  const node = root.append("g")
    .selectAll("g")
    .data(graph.nodes)
    .join("g")
    .attr("class", "node")
    .on("mousemove", (event, d) => showTooltip(event, nodeTooltip(d)))
    .on("mouseleave", hideTooltip)
    .on("click", (_, d) => showNodeDetails(d));

  node.append("rect")
    .attr("x", d => d.x0)
    .attr("y", d => d.y0)
    .attr("height", d => Math.max(2, d.y1 - d.y0))
    .attr("width", d => d.x1 - d.x0)
    .attr("fill", d => {
      if (filtered.controls.nodeSizeMode === "area" && filtered.controls.nodeSizeScaleMode === "global") {
        return areaGlobalFill(d);
      }
      return d.color || "#6f9ed4";
    });

  node.append("text")
    .attr("x", d => d.x1 + 5)
    .attr("y", d => (d.y0 + d.y1) / 2)
    .attr("dy", "0.35em")
    .attr("text-anchor", "start")
    .attr("font-size", 13)
    .text(d => `S${d.sheet_id} R${d.rank}`);

  chart.append("g")
    .attr("class", "timestep-label-layer")
    .selectAll("text")
    .data(timestepLabels)
    .join("text")
    .attr("class", "timestep-label")
    .attr("x", d => d.x)
    .attr("y", 15)
    .attr("text-anchor", "middle")
    .attr("dominant-baseline", "middle")
    .attr("font-size", 13)
    .each(function(d) {
      const fsValue = Number(d.label) / 41.341374575751;
      const text = d3.select(this);
      text.append("tspan")
        .attr("dy", "-0.55em")
        .attr("text-anchor", "middle")
        .text(`${d.t}. ${d.label}`);
      text.append("tspan")
        .attr("dy", "1.10em")
        .attr("font-size", 11)
        .attr("fill", "#6f7d8b")
        .attr("text-anchor", "middle")
        .text(Number.isFinite(fsValue) ? `${d3.format(".2f")(fsValue)} fs` : "");
    });

  updateStats(filtered);
  applyViewportTransform();
}

function updateStats(filtered) {
  const controls = getControls();

  const html = [
    ["visible timesteps", new Set(filtered.nodes.map(n => n.timestep_index)).size],
    ["visible nodes", filtered.nodes.length],
    ["visible links", filtered.links.length],
    ["ranges", controls.ranges.length ? controls.ranges.map(r => `${r.start}-${r.end}`).join(", ") : "all"]
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  document.getElementById("stats").innerHTML = html;
}

function showNodeDetails(d) {
  const image = d.thumbnail ? `<img class="thumb" src="${escapeHtml(d.thumbnail)}" alt="sheet ${d.sheet_id}">` : "";
  document.getElementById("detailsContent").innerHTML = `
    <h3>Sheet ${d.sheet_id}</h3>
    ${image}
    ${metadataTable(d)}
  `;
}

function showLinkDetails(d) {
  const s = d.source;
  const t = d.target;

  const images = `
    <div class="thumb-row">
      <div><h4>Source</h4>${s.thumbnail ? `<img class="thumb" src="${escapeHtml(s.thumbnail)}">` : "<p>No image</p>"}</div>
      <div><h4>Target</h4>${t.thumbnail ? `<img class="thumb" src="${escapeHtml(t.thumbnail)}">` : "<p>No image</p>"}</div>
    </div>`;

  document.getElementById("detailsContent").innerHTML = `
    <h3>Overlap ${d.overlap_vertices.toLocaleString()} vertices</h3>
    ${images}
    ${metadataTable({
      source: `${s.timestep_label} / sheet ${s.sheet_id}`,
      target: `${t.timestep_label} / sheet ${t.sheet_id}`,
      source_percent: d.source_percent,
      target_percent: d.target_percent,
      overlap_vertices: d.overlap_vertices,
      source_area: d.source_area,
      target_area: d.target_area
    })}
  `;
}

function metadataTable(obj) {
  return `<table class="meta">${Object.entries(obj)
    .filter(([_, v]) => v !== undefined && typeof v !== "object")
    .map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`)
    .join("")}</table>`;
}

function nodeTooltip(d) {
  const image = d.thumbnail
    ? `<br><img class="tooltip-thumb" src="${escapeHtml(d.thumbnail)}" alt="sheet ${escapeHtml(d.sheet_id)}">`
    : "";
  return `<b>Sheet ${d.sheet_id}</b><br>time ${d.timestep_label}<br>rank ${d.rank}<br>area ${format(d.area)}<br>vertices ${format(d.num_vertices)}${image}`;
}

function linkTooltip(d) {
  const sourceImage = d.source.thumbnail
    ? `<div><img src="${escapeHtml(d.source.thumbnail)}"><div class="tooltip-caption">source S${escapeHtml(d.source.sheet_id)}</div></div>`
    : `<div><div class="tooltip-caption">source S${escapeHtml(d.source.sheet_id)}<br>No image</div></div>`;
  const targetImage = d.target.thumbnail
    ? `<div><img src="${escapeHtml(d.target.thumbnail)}"><div class="tooltip-caption">target S${escapeHtml(d.target.sheet_id)}</div></div>`
    : `<div><div class="tooltip-caption">target S${escapeHtml(d.target.sheet_id)}<br>No image</div></div>`;
  return `<b>${format(d.overlap_vertices)} vertices</b><br>${d.source.sheet_id} → ${d.target.sheet_id}<br>source ${format(d.source_percent)}% target ${format(d.target_percent)}%<div class="tooltip-thumb-row">${sourceImage}${targetImage}</div>`;
}

function showTooltip(event, html) {
  tooltip
    .style("display", "block")
    .style("left", `${event.clientX + 12}px`)
    .style("top", `${event.clientY + 12}px`)
    .html(html);
}

function hideTooltip() { tooltip.style("display", "none"); }
function format(v) { return Number.isFinite(+v) ? d3.format(",.4~g")(+v) : v; }
function escapeHtml(v) {
  return String(v).replace(/[&<>"']/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

function renderRangeRows() {
  const holder = document.getElementById("rangeRows");
  holder.innerHTML = "";

  ranges.forEach((r, i) => {
    const row = document.createElement("div");
    row.className = `range-row${i === selectedRangeIndex ? " selected" : ""}`;
    row.innerHTML = `
      <input type="number" value="${r.start}" min="0">
      <input type="number" value="${r.end}" min="0">
      <button title="Remove range">×</button>`;

    row.addEventListener("click", event => {
      if (event.target.closest("input, button")) return;
      selectRangeIndex(i, { center: true });
    });

    const [start, end] = row.querySelectorAll("input");

    const commitRange = () => {
      ranges[i].start = +start.value;
      ranges[i].end = +end.value;
      renderRangeRows();
      renderSankey({ preserveFocus: true });
      if (i === selectedRangeIndex) requestAnimationFrame(() => centerSelectedRange(i));
    };

    start.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRange();
      }
    });

    end.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRange();
      }
    });

    start.addEventListener("blur", commitRange);
    end.addEventListener("blur", commitRange);

    row.querySelector("button").addEventListener("click", event => {
      event.stopPropagation();
      deleteRange(i);
    });

    holder.appendChild(row);
  });
}

function addRange(start = 0, end = 20) {
  ranges.push({ start, end });
  selectedRangeIndex = ranges.length - 1;
  renderRangeRows();
  renderSankey({ preserveFocus: true });
  requestAnimationFrame(() => centerSelectedRange(selectedRangeIndex));
}

function deleteRange(index = selectedRangeIndex) {
  if (!ranges.length || index < 0 || index >= ranges.length) return;

  ranges.splice(index, 1);
  selectedRangeIndex = ranges.length
    ? Math.max(0, Math.min(index, ranges.length - 1))
    : -1;

  renderRangeRows();
  renderSankey({ preserveFocus: true });
  if (selectedRangeIndex >= 0) requestAnimationFrame(() => centerSelectedRange(selectedRangeIndex));
}

function selectRangeIndex(index, { center = false } = {}) {
  if (!ranges.length) {
    selectedRangeIndex = -1;
    renderRangeRows();
    renderMiniMap();
    return;
  }

  selectedRangeIndex = Math.max(0, Math.min(index, ranges.length - 1));

  // Selecting an existing range must not redraw the Sankey. Redrawing while
  // a minimap click is propagating was the source of the blank/disappearing view.
  renderRangeRows();
  renderMiniMap();

  if (center) {
    requestAnimationFrame(() => centerSelectedRange(selectedRangeIndex));
  }
}

function centerSelectedRange(index = selectedRangeIndex) {
  if (!lastGraph || index < 0 || index >= ranges.length) return;

  const range = ranges[index];
  const start = Math.min(+range.start, +range.end);
  const end = Math.max(+range.start, +range.end);

  const nodes = lastGraph.nodes.filter(
    n => +n.timestep_index >= start && +n.timestep_index <= end
  );

  if (!nodes.length) {
    renderMiniMap();
    return;
  }

  const minX = d3.min(nodes, d => d.x0) ?? 0;
  const maxX = d3.max(nodes, d => d.x1) ?? 0;
  const minY = d3.min(nodes, d => d.y0) ?? 0;
  const maxY = d3.max(nodes, d => d.y1) ?? 0;
  viewFocus = { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
  scheduleViewportUpdate();
}

function centerSankey() {
  if (!lastGraph || !lastGraph.nodes.length) return;
  const bounds = graphBounds(lastGraph);
  viewFocus = {
    x: (bounds.minX + bounds.maxX) / 2,
    y: (bounds.minY + bounds.maxY) / 2
  };
  zoomScale = fitZoomForBounds(bounds);
  scheduleViewportUpdate();
}

function clampTimestep(value, maxIndex) {
  return Math.max(0, Math.min(maxIndex, value));
}

function startRangeDrag(event) {
  if (event.button !== 0 || !minimapBarScale) return;

  const svgNode = document.querySelector("#minimap svg");
  if (!svgNode) return;

  const [mx] = d3.pointer(event, svgNode);
  const timestep = clampTimestep(Math.round(minimapBarScale.invert(mx)), minimapBarMaxIndex);

  rangeDrag = { start: timestep, end: timestep };
  event.preventDefault();
  renderMiniMap();
}

function updateRangeDrag(event) {
  if (!rangeDrag || !minimapBarScale) return;

  const svgNode = document.querySelector("#minimap svg");
  if (!svgNode) return;

  const [mx] = d3.pointer(event, svgNode);
  rangeDrag.end = clampTimestep(Math.round(minimapBarScale.invert(mx)), minimapBarMaxIndex);
  renderMiniMap();
}

function finishRangeDrag() {
  if (!rangeDrag) return;

  const start = Math.min(rangeDrag.start, rangeDrag.end);
  const end = Math.max(rangeDrag.start, rangeDrag.end);
  const finalEnd = end === start ? Math.min(minimapBarMaxIndex, start + 1) : end;

  rangeDrag = null;
  ranges.push({ start, end: finalEnd });
  selectedRangeIndex = ranges.length - 1;

  renderRangeRows();
  renderSankey({ preserveFocus: true });
  requestAnimationFrame(() => centerSelectedRange(selectedRangeIndex));
}

function updateMiniMapState(graph) {
  const timeGroups = d3.groups(graph.nodes || [], d => +d.timestep_index)
    .map(([t, ns]) => ({
      timestep: +t,
      x: d3.mean(ns, n => (n.x0 + n.x1) / 2)
    }))
    .sort((a, b) => a.timestep - b.timestep);

  if (!timeGroups.length) {
    minimapState = null;
    return;
  }

  const minTime = d3.min(timeGroups, d => d.timestep);
  const maxTime = d3.max(timeGroups, d => d.timestep);
  const minX = d3.min(timeGroups, d => d.x);
  const maxX = d3.max(timeGroups, d => d.x);

  minimapState = {
    minTime,
    maxTime,
    graphToTime: d3.scaleLinear()
      .domain(minX === maxX ? [minX - 1, maxX + 1] : [minX, maxX])
      .range(minTime === maxTime ? [minTime - 0.5, maxTime + 0.5] : [minTime, maxTime])
      .clamp(true)
  };
}

function visibleTimestepWindow() {
  if (!minimapState || !viewFocus) return null;

  const width = Math.max(1, chartWrap.clientWidth);
  const startX = viewFocus.x - width / (2 * zoomScale);
  const endX = viewFocus.x + width / (2 * zoomScale);

  return {
    start: minimapState.graphToTime(startX),
    end: minimapState.graphToTime(endX)
  };
}

function renderMiniMap() {
  const holder = d3.select("#minimap");
  holder.selectAll("*").remove();

  const width = holder.node().clientWidth || 800;
  const height = 88;

  const svg = holder.append("svg").attr("width", width).attr("height", height);

  const timesteps = fullData.timesteps || [];
  const maxIndex = d3.max(timesteps, d => +d.index) ?? 0;

  minimapBarScale = d3.scaleLinear()
    .domain([0, maxIndex || 1])
    .range([20, width - 20]);

  minimapBarMaxIndex = maxIndex;
  const x = minimapBarScale;

  svg.append("rect")
    .attr("class", "range-bg")
    .attr("x", 20)
    .attr("y", 34)
    .attr("width", width - 40)
    .attr("height", 18)
    .style("cursor", "crosshair")
    .style("pointer-events", "all")
    .on("pointerdown", startRangeDrag);

  const rangeGroups = svg.selectAll("g.range-group")
    .data(ranges.map((range, index) => ({ range, index })))
    .join("g")
    .attr("class", "range-group");

  rangeGroups.append("rect")
    .attr("class", "range-hitbox")
    .attr("x", d => x(Math.min(d.range.start, d.range.end)) - 3)
    .attr("y", 24)
    .attr("width", d => Math.max(8, x(Math.max(d.range.start, d.range.end)) - x(Math.min(d.range.start, d.range.end)) + 6))
    .attr("height", 38)
    .on("pointerdown", event => {
      event.stopPropagation();
      event.preventDefault();
    })
    .on("click", (event, d) => {
      event.stopPropagation();
      event.preventDefault();
      selectRangeIndex(d.index, { center: true });
    });

  rangeGroups.append("rect")
    .attr("class", d => `range-selected${d.index === selectedRangeIndex ? " selected" : ""}`)
    .attr("x", d => x(Math.min(d.range.start, d.range.end)))
    .attr("y", 30)
    .attr("width", d => Math.max(3, x(Math.max(d.range.start, d.range.end)) - x(Math.min(d.range.start, d.range.end))))
    .attr("height", 26)
    .on("pointerdown", event => {
      event.stopPropagation();
      event.preventDefault();
    })
    .on("click", (event, d) => {
      event.stopPropagation();
      event.preventDefault();
      selectRangeIndex(d.index, { center: true });
    });

  if (rangeDrag) {
    const dragStart = Math.min(rangeDrag.start, rangeDrag.end);
    const dragEnd = Math.max(rangeDrag.start, rangeDrag.end);

    svg.append("rect")
      .attr("class", "range-drag-preview")
      .attr("x", x(dragStart))
      .attr("y", 30)
      .attr("width", Math.max(3, x(dragEnd) - x(dragStart)))
      .attr("height", 26);
  }

  svg.append("text").attr("x", 20).attr("y", 72).text(0);
  svg.append("text").attr("x", width - 20).attr("y", 72).attr("text-anchor", "end").text(maxIndex);

  const visible = visibleTimestepWindow();
  if (visible) {
    const start = Math.max(0, Math.min(visible.start, visible.end));
    const end = Math.min(maxIndex, Math.max(visible.start, visible.end));

    svg.append("rect")
      .attr("class", "viewport-window")
      .attr("x", x(start))
      .attr("y", 20)
      .attr("width", Math.max(4, x(end) - x(start)))
      .attr("height", 46);
  }
}

function bindControls() {
  chartWrap.style.cursor = "grab";
  chartWrap.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !isPanTarget(event.target)) return;

    if (!viewFocus) {
      const bounds = lastGraph ? graphBounds(lastGraph) : { minX: 0, maxX: 0, minY: 0, maxY: 0 };
      viewFocus = {
        x: (bounds.minX + bounds.maxX) / 2,
        y: (bounds.minY + bounds.maxY) / 2
      };
    }

    panDrag = {
      startX: event.clientX,
      startY: event.clientY,
      startFocusX: viewFocus.x,
      startFocusY: viewFocus.y,
      moved: false
    };
    chartWrap.setPointerCapture(event.pointerId);
    chartWrap.style.cursor = "grabbing";
    event.preventDefault();
  });

  chartWrap.addEventListener("pointermove", event => {
    if (!panDrag) return;

    const dx = event.clientX - panDrag.startX;
    const dy = event.clientY - panDrag.startY;
    if (!panDrag.moved && Math.hypot(dx, dy) < PAN_DRAG_THRESHOLD) return;

    panDrag.moved = true;
    viewFocus = {
      x: panDrag.startFocusX - dx / zoomScale,
      y: panDrag.startFocusY - dy / zoomScale
    };
    scheduleViewportUpdate();
    event.preventDefault();
  });

  chartWrap.addEventListener("pointerup", event => {
    if (!panDrag) return;

    try { chartWrap.releasePointerCapture(event.pointerId); } catch (_) {}
    chartWrap.style.cursor = "grab";
    panDrag = null;
  });

  chartWrap.addEventListener("pointercancel", event => {
    if (!panDrag) return;

    try { chartWrap.releasePointerCapture(event.pointerId); } catch (_) {}
    chartWrap.style.cursor = "grab";
    panDrag = null;
  });

  chartWrap.addEventListener("wheel", event => {
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0015);
    setZoomScale(zoomScale * factor);
  }, { passive: false });

  window.addEventListener("pointermove", event => {
    if (!rangeDrag) return;
    updateRangeDrag(event);
  });

  window.addEventListener("pointerup", event => {
    if (!rangeDrag) return;
    event.preventDefault();
    finishRangeDrag();
  });

  document.getElementById("threshold").addEventListener("input", event => {
    setThresholdValue(event.target.value, true);
  });

  const thresholdBox = document.getElementById("thresholdBox");
  thresholdBox.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    setThresholdValue(event.target.value, true);
    thresholdBox.blur();
  });

  thresholdBox.addEventListener("blur", event => {
    setThresholdValue(event.target.value, true);
  });

  ["percentMode", "hideIsolated"].forEach(id => {
    document.getElementById(id).addEventListener("input", () => {
      renderSankey({ preserveFocus: true });
    });
  });

  document.getElementById("ordering").addEventListener("change", () => {
    renderSankey({ preserveFocus: true });
  });

  document.getElementById("nodeSizeMode").addEventListener("change", () => {
    renderSankey({ preserveFocus: true });
  });

  document.getElementById("nodeSizeScaleMode").addEventListener("change", () => {
    renderSankey({ preserveFocus: true });
  });

  document.getElementById("addRange").addEventListener("click", () => {
    addRange(0, Math.min(20, (fullData.timesteps || []).length - 1));
  });

  document.getElementById("zoomOut").addEventListener("click", () => setZoomScale(zoomScale / ZOOM_STEP));
  document.getElementById("zoomIn").addEventListener("click", () => setZoomScale(zoomScale * ZOOM_STEP));
  document.getElementById("deleteRange").addEventListener("click", () => deleteRange());
  document.getElementById("centerView").addEventListener("click", () => centerSankey());

  document.addEventListener("keydown", event => {
    const tag = event.target?.tagName?.toLowerCase();
    if ((event.key === "Delete" || event.key === "Backspace") && tag !== "input" && tag !== "textarea") {
      deleteRange();
    }
  });

  window.addEventListener("resize", debounce(() => renderSankey({ preserveFocus: true }), 250));
}

d3.json("data.json").then(data => {
  fullData = data;
  const areaTotalsByTime = d3.rollups(
    fullData.nodes || [],
    v => d3.sum(v, n => Math.max(0, +n.area || 0)),
    n => +n.timestep_index
  );
  globalAreaColumnMax = d3.max(areaTotalsByTime, d => d[1]) ?? 0;
  globalAreaNodeMax = d3.max(fullData.nodes || [], n => Math.max(0, +n.area || 0)) ?? 0;
  globalAreaMinPositive = d3.min(fullData.nodes || [], n => {
    const area = Math.max(0, +n.area || 0);
    return area > 0 ? area : null;
  }) ?? 0;
  const nodeCountsByTime = d3.rollups(
    fullData.nodes || [],
    v => v.length,
    n => +n.timestep_index
  );
  globalAreaMaxNodesPerColumn = d3.max(nodeCountsByTime, d => d[1]) ?? 0;
  globalLinkMax = d3.max(fullData.links || [], l => Math.max(0, +l.overlap_vertices || +l.value || 0)) ?? 0;

  const maxInitial = Math.min(20, Math.max(0, (data.timesteps || []).length - 1));
  ranges = [{ start: 0, end: maxInitial }];
  selectedRangeIndex = 0;
  zoomScale = 1;
  viewFocus = null;
  panDrag = null;

  renderRangeRows();
  bindControls();
  setThresholdValue(document.getElementById("threshold").value, false);
  renderSankey({ preserveFocus: false });
  requestAnimationFrame(() => centerSelectedRange(selectedRangeIndex));
});
"""
    )
    return path


def write_viewer_files(data):
    if VIEWER_DIR.exists():
        shutil.rmtree(VIEWER_DIR)
    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    link_sheet_images(VIEWER_DIR)

    prepared = prepare_data(data, VIEWER_DIR)
    data_path = write_data_json(prepared)
    index_path = write_index_html()
    js_path = write_viewer_js()
    css_path = write_style_css()
    return index_path, data_path, js_path, css_path


def build_interactive_sankey_viewer_stage():
    data = load_data()
    index_path, data_path, js_path, css_path = write_viewer_files(data)

    print(f"Read overlap data: {OVERLAP_FILE}")
    print(f"Wrote viewer:      {VIEWER_DIR}")
    print(f"  {index_path.name}")
    print(f"  {data_path.name}")
    print(f"  {js_path.name}")
    print(f"  {css_path.name}")
    print()
    print("Open with:")
    print(f"  cd {VIEWER_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


if __name__ == "__main__":
    build_interactive_sankey_viewer_stage()
