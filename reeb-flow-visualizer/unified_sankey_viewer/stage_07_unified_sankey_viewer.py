#!/usr/bin/env python3

"""Build a unified Sankey dashboard for overlap and shape metrics."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from common import BASE_DIR, OUTPUT_DIR, SHEET_IMAGE_DIR, OVERLAP_FILE
from unified_sankey_viewer.viewer_common import (
    shared_viewer_css,
    shared_viewer_script_tags,
    write_viewer_common_js,
)

STORAGE_ROOT = BASE_DIR / "compareSheetShapesCache"
TIMESTEP_CACHE_DIR = STORAGE_ROOT / "cache" / "timesteps"
MATCHES_FILE = STORAGE_ROOT / "results" / "sheet_shape_matches.json"

UNIFIED_VIEWER_DIR = OUTPUT_DIR / "unified_sankey_viewer"
ROOT_INDEX_FILE = OUTPUT_DIR / "index.html"

SHAPE_METRICS = [
    {"id": "combined", "label": "combined", "field": "final_score"},
    {"id": "shape_iou", "label": "shape IoU", "field": "shape_iou"},
    {"id": "support_jaccard", "label": "vertex Jaccard", "field": "support_jaccard"},
    {"id": "area_ratio", "label": "area ratio", "field": "area_ratio"},
    {"id": "bbox_iou", "label": "bbox IoU", "field": "bbox_iou"},
    {"id": "centroid_similarity", "label": "centroid similarity", "field": "centroid_similarity"},
]

OVERLAP_METRICS = [
    {"id": "overlap_vertices", "label": "overlap vertices", "field": "overlap_vertices"},
    {"id": "overlap_source_percent", "label": "source overlap %", "field": "source_percent"},
    {"id": "overlap_target_percent", "label": "target overlap %", "field": "target_percent"},
    {"id": "overlap_max_percent", "label": "max overlap %", "field": "max_percent"},
]

DATA_MODES = [
    {
        "id": "overlap",
        "label": "Vertex overlap",
        "pair_field": "overlap_pairs",
        "default_metric": "overlap_max_percent",
        "metrics": OVERLAP_METRICS,
    },
    {
        "id": "shape",
        "label": "Shape metrics",
        "pair_field": "shape_pairs",
        "default_metric": "combined",
        "metrics": SHAPE_METRICS,
    },
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


def load_overlap_data() -> dict:
    if not OVERLAP_FILE.exists():
        raise FileNotFoundError(f"Overlap file does not exist: {OVERLAP_FILE}")
    return json.loads(OVERLAP_FILE.read_text())


def prepare_data(viewer_dir: Path) -> dict:
    timesteps, max_area, max_vertices = load_timestep_cache(viewer_dir)
    match_data = load_match_data()
    overlap_data = load_overlap_data()

    overlap_timestep_by_index = {
        safe_int(item.get("index")): item
        for item in overlap_data.get("timesteps", [])
    }
    overlap_node_by_timestep_sheet = {
        (safe_int(node.get("timestep_index")), safe_int(node.get("sheet_id"))): node
        for node in overlap_data.get("nodes", [])
    }

    for timestep in timesteps:
        timestep_index = safe_int(timestep.get("timestep_index"))
        overlap_timestep = overlap_timestep_by_index.get(timestep_index, {})
        timestep["rsijson_file"] = str(overlap_timestep.get("rsijson_file", ""))
        timestep["rsi_file"] = str(overlap_timestep.get("rsi_file", ""))
        timestep["num_vertices"] = safe_int(overlap_timestep.get("num_vertices"))
        timestep["num_regular_vertices"] = safe_int(overlap_timestep.get("num_regular_vertices"))
        timestep["num_singular_vertices"] = safe_int(overlap_timestep.get("num_singular_vertices"))
        timestep["top_n_sheets"] = safe_int(overlap_timestep.get("top_n_sheets"))

        for sheet in timestep.get("sheets", []):
            sheet_id = safe_int(sheet.get("sheet_id"))
            overlap_node = overlap_node_by_timestep_sheet.get((timestep_index, sheet_id), {})
            sheet["node_id"] = str(overlap_node.get("id", ""))
            sheet["rsi_file"] = str(overlap_node.get("rsi_file", "") or timestep["rsi_file"])
            sheet["rsijson_file"] = str(overlap_node.get("rsijson_file", "") or timestep["rsijson_file"])

    shape_maxima = {metric["id"]: 0.0 for metric in SHAPE_METRICS}
    overlap_maxima = {metric["id"]: 0.0 for metric in OVERLAP_METRICS}

    timestep_label_by_index = {
        safe_int(item.get("timestep_index")): str(item.get("label", ""))
        for item in timesteps
    }
    timestep_stem_by_index = {
        safe_int(item.get("timestep_index")): str(item.get("stem", ""))
        for item in timesteps
    }

    shape_pairs = []
    for pair in match_data.get("pairwise_matches", []):
        source_timestep_index = safe_int(pair.get("source_timestep_index"))
        target_timestep_index = safe_int(pair.get("target_timestep_index"))
        source_timestep = overlap_timestep_by_index.get(source_timestep_index, {})
        target_timestep = overlap_timestep_by_index.get(target_timestep_index, {})
        matches = []
        for match in pair.get("matches", []):
            source_sheet_id = safe_int(match.get("source_sheet_id"))
            target_sheet_id = safe_int(match.get("target_sheet_id"))
            source_node = overlap_node_by_timestep_sheet.get((source_timestep_index, source_sheet_id), {})
            target_node = overlap_node_by_timestep_sheet.get((target_timestep_index, target_sheet_id), {})
            metrics = {}
            for metric in SHAPE_METRICS:
                value = safe_float(match.get(metric["field"]))
                metrics[metric["id"]] = value
                shape_maxima[metric["id"]] = max(shape_maxima[metric["id"]], value)

            matches.append(
                {
                    "source_sheet_id": source_sheet_id,
                    "target_sheet_id": target_sheet_id,
                    "source_rank": safe_int(match.get("source_rank")),
                    "target_rank": safe_int(match.get("target_rank")),
                    "source_area": safe_float(match.get("source_area")),
                    "target_area": safe_float(match.get("target_area")),
                    "source_num_vertices": safe_int(match.get("source_num_vertices")),
                    "target_num_vertices": safe_int(match.get("target_num_vertices")),
                    "source_node_id": str(source_node.get("id", "")),
                    "target_node_id": str(target_node.get("id", "")),
                    "source_node_area": safe_float(source_node.get("area")),
                    "target_node_area": safe_float(target_node.get("area")),
                    "source_rsijson_file": str(source_timestep.get("rsijson_file", "")),
                    "target_rsijson_file": str(target_timestep.get("rsijson_file", "")),
                    "source_rsi_file": str(source_timestep.get("rsi_file", "")),
                    "target_rsi_file": str(target_timestep.get("rsi_file", "")),
                    "metrics": metrics,
                }
            )

        shape_pairs.append(
            {
                "source_timestep_index": source_timestep_index,
                "source_label": str(pair.get("source_label", "")),
                "source_stem": str(pair.get("source_stem", "")),
                "target_timestep_index": target_timestep_index,
                "target_label": str(pair.get("target_label", "")),
                "target_stem": str(pair.get("target_stem", "")),
                "source_rsijson_file": str(source_timestep.get("rsijson_file", "")),
                "target_rsijson_file": str(target_timestep.get("rsijson_file", "")),
                "source_rsi_file": str(source_timestep.get("rsi_file", "")),
                "target_rsi_file": str(target_timestep.get("rsi_file", "")),
                "global_bounds": pair.get("global_bounds", []),
                "pair_count": safe_int(pair.get("pair_count")),
                "matches": matches,
            }
        )

    overlap_node_index = {
        node.get("id"): node
        for node in overlap_data.get("nodes", [])
    }
    overlap_pairs_by_key: dict[tuple[int, int], dict] = {}
    for link in overlap_data.get("links", []):
        source_index = safe_int(link.get("source_timestep_index"))
        target_index = safe_int(link.get("target_timestep_index"))
        key = (source_index, target_index)
        pair = overlap_pairs_by_key.get(key)
        if pair is None:
            pair = {
                "source_timestep_index": source_index,
                "source_label": timestep_label_by_index.get(source_index, str(link.get("source_timestep_label", source_index))),
                "source_stem": timestep_stem_by_index.get(source_index, ""),
                "source_rsijson_file": str(link.get("source_rsijson_file", "") or overlap_timestep_by_index.get(source_index, {}).get("rsijson_file", "")),
                "source_rsi_file": str(link.get("source_rsi_file", "") or overlap_timestep_by_index.get(source_index, {}).get("rsi_file", "")),
                "target_timestep_index": target_index,
                "target_label": timestep_label_by_index.get(target_index, str(link.get("target_timestep_label", target_index))),
                "target_stem": timestep_stem_by_index.get(target_index, ""),
                "target_rsijson_file": str(link.get("target_rsijson_file", "") or overlap_timestep_by_index.get(target_index, {}).get("rsijson_file", "")),
                "target_rsi_file": str(link.get("target_rsi_file", "") or overlap_timestep_by_index.get(target_index, {}).get("rsi_file", "")),
                "pair_count": 0,
                "matches": [],
            }
            overlap_pairs_by_key[key] = pair

        source_node = overlap_node_index.get(link.get("source"), {})
        target_node = overlap_node_index.get(link.get("target"), {})
        source_percent = safe_float(link.get("source_percent"))
        target_percent = safe_float(link.get("target_percent"))
        overlap_vertices = safe_float(link.get("overlap_vertices"))
        max_percent = max(source_percent, target_percent)

        metrics = {
            "overlap_vertices": overlap_vertices,
            "overlap_source_percent": source_percent,
            "overlap_target_percent": target_percent,
            "overlap_max_percent": max_percent,
        }
        for metric_id, value in metrics.items():
            overlap_maxima[metric_id] = max(overlap_maxima[metric_id], value)

        pair["matches"].append(
            {
                "source_sheet_id": safe_int(link.get("source_sheet_id")),
                "target_sheet_id": safe_int(link.get("target_sheet_id")),
                "source_rank": safe_int(link.get("source_rank")),
                "target_rank": safe_int(link.get("target_rank")),
                "source_area": safe_float(link.get("source_area")),
                "target_area": safe_float(link.get("target_area")),
                "source_num_vertices": safe_int(link.get("source_num_vertices")),
                "target_num_vertices": safe_int(link.get("target_num_vertices")),
                "metrics": metrics,
                "overlap_vertices": safe_int(link.get("overlap_vertices")),
                "source_percent": source_percent,
                "target_percent": target_percent,
                "source_node_id": str(link.get("source", "")),
                "target_node_id": str(link.get("target", "")),
                "source_node_area": safe_float(source_node.get("area")),
                "target_node_area": safe_float(target_node.get("area")),
                "source_rsijson_file": str(link.get("source_rsijson_file", "")),
                "target_rsijson_file": str(link.get("target_rsijson_file", "")),
                "source_rsi_file": str(link.get("source_rsi_file", "")),
                "target_rsi_file": str(link.get("target_rsi_file", "")),
            }
        )
        pair["pair_count"] += 1

    overlap_pairs = sorted(
        overlap_pairs_by_key.values(),
        key=lambda item: (item["source_timestep_index"], item["target_timestep_index"]),
    )

    metric_maxima = {}
    metric_maxima.update(shape_maxima)
    metric_maxima.update(overlap_maxima)

    return {
        "meta": {
            "generated_from": {
                "matches": str(MATCHES_FILE),
                "overlap": str(OVERLAP_FILE),
            },
            "timesteps": len(timesteps),
            "data_modes": DATA_MODES,
            "metric_maxima": metric_maxima,
            "global_area_max": max_area,
            "global_vertex_max": max_vertices,
            "default_ranges": DEFAULT_RANGES,
            "node_height_fixed": 18,
            "link_thickness_min": 1.4,
            "link_thickness_max": 16,
        },
        "timesteps": timesteps,
        "shape_pairs": shape_pairs,
        "overlap_pairs": overlap_pairs,
        "viewer": {
            "generated_from": {
                "matches": str(MATCHES_FILE),
                "overlap": str(OVERLAP_FILE),
            },
            "sheet_image_dir": str(SHEET_IMAGE_DIR),
        },
    }


def write_data_json(data: dict) -> Path:
    path = UNIFIED_VIEWER_DIR / "data.json"
    path.write_text(json.dumps(data, indent=2, allow_nan=False))
    return path


def write_index_html() -> Path:
    path = UNIFIED_VIEWER_DIR / "index.html"
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reeb Flow Visualizer</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <div class="title-block">
      <h1>Reeb Flow Visualizer</h1>
      <p>Compare vertex-overlap and shape metrics in synchronized Sankey panels.</p>
    </div>
    <div class="header-actions">
      <button id="zoomOut">Zoom out</button>
      <button id="zoomIn">Zoom in</button>
      <button id="centerView">Center sankey</button>
      <button id="addPanel">+ Add panel</button>
    </div>
  </header>

  <main>
    <aside id="controls">
      <section>
        <h2>Timestep ranges</h2>
        <div id="rangeRows"></div>
        <div class="row-actions">
          <button id="addRange">+ Add range</button>
        </div>
        <p class="hint">Drag on the bar to create a range. Click a range to select it. Delete removes the selected range.</p>
      </section>

      <section>
        <h2>Summary</h2>
        <dl id="stats"></dl>
      </section>

      <section>
        <h2>Layout</h2>
        <label>
          Sorting
          <select id="orderingMode">
            <option value="crossings" selected>crossing-minimized</option>
            <option value="area">sheet area</option>
            <option value="vertices">vertex count</option>
          </select>
        </label>
        <label>
          Node Height Basis
          <select id="nodeSizeMode">
            <option value="vertices" selected>vertex count</option>
          </select>
        </label>
        <label>
          Top Sheets / timestep
          <input id="topSheets" type="number" min="1" step="1" value="10">
        </label>
        <label>
          Node Color
          <select id="nodeColorMode">
            <option value="solid" selected>solid</option>
            <option value="area">sheet area</option>
            <option value="vertices">vertex count</option>
          </select>
        </label>
        <label>
          Link Darkness
          <input id="linkDarkness" type="range" min="0" max="100" step="1" value="55">
          <span id="linkDarknessValue">55%</span>
        </label>
        <label class="inline">
          <input id="hideIsolated" type="checkbox">
          Hide nodes with no visible links
        </label>
      </section>
    </aside>

    <section id="viewer">
      <div id="rangeBarWrap">
        <svg id="rangeBar" aria-label="Timestep range selector"></svg>
      </div>
      <div id="panelList"></div>
    </section>

    <aside id="details">
      <h2>Details</h2>
      <div id="detailsContent">Click a node or link.</div>
    </aside>
  </main>

  <div id="tooltip" class="tooltip hidden"></div>

  {shared_viewer_script_tags(include_sankey=False)}
</body>
</html>
"""
    )
    return path


def write_style_css() -> Path:
    path = UNIFIED_VIEWER_DIR / "style.css"
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
  grid-template-columns: 300px minmax(0, 1fr) 340px;
  min-height: 0;
}
aside {
  overflow: auto;
  background: #fff;
  border-right: 1px solid #d9dee5;
  padding: 14px;
}
#details {
  border-right: 0;
  border-left: 1px solid #d9dee5;
  padding: 0;
}
#details h2 {
  margin: 0;
  padding: 12px 14px 8px;
  border-bottom: 1px solid #edf1f5;
}
#detailsContent {
  padding: 12px 14px;
  font-size: 13px;
}
#detailsContent .thumb {
  width: 100%;
  max-width: 150px;
  border-radius: 6px;
  display: block;
  background: #fff;
  margin: 8px 0;
}
#detailsContent .thumb-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin: 10px 0;
}
#detailsContent table.meta {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 12px;
}
#detailsContent table.meta td {
  border-bottom: 1px solid #e4e8ee;
  padding: 4px 6px;
  vertical-align: top;
  word-break: break-word;
}
section {
  margin-bottom: 18px;
}
label {
  display: grid;
  gap: 5px;
  margin: 10px 0;
  font-size: 13px;
}
label input[type="range"] {
  width: 100%;
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
label.inline {
  display: flex;
  align-items: center;
  gap: 8px;
}
label.inline input[type="checkbox"] {
  margin: 0;
  width: auto;
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
  stroke: rgba(20, 30, 40, 0.35);
  stroke-width: 0.6;
  transition: stroke-width 120ms ease, stroke 120ms ease, fill 120ms ease, opacity 120ms ease;
}
.node {
  cursor: pointer;
}
.node.hover rect,
.node:hover rect {
  stroke: rgba(17, 24, 39, 0.9);
  stroke-width: 1.3;
  opacity: 1;
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
  transition: opacity 120ms ease, filter 120ms ease;
}
.link.hover {
  filter: brightness(0.8) saturate(1.05);
  opacity: 0.98;
}
.panel-empty {
  padding: 18px;
  color: #6a7785;
}
#tooltip,
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
#tooltip.hidden,
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


def write_root_index_html() -> Path:
    ROOT_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROOT_INDEX_FILE.write_text(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unified Reeb Sankey Viewer</title>
  <meta http-equiv="refresh" content="0; url=unified_sankey_viewer/index.html">
</head>
<body>
  <script>
    window.location.replace("unified_sankey_viewer/index.html");
  </script>
  <noscript>
    <a href="unified_sankey_viewer/index.html">Open unified viewer</a>
  </noscript>
</body>
</html>
"""
    )
    return ROOT_INDEX_FILE


def write_viewer_js() -> Path:
    path = UNIFIED_VIEWER_DIR / "viewer.js"
    path.write_text(
        """const DATA = null;

d3.json("data.json").then(data => {
  const state = {
    ranges: (data.meta.default_ranges && data.meta.default_ranges.length ? data.meta.default_ranges : [{start: 0, end: 20}]).map(r => ({...r})),
    selectedRangeIndex: 0,
    panels: [{ id: 1, dataMode: "overlap", metricId: "overlap_max_percent", threshold: 0 }],
    nextPanelId: 2,
    rangeDrag: null,
    viewportDrag: null,
    tooltipLocked: false,
    activePanelId: 1,
    panelViews: new Map(),
    panelPan: null,
    layoutControls: {
      orderingMode: "crossings",
      nodeSizeMode: "vertices",
      topSheets: 10,
      nodeColorMode: "solid",
      linkDarkness: 55,
      hideIsolated: false
    }
  };

  let camera = null;
  let tooltipEngine = null;
  let rangeDispatcher = null;
  let renderAllPending = false;
  let thresholdSyncPending = false;

  const rangeBar = d3.select("#rangeBar");
  const panelList = d3.select("#panelList");
  const stats = d3.select("#stats");
  const tooltip = d3.select("#tooltip");
  const detailsContent = document.getElementById("detailsContent");

  const timestepLookup = window.ReebViewerCommon.createTimestepLookup(data.timesteps || [], {
    indexField: "timestep_index",
    labelField: "label"
  });
  const timestepByIndex = timestepLookup.byIndex;
  const dataModes = data.meta.data_modes || [];
  const modeById = new Map(dataModes.map(mode => [mode.id, mode]));
  const metricMaxima = data.meta.metric_maxima || {};
  const areaMax = data.meta.global_area_max || 1;
  const vertexMax = data.meta.global_vertex_max || 1;
  const linkMin = data.meta.link_thickness_min || 1.4;
  const linkMax = data.meta.link_thickness_max || 16;
  const timestepMax = timestepLookup.maxIndex;
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
        const ts = timestepLookup.itemAt(t);
        if (ts) visible.push(ts);
        seen.add(t);
      }
    }
    return visible;
  }

  function modeLabel(modeId) {
    return modeById.get(modeId)?.label || modeId;
  }

  function metricsForMode(modeId) {
    return modeById.get(modeId)?.metrics || [];
  }

  function pairFieldForMode(modeId) {
    return modeById.get(modeId)?.pair_field || "";
  }

  function pairsForMode(modeId) {
    const field = pairFieldForMode(modeId);
    if (!field) return [];
    return Array.isArray(data[field]) ? data[field] : [];
  }

  function metricLabel(modeId, metricId) {
    return (metricsForMode(modeId).find(metric => metric.id === metricId) || { label: metricId }).label;
  }

  function ensurePanelMetric(panel) {
    const metrics = metricsForMode(panel.dataMode);
    if (!metrics.length) {
      panel.metricId = "";
      return;
    }
    if (!metrics.some(metric => metric.id === panel.metricId)) {
      panel.metricId = metrics[0].id;
    }
  }

  function currentPanel() {
    return state.panelViews.get(state.activePanelId) || state.panelViews.values().next().value || null;
  }

  function getPanelById(id) {
    return state.panels.find(panel => panel.id === id);
  }

  function nodeMetricValue(node, mode) {
    if (mode === "vertices") {
      return Math.max(0, Number(node.num_vertices) || 0);
    }
    return Math.max(0, Number(node.area) || 0);
  }

  function nodeMetricMax(mode) {
    return mode === "vertices" ? Math.max(1, vertexMax) : Math.max(1, areaMax);
  }

  function nodeColorFill(node) {
    const mode = state.layoutControls.nodeColorMode;
    if (mode === "solid") {
      return "#6f9ed4";
    }
    const value = nodeMetricValue(node, mode);
    const maxValue = nodeMetricMax(mode);
    if (!(value > 0) || !(maxValue > 0)) {
      return "#93c5fd";
    }
    const ratio = clamp(value / maxValue, 0, 1);
    const mappedRatio = mode === "area"
      ? (Math.log1p(ratio * 40) / Math.log1p(40))
      : ratio;
    return d3.interpolateRgb("#8fbfff", "#123ea8")(mappedRatio);
  }

  function linkFillColor(opacity, hover) {
    const darkness = clamp(state.layoutControls.linkDarkness, 0, 100) / 100;
    const shade = Math.round(140 - darkness * 110);
    const blue = Math.round(168 - darkness * 124);
    const alpha = hover
      ? clamp(0.20 + opacity * 0.90, 0, 0.98)
      : clamp(0.14 + opacity * 0.80, 0, 0.92);
    return `rgba(${shade}, ${shade}, ${blue}, ${alpha})`;
  }

  function metricValue(link, metricId) {
    return Number(link.metrics?.[metricId] ?? 0);
  }

  function scoreOpacity(link, metricId) {
    const maxScore = metricMaxima[metricId] || 1;
    const value = metricValue(link, metricId);
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return 0.18 + ratio * 0.64;
  }

  function linkWidth(link, metricId) {
    const maxScore = metricMaxima[metricId] || 1;
    const value = metricValue(link, metricId);
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return linkMin + ratio * (linkMax - linkMin);
  }

  function rangeLabel(range) {
    return window.ReebViewerCommon.formatRangeLabel(range, {
      getLabel: value => timestepLookup.labelAt(value, String(value))
    });
  }

  function renderRangeRows() {
    window.ReebViewerCommon.renderRangeRows(document.getElementById("rangeRows"), {
      ranges: normalizedRanges(),
      selectedRangeIndex: state.selectedRangeIndex,
      timestepMax,
      onSelectRange: index => {
        if (rangeDispatcher) {
          rangeDispatcher.selectRange(index);
          return;
        }
        applyRangeAction({ type: "select", index });
        renderAll();
      },
      onCommitRange: (index, startValue, endValue) => {
        if (rangeDispatcher) {
          rangeDispatcher.commitRangeRow(index, startValue, endValue);
          return;
        }
        applyRangeAction({ type: "commit", index, startValue, endValue });
        renderAll();
      },
      onDeleteRange: index => removeRange(index)
    });
  }

  function addRange() {
    if (rangeDispatcher) {
      rangeDispatcher.addRange();
      return;
    }
    applyRangeAction({ type: "add" });
    renderAll();
  }

  function removeRange(index) {
    if (rangeDispatcher) {
      rangeDispatcher.deleteRange(index);
      return;
    }
    applyRangeAction({ type: "delete", index });
    renderAll();
  }

  function updateTooltip(html, x, y) {
    tooltipEngine?.showAt(html, x, y);
  }

  function hideTooltip() {
    tooltipEngine?.hide();
  }

  function imageFilename(path) {
    if (!path) return "";
    const tokens = String(path).split("/");
    return tokens[tokens.length - 1] || "";
  }

  function sheetByTimestepAndId(timestepIndex, sheetId) {
    return timestepByIndex.get(timestepIndex)?.sheets?.find(s => s.sheet_id === sheetId) || null;
  }

  function pathFilename(path) {
    if (!path) return "";
    const text = String(path);
    const tokens = text.split("/");
    return tokens[tokens.length - 1] || text;
  }

  function formatArrayValue(value) {
    if (!Array.isArray(value)) return "";
    return `[${value.map(item => formatScore(item)).join(", ")}]`;
  }

  function scalarMetadataTable(obj, skipKeys = null) {
    const skip = skipKeys || new Set();
    const rows = Object.entries(obj || {})
      .filter(([key, value]) => {
        if (skip.has(key)) return false;
        if (value === null || value === undefined) return false;
        return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
      })
      .map(([key, value]) => `<tr><td>${escapeHtml(key)}</td><td>${escapeHtml(String(value))}</td></tr>`)
      .join("");
    return rows ? `<table class="meta">${rows}</table>` : "";
  }

  function nodeTooltip(node) {
    const image = node.thumbnail ? `<img src="${escapeHtml(node.thumbnail)}" alt="Sheet ${escapeHtml(node.sheet_id)} image">` : "";
    const imageFile = node.thumbnail ? imageFilename(node.thumbnail) : "N/A";
    const rsiFile = pathFilename(node.rsi_file) || "N/A";
    const rsijsonFile = pathFilename(node.rsijson_file) || "N/A";
    return `
      <h3>Sheet ${escapeHtml(node.sheet_id)}</h3>
      <div class="meta-list">
        <div>Timestep</div><div>${escapeHtml(node.timestep_label)}</div>
        <div>Stem</div><div>${escapeHtml(node.stem || "N/A")}</div>
        <div>Node ID</div><div>${escapeHtml(node.node_id || "N/A")}</div>
        <div>Rank</div><div>${escapeHtml(node.rank)}</div>
        <div>Area</div><div>${escapeHtml(formatScore(node.area))}</div>
        <div>Vertices</div><div>${escapeHtml(node.num_vertices)}</div>
        <div>RSI</div><div>${escapeHtml(rsiFile)}</div>
        <div>RSI JSON</div><div>${escapeHtml(rsijsonFile)}</div>
        <div>Image</div><div>${escapeHtml(imageFile || "N/A")}</div>
      </div>
      ${image}
    `;
  }

  function linkTooltip(link, panel) {
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const sourceImage = sourceNode?.thumbnail ? `<img src="${escapeHtml(sourceNode.thumbnail)}" alt="Source sheet image">` : "";
    const targetImage = targetNode?.thumbnail ? `<img src="${escapeHtml(targetNode.thumbnail)}" alt="Target sheet image">` : "";
    const sourceRsi = pathFilename(link.source_rsi_file || sourceNode?.rsi_file) || "N/A";
    const targetRsi = pathFilename(link.target_rsi_file || targetNode?.rsi_file) || "N/A";
    const sourceRsijson = pathFilename(link.source_rsijson_file || sourceNode?.rsijson_file) || "N/A";
    const targetRsijson = pathFilename(link.target_rsijson_file || targetNode?.rsijson_file) || "N/A";
    const metricValueNow = metricValue(link, panel.metricId);
    const metricMax = metricMaxima[panel.metricId] || 1;
    const metricRatio = metricMax > 0 ? clamp(metricValueNow / metricMax, 0, 1) : 0;
    const scoreRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, metric.id)))}</div>`)
      .join("");
    return `
      <h3>Match ${escapeHtml(link.source_sheet_id)} → ${escapeHtml(link.target_sheet_id)}</h3>
      <div class="meta-list">
        <div>Source timestep</div><div>${escapeHtml(link.source_label)}</div>
        <div>Target timestep</div><div>${escapeHtml(link.target_label)}</div>
        <div>Source stem</div><div>${escapeHtml(link.source_stem || sourceNode?.stem || "N/A")}</div>
        <div>Target stem</div><div>${escapeHtml(link.target_stem || targetNode?.stem || "N/A")}</div>
        <div>Source RSI</div><div>${escapeHtml(sourceRsi)}</div>
        <div>Target RSI</div><div>${escapeHtml(targetRsi)}</div>
        <div>Source RSI JSON</div><div>${escapeHtml(sourceRsijson)}</div>
        <div>Target RSI JSON</div><div>${escapeHtml(targetRsijson)}</div>
        <div>${escapeHtml(metricLabel(panel.dataMode, panel.metricId))}</div><div>${escapeHtml(formatScore(metricValueNow))}</div>
        <div>Normalized</div><div>${escapeHtml(formatScore(metricRatio * 100))}%</div>
      </div>
      <div class="tooltip-grid" style="margin-top:10px;">
        <div>${sourceImage}</div>
        <div>${targetImage}</div>
      </div>
      <div class="meta-list" style="margin-top:10px;">${scoreRows}</div>
    `;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, token => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[token]));
  }

  function showNodeDetails(node) {
    if (!detailsContent) return;
    const image = node.thumbnail ? `<img class="thumb" src="${escapeHtml(node.thumbnail)}" alt="Sheet ${escapeHtml(node.sheet_id)}">` : "";
    const imageFile = node.thumbnail ? imageFilename(node.thumbnail) : "N/A";
    const rawTable = scalarMetadataTable({
      node_id: node.node_id || "",
      sheet_id: node.sheet_id,
      rank: node.rank,
      timestep_index: node.timestep_index,
      timestep_label: node.timestep_label || "",
      stem: node.stem || "",
      area: node.area,
      num_vertices: node.num_vertices,
      rsi_file: node.rsi_file || "",
      rsijson_file: node.rsijson_file || "",
      thumbnail: node.thumbnail || "",
      bbox: formatArrayValue(node.bbox),
      centroid: formatArrayValue(node.centroid)
    });
    detailsContent.innerHTML = `
      <h3>Sheet ${escapeHtml(node.sheet_id)}</h3>
      ${image}
      <div class="meta-list">
        <div>Timestep</div><div>${escapeHtml(node.timestep_label)}</div>
        <div>Stem</div><div>${escapeHtml(node.stem || "N/A")}</div>
        <div>Node ID</div><div>${escapeHtml(node.node_id || "N/A")}</div>
        <div>Rank</div><div>${escapeHtml(node.rank)}</div>
        <div>Area</div><div>${escapeHtml(formatScore(node.area))}</div>
        <div>Vertices</div><div>${escapeHtml(node.num_vertices)}</div>
        <div>RSI file</div><div>${escapeHtml(node.rsi_file || "N/A")}</div>
        <div>RSI JSON file</div><div>${escapeHtml(node.rsijson_file || "N/A")}</div>
        <div>Image file</div><div>${escapeHtml(imageFile)}</div>
      </div>
      ${rawTable}
    `;
  }

  function showLinkDetails(link, panel) {
    if (!detailsContent) return;
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const sourceImage = sourceNode?.thumbnail ? `<img class="thumb" src="${escapeHtml(sourceNode.thumbnail)}" alt="Source">` : "<p>No image</p>";
    const targetImage = targetNode?.thumbnail ? `<img class="thumb" src="${escapeHtml(targetNode.thumbnail)}" alt="Target">` : "<p>No image</p>";
    const selectedMetricLabel = metricLabel(panel.dataMode, panel.metricId);
    const selectedMetricValue = metricValue(link, panel.metricId);
    const sourceImageFile = sourceNode?.thumbnail ? imageFilename(sourceNode.thumbnail) : "N/A";
    const targetImageFile = targetNode?.thumbnail ? imageFilename(targetNode.thumbnail) : "N/A";
    const sourceRsi = link.source_rsi_file || sourceNode?.rsi_file || "";
    const targetRsi = link.target_rsi_file || targetNode?.rsi_file || "";
    const sourceRsijson = link.source_rsijson_file || sourceNode?.rsijson_file || "";
    const targetRsijson = link.target_rsijson_file || targetNode?.rsijson_file || "";
    const metricRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, metric.id)))}</div>`)
      .join("");
    const rawTable = scalarMetadataTable({
      mode: panel.dataMode,
      metric_id: panel.metricId,
      source_timestep_index: link.source_timestep_index,
      target_timestep_index: link.target_timestep_index,
      source_label: link.source_label || "",
      target_label: link.target_label || "",
      source_stem: link.source_stem || "",
      target_stem: link.target_stem || "",
      source_sheet_id: link.source_sheet_id,
      target_sheet_id: link.target_sheet_id,
      source_rank: link.source_rank,
      target_rank: link.target_rank,
      source_area: link.source_area,
      target_area: link.target_area,
      source_num_vertices: link.source_num_vertices,
      target_num_vertices: link.target_num_vertices,
      source_percent: link.source_percent,
      target_percent: link.target_percent,
      overlap_vertices: link.overlap_vertices,
      source_node_id: link.source_node_id || "",
      target_node_id: link.target_node_id || "",
      source_node_area: link.source_node_area,
      target_node_area: link.target_node_area,
      source_rsi_file: sourceRsi,
      target_rsi_file: targetRsi,
      source_rsijson_file: sourceRsijson,
      target_rsijson_file: targetRsijson,
      selected_metric: selectedMetricLabel,
      selected_metric_value: selectedMetricValue,
      width: link.width,
      opacity: link.opacity,
      score: link.score
    });
    detailsContent.innerHTML = `
      <h3>Link ${escapeHtml(link.source_sheet_id)} → ${escapeHtml(link.target_sheet_id)}</h3>
      <div class="meta-list">
        <div>Mode</div><div>${escapeHtml(modeLabel(panel.dataMode))}</div>
        <div>Source timestep</div><div>${escapeHtml(link.source_label)}</div>
        <div>Target timestep</div><div>${escapeHtml(link.target_label)}</div>
        <div>Source stem</div><div>${escapeHtml(link.source_stem || sourceNode?.stem || "N/A")}</div>
        <div>Target stem</div><div>${escapeHtml(link.target_stem || targetNode?.stem || "N/A")}</div>
        <div>Source image file</div><div>${escapeHtml(sourceImageFile)}</div>
        <div>Target image file</div><div>${escapeHtml(targetImageFile)}</div>
        <div>${escapeHtml(selectedMetricLabel)}</div><div>${escapeHtml(formatScore(selectedMetricValue))}</div>
      </div>
      <div class="thumb-row">
        <div>${sourceImage}</div>
        <div>${targetImage}</div>
      </div>
      <div class="meta-list">${metricRows}</div>
      ${rawTable}
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
        const ts = timestepLookup.itemAt(t);
        if (!ts) continue;
        columns.push({ type: "timestep", timestep: ts });
        seen.add(t);
      }
    }
    return columns;
  }

  function gatherVisibleMatchEdges(panel, thresholdPercent) {
    const edges = [];
    const pairs = pairsForMode(panel.dataMode);
    const ranges = normalizedRanges();
    const threshold = clamp(Number(thresholdPercent) || 0, 0, 100) / 100;
    const metricMax = metricMaxima[panel.metricId] || 1;
    const visible = new Set();
    for (const range of ranges.length ? ranges : [{ start: 0, end: timestepMax }]) {
      for (let t = range.start; t <= range.end; t += 1) visible.add(t);
    }

    for (const pair of pairs) {
      if (!visible.has(pair.source_timestep_index) || !visible.has(pair.target_timestep_index)) continue;
      for (const match of pair.matches) {
        const score = metricValue(match, panel.metricId);
        const normalized = metricMax > 0 ? score / metricMax : 0;
        if (normalized < threshold) continue;
        edges.push({
          ...match,
          source_timestep_index: pair.source_timestep_index,
          source_label: pair.source_label,
          source_stem: pair.source_stem || "",
          source_rsijson_file: pair.source_rsijson_file || match.source_rsijson_file || "",
          source_rsi_file: pair.source_rsi_file || match.source_rsi_file || "",
          target_timestep_index: pair.target_timestep_index,
          target_label: pair.target_label,
          target_stem: pair.target_stem || "",
          target_rsijson_file: pair.target_rsijson_file || match.target_rsijson_file || "",
          target_rsi_file: pair.target_rsi_file || match.target_rsi_file || "",
          global_bounds: pair.global_bounds || [],
          score,
          width: linkWidth(match, panel.metricId),
          opacity: scoreOpacity(match, panel.metricId)
        });
      }
    }
    return edges;
  }

  function gatherVisiblePairs(panel, thresholdPercent, nodeByKey, edgeList = null) {
    const links = [];
    const edges = edgeList || gatherVisibleMatchEdges(panel, thresholdPercent);
    for (const edge of edges) {
      const sourceNode = nodeByKey.get(`${edge.source_timestep_index}:${edge.source_sheet_id}`);
      const targetNode = nodeByKey.get(`${edge.target_timestep_index}:${edge.target_sheet_id}`);
      if (!sourceNode || !targetNode) continue;
      links.push({
        ...edge,
        sourceNode,
        targetNode
      });
    }
    return links;
  }

  function nodeSortComparator(mode) {
    if (mode === "vertices") {
      return (a, b) =>
        d3.descending(+a.num_vertices || 0, +b.num_vertices || 0) ||
        d3.descending(+a.area || 0, +b.area || 0) ||
        d3.ascending(+a.rank || 0, +b.rank || 0) ||
        d3.ascending(+a.sheet_id || 0, +b.sheet_id || 0);
    }
    return (a, b) =>
      d3.descending(+a.area || 0, +b.area || 0) ||
      d3.descending(+a.num_vertices || 0, +b.num_vertices || 0) ||
      d3.ascending(+a.rank || 0, +b.rank || 0) ||
      d3.ascending(+a.sheet_id || 0, +b.sheet_id || 0);
  }

  function normalizeTopSheets(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 10;
    return Math.max(1, Math.floor(n));
  }

  function pickTopSheetsByRank(sheets, topSheets) {
    const limit = normalizeTopSheets(topSheets);
    return (sheets || [])
      .slice()
      .sort((a, b) =>
        d3.ascending(+a.rank || 0, +b.rank || 0) ||
        d3.descending(+a.area || 0, +b.area || 0) ||
        d3.descending(+a.num_vertices || 0, +b.num_vertices || 0) ||
        d3.ascending(+a.sheet_id || 0, +b.sheet_id || 0)
      )
      .slice(0, limit);
  }

  function computeCrossingOrder(columnNodesByTime, edgeList, fallbackComparator) {
    const timeKeys = [...columnNodesByTime.keys()].sort((a, b) => a - b);
    const orderByKey = new Map();
    const nodeKey = (t, s) => `${t}:${s}`;

    for (const t of timeKeys) {
      const nodes = columnNodesByTime.get(t) || [];
      nodes.sort(fallbackComparator);
      nodes.forEach((node, index) => {
        orderByKey.set(nodeKey(t, node.sheet_id), index);
      });
    }

    const incomingByNode = new Map();
    const outgoingByNode = new Map();
    for (const edge of edgeList) {
      const sourceKey = nodeKey(edge.source_timestep_index, edge.source_sheet_id);
      const targetKey = nodeKey(edge.target_timestep_index, edge.target_sheet_id);
      if (!incomingByNode.has(targetKey)) incomingByNode.set(targetKey, []);
      if (!outgoingByNode.has(sourceKey)) outgoingByNode.set(sourceKey, []);
      incomingByNode.get(targetKey).push(sourceKey);
      outgoingByNode.get(sourceKey).push(targetKey);
    }

    const sweepSort = (nodes, neighborsByNode, timestep, direction) => {
      const withScores = nodes.map(node => {
        const key = nodeKey(timestep, node.sheet_id);
        const neighbors = neighborsByNode.get(key) || [];
        const values = neighbors
          .map(neighborKey => orderByKey.get(neighborKey))
          .filter(v => Number.isFinite(v));
        return {
          node,
          barycenter: values.length ? d3.mean(values) : Number.POSITIVE_INFINITY
        };
      });

      withScores.sort((a, b) =>
        d3.ascending(a.barycenter, b.barycenter) ||
        fallbackComparator(a.node, b.node)
      );

      withScores.forEach((entry, index) => {
        orderByKey.set(nodeKey(timestep, entry.node.sheet_id), index);
      });
      columnNodesByTime.set(timestep, withScores.map(entry => entry.node));
    };

    for (let iteration = 0; iteration < 4; iteration += 1) {
      for (let i = 1; i < timeKeys.length; i += 1) {
        const t = timeKeys[i];
        sweepSort(columnNodesByTime.get(t) || [], incomingByNode, t, "left");
      }
      for (let i = timeKeys.length - 2; i >= 0; i -= 1) {
        const t = timeKeys[i];
        sweepSort(columnNodesByTime.get(t) || [], outgoingByNode, t, "right");
      }
    }
  }

  function layoutForPanel(columns, panel, edgeList) {
    const xStart = 26;
    const colWidth = 112;
    const gapScale = 2.2;
    const nodeWidth = 18;
    const topPad = 54;
    const bottomPad = 22;
    const nodeGap = 6;
    const gapWidth = 34;
    const targetColumnHeight = 600;
    const maxAllowedColumnHeight = 860;
    const minNodeHeight = 5;
    const fallbackNodeHeight = 10;
    const linkHeadroom = 1.04;

    const visibleNodes = [];
    const nodeByKey = new Map();
    let usedMaxColumnHeight = 0;
    let xCursor = xStart;

    const fallbackComparator = nodeSortComparator(state.layoutControls.orderingMode === "vertices" ? "vertices" : "area");
    const columnNodesByTime = new Map();
    for (const column of columns) {
      if (column.type !== "timestep") continue;
      const timestep = column.timestep;
      const topSheets = state.layoutControls.topSheets;
      const nodes = pickTopSheetsByRank(timestep.sheets || [], topSheets).map(node => ({
        ...node,
        timestep_index: timestep.timestep_index,
        timestep_label: timestep.label,
        stem: timestep.stem
      }));
      nodes.sort(fallbackComparator);
      columnNodesByTime.set(+timestep.timestep_index, nodes);
    }

    if (state.layoutControls.orderingMode === "crossings") {
      computeCrossingOrder(columnNodesByTime, edgeList, fallbackComparator);
    } else {
      const comparator = nodeSortComparator(state.layoutControls.orderingMode);
      for (const timestep of columnNodesByTime.keys()) {
        const nodes = columnNodesByTime.get(timestep) || [];
        nodes.sort(comparator);
        columnNodesByTime.set(timestep, nodes);
      }
    }

    const selectedNodeKeys = new Set();
    for (const [timeIndex, nodes] of columnNodesByTime.entries()) {
      for (const node of nodes) {
        selectedNodeKeys.add(`${timeIndex}:${node.sheet_id}`);
      }
    }
    const outgoingTotalsByNode = new Map();
    const incomingTotalsByNode = new Map();
    for (const edge of edgeList || []) {
      const sourceKey = `${edge.source_timestep_index}:${edge.source_sheet_id}`;
      const targetKey = `${edge.target_timestep_index}:${edge.target_sheet_id}`;
      if (!selectedNodeKeys.has(sourceKey) || !selectedNodeKeys.has(targetKey)) continue;
      const width = Math.max(0, Number(edge.width) || 0);
      if (width <= 0) continue;
      outgoingTotalsByNode.set(sourceKey, (outgoingTotalsByNode.get(sourceKey) || 0) + width);
      incomingTotalsByNode.set(targetKey, (incomingTotalsByNode.get(targetKey) || 0) + width);
    }
    const incidentLinkFloorByNode = new Map();
    for (const key of selectedNodeKeys) {
      incidentLinkFloorByNode.set(
        key,
        Math.max(outgoingTotalsByNode.get(key) || 0, incomingTotalsByNode.get(key) || 0)
      );
    }

    const sizeMode = "vertices";
    const metricTotalsByTime = new Map();
    let globalTotal = 0;
    for (const [timestep, nodes] of columnNodesByTime.entries()) {
      const total = d3.sum(nodes, node => nodeMetricValue(node, sizeMode));
      metricTotalsByTime.set(timestep, total);
      globalTotal = Math.max(globalTotal, total);
    }
    const globalScale = globalTotal > 0 ? targetColumnHeight / globalTotal : 1;

    columns.forEach(column => {
      if (column.type === "gap") {
        xCursor += column.span * (colWidth + gapWidth) * gapScale;
        return;
      }
      const timestep = column.timestep;
      const nodes = [...(columnNodesByTime.get(+timestep.timestep_index) || [])];
      let y = topPad;
      let columnHeight = 0;
      const metricValues = nodes.map(node => nodeMetricValue(node, sizeMode));
      const baseScale = globalScale;
      let heights = metricValues.map((value, nodeIndex) => {
        const node = nodes[nodeIndex];
        const nodeKey = `${node.timestep_index}:${node.sheet_id}`;
        const linkFloor = (incidentLinkFloorByNode.get(nodeKey) || 0) * linkHeadroom;
        const baseHeight = value > 0
          ? Math.max(minNodeHeight, value * baseScale)
          : fallbackNodeHeight;
        return Math.max(baseHeight, linkFloor);
      });

      const allowedHeight = Math.max(160, maxAllowedColumnHeight - nodeGap * Math.max(0, nodes.length - 1));
      const sumHeights = d3.sum(heights);
      if (sumHeights > allowedHeight && sumHeights > 0) {
        const factor = allowedHeight / sumHeights;
        heights = heights.map(value => Math.max(2, value * factor));
      }

      nodes.forEach((node, nodeIndex) => {
        const height = heights[nodeIndex];
        const layoutNode = {
          ...node,
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

      usedMaxColumnHeight = Math.max(usedMaxColumnHeight, columnHeight);
      xCursor += colWidth;
    });

    const height = Math.max(260, usedMaxColumnHeight + bottomPad);
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
      const metricMax = metricMaxima[panel.metricId] || 1;
      const hideIsolated = Boolean(state.layoutControls.hideIsolated);
      if (!hideIsolated) {
        if (view.nodeSelection) view.nodeSelection.style("display", null);
        view.linkSelection
          .style("display", d => {
            const normalized = metricMax > 0 ? metricValue(d, panel.metricId) / metricMax : 0;
            return normalized >= threshold ? null : "none";
          })
          .style("pointer-events", d => {
            const normalized = metricMax > 0 ? metricValue(d, panel.metricId) / metricMax : 0;
            return normalized >= threshold ? "all" : "none";
          });
        return;
      }

      const incidentNodes = new Set();
      view.linkSelection.each(function(d) {
        const normalized = metricMax > 0 ? metricValue(d, panel.metricId) / metricMax : 0;
        const visible = normalized >= threshold;
        this.style.display = visible ? "" : "none";
        this.style.pointerEvents = visible ? "all" : "none";
        if (visible) {
          incidentNodes.add(`${d.source_timestep_index}:${d.source_sheet_id}`);
          incidentNodes.add(`${d.target_timestep_index}:${d.target_sheet_id}`);
        }
      });
      if (view.nodeSelection) {
        view.nodeSelection.each(function(d) {
          this.style.display = incidentNodes.has(`${d.timestep_index}:${d.sheet_id}`) ? "" : "none";
        });
      }
    });
    renderStats();
    renderRangeBar();
  }

  function visibleTimestepWindow() {
    const panel = currentPanel();
    if (!panel || !panel.graphToTime || !panel.canvasNode) return null;
    return window.ReebViewerCommon.computeVisibleTimestepWindow({
      graphToTime: panel.graphToTime,
      camera,
      viewportWidth: Math.max(1, panel.canvasNode.clientWidth || 1)
    });
  }

  function recenterViewportFromBarIndex(targetTime) {
    const panel = currentPanel();
    window.ReebViewerCommon.recenterCameraFromRangeBar(targetTime, {
      graphToTime: panel?.graphToTime,
      maxTime: timestepMax,
      camera,
      viewportWidth: Math.max(1, panel?.canvasNode?.clientWidth || 1),
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

    window.ReebViewerCommon.fitAndCenter(
      camera,
      bounds,
      b => fitZoomForBounds(b, panel.canvasNode),
      { fit: true }
    );
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

    const rangeBarController = window.ReebViewerCommon.createRangeBarController({
      getState: () => ({
        ranges: state.ranges,
        selectedRangeIndex: state.selectedRangeIndex,
        rangeDrag: state.rangeDrag,
        viewportDrag: state.viewportDrag
      }),
      applyRangeAction,
      setViewportDrag: next => {
        state.viewportDrag = next;
      },
      onRangeCommitted: () => {
        if (rangeDispatcher) {
          rangeDispatcher.runPlan("rangeCommitted");
          return;
        }
        renderAll();
      },
      onBarOnlyUpdate: () => {
        if (rangeDispatcher) {
          rangeDispatcher.runPlan("barOnly");
          return;
        }
        renderRangeBar();
      },
      onViewportRecenter: idx => {
        recenterViewportFromBarIndex(idx);
      }
    });

    window.ReebViewerCommon.renderRangeBar(svg, {
      width,
      height,
      timestepMax,
      tickValues: timestepLookup.tickValues(12),
      ranges,
      selectedRangeIndex: state.selectedRangeIndex,
      rangeDrag: state.rangeDrag,
      viewportDrag: state.viewportDrag,
      visibleWindow: visibleTimestepWindow,
      rangeLabelFn: range => rangeLabel(range),
      tickLabelFn: value => timestepLookup.labelAt(value, String(value)),
      onRangeSelected: index => rangeBarController.onRangeSelected(index),
      onRangeDragStart: idx => rangeBarController.onRangeDragStart(idx),
      onRangeDragMove: idx => rangeBarController.onRangeDragMove(idx),
      onRangeDragEnd: idx => rangeBarController.onRangeDragEnd(idx),
      onViewportClick: idx => rangeBarController.onViewportClick(idx),
      onViewportDragStart: () => rangeBarController.onViewportDragStart(),
      onViewportDragMove: idx => rangeBarController.onViewportDragMove(idx),
      onViewportDragEnd: () => rangeBarController.onViewportDragEnd()
    });
  }

  function renderStats() {
    const ranges = normalizedRanges();
    const visible = visibleTimesteps();
    const visibleNodes = visible.reduce((sum, t) => sum + (t.sheets?.length || 0), 0);
    const visibleSet = new Set();
    for (const range of ranges.length ? ranges : [{ start: 0, end: timestepMax }]) {
      for (let t = range.start; t <= range.end; t += 1) visibleSet.add(t);
    }
    const pairCountByMode = dataModes.map(mode => {
      const pairs = pairsForMode(mode.id);
      const count = pairs.filter(pair =>
        visibleSet.has(pair.source_timestep_index) &&
        visibleSet.has(pair.target_timestep_index)
      ).length;
      return `${mode.label}: ${count}`;
    }).join(" | ");

    const entries = [
      ["Timesteps", `${visible.length} / ${data.timesteps.length}`],
      ["Nodes", String(visibleNodes)],
      ["Pairs", pairCountByMode],
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
    ensurePanelMetric(panel);
    const container = panel.container;
    container.html("");

    const header = container.append("div").attr("class", "panel-header");
    const title = header.append("div").attr("class", "panel-title");
    title.append("strong").text(`Panel ${panel.id}`);
    title.append("span")
      .style("font-size", "12px")
      .style("color", "#5b6673")
      .text(`${modeLabel(panel.dataMode)} · ${metricLabel(panel.dataMode, panel.metricId)}`);

    const controls = header.append("div").attr("class", "panel-controls");
    const dataModeSelect = controls.append("select");
    dataModes.forEach(mode => {
      dataModeSelect.append("option")
        .attr("value", mode.id)
        .property("selected", mode.id === panel.dataMode)
        .text(mode.label);
    });
    dataModeSelect.on("change", event => {
      panel.dataMode = event.target.value;
      ensurePanelMetric(panel);
      renderAll();
    });

    const metricSelect = controls.append("select");
    metricsForMode(panel.dataMode).forEach(metric => {
      metricSelect.append("option")
        .attr("value", metric.id)
        .property("selected", metric.id === panel.metricId)
        .text(metric.label);
    });
    metricSelect.on("change", event => {
      panel.metricId = event.target.value;
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
        state.panels.push({ id: state.nextPanelId++, dataMode: "overlap", metricId: "overlap_max_percent", threshold: 0 });
      }
      renderAll();
    });

    const canvas = container.append("div").attr("class", "panel-canvas");
    const svg = canvas.append("svg").attr("class", "summary-chart");

    const columns = buildVisibleColumns();
    const edgeList = gatherVisibleMatchEdges(panel, 0);
    const layout = layoutForPanel(columns, panel, edgeList);
    const links = assignLinkOffsets(gatherVisiblePairs(panel, 0, layout.nodeByKey, edgeList));
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
      window.ReebViewerCommon.fitAndCenter(
        camera,
        layoutBounds,
        b => fitZoomForBounds(b, canvas.node()),
        { fit: true }
      );
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
      .attr("fill", d => linkFillColor(d.opacity, false))
      .on("mouseenter", function(event, d) {
        d3.select(this).classed("hover", true);
        d3.select(this).attr("fill", linkFillColor(d.opacity, true));
        updateTooltip(linkTooltip(d, panel), event.clientX, event.clientY);
      })
      .on("mousemove", (event, d) => updateTooltip(linkTooltip(d, panel), event.clientX, event.clientY))
      .on("mouseleave", function(event, d) {
        d3.select(this).classed("hover", false);
        d3.select(this).attr("fill", linkFillColor(d.opacity, false));
        hideTooltip();
      })
      .on("click", (_, d) => showLinkDetails(d, panel));

    state.panelViews.get(panel.id).linkSelection = linkSelection;

    const node = root.append("g")
      .selectAll("g")
      .data(layout.visibleNodes)
      .join("g")
      .attr("class", "node")
      .on("mouseenter", function(event, d) {
        d3.select(this).classed("hover", true);
        updateTooltip(nodeTooltip(d), event.clientX, event.clientY);
      })
      .on("mousemove", (event, d) => updateTooltip(nodeTooltip(d), event.clientX, event.clientY))
      .on("mouseleave", function() {
        d3.select(this).classed("hover", false);
        hideTooltip();
      })
      .on("click", (_, d) => showNodeDetails(d));

    node.append("rect")
      .attr("x", d => d.x0)
      .attr("y", d => d.y0)
      .attr("width", d => d.x1 - d.x0)
      .attr("height", d => Math.max(2, d.height))
      .attr("fill", d => nodeColorFill(d));

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

  function refreshLinkDarkness() {
    state.panelViews.forEach(view => {
      if (!view.linkSelection) return;
      view.linkSelection.attr("fill", d => linkFillColor(d.opacity, false));
    });
  }

  function bindLayoutControls() {
    const orderingNode = document.getElementById("orderingMode");
    const nodeSizeNode = document.getElementById("nodeSizeMode");
    const topSheetsNode = document.getElementById("topSheets");
    const nodeColorNode = document.getElementById("nodeColorMode");
    const darknessNode = document.getElementById("linkDarkness");
    const darknessValueNode = document.getElementById("linkDarknessValue");
    const hideIsolatedNode = document.getElementById("hideIsolated");

    if (orderingNode) {
      orderingNode.value = state.layoutControls.orderingMode;
      orderingNode.addEventListener("change", event => {
        state.layoutControls.orderingMode = event.target.value;
        scheduleRenderAll();
      });
    }

    if (nodeSizeNode) {
      nodeSizeNode.value = "vertices";
      nodeSizeNode.addEventListener("change", () => {
        state.layoutControls.nodeSizeMode = "vertices";
        scheduleRenderAll();
      });
    }

    if (topSheetsNode) {
      const commitTopSheets = value => {
        const next = normalizeTopSheets(value);
        state.layoutControls.topSheets = next;
        topSheetsNode.value = String(next);
        scheduleRenderAll();
      };
      topSheetsNode.value = String(normalizeTopSheets(state.layoutControls.topSheets));
      topSheetsNode.addEventListener("change", event => commitTopSheets(event.target.value));
      topSheetsNode.addEventListener("blur", event => commitTopSheets(event.target.value));
      topSheetsNode.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        commitTopSheets(event.target.value);
        event.target.blur();
      });
    }

    if (nodeColorNode) {
      nodeColorNode.value = state.layoutControls.nodeColorMode;
      nodeColorNode.addEventListener("change", event => {
        state.layoutControls.nodeColorMode = event.target.value;
        scheduleRenderAll();
      });
    }

    if (darknessNode && darknessValueNode) {
      const applyDarkness = value => {
        state.layoutControls.linkDarkness = clamp(Number(value) || 0, 0, 100);
        darknessNode.value = String(state.layoutControls.linkDarkness);
        darknessValueNode.textContent = `${state.layoutControls.linkDarkness}%`;
        refreshLinkDarkness();
      };
      applyDarkness(state.layoutControls.linkDarkness);
      darknessNode.addEventListener("input", event => applyDarkness(event.target.value));
      darknessNode.addEventListener("change", event => applyDarkness(event.target.value));
    }

    if (hideIsolatedNode) {
      hideIsolatedNode.checked = Boolean(state.layoutControls.hideIsolated);
      hideIsolatedNode.addEventListener("change", event => {
        state.layoutControls.hideIsolated = Boolean(event.target.checked);
        scheduleThresholdSync();
      });
    }
  }

  function addPanel() {
    state.panels.push({ id: state.nextPanelId++, dataMode: "shape", metricId: "combined", threshold: 0 });
    renderAll();
  }

  function initRangeDispatcher() {
    rangeDispatcher = window.ReebViewerCommon.createRangeActionDispatcher({
      applyRangeAction,
      getState: () => ({
        ranges: state.ranges,
        selectedRangeIndex: state.selectedRangeIndex,
        rangeDrag: state.rangeDrag
      }),
      handlers: {
        all: () => renderAll(),
        bar: () => renderRangeBar()
      },
      plans: {
        rowCommit: ["all"],
        rangeCommitted: ["all"],
        barOnly: ["bar"]
      }
    });
  }

  document.getElementById("addRange").addEventListener("click", addRange);
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
  bindLayoutControls();
  initRangeDispatcher();
  renderAll();
}).catch(error => {
  console.error(error);
  document.body.insertAdjacentHTML("beforeend", `<pre style="padding:16px;color:#b00020;">${error}</pre>`);
});
"""
    )
    return path


def build_unified_sankey_viewer_stage() -> None:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Expected match results at {MATCHES_FILE}")

    if UNIFIED_VIEWER_DIR.exists():
        shutil.rmtree(UNIFIED_VIEWER_DIR)
    UNIFIED_VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    link_sheet_images(UNIFIED_VIEWER_DIR)

    data = prepare_data(UNIFIED_VIEWER_DIR)
    data_path = write_data_json(data)
    index_path = write_index_html()
    js_path = write_viewer_js()
    css_path = write_style_css()
    common_js_path = write_viewer_common_js(UNIFIED_VIEWER_DIR)
    root_index_path = write_root_index_html()

    print(f"Wrote unified sankey viewer: {UNIFIED_VIEWER_DIR}")
    for artifact in (data_path, index_path, js_path, css_path, common_js_path):
      print(f"  {artifact.name}")
    print(f"Wrote root entry: {root_index_path}")
    print("\nOpen with:")
    print(f"  cd {OUTPUT_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


def main() -> int:
    build_unified_sankey_viewer_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
