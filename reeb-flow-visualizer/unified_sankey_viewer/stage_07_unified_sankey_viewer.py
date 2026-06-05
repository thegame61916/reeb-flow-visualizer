#!/usr/bin/env python3

"""Build a unified Sankey dashboard for overlap and shape metrics."""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

from common import (
    BASE_DIR,
    CENTROID_AXIS_DIAGONAL_COLORS,
    CENTROID_COLOR_CORNERS,
    FIBER_SURFACE_IMAGE_DIR,
    HYBRID_SCORE_DEFAULT_WEIGHTS,
    HYBRID_VERTEX_METRIC_DEFAULT,
    OUTPUT_DIR,
    OVERLAP_FILE,
    SHEET_IMAGE_DIR,
    SHAPE_SCORE_DEFAULT_WEIGHTS,
    TRACKING_ANALYSIS_EVENT_SCORE_TERMS,
    TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
    TRACKING_ANALYSIS_THRESHOLDS,
    TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
    TRACKING_ANALYSIS_TOP_FEATURES,
    TRACKING_ANALYSIS_TOP_INTERVALS,
    TRACKING_ANALYSIS_VIEWER_FILE,
    TRACKING_DATA_FILE,
    tracking_analysis_event_score_formula_text,
    VIEWER_DEFAULT_TOP_SHEETS,
)
from unified_sankey_viewer.viewer_common import (
    shared_viewer_css,
    shared_viewer_script_tags,
    write_viewer_common_js,
)

STORAGE_ROOT = BASE_DIR / "compareSheetShapesCache"
TIMESTEP_CACHE_DIR = STORAGE_ROOT / "cache" / "timesteps"
MATCHES_FILE = STORAGE_ROOT / "results" / "sheet_shape_matches.json"

UNIFIED_VIEWER_DIR = OUTPUT_DIR / "unified_sankey_viewer"

SHAPE_METRICS = [
    {"id": "combined", "label": "combined", "field": "final_score"},
    {"id": "shape_iou", "label": "shape IoU", "field": "shape_iou"},
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

HYBRID_METRICS = [
    {"id": "hybrid_combined", "label": "hybrid combined", "field": "hybrid_combined"},
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
    {
        "id": "hybrid",
        "label": "Hybrid metrics",
        "pair_field": "",
        "default_metric": "hybrid_combined",
        "metrics": HYBRID_METRICS,
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


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = str(hex_color or "").strip().lstrip("#")
    if len(value) != 6:
        return (111, 158, 212)
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (111, 158, 212)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    channels = [round(clamp(channel, 0.0, 255.0)) for channel in rgb]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def lerp_rgb(
    a: tuple[int, int, int] | tuple[float, float, float],
    b: tuple[int, int, int] | tuple[float, float, float],
    t: float,
) -> tuple[float, float, float]:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))  # type: ignore[return-value]


def safe_bounds(value) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    bounds = tuple(safe_float(item) for item in value)
    xmin, ymin, xmax, ymax = bounds
    if not all(math.isfinite(item) for item in bounds):
        return None
    if xmax == xmin:
        xmin -= 0.5
        xmax += 0.5
    if ymax == ymin:
        ymin -= 0.5
        ymax += 0.5
    return xmin, ymin, xmax, ymax


def merge_bounds(
    current: tuple[float, float, float, float] | None,
    incoming: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    return (
        min(current[0], incoming[0]),
        min(current[1], incoming[1]),
        max(current[2], incoming[2]),
        max(current[3], incoming[3]),
    )


def origin_symmetric_bounds(
    bounds: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    xmin, ymin, xmax, ymax = bounds
    x_extent = max(abs(xmin), abs(xmax))
    y_extent = max(abs(ymin), abs(ymax))
    if x_extent == 0.0:
        x_extent = 0.5
    if y_extent == 0.0:
        y_extent = 0.5
    return (-x_extent, -y_extent, x_extent, y_extent)


def bounds_from_centroids(cache_items: list[dict]) -> tuple[float, float, float, float]:
    bounds: tuple[float, float, float, float] | None = None
    for data in cache_items:
        for sheet in data.get("sheets", []):
            centroid = sheet.get("centroid", [])
            if not isinstance(centroid, (list, tuple)) or len(centroid) < 2:
                continue
            x = safe_float(centroid[0])
            y = safe_float(centroid[1])
            if math.isfinite(x) and math.isfinite(y):
                bounds = merge_bounds(bounds, (x, y, x, y))
    return safe_bounds(bounds or (0.0, 0.0, 1.0, 1.0)) or (0.0, 0.0, 1.0, 1.0)


def centroid_color(centroid, bounds: tuple[float, float, float, float]) -> tuple[str, list[float]]:
    if not isinstance(centroid, (list, tuple)) or len(centroid) < 2:
        return "#6f9ed4", [0.0, 0.0]
    xmin, ymin, xmax, ymax = bounds
    x = safe_float(centroid[0])
    y = safe_float(centroid[1])
    tx = clamp((x - xmin) / (xmax - xmin), 0.0, 1.0) if xmax > xmin else 0.0
    ty = clamp((y - ymin) / (ymax - ymin), 0.0, 1.0) if ymax > ymin else 0.0
    corners = {key: hex_to_rgb(value) for key, value in CENTROID_COLOR_CORNERS.items()}
    bottom = lerp_rgb(corners["bottom_left"], corners["bottom_right"], tx)
    top = lerp_rgb(corners["top_left"], corners["top_right"], tx)
    return rgb_to_hex(lerp_rgb(bottom, top, ty)), [tx, ty]


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


def link_fiber_surface_images(viewer_dir: Path) -> None:
    if not FIBER_SURFACE_IMAGE_DIR.exists():
        return

    target = viewer_dir / "fiber_surface_images"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    try:
        target.symlink_to(FIBER_SURFACE_IMAGE_DIR.resolve(), target_is_directory=True)
    except OSError:
        shutil.copytree(FIBER_SURFACE_IMAGE_DIR, target)


def find_sheet_image(stem: str, sheet_id: int, viewer_dir: Path) -> str | None:
    folder = SHEET_IMAGE_DIR / stem
    if not folder.exists():
        return None

    # Prefer the new uniform-color filename, then fall back to the legacy
    # "<sheet_id>_<hex>.png" format for backward compatibility.
    matches = []
    preferred = folder / f"sheet_{sheet_id}.png"
    if preferred.exists():
        matches.append(preferred)
    else:
        matches = sorted(folder.glob(f"{sheet_id}_*.png"))
    if not matches:
        return None

    linked = viewer_dir / "sheet_images" / stem / matches[0].name
    return linked.relative_to(viewer_dir).as_posix()


def find_fiber_surface_image(stem: str, sheet_id: int, viewer_dir: Path) -> str | None:
    folder = FIBER_SURFACE_IMAGE_DIR / stem
    if not folder.exists():
        return None

    image = folder / f"sheet_{sheet_id}.png"
    if not image.exists():
        return None

    linked = viewer_dir / "fiber_surface_images" / stem / image.name
    return linked.relative_to(viewer_dir).as_posix()


def load_timestep_cache(viewer_dir: Path) -> tuple[list[dict], float, int, tuple[float, float, float, float]]:
    if not TIMESTEP_CACHE_DIR.exists():
        raise FileNotFoundError(f"Timestep cache directory missing: {TIMESTEP_CACHE_DIR}")

    cache_items = [json.loads(path.read_text()) for path in sorted(TIMESTEP_CACHE_DIR.glob("*.json"))]
    centroid_color_bounds: tuple[float, float, float, float] | None = None
    for data in cache_items:
        centroid_color_bounds = merge_bounds(centroid_color_bounds, safe_bounds(data.get("global_bounds")))
    if centroid_color_bounds is None:
        centroid_color_bounds = bounds_from_centroids(cache_items)
    centroid_color_bounds = origin_symmetric_bounds(centroid_color_bounds) or (-0.5, -0.5, 0.5, 0.5)

    timesteps: list[dict] = []
    max_area = 0.0
    max_vertices = 0

    for data in cache_items:
        sheets = []
        stem = str(data.get("stem", ""))
        for sheet in data.get("sheets", []):
            area = safe_float(sheet.get("area"))
            vertices = safe_int(sheet.get("num_vertices"))
            max_area = max(max_area, area)
            max_vertices = max(max_vertices, vertices)
            color, color_position = centroid_color(sheet.get("centroid", []), centroid_color_bounds)
            sheets.append(
                {
                    "sheet_id": safe_int(sheet.get("sheet_id")),
                    "rank": safe_int(sheet.get("rank")),
                    "area": area,
                    "num_vertices": vertices,
                    "bbox": sheet.get("bbox", []),
                    "centroid": sheet.get("centroid", []),
                    "centroid_color": color,
                    "centroid_color_position": color_position,
                    "thumbnail": find_sheet_image(stem, safe_int(sheet.get("sheet_id")), viewer_dir),
                    "fiber_surface_image": find_fiber_surface_image(stem, safe_int(sheet.get("sheet_id")), viewer_dir),
                }
            )

        timesteps.append(
            {
                "timestep_index": safe_int(data.get("timestep_index")),
                "label": str(data.get("label", "")),
                "stem": stem,
                "sheets": sheets,
            }
        )

    timesteps.sort(key=lambda item: item["timestep_index"])
    return timesteps, max_area, max_vertices, centroid_color_bounds

def load_match_data() -> dict:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Match file does not exist: {MATCHES_FILE}")
    return json.loads(MATCHES_FILE.read_text())


def load_overlap_data() -> dict:
    if not OVERLAP_FILE.exists():
        raise FileNotFoundError(f"Overlap file does not exist: {OVERLAP_FILE}")
    return json.loads(OVERLAP_FILE.read_text())


def prepare_data(viewer_dir: Path) -> dict:
    timesteps, max_area, max_vertices, centroid_color_bounds = load_timestep_cache(viewer_dir)
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
            "shape_score_components": list(SHAPE_SCORE_DEFAULT_WEIGHTS.keys()),
            "shape_score_default_weights": SHAPE_SCORE_DEFAULT_WEIGHTS,
            "hybrid_score_default_weights": HYBRID_SCORE_DEFAULT_WEIGHTS,
            "hybrid_vertex_metric_default": HYBRID_VERTEX_METRIC_DEFAULT,
            "tracking_analysis_thresholds": list(TRACKING_ANALYSIS_THRESHOLDS),
            "tracking_analysis_preferred_threshold": TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
            "tracking_analysis_top_intervals": TRACKING_ANALYSIS_TOP_INTERVALS,
            "tracking_analysis_top_features": TRACKING_ANALYSIS_TOP_FEATURES,
            "tracking_analysis_top_disagreements": TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
            "tracking_analysis_event_score_terms": list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS),
            "tracking_analysis_event_score_formula": tracking_analysis_event_score_formula_text(),
            "global_area_max": max_area,
            "global_vertex_max": max_vertices,
            "centroid_color_bounds": list(centroid_color_bounds),
            "centroid_color_corners": CENTROID_COLOR_CORNERS,
            "centroid_axis_diagonal_colors": CENTROID_AXIS_DIAGONAL_COLORS,
            "default_ranges": DEFAULT_RANGES,
            "viewer_default_top_sheets": max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10)),
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
            "fiber_surface_image_dir": str(FIBER_SURFACE_IMAGE_DIR),
        },
    }


def write_json_file(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False))
    return path


def write_tracking_data_json(data: dict) -> Path:
    return write_json_file(data, TRACKING_DATA_FILE)


def write_viewer_data_json(data: dict) -> Path:
    return write_json_file(data, UNIFIED_VIEWER_DIR / "data.json")


def apply_current_tracking_analysis_meta(data: dict) -> dict:
    meta = data.setdefault("meta", {})
    meta["data_modes"] = DATA_MODES
    meta["shape_score_components"] = list(SHAPE_SCORE_DEFAULT_WEIGHTS.keys())
    meta["shape_score_default_weights"] = SHAPE_SCORE_DEFAULT_WEIGHTS
    meta["hybrid_score_default_weights"] = HYBRID_SCORE_DEFAULT_WEIGHTS
    meta["hybrid_vertex_metric_default"] = HYBRID_VERTEX_METRIC_DEFAULT
    meta["centroid_color_corners"] = CENTROID_COLOR_CORNERS
    meta["centroid_axis_diagonal_colors"] = CENTROID_AXIS_DIAGONAL_COLORS
    meta["viewer_default_top_sheets"] = max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10))
    meta["tracking_analysis_thresholds"] = list(TRACKING_ANALYSIS_THRESHOLDS)
    meta["tracking_analysis_preferred_threshold"] = TRACKING_ANALYSIS_PREFERRED_THRESHOLD
    meta["tracking_analysis_top_intervals"] = TRACKING_ANALYSIS_TOP_INTERVALS
    meta["tracking_analysis_top_features"] = TRACKING_ANALYSIS_TOP_FEATURES
    meta["tracking_analysis_top_disagreements"] = TRACKING_ANALYSIS_TOP_DISAGREEMENTS
    meta["tracking_analysis_event_score_terms"] = list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS)
    meta["tracking_analysis_event_score_formula"] = tracking_analysis_event_score_formula_text()
    return data


def load_tracking_data() -> dict:
    if TRACKING_DATA_FILE.exists():
        return apply_current_tracking_analysis_meta(json.loads(TRACKING_DATA_FILE.read_text()))
    return apply_current_tracking_analysis_meta(prepare_data(UNIFIED_VIEWER_DIR))


def load_analysis_for_viewer() -> dict | None:
    if not TRACKING_ANALYSIS_VIEWER_FILE.exists():
        return None
    try:
        return json.loads(TRACKING_ANALYSIS_VIEWER_FILE.read_text())
    except Exception:
        return None


def build_unified_sankey_data_stage() -> None:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Expected match results at {MATCHES_FILE}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = apply_current_tracking_analysis_meta(prepare_data(UNIFIED_VIEWER_DIR))
    data_path = write_tracking_data_json(data)
    print(f"Wrote tracking data: {data_path}")


def write_index_html() -> Path:
    path = UNIFIED_VIEWER_DIR / "index.html"
    asset_version = str(int(time.time()))
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reeb Flow Visualizer</title>
  <link rel="stylesheet" href="style.css?v={asset_version}">
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
          Top Sheets
          <input id="topSheets" type="number" min="1" step="1" value="{max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10))}">
        </label>
        <label>
          Node Color
          <select id="nodeColorMode">
            <option value="solid" selected>solid</option>
            <option value="area">sheet area</option>
            <option value="vertices">vertex count</option>
            <option value="centroid_position">centroid corners</option>
            <option value="centroid_axis_diagonal">centroid red/blue axes</option>
          </select>
        </label>
        <div id="centroidColorLegend" class="centroid-color-legend" hidden>
          <div class="centroid-color-title">2D centroid color</div>
          <div id="centroidYMax" class="centroid-axis-label"></div>
          <div class="centroid-color-row">
            <div id="centroidXMin" class="centroid-axis-label"></div>
            <canvas id="centroidColorCanvas" width="112" height="112" aria-label="Centroid color space"></canvas>
            <div id="centroidXMax" class="centroid-axis-label"></div>
          </div>
          <div id="centroidYMin" class="centroid-axis-label"></div>
        </div>
        <label>
          Link Darkness
          <input id="linkDarkness" type="range" min="0" max="100" step="1" value="55">
          <span id="linkDarknessValue">55%</span>
        </label>
        <label class="inline">
          <input id="hideIsolated" type="checkbox">
          Hide nodes with no visible links
        </label>
        <label class="inline">
          <input id="strongestOutgoingOnly" type="checkbox">
          Keep strongest outgoing link per node
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

  {shared_viewer_script_tags(include_sankey=False, version=asset_version)}
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
#detailsContent .media-stack {
  display: grid;
  gap: 8px;
  margin: 8px 0;
}
#detailsContent .media-stack .thumb {
  margin: 0;
}
#detailsContent .zoomable-image {
  cursor: zoom-in;
}
#detailsContent .zoomable-image:hover {
  outline: 2px solid #2f80c9;
  outline-offset: 2px;
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
#imageZoomOverlay {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: none;
  grid-template-rows: auto minmax(0, 1fr);
  background: rgba(10, 15, 23, 0.94);
  color: #fff;
}
#imageZoomOverlay.open {
  display: grid;
}
.image-zoom-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.24);
}
.image-zoom-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #edf2f7;
}
.image-zoom-actions {
  display: flex;
  gap: 8px;
  flex: 0 0 auto;
}
.image-zoom-actions button {
  min-width: 34px;
  border-color: rgba(255, 255, 255, 0.36);
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
}
.image-zoom-actions button:hover {
  background: rgba(255, 255, 255, 0.22);
}
.image-zoom-stage {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  cursor: grab;
}
.image-zoom-stage.dragging {
  cursor: grabbing;
}
.image-zoom-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(0, 1fr));
  height: 100%;
  min-width: 0;
  min-height: 0;
}
.image-zoom-pane {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-left: 1px solid rgba(255, 255, 255, 0.18);
}
.image-zoom-pane:first-child {
  border-left: 0;
}
.image-zoom-pane-title {
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 1;
  max-width: calc(100% - 20px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-radius: 5px;
  padding: 4px 7px;
  background: rgba(0, 0, 0, 0.46);
  color: #fff;
  font-size: 12px;
}
.image-zoom-pane img {
  position: absolute;
  top: 0;
  left: 0;
  max-width: none;
  user-select: none;
  pointer-events: none;
  transform-origin: 0 0;
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
.shape-weight-controls {
  display: grid;
  grid-template-columns: repeat(5, minmax(72px, 1fr));
  gap: 6px;
  width: 100%;
  flex-basis: 100%;
}
.shape-weight-item {
  display: grid;
  gap: 3px;
  font-size: 11px;
  color: #556371;
}
.shape-weight-item input {
  height: 26px;
  min-width: 0;
}
.hybrid-controls {
  display: grid;
  grid-template-columns: repeat(3, minmax(88px, 1fr));
  gap: 6px;
  width: 100%;
  flex-basis: 100%;
}
.hybrid-item {
  display: grid;
  gap: 3px;
  font-size: 11px;
  color: #556371;
}
.hybrid-item input,
.hybrid-item select {
  height: 26px;
  min-width: 0;
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
.centroid-color-legend {
  border: 1px solid #d9dee5;
  border-radius: 6px;
  padding: 8px;
  margin: -2px 0 10px;
  background: #fafbfc;
}
.centroid-color-legend[hidden] {
  display: none;
}
.centroid-color-title {
  color: #3f4d5a;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 6px;
}
.centroid-color-row {
  display: grid;
  grid-template-columns: 48px 112px 48px;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.centroid-axis-label {
  min-height: 14px;
  overflow: hidden;
  text-align: center;
  color: #5f6d7b;
  font-size: 11px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
#centroidColorCanvas {
  width: 112px;
  height: 112px;
  display: block;
  border: 1px solid #aeb8c4;
  border-radius: 4px;
  background: #eef2f6;
}
.color-swatch {
  width: 12px;
  height: 12px;
  display: inline-block;
  border: 1px solid rgba(20, 30, 40, 0.35);
  border-radius: 3px;
  vertical-align: -2px;
  margin-right: 5px;
}
.panel-canvas {
  border: 1px solid #d9dee5;
  border-radius: 6px;
  height: 560px;
  min-height: 360px;
  max-height: none;
  overflow: hidden;
  background: #fff;
  cursor: grab;
  touch-action: none;
}
.panel-resizer {
  margin-top: 6px;
  height: 12px;
  border-radius: 6px;
  cursor: ns-resize;
  user-select: none;
  touch-action: none;
  background:
    linear-gradient(#edf1f6, #edf1f6) padding-box,
    repeating-linear-gradient(
      90deg,
      #b3bfce 0 12px,
      transparent 12px 20px
    ) border-box;
  border: 4px solid transparent;
}
.panel-resizer.active {
  background:
    linear-gradient(#e3ebf5, #e3ebf5) padding-box,
    repeating-linear-gradient(
      90deg,
      #8ca2bb 0 12px,
      transparent 12px 20px
    ) border-box;
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

.analysis-box {
  border: 1px solid #dce3eb;
  border-radius: 6px;
  background: #f8fafc;
  padding: 8px;
  margin: 0 0 10px;
}
.analysis-toolbar,
.analysis-tabs,
.analysis-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.analysis-toolbar {
  justify-content: space-between;
  margin-bottom: 7px;
}
.analysis-title {
  font-weight: 700;
  color: #233040;
}
.analysis-tabs {
  margin-bottom: 8px;
}
.analysis-tab {
  border: 1px solid #cbd5e1;
  background: #fff;
  border-radius: 5px;
  padding: 4px 8px;
  cursor: pointer;
}
.analysis-tab.active {
  background: #1f6feb;
  border-color: #1f6feb;
  color: #fff;
}
.analysis-graph {
  border: 1px solid #d9e2ec;
  border-radius: 5px;
  background: #fff;
  padding: 6px 8px 4px;
  margin: 7px 0 8px;
}
.analysis-graph svg {
  cursor: grab;
  display: block;
  width: 100%;
  height: 240px;
  touch-action: none;
}
.analysis-graph.dragging svg {
  cursor: grabbing;
}
.analysis-graph-axis path,
.analysis-graph-axis line {
  stroke: #cbd5e1;
}
.analysis-graph-axis text {
  fill: #536273;
  font-size: 11px;
}
.analysis-graph-label {
  fill: #536273;
  font-size: 11px;
  font-weight: 600;
}
.analysis-graph-grid line {
  stroke: #edf2f7;
}
.analysis-graph-line {
  fill: none;
  stroke: #1f6feb;
  stroke-width: 1.6px;
}
.analysis-graph-zoom {
  fill: transparent;
  pointer-events: all;
}
.analysis-graph-dot {
  fill: #1f6feb;
  stroke: #fff;
  stroke-width: 1.4px;
}
.analysis-graph-dot.active {
  fill: #ef4444;
  stroke: #991b1b;
  stroke-width: 1.8px;
}
.analysis-graph-hit {
  fill: transparent;
  cursor: pointer;
}
.analysis-agreement-line {
  fill: none;
  opacity: 1;
  stroke-opacity: 1;
  stroke-width: 2.2px;
}
.analysis-agreement-dot {
  stroke: #fff;
  stroke-width: 1.4px;
}
.analysis-agreement-dot.active {
  stroke: #111827;
  stroke-width: 2px;
}
.analysis-agreement-hit {
  fill: transparent;
  cursor: pointer;
}
.analysis-metric-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 6px;
  margin: 7px 0 4px;
}
.analysis-metric-card {
  border: 1px solid #d9e2ec;
  border-radius: 5px;
  background: #fff;
  padding: 6px 8px;
  font-size: 12px;
}
.analysis-metric-card header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}
.analysis-metric-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
  color: #233040;
}
.analysis-metric-color {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  display: inline-block;
}
.analysis-metric-card button {
  padding: 2px 6px;
}
.analysis-graph-resizer {
  margin: 4px 0 8px;
}
.analysis-list {
  display: grid;
  gap: 6px;
  max-height: 190px;
  overflow: auto;
}
.analysis-row {
  border: 1px solid #d9e2ec;
  border-radius: 5px;
  background: #fff;
  padding: 6px 8px;
  cursor: pointer;
  text-align: left;
}
.analysis-row:hover {
  border-color: #93b4df;
  background: #f1f6fd;
}
.analysis-row strong {
  display: block;
  color: #1f2937;
  margin-bottom: 2px;
}
.analysis-row span,
.analysis-hint {
  color: #536273;
  font-size: 12px;
}
.analysis-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.analysis-table th,
.analysis-table td {
  border-bottom: 1px solid #e5ebf2;
  padding: 5px 6px;
  text-align: left;
}
.analysis-table button {
  padding: 3px 7px;
}
.node.analysis-highlight rect {
  stroke: #ef4444;
  stroke-width: 2.4px;
}
.link.analysis-highlight {
  filter: drop-shadow(0 0 3px rgba(239, 68, 68, 0.9)) brightness(0.88) saturate(1.2);
}
.analysis-timestep-focus {
  fill: rgba(245, 158, 11, 0.12);
  stroke: rgba(217, 119, 6, 0.78);
  stroke-width: 2px;
  stroke-dasharray: 7 5;
  pointer-events: none;
  animation: analysisTimestepFocusFade 1800ms ease-out forwards;
}
@keyframes analysisTimestepFocusFade {
  0% { opacity: 0.92; }
  65% { opacity: 0.34; }
  100% { opacity: 0; }
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
.media-stack {
  display: grid;
  gap: 7px;
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
    path = UNIFIED_VIEWER_DIR / "viewer.js"
    path.write_text(
        """const DATA = null;

d3.json("data.json").then(data => {
  const PANEL_HEIGHT_DEFAULT = 560;
  const DEFAULT_TOP_SHEETS = Math.max(
    1,
    Math.floor(Number(data?.meta?.viewer_default_top_sheets) || 10)
  );
  const state = {
    ranges: (data.meta.default_ranges && data.meta.default_ranges.length ? data.meta.default_ranges : [{start: 0, end: 20}]).map(r => ({...r})),
    selectedRangeIndex: 0,
    panels: [{ id: 1, dataMode: "overlap", metricId: "overlap_max_percent", threshold: 0, panelHeight: PANEL_HEIGHT_DEFAULT }],
    nextPanelId: 2,
    rangeDrag: null,
    viewportDrag: null,
    tooltipLocked: false,
    activePanelId: 1,
    panelViews: new Map(),
    panelPan: null,
    analysisFocusPulse: null,
    layoutControls: {
      orderingMode: "crossings",
      nodeSizeMode: "vertices",
      topSheets: DEFAULT_TOP_SHEETS,
      nodeColorMode: "solid",
      linkDarkness: 55,
      hideIsolated: false,
      strongestOutgoingOnly: false
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
  const embeddedAnalysisData = data.analysis || null;
  const metaAnalysisThresholds = Array.isArray(data.meta?.tracking_analysis_thresholds) && data.meta.tracking_analysis_thresholds.length
    ? data.meta.tracking_analysis_thresholds.map(Number).filter(Number.isFinite)
    : [0.3, 0.4, 0.5, 0.6, 0.7];
  const metaEventScoreTerms = Array.isArray(data.meta?.tracking_analysis_event_score_terms) && data.meta.tracking_analysis_event_score_terms.length
    ? data.meta.tracking_analysis_event_score_terms
    : [
        { component: "source_weak_count", weight: 1 },
        { component: "target_weak_count", weight: 1 },
        { component: "possible_splits", weight: 0.5 },
        { component: "possible_merges", weight: 0.5 },
        { component: "continuation_gap_source_count", weight: 1 },
      ];
  const analysisData = embeddedAnalysisData
    ? {
        ...embeddedAnalysisData,
        event_score_terms: embeddedAnalysisData.event_score_terms || metaEventScoreTerms,
        event_score_formula: embeddedAnalysisData.event_score_formula || data.meta?.tracking_analysis_event_score_formula || "",
      }
    : {
        thresholds: metaAnalysisThresholds,
        preferred_threshold: Number(data.meta?.tracking_analysis_preferred_threshold) || 0.5,
        top_intervals: Number(data.meta?.tracking_analysis_top_intervals) || 12,
        top_features: Number(data.meta?.tracking_analysis_top_features) || 12,
        split_merge_weight: 0.5,
        event_score_terms: metaEventScoreTerms,
        event_score_formula: data.meta?.tracking_analysis_event_score_formula || "",
        sensitivity: [],
        best_target_agreement: [],
        domain_shape_disagreement_summary: [],
        domain_shape_disagreements: [],
      };
  const hasEmbeddedAnalysisData = Boolean(embeddedAnalysisData);
  const analysisThresholds = Array.isArray(analysisData?.thresholds) && analysisData.thresholds.length
    ? analysisData.thresholds.map(Number).filter(Number.isFinite)
    : [0.5];
  const dataModes = data.meta.data_modes || [];
  const modeById = new Map(dataModes.map(mode => [mode.id, mode]));
  const metricMaxima = data.meta.metric_maxima || {};
  const overlapMetricIds = (modeById.get("overlap")?.metrics || []).map(metric => metric.id);
  const shapeMetricIds = (modeById.get("shape")?.metrics || []).map(metric => metric.id);
  const shapeScoreComponentFallback = ["shape_iou", "area_ratio", "bbox_iou", "centroid_similarity"];
  const shapeScoreComponentRaw = Array.isArray(data.meta.shape_score_components) && data.meta.shape_score_components.length
    ? data.meta.shape_score_components.slice()
    : shapeScoreComponentFallback;
  const shapeScoreComponentIds = shapeScoreComponentRaw.filter(metricId =>
    metricId !== "combined" && shapeMetricIds.includes(metricId)
  );
  if (!shapeScoreComponentIds.length) shapeScoreComponentIds.push(...shapeScoreComponentFallback);
  const shapeScoreDefaultWeightsRaw = data.meta.shape_score_default_weights || {};
  const hybridScoreDefaultWeightsRaw = data.meta.hybrid_score_default_weights || {};
  const hybridVertexMetricDefault = overlapMetricIds.includes(data.meta.hybrid_vertex_metric_default)
    ? data.meta.hybrid_vertex_metric_default
    : (overlapMetricIds.includes("overlap_max_percent") ? "overlap_max_percent" : (overlapMetricIds[0] || "overlap_max_percent"));
  const areaMax = data.meta.global_area_max || 1;
  const vertexMax = data.meta.global_vertex_max || 1;
  const centroidColorBounds = Array.isArray(data.meta.centroid_color_bounds) && data.meta.centroid_color_bounds.length === 4
    ? data.meta.centroid_color_bounds.map(Number)
    : [0, 0, 1, 1];
  const centroidCornerColors = {
    bottom_left: data.meta.centroid_color_corners?.bottom_left || "#2563eb",
    bottom_right: data.meta.centroid_color_corners?.bottom_right || "#dc2626",
    top_left: data.meta.centroid_color_corners?.top_left || "#16a34a",
    top_right: data.meta.centroid_color_corners?.top_right || "#f59e0b"
  };
  const centroidAxisDiagonalColors = {
    origin: data.meta.centroid_axis_diagonal_colors?.origin || "#808080",
    x_axis: data.meta.centroid_axis_diagonal_colors?.x_axis || "#0000ff",
    y_axis: data.meta.centroid_axis_diagonal_colors?.y_axis || "#ff0000"
  };
  const linkMin = data.meta.link_thickness_min || 1.4;
  const linkMax = data.meta.link_thickness_max || 16;
  const timestepMax = timestepLookup.maxIndex;
  const VIEWPORT_ANCHOR_Y = 0.50;
  const ZOOM_MIN = 0.1;
  const ZOOM_MAX = 20;
  const ZOOM_STEP = 1.2;
  const PAN_DRAG_THRESHOLD = 4;
  const PANEL_HEIGHT_MIN = 360;
  const PANEL_HEIGHT_MAX = 1400;
  const INTERVAL_GRAPH_HEIGHT_DEFAULT = 240;
  const INTERVAL_GRAPH_HEIGHT_MIN = 160;
  const INTERVAL_GRAPH_HEIGHT_MAX = 680;
  const IMAGE_ZOOM_MIN = 0.03;
  const IMAGE_ZOOM_MAX = 20;
  const TIMESTEP_LABEL_OPTIONS = { divisor: 41.341374575751, digits: 2 };
  const AGREEMENT_SERIES_COLORS = ["#1f6feb", "#dc2626", "#16a34a", "#f59e0b", "#7c3aed", "#0891b2", "#db2777", "#4b5563"];
  const ANALYSIS_DOT_RADIUS = 5.6;
  const ANALYSIS_DOT_SELECTED_RADIUS = 7.0;
  const ANALYSIS_DOT_HIT_RADIUS = 12;

  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const shapePairLookup = new Map();
  const overlapPairLookup = new Map();
  const analysisRuntimeCache = new Map();
  const shapeMatchLookup = new Map();
  const shapeOutgoingByNode = new Map();
  const shapeIncomingByNode = new Map();
  for (const pair of (Array.isArray(data.overlap_pairs) ? data.overlap_pairs : [])) {
    overlapPairLookup.set(`${pair.source_timestep_index}:${pair.target_timestep_index}`, pair);
  }

  for (const pair of (Array.isArray(data.shape_pairs) ? data.shape_pairs : [])) {
    shapePairLookup.set(`${pair.source_timestep_index}:${pair.target_timestep_index}`, pair);
    for (const match of (pair.matches || [])) {
      const key = `${pair.source_timestep_index}:${match.source_sheet_id}->${pair.target_timestep_index}:${match.target_sheet_id}`;
      const enriched = {
        ...match,
        source_timestep_index: pair.source_timestep_index,
        target_timestep_index: pair.target_timestep_index,
        source_label: pair.source_label,
        target_label: pair.target_label,
        source_stem: pair.source_stem || "",
        target_stem: pair.target_stem || "",
      };
      shapeMatchLookup.set(key, enriched);
      const sourceKey = `${pair.source_timestep_index}:${match.source_sheet_id}`;
      const targetKey = `${pair.target_timestep_index}:${match.target_sheet_id}`;
      if (!shapeOutgoingByNode.has(sourceKey)) shapeOutgoingByNode.set(sourceKey, []);
      if (!shapeIncomingByNode.has(targetKey)) shapeIncomingByNode.set(targetKey, []);
      shapeOutgoingByNode.get(sourceKey).push(enriched);
      shapeIncomingByNode.get(targetKey).push(enriched);
    }
  }

  function clamp(n, low, high) {
    return Math.min(high, Math.max(low, n));
  }

  const imageZoom = {
    overlay: null,
    stage: null,
    grid: null,
    title: null,
    panes: [],
    images: [],
    scale: 1,
    x: 0,
    y: 0,
    drag: null
  };

  function bindImageZoomViewer() {
    if (!detailsContent) return;

    const overlay = document.createElement("div");
    overlay.id = "imageZoomOverlay";
    overlay.innerHTML = `
      <div class="image-zoom-toolbar">
        <div class="image-zoom-title"></div>
        <div class="image-zoom-actions">
          <button type="button" data-action="zoom-out" title="Zoom out">-</button>
          <button type="button" data-action="zoom-in" title="Zoom in">+</button>
          <button type="button" data-action="reset" title="Reset view">Reset</button>
          <button type="button" data-action="close" title="Close">Close</button>
        </div>
      </div>
      <div class="image-zoom-stage"><div class="image-zoom-grid"></div></div>
    `;
    document.body.appendChild(overlay);

    imageZoom.overlay = overlay;
    imageZoom.stage = overlay.querySelector(".image-zoom-stage");
    imageZoom.grid = overlay.querySelector(".image-zoom-grid");
    imageZoom.title = overlay.querySelector(".image-zoom-title");

    detailsContent.addEventListener("click", event => {
      const target = event.target;
      if (!(target instanceof HTMLImageElement) || !target.classList.contains("zoomable-image")) return;
      const linkImages = zoomImagesFromLinkRow(target);
      if (linkImages.images.length) {
        openImageZoom(linkImages.images, linkImages.title);
        return;
      }
      const images = zoomImagesFromTarget(target);
      if (images.length) openImageZoom(images, target.dataset.zoomTitle || "");
    });

    overlay.addEventListener("click", event => {
      const action = event.target?.dataset?.action;
      if (action === "close") closeImageZoom();
      if (action === "reset") fitImageZoom();
      if (action === "zoom-in") zoomImageAt(1.25);
      if (action === "zoom-out") zoomImageAt(0.8);
    });

    imageZoom.stage.addEventListener("wheel", event => {
      event.preventDefault();
      zoomImageAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY);
    }, { passive: false });

    imageZoom.stage.addEventListener("pointerdown", event => {
      imageZoom.drag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: imageZoom.x, y: imageZoom.y };
      imageZoom.stage.classList.add("dragging");
      imageZoom.stage.setPointerCapture(event.pointerId);
    });

    imageZoom.stage.addEventListener("pointermove", event => {
      if (!imageZoom.drag || imageZoom.drag.pointerId !== event.pointerId) return;
      imageZoom.x = imageZoom.drag.x + event.clientX - imageZoom.drag.startX;
      imageZoom.y = imageZoom.drag.y + event.clientY - imageZoom.drag.startY;
      applyImageZoomTransform();
    });

    imageZoom.stage.addEventListener("pointerup", endImageZoomDrag);
    imageZoom.stage.addEventListener("pointercancel", endImageZoomDrag);

    document.addEventListener("keydown", event => {
      if (!imageZoom.overlay?.classList.contains("open")) return;
      if (event.key === "Escape") closeImageZoom();
      if (event.key === "0") fitImageZoom();
      if (event.key === "+" || event.key === "=") zoomImageAt(1.25);
      if (event.key === "-") zoomImageAt(0.8);
    });

    window.addEventListener("resize", () => {
      if (imageZoom.overlay?.classList.contains("open")) fitImageZoom();
    });
  }

  function zoomImagesFromLinkRow(target) {
    const row = target.closest(".link-media-row");
    const stack = target.closest(".media-stack");
    if (!row || !stack) return { images: [], title: "" };

    const clickedStackImages = Array.from(stack.querySelectorAll("img.zoomable-image"));
    const mediaIndex = clickedStackImages.indexOf(target);
    if (mediaIndex < 0) return { images: [], title: "" };

    const title = mediaIndex === 0 ? "Sheet images" : "Fiber surface images";
    const images = Array.from(row.querySelectorAll(".media-stack")).map((item, index) => {
      const img = item.querySelectorAll("img.zoomable-image")[mediaIndex];
      if (!img) return null;
      return {
        src: img.currentSrc || img.src,
        label: img.dataset.zoomLabel || img.alt || (index === 0 ? "Source" : "Target")
      };
    }).filter(Boolean);

    return { images: images.length > 1 ? images : [], title };
  }

  function zoomImagesFromTarget(target) {
    const paired = target.dataset.zoomLeftSrc || target.dataset.zoomRightSrc;
    if (paired) {
      return [
        { src: target.dataset.zoomLeftSrc || "", label: target.dataset.zoomLeftLabel || "Source" },
        { src: target.dataset.zoomRightSrc || "", label: target.dataset.zoomRightLabel || "Target" }
      ].filter(item => item.src);
    }
    const src = target.dataset.zoomSrc || target.currentSrc || target.src;
    const label = target.dataset.zoomLabel || target.alt || imageFilename(src || "");
    return src ? [{ src, label }] : [];
  }

  function openImageZoom(images, title = "") {
    if (!imageZoom.overlay || !imageZoom.grid) return;
    imageZoom.grid.innerHTML = "";
    imageZoom.panes = [];
    imageZoom.images = [];
    imageZoom.title.textContent = title || images.map(item => item.label).join(" | ");

    let pending = images.length;
    const markLoaded = () => {
      pending -= 1;
      if (pending <= 0) fitImageZoom();
    };

    for (const item of images) {
      const pane = document.createElement("div");
      pane.className = "image-zoom-pane";
      const label = document.createElement("div");
      label.className = "image-zoom-pane-title";
      label.textContent = item.label || imageFilename(item.src);
      const img = document.createElement("img");
      img.alt = item.label || "Zoomed image";
      img.onload = markLoaded;
      img.onerror = markLoaded;
      img.src = item.src;
      pane.appendChild(label);
      pane.appendChild(img);
      imageZoom.grid.appendChild(pane);
      imageZoom.panes.push(pane);
      imageZoom.images.push(img);
      if (img.complete) markLoaded();
    }

    imageZoom.overlay.classList.add("open");
    document.body.style.overflow = "hidden";
    if (!images.length) fitImageZoom();
  }

  function closeImageZoom() {
    if (!imageZoom.overlay) return;
    imageZoom.overlay.classList.remove("open");
    document.body.style.overflow = "";
    imageZoom.drag = null;
    imageZoom.stage?.classList.remove("dragging");
  }

  function fitImageZoom() {
    if (!imageZoom.images.length || !imageZoom.panes.length) return;
    const fitScales = imageZoom.images.map((img, index) => {
      const rect = imageZoom.panes[index].getBoundingClientRect();
      const width = img.naturalWidth || img.width || 1;
      const height = img.naturalHeight || img.height || 1;
      return Math.min(rect.width / width, rect.height / height) * 0.94;
    }).filter(value => Number.isFinite(value) && value > 0);

    const firstRect = imageZoom.panes[0].getBoundingClientRect();
    const firstImage = imageZoom.images[0];
    const width = firstImage.naturalWidth || firstImage.width || 1;
    const height = firstImage.naturalHeight || firstImage.height || 1;
    imageZoom.scale = clamp(Math.min(...fitScales, 1) || 1, IMAGE_ZOOM_MIN, IMAGE_ZOOM_MAX);
    imageZoom.x = (firstRect.width - width * imageZoom.scale) * 0.5;
    imageZoom.y = (firstRect.height - height * imageZoom.scale) * 0.5;
    applyImageZoomTransform();
  }

  function zoomImageAt(factor, clientX = null, clientY = null) {
    if (!imageZoom.images.length || !imageZoom.panes.length) return;
    const point = imageZoomPoint(clientX, clientY);
    const oldScale = imageZoom.scale || 1;
    const nextScale = clamp(oldScale * factor, IMAGE_ZOOM_MIN, IMAGE_ZOOM_MAX);
    const imageX = (point.x - imageZoom.x) / oldScale;
    const imageY = (point.y - imageZoom.y) / oldScale;
    imageZoom.scale = nextScale;
    imageZoom.x = point.x - imageX * nextScale;
    imageZoom.y = point.y - imageY * nextScale;
    applyImageZoomTransform();
  }

  function imageZoomPoint(clientX, clientY) {
    const fallbackRect = imageZoom.panes[0].getBoundingClientRect();
    if (clientX === null || clientY === null) {
      return { x: fallbackRect.width * 0.5, y: fallbackRect.height * 0.5 };
    }
    for (const pane of imageZoom.panes) {
      const rect = pane.getBoundingClientRect();
      if (clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom) {
        return { x: clientX - rect.left, y: clientY - rect.top };
      }
    }
    return { x: clientX - fallbackRect.left, y: clientY - fallbackRect.top };
  }

  function applyImageZoomTransform() {
    const transform = `matrix(${imageZoom.scale}, 0, 0, ${imageZoom.scale}, ${imageZoom.x}, ${imageZoom.y})`;
    for (const img of imageZoom.images) {
      img.style.transform = transform;
    }
  }

  function endImageZoomDrag(event) {
    if (!imageZoom.drag || imageZoom.drag.pointerId !== event.pointerId) return;
    imageZoom.drag = null;
    imageZoom.stage?.classList.remove("dragging");
  }

  function clampPanelHeight(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return PANEL_HEIGHT_DEFAULT;
    return Math.round(clamp(numeric, PANEL_HEIGHT_MIN, PANEL_HEIGHT_MAX));
  }

  function clampIntervalGraphHeight(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return INTERVAL_GRAPH_HEIGHT_DEFAULT;
    return Math.round(clamp(numeric, INTERVAL_GRAPH_HEIGHT_MIN, INTERVAL_GRAPH_HEIGHT_MAX));
  }

  function formatScore(value) {
    return numberFormat.format(Number(value || 0));
  }

  function parseHexColor(hexColor) {
    const value = String(hexColor || "").replace("#", "").trim();
    if (!/^[0-9a-fA-F]{6}$/.test(value)) return [111, 158, 212];
    return [0, 2, 4].map(offset => parseInt(value.slice(offset, offset + 2), 16));
  }

  function rgbToHex(rgb) {
    return "#" + rgb
      .map(channel => Math.round(clamp(channel, 0, 255)).toString(16).padStart(2, "0"))
      .join("");
  }

  function lerpRgb(a, b, t) {
    return a.map((channel, index) => channel + (b[index] - channel) * t);
  }

  function centroidRgbFromUnit(tx, ty) {
    const x = clamp(Number(tx) || 0, 0, 1);
    const y = clamp(Number(ty) || 0, 0, 1);
    const bottom = lerpRgb(parseHexColor(centroidCornerColors.bottom_left), parseHexColor(centroidCornerColors.bottom_right), x);
    const top = lerpRgb(parseHexColor(centroidCornerColors.top_left), parseHexColor(centroidCornerColors.top_right), x);
    return lerpRgb(bottom, top, y);
  }

  function centroidAxisDiagonalRgbFromUnit(tx, ty) {
    const nx = clamp((Number(tx) || 0) * 2 - 1, -1, 1);
    const ny = clamp((Number(ty) || 0) * 2 - 1, -1, 1);
    const ax = Math.abs(nx);
    const ay = Math.abs(ny);
    const radius = clamp(Math.hypot(nx, ny), 0, 1);
    if (!(radius > 0)) return parseHexColor(centroidAxisDiagonalColors.origin);

    const angleT = clamp(Math.atan2(ay, ax) / (Math.PI / 2), 0, 1);
    const target = lerpRgb(
      parseHexColor(centroidAxisDiagonalColors.x_axis),
      parseHexColor(centroidAxisDiagonalColors.y_axis),
      angleT
    );
    return lerpRgb(parseHexColor(centroidAxisDiagonalColors.origin), target, radius);
  }

  function centroidAxisDiagonalColorFromCentroid(centroid) {
    const position = centroidPositionFromCentroid(centroid);
    if (!position) return "#6f9ed4";
    return rgbToHex(centroidAxisDiagonalRgbFromUnit(position[0], position[1]));
  }

  function centroidPositionFromCentroid(centroid) {
    if (!Array.isArray(centroid) || centroid.length < 2) return null;
    const [xmin, ymin, xmax, ymax] = centroidColorBounds;
    const x = Number(centroid[0]);
    const y = Number(centroid[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !(xmax > xmin) || !(ymax > ymin)) return null;
    return [
      clamp((x - xmin) / (xmax - xmin), 0, 1),
      clamp((y - ymin) / (ymax - ymin), 0, 1)
    ];
  }

  function centroidColorFromCentroid(centroid) {
    const position = centroidPositionFromCentroid(centroid);
    if (!position) return "#6f9ed4";
    return rgbToHex(centroidRgbFromUnit(position[0], position[1]));
  }

  function formatBound(value, axis) {
    return `${axis} ${formatScore(value)}`;
  }

  function drawCentroidColorLegend() {
    const canvas = document.getElementById("centroidColorCanvas");
    if (!canvas || !canvas.getContext) return;
    const context = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;
    const image = context.createImageData(width, height);
    for (let py = 0; py < height; py += 1) {
      const ty = height > 1 ? 1 - py / (height - 1) : 0;
      for (let px = 0; px < width; px += 1) {
        const tx = width > 1 ? px / (width - 1) : 0;
        const rgb = state.layoutControls.nodeColorMode === "centroid_axis_diagonal"
          ? centroidAxisDiagonalRgbFromUnit(tx, ty)
          : centroidRgbFromUnit(tx, ty);
        const offset = (py * width + px) * 4;
        image.data[offset] = Math.round(rgb[0]);
        image.data[offset + 1] = Math.round(rgb[1]);
        image.data[offset + 2] = Math.round(rgb[2]);
        image.data[offset + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0);
    const title = document.querySelector(".centroid-color-title");
    if (title) {
      title.textContent = state.layoutControls.nodeColorMode === "centroid_axis_diagonal"
        ? "2D red/blue axis color"
        : "2D centroid color";
    }
    const [xmin, ymin, xmax, ymax] = centroidColorBounds;
    const labels = {
      centroidXMin: formatBound(xmin, "x"),
      centroidXMax: formatBound(xmax, "x"),
      centroidYMin: formatBound(ymin, "y"),
      centroidYMax: formatBound(ymax, "y")
    };
    for (const [id, label] of Object.entries(labels)) {
      const node = document.getElementById(id);
      if (node) node.textContent = label;
    }
  }

  function updateCentroidColorLegendVisibility() {
    const legend = document.getElementById("centroidColorLegend");
    if (!legend) return;
    legend.hidden = !["centroid_position", "centroid_axis_diagonal"].includes(state.layoutControls.nodeColorMode);
  }

  function sanitizeShapeWeights(weights) {
    const next = {};
    let anyPositive = false;
    for (const metricId of shapeScoreComponentIds) {
      const fallback = Math.max(0, Number(shapeScoreDefaultWeightsRaw?.[metricId]) || 0);
      const raw = Number(weights?.[metricId]);
      const value = Number.isFinite(raw) && raw >= 0 ? raw : fallback;
      next[metricId] = value;
      anyPositive = anyPositive || value > 0;
    }
    if (!anyPositive) {
      for (const metricId of shapeScoreComponentIds) {
        next[metricId] = Math.max(0, Number(shapeScoreDefaultWeightsRaw?.[metricId]) || 0);
      }
    }
    return next;
  }

  function cloneDefaultShapeWeights() {
    return sanitizeShapeWeights(shapeScoreDefaultWeightsRaw);
  }

  function sanitizeHybridWeights(weights) {
    const fallbackVertex = Math.max(0, Number(hybridScoreDefaultWeightsRaw?.vertex_overlap) || 0);
    const fallbackShape = Math.max(0, Number(hybridScoreDefaultWeightsRaw?.shape_combined) || 0);
    const vertexRaw = Number(weights?.vertex_overlap);
    const shapeRaw = Number(weights?.shape_combined);
    const vertex = Number.isFinite(vertexRaw) && vertexRaw >= 0 ? vertexRaw : fallbackVertex;
    const shape = Number.isFinite(shapeRaw) && shapeRaw >= 0 ? shapeRaw : fallbackShape;
    if (vertex > 0 || shape > 0) {
      return { vertex_overlap: vertex, shape_combined: shape };
    }
    return { vertex_overlap: fallbackVertex, shape_combined: fallbackShape };
  }

  function cloneDefaultHybridWeights() {
    return sanitizeHybridWeights(hybridScoreDefaultWeightsRaw);
  }

  function sanitizeHybridVertexMetric(metricId) {
    const id = String(metricId || "");
    if (overlapMetricIds.includes(id)) return id;
    return hybridVertexMetricDefault;
  }

  function ensurePanelShapeWeights(panel) {
    if (!panel || (panel.dataMode !== "shape" && panel.dataMode !== "hybrid")) return;
    panel.shapeWeights = sanitizeShapeWeights(panel.shapeWeights || cloneDefaultShapeWeights());
  }

  function ensurePanelHybridConfig(panel) {
    if (!panel || panel.dataMode !== "hybrid") return;
    panel.hybridWeights = sanitizeHybridWeights(panel.hybridWeights || cloneDefaultHybridWeights());
    panel.hybridVertexMetric = sanitizeHybridVertexMetric(panel.hybridVertexMetric);
  }

  function combinedShapeScore(metrics, weights) {
    let weightedSum = 0;
    let weightSum = 0;
    for (const metricId of shapeScoreComponentIds) {
      const weight = Math.max(0, Number(weights?.[metricId]) || 0);
      const value = Math.max(0, Number(metrics?.[metricId]) || 0);
      weightedSum += weight * value;
      weightSum += weight;
    }
    return weightSum > 0 ? (weightedSum / weightSum) : 0;
  }

  function metricValue(link, panel, metricId = null) {
    const id = metricId || panel?.metricId || "";
    if (panel?.dataMode === "shape" && id === "combined") {
      return combinedShapeScore(link.metrics || {}, panel.shapeWeights || cloneDefaultShapeWeights());
    }
    if (panel?.dataMode === "hybrid" && id === "hybrid_combined") {
      const weights = sanitizeHybridWeights(panel.hybridWeights || cloneDefaultHybridWeights());
      const overlapMetricId = sanitizeHybridVertexMetric(panel.hybridVertexMetric);
      const overlapRaw = Math.max(0, Number(link.metrics?.[overlapMetricId]) || 0);
      const overlapMax = Math.max(1e-12, Number(metricMaxima?.[overlapMetricId]) || 0);
      const overlapNorm = overlapMax > 0 ? clamp(overlapRaw / overlapMax, 0, 1) : 0;
      const shapeNorm = combinedShapeScore(link.metrics || {}, panel.shapeWeights || cloneDefaultShapeWeights());
      const wVertex = Math.max(0, Number(weights.vertex_overlap) || 0);
      const wShape = Math.max(0, Number(weights.shape_combined) || 0);
      const denom = wVertex + wShape;
      return denom > 0 ? ((wVertex * overlapNorm + wShape * shapeNorm) / denom) : 0;
    }
    return Number(link.metrics?.[id] ?? 0);
  }

  function metricMaxForPanel(panel, metricId = null) {
    const id = metricId || panel?.metricId || "";
    if (panel?.dataMode === "shape" && id === "combined") {
      const pairs = pairsForMode("shape");
      let maxValue = 0;
      for (const pair of pairs) {
        for (const match of pair.matches || []) {
          maxValue = Math.max(maxValue, combinedShapeScore(match.metrics || {}, panel.shapeWeights || cloneDefaultShapeWeights()));
        }
      }
      return maxValue > 0 ? maxValue : 1;
    }
    if (panel?.dataMode === "hybrid" && id === "hybrid_combined") {
      return 1;
    }
    return metricMaxima[id] || 1;
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

  function buildHybridPairsForPanel(panel) {
    const overlapPairs = Array.isArray(data.overlap_pairs) ? data.overlap_pairs : [];
    const hybridPairs = [];
    for (const pair of overlapPairs) {
      const matches = [];
      for (const overlapMatch of (pair.matches || [])) {
        const key = `${pair.source_timestep_index}:${overlapMatch.source_sheet_id}->${pair.target_timestep_index}:${overlapMatch.target_sheet_id}`;
        const shapeMatch = shapeMatchLookup.get(key) || null;
        const shapeMetrics = shapeMatch?.metrics || {};
        const mergedMetrics = {
          ...(overlapMatch.metrics || {}),
          ...(shapeMetrics || {}),
        };
        matches.push({
          ...overlapMatch,
          metrics: mergedMetrics,
        });
      }
      hybridPairs.push({
        ...pair,
        matches,
      });
    }
    return hybridPairs;
  }

  function pairsForMode(modeId, panel = null) {
    if (modeId === "hybrid") {
      if (!panel) return Array.isArray(data.overlap_pairs) ? data.overlap_pairs : [];
      return buildHybridPairsForPanel(panel);
    }
    const field = pairFieldForMode(modeId);
    if (!field) return [];
    return Array.isArray(data[field]) ? data[field] : [];
  }

  function metricLabel(modeId, metricId) {
    return (metricsForMode(modeId).find(metric => metric.id === metricId) || { label: metricId }).label;
  }


  function thresholdKey(value) {
    if (!analysisThresholds.length) return String(value ?? "");
    const numeric = Number(value);
    const selected = Number.isFinite(numeric) ? numeric : analysisThresholds[0];
    let best = analysisThresholds[0];
    let bestDelta = Math.abs(best - selected);
    for (const threshold of analysisThresholds) {
      const delta = Math.abs(threshold - selected);
      if (delta < bestDelta) {
        best = threshold;
        bestDelta = delta;
      }
    }
    return String(best);
  }

  function defaultAnalysisTheta() {
    const preferred = Number(analysisData?.preferred_threshold);
    if (Number.isFinite(preferred)) return preferred;
    return analysisThresholds.length ? analysisThresholds[0] : 0.5;
  }

  function ensurePanelAnalysis(panel) {
    if (!panel.analysis) {
      panel.analysis = {
        tab: "intervals",
        theta: defaultAnalysisTheta(),
        topIntervals: Math.min(5, Math.max(1, Number(analysisData?.top_intervals) || 5)),
        topFeatures: Math.min(5, Math.max(1, Number(analysisData?.top_features) || 5)),
        topDisagreements: Math.min(12, Math.max(1, Number(analysisData?.top_disagreements ?? data.meta?.tracking_analysis_top_disagreements) || 12)),
        intervalGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        intervalGraphZoomScale: 1,
        intervalGraphFocus: null,
        intervalGraphKey: "",
        trackGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        trackGraphZoomScale: 1,
        trackGraphFocus: null,
        trackGraphKey: "",
        agreementGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        agreementGraphZoomScale: 1,
        agreementGraphFocus: null,
        agreementGraphKey: "",
        disagreementGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        disagreementGraphZoomScale: 1,
        disagreementGraphFocus: null,
        disagreementGraphKey: "",
        selectedAgreementMetrics: [],
        selectedAgreementPointKeys: [],
        selectedDisagreementKeys: [],
        selectedIntervalKeys: [],
        selectedTrackKeys: [],
        highlight: null,
      };
    }
    if (!analysisThresholds.includes(Number(panel.analysis.theta)) && analysisThresholds.length) {
      panel.analysis.theta = defaultAnalysisTheta();
    }
    panel.analysis.topIntervals = Math.max(1, Math.floor(Number(panel.analysis.topIntervals) || 1));
    panel.analysis.topFeatures = Math.max(1, Math.floor(Number(panel.analysis.topFeatures) || 1));
    panel.analysis.topDisagreements = Math.max(1, Math.floor(Number(panel.analysis.topDisagreements) || 1));
    panel.analysis.intervalGraphHeight = clampIntervalGraphHeight(panel.analysis.intervalGraphHeight);
    panel.analysis.intervalGraphZoomScale = Number.isFinite(Number(panel.analysis.intervalGraphZoomScale))
      ? Number(panel.analysis.intervalGraphZoomScale)
      : 1;
    panel.analysis.trackGraphHeight = clampIntervalGraphHeight(panel.analysis.trackGraphHeight);
    panel.analysis.trackGraphZoomScale = Number.isFinite(Number(panel.analysis.trackGraphZoomScale))
      ? Number(panel.analysis.trackGraphZoomScale)
      : 1;
    panel.analysis.agreementGraphHeight = clampIntervalGraphHeight(panel.analysis.agreementGraphHeight);
    panel.analysis.agreementGraphZoomScale = Number.isFinite(Number(panel.analysis.agreementGraphZoomScale))
      ? Number(panel.analysis.agreementGraphZoomScale)
      : 1;
    panel.analysis.disagreementGraphHeight = clampIntervalGraphHeight(panel.analysis.disagreementGraphHeight);
    panel.analysis.disagreementGraphZoomScale = Number.isFinite(Number(panel.analysis.disagreementGraphZoomScale))
      ? Number(panel.analysis.disagreementGraphZoomScale)
      : 1;
    if (!Array.isArray(panel.analysis.selectedAgreementMetrics)) panel.analysis.selectedAgreementMetrics = [];
    if (!Array.isArray(panel.analysis.selectedAgreementPointKeys)) panel.analysis.selectedAgreementPointKeys = [];
    if (!Array.isArray(panel.analysis.selectedDisagreementKeys)) panel.analysis.selectedDisagreementKeys = [];
    if (!Array.isArray(panel.analysis.selectedIntervalKeys)) {
      panel.analysis.selectedIntervalKeys = Array.isArray(panel.analysis.selectedIntervals)
        ? panel.analysis.selectedIntervals.map(item => item?.intervalKey).filter(Boolean)
        : [];
    }
    if (!Array.isArray(panel.analysis.selectedTrackKeys)) panel.analysis.selectedTrackKeys = [];
  }

  function analysisEventScoreTerms() {
    const terms = Array.isArray(analysisData?.event_score_terms) ? analysisData.event_score_terms : [];
    return terms
      .map(term => ({
        component: String(term?.component || ""),
        weight: Number(term?.weight),
      }))
      .filter(term => term.component && Number.isFinite(term.weight));
  }

  function analysisEventScore(components) {
    return analysisEventScoreTerms().reduce((score, term) => {
      const value = Number(components?.[term.component]) || 0;
      return score + term.weight * value;
    }, 0);
  }

  function analysisMetricKey(panel) {
    const mode = panel?.dataMode || "shape";
    const metric = panel?.metricId || "combined";
    if (mode === "shape" && metric === "combined") return `${mode}:${metric}:stored`;
    const shapeWeightKey = shapeScoreComponentIds
      .map(metricId => `${metricId}:${formatWeight((panel?.shapeWeights || cloneDefaultShapeWeights())[metricId])}`)
      .join(",");
    if (mode === "hybrid") {
      const hybridWeights = sanitizeHybridWeights(panel?.hybridWeights || cloneDefaultHybridWeights());
      const hybridWeightKey = `vertex:${formatWeight(hybridWeights.vertex_overlap)},shape:${formatWeight(hybridWeights.shape_combined)}`;
      return `${mode}:${metric}:overlap=${sanitizeHybridVertexMetric(panel?.hybridVertexMetric)}:${shapeWeightKey}:${hybridWeightKey}`;
    }
    if (mode === "shape") return `${mode}:${metric}`;
    return `${mode}:${metric}`;
  }

  function analysisCacheKey(kind, panel, thetaOverride = null) {
    ensurePanelAnalysis(panel);
    const theta = thetaOverride === null || thetaOverride === undefined ? panel.analysis.theta : thetaOverride;
    return `${kind}:${analysisMetricKey(panel)}:${thresholdKey(theta)}`;
  }

  function analysisMetricScale(panel) {
    const mode = panel?.dataMode || "shape";
    const metricId = panel?.metricId || "";
    if (mode === "overlap") {
      if (String(metricId).includes("percent")) return 100;
      const maxValue = Number(metricMaxForPanel(panel, metricId));
      return maxValue > 0 ? maxValue : 1;
    }
    return 1;
  }

  function panelAnalysisScore(match, panel) {
    if (panel?.dataMode === "shape" && panel?.metricId === "combined") {
      return analysisCombinedScore(match);
    }
    const raw = Number(metricValue(match, panel, panel?.metricId));
    if (!Number.isFinite(raw)) return 0;
    const scale = analysisMetricScale(panel);
    return scale > 0 ? raw / scale : raw;
  }

  function bestPanelAnalysisMatch(matches, panel) {
    let best = null;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (const match of matches || []) {
      const score = panelAnalysisScore(match, panel);
      if (score > bestScore) {
        best = match;
        bestScore = score;
      }
    }
    return best;
  }

  function selectedAnalysisPairs(panel) {
    return pairsForMode(panel?.dataMode || "shape", panel) || [];
  }

  function selectedAnalysisPair(panel, sourceIndex, targetIndex) {
    return selectedAnalysisPairs(panel).find(pair =>
      Number(pair.source_timestep_index) === Number(sourceIndex) &&
      Number(pair.target_timestep_index) === Number(targetIndex)
    ) || null;
  }

  function meanNumber(values) {
    const cleaned = (values || []).map(Number).filter(Number.isFinite);
    return cleaned.length ? cleaned.reduce((sum, value) => sum + value, 0) / cleaned.length : 0;
  }

  function sheetKey(timestepIndex, sheetId) {
    return `${Number(timestepIndex)}:${Number(sheetId)}`;
  }

  function linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId) {
    return `${Number(sourceIndex)}:${Number(sourceSheetId)}->${Number(targetIndex)}:${Number(targetSheetId)}`;
  }

  function groupMatchesByField(matches, field) {
    const groups = new Map();
    for (const match of matches || []) {
      const key = Number(match?.[field]);
      if (!Number.isFinite(key)) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(match);
    }
    return groups;
  }

  function analysisCombinedScore(match) {
    const metrics = match?.metrics || match || {};
    const value = Number(metrics.combined ?? metrics.final_score);
    return Number.isFinite(value) ? value : 0;
  }

  function bestAnalysisMatch(matches) {
    let best = null;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (const match of matches || []) {
      const score = analysisCombinedScore(match);
      if (score > bestScore) {
        best = match;
        bestScore = score;
      }
    }
    return best;
  }

  function overlapMaxPercent(match) {
    const metrics = match?.metrics || {};
    const sourcePercent = Number(metrics.overlap_source_percent ?? match?.source_percent ?? 0) || 0;
    const targetPercent = Number(metrics.overlap_target_percent ?? match?.target_percent ?? 0) || 0;
    const value = Number(metrics.overlap_max_percent ?? Math.max(sourcePercent, targetPercent));
    return Number.isFinite(value) ? value : 0;
  }

  function bestOverlapMatch(matches) {
    let best = null;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (const match of matches || []) {
      const score = overlapMaxPercent(match);
      if (score > bestScore) {
        best = match;
        bestScore = score;
      }
    }
    return best;
  }

  function pairDomainShapeAgreementFraction(sourceIndex, targetIndex) {
    const shapePair = shapePairLookup.get(`${sourceIndex}:${targetIndex}`);
    const overlapPair = overlapPairLookup.get(`${sourceIndex}:${targetIndex}`);
    if (!shapePair || !overlapPair) return 0;

    const shapeBySource = groupMatchesByField(shapePair.matches || [], "source_sheet_id");
    const overlapBySource = groupMatchesByField(overlapPair.matches || [], "source_sheet_id");
    let compared = 0;
    let agreements = 0;
    for (const [sourceSheetId, overlapMatches] of overlapBySource.entries()) {
      const shapeBest = bestAnalysisMatch(shapeBySource.get(sourceSheetId) || []);
      const overlapBest = bestOverlapMatch(overlapMatches || []);
      if (!shapeBest || !overlapBest) continue;
      compared += 1;
      if (Number(shapeBest.target_sheet_id) === Number(overlapBest.target_sheet_id)) agreements += 1;
    }
    return compared ? agreements / compared : 0;
  }

  function domainRangeDisagreementData() {
    const cacheKey = "domain-range-disagreements";
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const examples = [];
    const summary = [];
    for (const shapePair of (Array.isArray(data.shape_pairs) ? data.shape_pairs : [])) {
      const sourceIndex = Number(shapePair.source_timestep_index);
      const targetIndex = Number(shapePair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;
      const pairKey = `${sourceIndex}:${targetIndex}`;
      const overlapPair = overlapPairLookup.get(pairKey);
      if (!overlapPair) continue;

      const shapeBySource = groupMatchesByField(shapePair.matches || [], "source_sheet_id");
      const overlapBySource = groupMatchesByField(overlapPair.matches || [], "source_sheet_id");
      let compared = 0;
      let agreements = 0;
      const pairExamples = [];

      for (const [sourceSheetId, overlapMatches] of overlapBySource.entries()) {
        const shapeMatches = shapeBySource.get(sourceSheetId) || [];
        const shapeBest = bestAnalysisMatch(shapeMatches);
        const overlapBest = bestOverlapMatch(overlapMatches || []);
        if (!shapeBest || !overlapBest) continue;

        compared += 1;
        const shapeTarget = Number(shapeBest.target_sheet_id);
        const overlapTarget = Number(overlapBest.target_sheet_id);
        const shapeScore = analysisCombinedScore(shapeBest);
        const overlapScore = overlapMaxPercent(overlapBest);
        if (shapeTarget === overlapTarget) {
          agreements += 1;
          continue;
        }

        const shapeForDomainTarget = shapeMatches.find(match => Number(match.target_sheet_id) === overlapTarget) || null;
        const overlapForRangeTarget = (overlapMatches || []).find(match => Number(match.target_sheet_id) === shapeTarget) || null;
        const shapeScoreForDomainTarget = shapeForDomainTarget ? analysisCombinedScore(shapeForDomainTarget) : 0;
        const overlapScoreForRangeTarget = overlapForRangeTarget ? overlapMaxPercent(overlapForRangeTarget) : 0;
        const shapeLoss = Math.max(0, shapeScore - shapeScoreForDomainTarget);
        const overlapLoss = Math.max(0, overlapScore - overlapScoreForRangeTarget);
        const confidence = Math.min(shapeScore, overlapScore);
        const disagreementScore = 0.5 * (shapeLoss + overlapLoss) * confidence;
        const example = {
          id: `disagreement:${sourceIndex}:${targetIndex}:${sourceSheetId}`,
          source_timestep_index: sourceIndex,
          target_timestep_index: targetIndex,
          source_label: shapePair.source_label,
          target_label: shapePair.target_label,
          source_sheet_id: Number(sourceSheetId),
          shape_target_sheet_id: shapeTarget,
          overlap_target_sheet_id: overlapTarget,
          shape_score: shapeScore,
          overlap_max_percent: overlapScore,
          shape_score_for_domain_target: shapeScoreForDomainTarget,
          overlap_score_for_range_target: overlapScoreForRangeTarget,
          shape_loss: shapeLoss,
          overlap_loss: overlapLoss,
          confidence,
          disagreement_score: disagreementScore,
          highlight: {
            nodes: [
              sheetKey(sourceIndex, sourceSheetId),
              sheetKey(targetIndex, shapeTarget),
              sheetKey(targetIndex, overlapTarget),
            ],
            links: [
              linkKeyParts(sourceIndex, sourceSheetId, targetIndex, shapeTarget),
              linkKeyParts(sourceIndex, sourceSheetId, targetIndex, overlapTarget),
            ],
          },
        };
        pairExamples.push(example);
        examples.push(example);
      }

      pairExamples.sort((a, b) => Number(b.disagreement_score) - Number(a.disagreement_score));
      const scores = pairExamples.map(item => Number(item.disagreement_score) || 0);
      const shapeLosses = pairExamples.map(item => Number(item.shape_loss) || 0);
      const overlapLosses = pairExamples.map(item => Number(item.overlap_loss) || 0);
      if (!pairExamples.length) continue;
      summary.push({
        id: `disagreement_pair:${sourceIndex}:${targetIndex}`,
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: shapePair.source_label,
        target_label: shapePair.target_label,
        pair_label: `${shapePair.source_label || sourceIndex}->${shapePair.target_label || targetIndex}`,
        compared_sources: compared,
        agreement_count: agreements,
        disagreement_count: pairExamples.length,
        agreement_fraction: compared ? agreements / compared : 0,
        disagreement_fraction: compared ? pairExamples.length / compared : 0,
        max_disagreement_score: scores.length ? Math.max(...scores) : 0,
        mean_disagreement_score: meanNumber(scores),
        max_shape_loss: shapeLosses.length ? Math.max(...shapeLosses) : 0,
        mean_shape_loss: meanNumber(shapeLosses),
        max_overlap_loss: overlapLosses.length ? Math.max(...overlapLosses) : 0,
        mean_overlap_loss: meanNumber(overlapLosses),
        strongest_disagreement: pairExamples[0] || null,
      });
    }

    if (!summary.length && Array.isArray(analysisData?.domain_shape_disagreement_summary)) {
      const embeddedSummary = analysisData.domain_shape_disagreement_summary.slice();
      const embeddedExamples = Array.isArray(analysisData?.domain_shape_disagreements) ? analysisData.domain_shape_disagreements.slice() : [];
      const result = {
        examples: embeddedExamples,
        summary: embeddedSummary.slice().sort((a, b) => Number(a.source_timestep_index) - Number(b.source_timestep_index)),
        rankedSummary: embeddedSummary.slice().sort((a, b) => Number(b.max_disagreement_score || 0) - Number(a.max_disagreement_score || 0)),
      };
      analysisRuntimeCache.set(cacheKey, result);
      return result;
    }

    const result = {
      examples: examples.slice().sort((a, b) => Number(b.disagreement_score || 0) - Number(a.disagreement_score || 0)),
      summary: summary.slice().sort((a, b) => Number(a.source_timestep_index) - Number(b.source_timestep_index) || Number(a.target_timestep_index) - Number(b.target_timestep_index)),
      rankedSummary: summary.slice().sort((a, b) =>
        Number(b.max_disagreement_score || 0) - Number(a.max_disagreement_score || 0) ||
        Number(b.disagreement_fraction || 0) - Number(a.disagreement_fraction || 0) ||
        Number(b.disagreement_count || 0) - Number(a.disagreement_count || 0)
      ),
    };
    analysisRuntimeCache.set(cacheKey, result);
    return result;
  }

  function computeRuntimeIntervals(panel, thetaOverride = null) {
    ensurePanelAnalysis(panel);
    const theta = Number(thetaOverride === null || thetaOverride === undefined ? panel.analysis.theta : thetaOverride);
    const cacheKey = analysisCacheKey("intervals", panel, theta);
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const rows = [];
    for (const pair of selectedAnalysisPairs(panel)) {
      const sourceIndex = Number(pair.source_timestep_index);
      const targetIndex = Number(pair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;

      const sourceSheets = timestepByIndex.get(sourceIndex)?.sheets || [];
      const targetSheets = timestepByIndex.get(targetIndex)?.sheets || [];
      const bySource = groupMatchesByField(pair.matches || [], "source_sheet_id");
      const byTarget = groupMatchesByField(pair.matches || [], "target_sheet_id");
      const bestSourceScores = [];
      const bestTargetScores = [];

      for (const sheet of sourceSheets) {
        const best = bestPanelAnalysisMatch(bySource.get(Number(sheet.sheet_id)) || [], panel);
        bestSourceScores.push(best ? panelAnalysisScore(best, panel) : 0);
      }

      for (const sheet of targetSheets) {
        const best = bestPanelAnalysisMatch(byTarget.get(Number(sheet.sheet_id)) || [], panel);
        bestTargetScores.push(best ? panelAnalysisScore(best, panel) : 0);
      }

      const sourceWeakCount = bestSourceScores.filter(value => value < theta).length;
      const targetWeakCount = bestTargetScores.filter(value => value < theta).length;
      let possibleSplits = 0;
      for (const matches of bySource.values()) {
        if ((matches || []).filter(match => panelAnalysisScore(match, panel) >= theta).length >= 2) possibleSplits += 1;
      }
      let possibleMerges = 0;
      for (const matches of byTarget.values()) {
        if ((matches || []).filter(match => panelAnalysisScore(match, panel) >= theta).length >= 2) possibleMerges += 1;
      }

      const meanBestScore = meanNumber(bestSourceScores);
      const minBestScore = bestSourceScores.length ? Math.min(...bestSourceScores) : 0;
      const sourceSheetCount = sourceSheets.length;
      const eventScoreComponents = {
        source_weak_count: sourceWeakCount,
        target_weak_count: targetWeakCount,
        possible_splits: possibleSplits,
        possible_merges: possibleMerges,
        continuation_gap_source_count: (1 - meanBestScore) * Math.max(sourceSheetCount, 1),
      };
      const eventScore = analysisEventScore(eventScoreComponents);

      rows.push({
        id: `interval_runtime:${analysisMetricKey(panel)}:${thresholdKey(theta)}:${sourceIndex}:${targetIndex}`,
        threshold: theta,
        analysis_mode: panel.dataMode,
        analysis_metric_id: panel.metricId,
        analysis_metric_label: metricLabel(panel.dataMode, panel.metricId),
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: pair.source_label,
        target_label: pair.target_label,
        source_stem: pair.source_stem || "",
        target_stem: pair.target_stem || "",
        pair_label: `${pair.source_label || sourceIndex}->${pair.target_label || targetIndex}`,
        source_sheet_count: sourceSheetCount,
        target_sheet_count: targetSheets.length,
        candidate_match_count: Number(pair.pair_count ?? (pair.matches || []).length) || 0,
        mean_best_score: meanBestScore,
        min_best_score: minBestScore,
        mean_best_combined: meanBestScore,
        min_best_combined: minBestScore,
        source_weak_count: sourceWeakCount,
        target_weak_count: targetWeakCount,
        possible_splits: possibleSplits,
        possible_merges: possibleMerges,
        event_score: eventScore,
        domain_shape_agreement_fraction: pairDomainShapeAgreementFraction(sourceIndex, targetIndex),
      });
    }

    rows.sort((a, b) => Number(a.source_timestep_index) - Number(b.source_timestep_index) || Number(a.target_timestep_index) - Number(b.target_timestep_index));
    const result = {
      series: rows,
      ranked: rows.slice().sort((a, b) => Number(b.event_score) - Number(a.event_score) || Number(a.source_timestep_index) - Number(b.source_timestep_index)),
    };
    analysisRuntimeCache.set(cacheKey, result);
    return result;
  }

  function analysisIntervals(panel) {
    return computeRuntimeIntervals(panel).ranked;
  }

  function computeRuntimeTracks(panel, thetaOverride = null) {
    ensurePanelAnalysis(panel);
    const theta = Number(thetaOverride === null || thetaOverride === undefined ? panel.analysis.theta : thetaOverride);
    const cacheKey = analysisCacheKey("tracks", panel, theta);
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const nodeMeta = new Map();
    for (const timestep of (data.timesteps || [])) {
      const timestepIndex = Number(timestep.timestep_index);
      for (const sheet of (timestep.sheets || [])) {
        nodeMeta.set(sheetKey(timestepIndex, sheet.sheet_id), { timestep, sheet });
      }
    }

    const edges = new Map();
    for (const pair of selectedAnalysisPairs(panel)) {
      const sourceIndex = Number(pair.source_timestep_index);
      const targetIndex = Number(pair.target_timestep_index);
      const bySource = groupMatchesByField(pair.matches || [], "source_sheet_id");
      for (const [sourceSheetId, matches] of bySource.entries()) {
        const best = bestPanelAnalysisMatch(matches || [], panel);
        if (!best) continue;
        const score = panelAnalysisScore(best, panel);
        if (score < theta) continue;
        const targetSheetId = Number(best.target_sheet_id);
        edges.set(sheetKey(sourceIndex, sourceSheetId), {
          targetKey: sheetKey(targetIndex, targetSheetId),
          sourceIndex,
          sourceSheetId,
          targetIndex,
          targetSheetId,
          score,
        });
      }
    }

    const incoming = new Map();
    for (const [sourceKey, edge] of edges.entries()) {
      if (!incoming.has(edge.targetKey)) incoming.set(edge.targetKey, []);
      incoming.get(edge.targetKey).push(sourceKey);
    }

    const starts = [...nodeMeta.keys()].filter(key => !(incoming.get(key) || []).length).sort((a, b) => {
      const [ta, sa] = a.split(":").map(Number);
      const [tb, sb] = b.split(":").map(Number);
      return ta - tb || sa - sb;
    });

    const rows = [];
    const usedStarts = new Set();
    let trackId = 0;
    for (const start of starts) {
      if (usedStarts.has(start)) continue;
      trackId += 1;
      let current = start;
      const seen = new Set();
      const nodes = [];
      const scores = [];
      const links = [];

      while (nodeMeta.has(current) && !seen.has(current)) {
        seen.add(current);
        nodes.push(current);
        const edge = edges.get(current);
        if (!edge) break;
        links.push(linkKeyParts(edge.sourceIndex, edge.sourceSheetId, edge.targetIndex, edge.targetSheetId));
        scores.push(edge.score);
        current = edge.targetKey;
      }
      usedStarts.add(start);

      const sheets = nodes.map(key => nodeMeta.get(key)?.sheet).filter(Boolean);
      const ranks = sheets.map(sheet => Number(sheet.rank) || 0);
      const areas = sheets.map(sheet => Number(sheet.area) || 0);
      const firstNode = nodes[0]?.split(":").map(Number) || [0, 0];
      const lastNode = nodes[nodes.length - 1]?.split(":").map(Number) || firstNode;
      const firstTs = timestepByIndex.get(firstNode[0]);
      const lastTs = timestepByIndex.get(lastNode[0]);

      rows.push({
        id: `track_runtime:${analysisMetricKey(panel)}:${thresholdKey(theta)}:${trackId}`,
        threshold: theta,
        analysis_mode: panel.dataMode,
        analysis_metric_id: panel.metricId,
        analysis_metric_label: metricLabel(panel.dataMode, panel.metricId),
        track_id: trackId,
        length: nodes.length,
        start_timestep_index: firstNode[0],
        end_timestep_index: lastNode[0],
        start_label: firstTs?.label || String(firstNode[0]),
        end_label: lastTs?.label || String(lastNode[0]),
        start_sheet_id: firstNode[1],
        end_sheet_id: lastNode[1],
        rank_min: ranks.length ? Math.min(...ranks) : 0,
        rank_max: ranks.length ? Math.max(...ranks) : 0,
        area_mean: meanNumber(areas),
        mean_continuation_score: meanNumber(scores),
        min_continuation_score: scores.length ? Math.min(...scores) : 0,
        highlight: { nodes, links },
      });
    }

    rows.sort((a, b) =>
      Number(b.length) - Number(a.length) ||
      Number(b.mean_continuation_score) - Number(a.mean_continuation_score) ||
      Number(b.min_continuation_score) - Number(a.min_continuation_score)
    );
    analysisRuntimeCache.set(cacheKey, rows);
    return rows;
  }

  function analysisTracks(panel) {
    return computeRuntimeTracks(panel);
  }

  function quantileNumber(values, fraction) {
    const cleaned = (values || []).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    if (!cleaned.length) return 0;
    const index = Math.max(0, Math.min(cleaned.length - 1, Math.round(fraction * (cleaned.length - 1))));
    return cleaned[index];
  }

  function computeRuntimeSensitivity(panel) {
    ensurePanelAnalysis(panel);
    const cacheKey = `sensitivity:${analysisMetricKey(panel)}`;
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);
    const rows = analysisThresholds.map(threshold => {
      const intervalResult = computeRuntimeIntervals(panel, threshold);
      const intervalRows = intervalResult.series || [];
      const rankedIntervals = intervalResult.ranked || [];
      const tracks = computeRuntimeTracks(panel, threshold);
      const topEvent = rankedIntervals[0] || {};
      const lengths = tracks.map(row => Number(row.length) || 0);
      return {
        threshold,
        event_count: intervalRows.length,
        mean_event_score: meanNumber(intervalRows.map(row => Number(row.event_score) || 0)),
        max_event_score: Number(topEvent.event_score) || 0,
        top_event_pair_label: topEvent.pair_label || "",
        top_event_source_timestep_index: Number(topEvent.source_timestep_index),
        top_event_target_timestep_index: Number(topEvent.target_timestep_index),
        track_count: tracks.length,
        max_lifetime: lengths.length ? Math.max(...lengths) : 0,
        median_lifetime: quantileNumber(lengths, 0.5),
        mean_lifetime: meanNumber(lengths),
      };
    });
    analysisRuntimeCache.set(cacheKey, rows);
    return rows;
  }

  function intervalKeyFromItem(item) {
    return `${Number(item?.source_timestep_index)}:${Number(item?.target_timestep_index)}`;
  }

  function intervalHighlightFromData(item, panel) {
    const sourceIndex = Number(item?.source_timestep_index);
    const targetIndex = Number(item?.target_timestep_index);
    const threshold = Number(item?.threshold ?? panel?.analysis?.theta ?? defaultAnalysisTheta());
    const pair = selectedAnalysisPair(panel, sourceIndex, targetIndex);
    const highlightNodes = new Set();
    const highlightLinks = new Set();
    const weakSourceNodes = [];
    const weakTargetNodes = [];
    const splitSourceNodes = [];
    const mergeTargetNodes = [];

    if (!pair) {
      return {
        nodes: [],
        links: [],
        weak_source_nodes: [],
        weak_target_nodes: [],
        split_source_nodes: [],
        merge_target_nodes: [],
      };
    }

    const bySource = groupMatchesByField(pair.matches || [], "source_sheet_id");
    const byTarget = groupMatchesByField(pair.matches || [], "target_sheet_id");
    const sourceSheets = timestepByIndex.get(sourceIndex)?.sheets || [];
    const targetSheets = timestepByIndex.get(targetIndex)?.sheets || [];

    for (const sheet of sourceSheets) {
      const sourceSheetId = Number(sheet.sheet_id);
      const best = bestPanelAnalysisMatch(bySource.get(sourceSheetId) || [], panel);
      const bestScore = best ? panelAnalysisScore(best, panel) : 0;
      if (bestScore < threshold) {
        const sourceKey = sheetKey(sourceIndex, sourceSheetId);
        weakSourceNodes.push(sourceKey);
        highlightNodes.add(sourceKey);
        if (best) {
          const targetSheetId = Number(best.target_sheet_id);
          highlightNodes.add(sheetKey(targetIndex, targetSheetId));
          highlightLinks.add(linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId));
        }
      }
    }

    for (const sheet of targetSheets) {
      const targetSheetId = Number(sheet.sheet_id);
      const best = bestPanelAnalysisMatch(byTarget.get(targetSheetId) || [], panel);
      const bestScore = best ? panelAnalysisScore(best, panel) : 0;
      if (bestScore < threshold) {
        const targetKey = sheetKey(targetIndex, targetSheetId);
        weakTargetNodes.push(targetKey);
        highlightNodes.add(targetKey);
        if (best) {
          const sourceSheetId = Number(best.source_sheet_id);
          highlightNodes.add(sheetKey(sourceIndex, sourceSheetId));
          highlightLinks.add(linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId));
        }
      }
    }

    for (const [sourceSheetId, matches] of bySource.entries()) {
      const above = (matches || []).filter(match => panelAnalysisScore(match, panel) >= threshold);
      if (above.length >= 2) {
        const sourceKey = sheetKey(sourceIndex, sourceSheetId);
        splitSourceNodes.push(sourceKey);
        highlightNodes.add(sourceKey);
        for (const match of above) {
          const targetSheetId = Number(match.target_sheet_id);
          highlightNodes.add(sheetKey(targetIndex, targetSheetId));
          highlightLinks.add(linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId));
        }
      }
    }

    for (const [targetSheetId, matches] of byTarget.entries()) {
      const above = (matches || []).filter(match => panelAnalysisScore(match, panel) >= threshold);
      if (above.length >= 2) {
        const targetKey = sheetKey(targetIndex, targetSheetId);
        mergeTargetNodes.push(targetKey);
        highlightNodes.add(targetKey);
        for (const match of above) {
          const sourceSheetId = Number(match.source_sheet_id);
          highlightNodes.add(sheetKey(sourceIndex, sourceSheetId));
          highlightLinks.add(linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId));
        }
      }
    }

    return {
      nodes: [...highlightNodes].sort(),
      links: [...highlightLinks].sort(),
      weak_source_nodes: weakSourceNodes,
      weak_target_nodes: weakTargetNodes,
      split_source_nodes: splitSourceNodes,
      merge_target_nodes: mergeTargetNodes,
    };
  }

  function intervalHighlightPayload(item, panel) {
    const sourceIndex = Number(item?.source_timestep_index);
    const targetIndex = Number(item?.target_timestep_index);
    const highlight = item?.highlight || intervalHighlightFromData(item, panel);
    return {
      id: item?.id || `interval:${intervalKeyFromItem(item)}`,
      intervalKey: intervalKeyFromItem(item),
      label: `Interval ${item?.source_label || sourceIndex} -> ${item?.target_label || targetIndex}`,
      nodes: highlight.nodes || [],
      links: highlight.links || [],
      start: sourceIndex,
      end: targetIndex,
    };
  }

  function intervalTimestepLabel(item, role = "source") {
    const index = Number(role === "target" ? item?.target_timestep_index : item?.source_timestep_index);
    const direct = role === "target" ? item?.target_label : item?.source_label;
    const fallback = Number.isFinite(index) ? String(index) : "";
    return String(direct ?? timestepLookup.labelAt(index, fallback) ?? fallback);
  }

  function intervalTimestepPrimary(item, role = "source") {
    const index = Number(role === "target" ? item?.target_timestep_index : item?.source_timestep_index);
    return window.ReebViewerCommon.formatTimestepPrimary(index, intervalTimestepLabel(item, role));
  }

  function intervalTimeFs(item) {
    const fsRaw = window.ReebViewerCommon.formatFsFromLabel(intervalTimestepLabel(item, "source"), TIMESTEP_LABEL_OPTIONS);
    const fs = Number(fsRaw);
    if (Number.isFinite(fs)) return fs;
    return Number(item?.source_timestep_index) || 0;
  }

  function formatIntervalFs(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${numeric.toFixed(TIMESTEP_LABEL_OPTIONS.digits)} fs` : "";
  }

  function intervalGraphTooltip(item, panel) {
    const theta = panelTheta(panel);
    return `
      <strong>${escapeHtml(intervalTimestepPrimary(item, "source"))} -> ${escapeHtml(intervalTimestepPrimary(item, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Time</div><div>${escapeHtml(formatIntervalFs(intervalTimeFs(item)))}</div>
        <div>Event score</div><div>${escapeHtml(formatScore(item.event_score))}</div>
        <div>Analysis metric</div><div>${escapeHtml(item.analysis_metric_label || metricLabel(panel.dataMode, panel.metricId))}</div>
        <div>Theta</div><div>${escapeHtml(formatScore(theta))}</div>
        <div>Weak continuation source/target</div><div>${escapeHtml(item.source_weak_count)}/${escapeHtml(item.target_weak_count)}</div>
        <div>Splits / merges</div><div>${escapeHtml(item.possible_splits)} / ${escapeHtml(item.possible_merges)}</div>
        <div>Mean continuation</div><div>${escapeHtml(formatScore(item.mean_best_score ?? item.mean_best_combined))}</div>
        <div>Min continuation</div><div>${escapeHtml(formatScore(item.min_best_score ?? item.min_best_combined))}</div>
        <div>Domain/range agreement</div><div>${escapeHtml(formatScore(100 * Number(item.domain_shape_agreement_fraction || 0)))}%</div>
      </div>`;
  }

  function trackKeyFromItem(item) {
    return String(item?.id || `track:${Number(item?.track_id)}:${Number(item?.start_timestep_index)}:${Number(item?.start_sheet_id)}`);
  }

  function trackHighlightPayload(item, panel) {
    return {
      id: item?.id || `track:${trackKeyFromItem(item)}`,
      trackKey: trackKeyFromItem(item),
      label: `Feature ${item?.track_id ?? ""}`,
      nodes: item?.highlight?.nodes || [],
      links: item?.highlight?.links || [],
      start: Number(item?.start_timestep_index),
      end: Number(item?.end_timestep_index),
    };
  }

  function trackGraphTooltip(item, panel) {
    const theta = panelTheta(panel);
    return `
      <strong>Feature ${escapeHtml(item.track_id)}: S${escapeHtml(item.start_sheet_id)} ${escapeHtml(item.start_label)} -> ${escapeHtml(item.end_label)}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Length</div><div>${escapeHtml(item.length)}</div>
        <div>Analysis metric</div><div>${escapeHtml(item.analysis_metric_label || metricLabel(panel.dataMode, panel.metricId))}</div>
        <div>Mean score</div><div>${escapeHtml(formatScore(item.mean_continuation_score))}</div>
        <div>Min score</div><div>${escapeHtml(formatScore(item.min_continuation_score))}</div>
        <div>Theta</div><div>${escapeHtml(formatScore(theta))}</div>
        <div>Rank min/max</div><div>${escapeHtml(item.rank_min)} / ${escapeHtml(item.rank_max)}</div>
        <div>Mean area</div><div>${escapeHtml(formatScore(item.area_mean))}</div>
      </div>`;
  }

  function renderAnalysisPointGraph(container, panel, rows, options) {
    ensurePanelAnalysis(panel);
    const keyFn = options.keyFn;
    const xValue = options.xValue;
    const yValue = options.yValue;
    const valid = (rows || [])
      .filter(item => Number.isFinite(Number(xValue(item))) && Number.isFinite(Number(yValue(item))))
      .slice()
      .sort(options.sort || ((a, b) => Number(xValue(a)) - Number(xValue(b)) || Number(yValue(a)) - Number(yValue(b))));
    if (!valid.length) return;

    const heightKey = options.heightKey;
    const zoomKey = options.zoomKey;
    const focusKey = options.focusKey;
    const graphKeyField = options.graphKey;
    const selectedKeysKey = options.selectedKeysKey;
    const width = 760;
    const height = clampIntervalGraphHeight(panel.analysis[heightKey]);
    panel.analysis[heightKey] = height;
    const margin = { top: 12, right: 18, bottom: 46, left: 46 };
    const innerWidth = width - margin.left - margin.right;
    const innerHeight = Math.max(1, height - margin.top - margin.bottom);
    let xDomain = options.xDomain ? options.xDomain(valid) : d3.extent(valid, xValue);
    if (!Array.isArray(xDomain) || xDomain.length !== 2 || !xDomain.every(Number.isFinite)) xDomain = [0, 1];
    if (xDomain[0] === xDomain[1]) {
      const pad = Number(options.xSingleValuePad ?? 1) || 1;
      xDomain = [xDomain[0] - pad, xDomain[1] + pad];
    }
    let yDomain = options.yDomain ? options.yDomain(valid) : [0, (d3.max(valid, yValue) || 1) * 1.05 || 1];
    if (!Array.isArray(yDomain) || yDomain.length !== 2 || !yDomain.every(Number.isFinite)) yDomain = [0, 1];
    if (yDomain[0] === yDomain[1]) {
      const pad = Number(options.ySingleValuePad ?? 1) || 1;
      yDomain = [yDomain[0] - pad, yDomain[1] + pad];
    }
    const xBase = d3.scaleLinear().domain(xDomain).range([0, innerWidth]);
    const yBase = d3.scaleLinear().domain(yDomain).range([innerHeight, 0]).nice();
    const validKeys = new Set(valid.map(keyFn));
    const selectedBeforePrune = panel.analysis[selectedKeysKey] || [];
    panel.analysis[selectedKeysKey] = selectedBeforePrune.filter(key => validKeys.has(key));
    if (panel.analysis[selectedKeysKey].length !== selectedBeforePrune.length) {
      panel.analysis.highlight = options.aggregateHighlight(panel);
    }
    const clipId = `analysis-graph-clip-${String(panel.id).replace(/[^a-zA-Z0-9_-]/g, "")}-${options.id}`;
    const graphStateKey = options.stateKey ? options.stateKey(panel) : thresholdKey(panel.analysis.theta);
    const graphKey = `${options.id}:${graphStateKey}:${valid.map(keyFn).join("|")}`;
    if (panel.analysis[graphKeyField] !== graphKey) {
      panel.analysis[graphKeyField] = graphKey;
      panel.analysis[zoomKey] = 1;
      panel.analysis[focusKey] = null;
    }

    const centerFocus = { x: innerWidth / 2, y: innerHeight / 2 };
    const storedFocus = panel.analysis[focusKey];
    const initialFocus = storedFocus &&
      Number.isFinite(Number(storedFocus.x)) &&
      Number.isFinite(Number(storedFocus.y))
        ? { x: Number(storedFocus.x), y: Number(storedFocus.y) }
        : centerFocus;
    const initialZoom = Number.isFinite(Number(panel.analysis[zoomKey]))
      ? Number(panel.analysis[zoomKey])
      : 1;

    const graph = container.append("div").attr("class", "analysis-graph");
    const svg = graph.append("svg")
      .attr("viewBox", `0 0 ${width} ${height}`)
      .style("height", `${height}px`);
    svg.append("defs")
      .append("clipPath")
      .attr("id", clipId)
      .append("rect")
      .attr("width", innerWidth)
      .attr("height", innerHeight);
    const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);

    const grid = g.append("g").attr("class", "analysis-graph-grid");
    const xAxis = g.append("g")
      .attr("class", "analysis-graph-axis")
      .attr("transform", `translate(0,${innerHeight})`);
    const yAxis = g.append("g").attr("class", "analysis-graph-axis");
    const marks = g.append("g").attr("clip-path", `url(#${clipId})`);
    const linePath = options.drawLine ? marks.append("path").attr("class", "analysis-graph-line") : null;
    const dotLayer = marks.append("g");
    const hitLayer = marks.append("g");

    g.append("text")
      .attr("class", "analysis-graph-label")
      .attr("x", innerWidth / 2)
      .attr("y", innerHeight + 38)
      .attr("text-anchor", "middle")
      .text(options.xLabel);

    g.append("text")
      .attr("class", "analysis-graph-label")
      .attr("x", -innerHeight / 2)
      .attr("y", -34)
      .attr("text-anchor", "middle")
      .attr("transform", "rotate(-90)")
      .text(options.yLabel);

    g.insert("rect", ":first-child")
      .attr("class", "analysis-graph-zoom")
      .attr("width", innerWidth)
      .attr("height", innerHeight);

    function viewState(next = null) {
      const focus = next?.viewFocus || graphCamera?.getViewFocus?.() || centerFocus;
      const zoomScale = Math.max(1e-6, Number(next?.zoomScale ?? graphCamera?.getZoomScale?.() ?? initialZoom) || 1);
      return { focus, zoomScale };
    }

    function screenXFromWorld(worldX, stateForDraw) {
      return innerWidth / 2 + (worldX - stateForDraw.focus.x) * stateForDraw.zoomScale;
    }

    function screenYFromWorld(worldY, stateForDraw) {
      return innerHeight / 2 + (worldY - stateForDraw.focus.y) * stateForDraw.zoomScale;
    }

    function draw(next = null) {
      const activeKeys = options.selectedKeySet(panel);
      const stateForDraw = viewState(next);
      const visibleLeft = stateForDraw.focus.x - innerWidth / (2 * stateForDraw.zoomScale);
      const visibleRight = stateForDraw.focus.x + innerWidth / (2 * stateForDraw.zoomScale);
      const visibleTop = stateForDraw.focus.y - innerHeight / (2 * stateForDraw.zoomScale);
      const visibleBottom = stateForDraw.focus.y + innerHeight / (2 * stateForDraw.zoomScale);
      const xAxisScale = d3.scaleLinear()
        .domain([xBase.invert(visibleLeft), xBase.invert(visibleRight)])
        .range([0, innerWidth]);
      const yAxisScale = d3.scaleLinear()
        .domain([yBase.invert(visibleBottom), yBase.invert(visibleTop)])
        .range([innerHeight, 0]);

      grid.call(d3.axisLeft(yAxisScale).ticks(4).tickSize(-innerWidth).tickFormat(""));
      xAxis.call(d3.axisBottom(xAxisScale).ticks(Math.min(8, valid.length)).tickFormat(options.xTickFormat || (value => Number(value).toFixed(2))));
      yAxis.call(d3.axisLeft(yAxisScale).ticks(4).tickFormat(options.yTickFormat || undefined));

      if (linePath) {
        linePath.attr("d", d3.line()
          .x(d => screenXFromWorld(xBase(xValue(d)), stateForDraw))
          .y(d => screenYFromWorld(yBase(yValue(d)), stateForDraw))(valid));
      }

      dotLayer.selectAll("circle.analysis-graph-dot")
        .data(valid, keyFn)
        .join("circle")
        .attr("class", d => `analysis-graph-dot${activeKeys.has(keyFn(d)) ? " active" : ""}`)
        .attr("cx", d => screenXFromWorld(xBase(xValue(d)), stateForDraw))
        .attr("cy", d => screenYFromWorld(yBase(yValue(d)), stateForDraw))
        .attr("r", d => activeKeys.has(keyFn(d)) ? ANALYSIS_DOT_SELECTED_RADIUS : ANALYSIS_DOT_RADIUS);

      hitLayer.selectAll("circle.analysis-graph-hit")
        .data(valid, keyFn)
        .join("circle")
        .attr("class", "analysis-graph-hit")
        .attr("cx", d => screenXFromWorld(xBase(xValue(d)), stateForDraw))
        .attr("cy", d => screenYFromWorld(yBase(yValue(d)), stateForDraw))
        .attr("r", ANALYSIS_DOT_HIT_RADIUS)
        .on("mouseenter", (event, d) => updateTooltip(options.tooltip(d, panel), event.clientX, event.clientY))
        .on("mousemove", (event, d) => updateTooltip(options.tooltip(d, panel), event.clientX, event.clientY))
        .on("mouseleave", hideTooltip)
        .on("click", (event, d) => {
          event.stopPropagation();
          hideTooltip();
          options.onClick(panel, d);
        });
    }

    const graphCamera = window.ReebViewerCommon.createCameraController({
      zoomMin: 0.1,
      zoomMax: Math.max(20, Math.min(80, valid.length * 2)),
      zoomStep: ZOOM_STEP,
      panDragThreshold: PAN_DRAG_THRESHOLD,
      initialZoomScale: initialZoom,
      initialViewFocus: initialFocus,
      applyTransform: next => {
        panel.analysis[zoomKey] = next.zoomScale;
        panel.analysis[focusKey] = next.viewFocus
          ? { x: next.viewFocus.x, y: next.viewFocus.y }
          : null;
        draw(next);
      }
    });

    draw({ zoomScale: graphCamera.getZoomScale(), viewFocus: graphCamera.getViewFocus() || centerFocus });
    graphCamera.bindPanAndWheel(svg.node(), {
      cursorTarget: svg.node(),
      isPanTarget: target => !target.closest(".analysis-graph-hit, .analysis-graph-dot, button, input, select, label"),
      ensureFocus: () => graphCamera.getViewFocus() || centerFocus,
      onPanState: active => graph.classed("dragging", active)
    });

    const handle = container.append("div")
      .attr("class", "panel-resizer analysis-graph-resizer")
      .attr("title", options.resizeTitle || "Drag to resize plot");
    const handleNode = handle.node();
    let resizeState = null;

    const finishResize = event => {
      if (!resizeState) return;
      resizeState = null;
      handle.classed("active", false);
      try {
        if (handleNode.hasPointerCapture(event.pointerId)) handleNode.releasePointerCapture(event.pointerId);
      } catch (_) {}
      renderAll();
    };

    handleNode.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      resizeState = { startY: event.clientY, startHeight: panel.analysis[heightKey] };
      handle.classed("active", true);
      handleNode.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    handleNode.addEventListener("pointermove", event => {
      if (!resizeState) return;
      const nextHeight = clampIntervalGraphHeight(resizeState.startHeight + event.clientY - resizeState.startY);
      panel.analysis[heightKey] = nextHeight;
      svg.attr("viewBox", `0 0 ${width} ${nextHeight}`).style("height", `${nextHeight}px`);
      event.preventDefault();
    });

    handleNode.addEventListener("pointerup", finishResize);
    handleNode.addEventListener("pointercancel", finishResize);
  }

  function renderIntervalScoreGraph(container, panel, rows) {
    renderAnalysisPointGraph(container, panel, rows, {
      id: "intervals",
      heightKey: "intervalGraphHeight",
      zoomKey: "intervalGraphZoomScale",
      focusKey: "intervalGraphFocus",
      graphKey: "intervalGraphKey",
      selectedKeysKey: "selectedIntervalKeys",
      keyFn: intervalKeyFromItem,
      stateKey: panel => analysisCacheKey("interval-graph", panel),
      xValue: intervalTimeFs,
      yValue: item => Number(item.event_score),
      sort: (a, b) => intervalTimeFs(a) - intervalTimeFs(b),
      xLabel: "time (fs)",
      yLabel: "event score",
      xTickFormat: value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits),
      drawLine: true,
      tooltip: intervalGraphTooltip,
      selectedKeySet: selectedIntervalKeySet,
      aggregateHighlight: aggregateSelectedIntervalHighlight,
      onClick: toggleIntervalGraphSelection,
      resizeTitle: "Drag to resize interval plot"
    });
  }

  function renderTrackScoreGraph(container, panel, rows) {
    renderAnalysisPointGraph(container, panel, rows, {
      id: "tracks",
      heightKey: "trackGraphHeight",
      zoomKey: "trackGraphZoomScale",
      focusKey: "trackGraphFocus",
      graphKey: "trackGraphKey",
      selectedKeysKey: "selectedTrackKeys",
      keyFn: trackKeyFromItem,
      stateKey: panel => analysisCacheKey("track-graph", panel),
      xValue: item => Number(item.length),
      yValue: item => Number(item.mean_continuation_score),
      sort: (a, b) => Number(a.length) - Number(b.length) || Number(b.mean_continuation_score) - Number(a.mean_continuation_score),
      xDomain: valid => [0, (d3.max(valid, item => Number(item.length)) || 1) + 0.5],
      yDomain: valid => [0, Math.max(1, d3.max(valid, item => Number(item.mean_continuation_score)) || 1) * 1.05],
      xLabel: "feature length",
      yLabel: "mean score",
      xTickFormat: value => String(Math.round(Number(value))),
      yTickFormat: value => formatScore(value),
      drawLine: false,
      tooltip: trackGraphTooltip,
      selectedKeySet: selectedTrackKeySet,
      aggregateHighlight: aggregateSelectedTrackHighlight,
      onClick: toggleTrackGraphSelection,
      resizeTitle: "Drag to resize continuing-feature plot"
    });
  }

  function disagreementSummaryKey(item) {
    return String(item?.id || `disagreement_pair:${Number(item?.source_timestep_index)}:${Number(item?.target_timestep_index)}`);
  }

  function disagreementHighlightPayload(item) {
    const strongest = item?.strongest_disagreement || item;
    return {
      id: disagreementSummaryKey(item),
      disagreementKey: disagreementSummaryKey(item),
      label: "Domain/range disagreement",
      nodes: strongest?.highlight?.nodes || item?.highlight?.nodes || [],
      links: strongest?.highlight?.links || item?.highlight?.links || [],
      start: Number(item?.source_timestep_index),
      end: Number(item?.target_timestep_index),
    };
  }

  function selectedDisagreementKeySet(panel) {
    ensurePanelAnalysis(panel);
    return new Set((panel.analysis.selectedDisagreementKeys || []).filter(Boolean));
  }

  function aggregateSelectedDisagreementHighlight(panel) {
    ensurePanelAnalysis(panel);
    const selectedKeys = selectedDisagreementKeySet(panel);
    if (!selectedKeys.size) return null;
    const selected = domainRangeDisagreementData().rankedSummary
      .filter(item => selectedKeys.has(disagreementSummaryKey(item)))
      .map(disagreementHighlightPayload);
    if (!selected.length) return null;
    const label = `${selected.length} selected domain/range disagreement${selected.length === 1 ? "" : "s"}`;
    return {
      ...combinedHighlight(selected, label),
      id: "selected-domain-range-disagreements",
      disagreementKeys: selected.map(item => item.disagreementKey).filter(Boolean),
    };
  }

  function toggleDisagreementGraphSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const payload = disagreementHighlightPayload(item);
    const key = payload.disagreementKey;
    if (!key) return;
    const selected = panel.analysis.selectedDisagreementKeys || [];
    const alreadySelected = selected.includes(key);
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = alreadySelected ? [] : [key];
    panel.analysis.highlight = aggregateSelectedDisagreementHighlight(panel);
    if (!alreadySelected) queueAnalysisFocusPulse(panel, payload);
    expandRangesForHighlight(panel.analysis.highlight);
    renderAll();
  }

  function disagreementGraphTooltip(item) {
    const strongest = item?.strongest_disagreement || {};
    return `
      <strong>${escapeHtml(intervalTimestepPrimary(item, "source"))} -> ${escapeHtml(intervalTimestepPrimary(item, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Time</div><div>${escapeHtml(formatIntervalFs(intervalTimeFs(item)))}</div>
        <div>Max disagreement score</div><div>${escapeHtml(formatScore(item.max_disagreement_score))}</div>
        <div>Mean disagreement score</div><div>${escapeHtml(formatScore(item.mean_disagreement_score))}</div>
        <div>Disagreements / compared</div><div>${escapeHtml(item.disagreement_count)} / ${escapeHtml(item.compared_sources)}</div>
        <div>Disagreement fraction</div><div>${escapeHtml(formatScore(100 * Number(item.disagreement_fraction || 0)))}%</div>
        <div>Strongest source sheet</div><div>S${escapeHtml(strongest.source_sheet_id ?? "-")}</div>
        <div>Range target</div><div>S${escapeHtml(strongest.shape_target_sheet_id ?? "-")} (${escapeHtml(formatScore(strongest.shape_score))})</div>
        <div>Domain target</div><div>S${escapeHtml(strongest.overlap_target_sheet_id ?? "-")} (${escapeHtml(formatScore(strongest.overlap_max_percent))})</div>
        <div>Shape / domain loss</div><div>${escapeHtml(formatScore(strongest.shape_loss))} / ${escapeHtml(formatScore(strongest.overlap_loss))}</div>
      </div>`;
  }

  function renderDisagreementScoreGraph(container, panel, rows) {
    renderAnalysisPointGraph(container, panel, rows, {
      id: "domain-range-disagreement",
      heightKey: "disagreementGraphHeight",
      zoomKey: "disagreementGraphZoomScale",
      focusKey: "disagreementGraphFocus",
      graphKey: "disagreementGraphKey",
      selectedKeysKey: "selectedDisagreementKeys",
      keyFn: disagreementSummaryKey,
      xValue: intervalTimeFs,
      yValue: item => Number(item.max_disagreement_score),
      sort: (a, b) => intervalTimeFs(a) - intervalTimeFs(b),
      xLabel: "time (fs)",
      yLabel: "max disagreement score",
      xTickFormat: value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits),
      yTickFormat: value => formatScore(value),
      drawLine: false,
      tooltip: disagreementGraphTooltip,
      selectedKeySet: selectedDisagreementKeySet,
      aggregateHighlight: aggregateSelectedDisagreementHighlight,
      onClick: toggleDisagreementGraphSelection,
      resizeTitle: "Drag to resize domain/range disagreement plot"
    });
  }

  function agreementMetricKey(scope, metric) {
    return `${scope}:${metric}`;
  }

  function agreementMetricOptions() {
    const options = [{
      key: agreementMetricKey("shape", "combined"),
      scope: "shape",
      metric: "combined",
      label: metricLabel("shape", "combined"),
    }];
    options.push(...shapeScoreComponentIds
      .filter(metricId => metricId !== "combined")
      .map(metricId => ({
        key: agreementMetricKey("shape", metricId),
        scope: "shape",
        metric: metricId,
        label: metricLabel("shape", metricId),
      })));
    if (overlapMetricIds.includes("overlap_max_percent") || overlapPairLookup.size) {
      options.push({
        key: agreementMetricKey("overlap", "overlap_max_percent"),
        scope: "overlap",
        metric: "overlap_max_percent",
        label: metricLabel("overlap", "overlap_max_percent"),
      });
    }
    return options;
  }

  function agreementMetricInfo(metricKey) {
    return agreementMetricOptions().find(option => option.key === metricKey) || null;
  }

  function agreementMetricColor(metricKey, index = 0) {
    const options = agreementMetricOptions();
    const optionIndex = options.findIndex(option => option.key === metricKey);
    const colorIndex = optionIndex >= 0 ? optionIndex : index;
    return AGREEMENT_SERIES_COLORS[colorIndex % AGREEMENT_SERIES_COLORS.length];
  }

  function bestByScore(rows, scoreFn) {
    let best = null;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (const row of rows || []) {
      const score = Number(scoreFn(row));
      if (!Number.isFinite(score)) continue;
      if (score > bestScore) {
        best = row;
        bestScore = score;
      }
    }
    return best;
  }

  function shapeMetricScore(match, metric) {
    if (metric === "combined") return analysisCombinedScore(match);
    const value = Number(match?.metrics?.[metric] ?? match?.[metric]);
    return Number.isFinite(value) ? value : 0;
  }

  function overlapMetricScore(match, metric) {
    const metrics = match?.metrics || match || {};
    const sourcePercent = Number(metrics.overlap_source_percent ?? match?.source_percent ?? 0) || 0;
    const targetPercent = Number(metrics.overlap_target_percent ?? match?.target_percent ?? 0) || 0;
    const fallback = Math.max(sourcePercent, targetPercent);
    const value = Number(metrics[metric] ?? metrics.overlap_max_percent ?? fallback);
    return Number.isFinite(value) ? value : 0;
  }

  function meanOrZero(sum, count) {
    return count > 0 ? sum / count : 0;
  }

  function metricAgreementRows(metricKey) {
    const cacheKey = `agreement:${metricKey}`;
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);
    const info = agreementMetricInfo(metricKey);
    if (!info) return [];

    const rows = [];
    const shapePairs = Array.isArray(data.shape_pairs) ? data.shape_pairs : [];
    for (const shapePair of shapePairs) {
      const sourceIndex = Number(shapePair.source_timestep_index);
      const targetIndex = Number(shapePair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;
      const pairKey = `${sourceIndex}:${targetIndex}`;
      const shapeGroups = groupMatchesByField(shapePair.matches || [], "source_sheet_id");
      const overlapPair = overlapPairLookup.get(pairKey);
      const overlapGroups = overlapPair ? groupMatchesByField(overlapPair.matches || [], "source_sheet_id") : new Map();

      let compared = 0;
      let agreements = 0;
      let lossSum = 0;
      let lossCount = 0;
      let referenceBestSum = 0;
      let candidateMetricBestSum = 0;
      let candidateReferenceSum = 0;
      let candidateReferenceCount = 0;

      if (info.scope === "shape") {
        for (const matches of shapeGroups.values()) {
          const combinedBest = bestByScore(matches, match => shapeMetricScore(match, "combined"));
          const metricBest = bestByScore(matches, match => shapeMetricScore(match, info.metric));
          if (!combinedBest || !metricBest) continue;
          compared += 1;
          if (Number(combinedBest.target_sheet_id) === Number(metricBest.target_sheet_id)) agreements += 1;
          const referenceScore = shapeMetricScore(combinedBest, "combined");
          const candidateReferenceScore = shapeMetricScore(metricBest, "combined");
          referenceBestSum += referenceScore;
          candidateMetricBestSum += shapeMetricScore(metricBest, info.metric);
          candidateReferenceSum += candidateReferenceScore;
          candidateReferenceCount += 1;
          lossSum += referenceScore - candidateReferenceScore;
          lossCount += 1;
        }
      } else if (info.scope === "overlap") {
        for (const [sourceSheetId, overlapMatches] of overlapGroups.entries()) {
          const shapeMatches = shapeGroups.get(sourceSheetId) || [];
          const combinedBest = bestByScore(shapeMatches, match => shapeMetricScore(match, "combined"));
          const overlapBest = bestByScore(overlapMatches, match => overlapMetricScore(match, info.metric));
          if (!combinedBest || !overlapBest) continue;
          compared += 1;
          if (Number(combinedBest.target_sheet_id) === Number(overlapBest.target_sheet_id)) agreements += 1;
          const referenceScore = shapeMetricScore(combinedBest, "combined");
          referenceBestSum += referenceScore;
          candidateMetricBestSum += overlapMetricScore(overlapBest, info.metric);
          const overlapTarget = Number(overlapBest.target_sheet_id);
          const shapeForOverlapTarget = (shapeMatches || []).find(match => Number(match.target_sheet_id) === overlapTarget);
          if (shapeForOverlapTarget) {
            const candidateReferenceScore = shapeMetricScore(shapeForOverlapTarget, "combined");
            candidateReferenceSum += candidateReferenceScore;
            candidateReferenceCount += 1;
            lossSum += referenceScore - candidateReferenceScore;
            lossCount += 1;
          }
        }
      }

      if (!compared) continue;
      rows.push({
        metricKey,
        candidate_scope: info.scope,
        candidate_metric: info.metric,
        label: info.label,
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: shapePair.source_label || overlapPair?.source_label || String(sourceIndex),
        target_label: shapePair.target_label || overlapPair?.target_label || String(targetIndex),
        compared_sources: compared,
        agreements,
        agreement_fraction: agreements / compared,
        mean_reference_loss_if_candidate_used: meanOrZero(lossSum, lossCount),
        average_reference_best_score: meanOrZero(referenceBestSum, compared),
        average_candidate_best_score: meanOrZero(candidateMetricBestSum, compared),
        average_candidate_reference_score: meanOrZero(candidateReferenceSum, candidateReferenceCount),
      });
    }
    rows.sort((a, b) => Number(a.source_timestep_index) - Number(b.source_timestep_index) || Number(a.target_timestep_index) - Number(b.target_timestep_index));
    analysisRuntimeCache.set(cacheKey, rows);
    return rows;
  }

  function agreementRowsInVisibleRange(metricKey) {
    return metricAgreementRows(metricKey).filter(row => inRanges(Number(row.source_timestep_index)) && inRanges(Number(row.target_timestep_index)));
  }

  function summarizeAgreementRows(rows) {
    let compared = 0;
    let agreements = 0;
    let weightedLoss = 0;
    let weightedReferenceBest = 0;
    let weightedCandidateBest = 0;
    for (const row of rows || []) {
      const count = Math.max(0, Number(row.compared_sources) || 0);
      compared += count;
      agreements += Math.max(0, Number(row.agreements) || 0);
      weightedLoss += (Number(row.mean_reference_loss_if_candidate_used) || 0) * count;
      weightedReferenceBest += (Number(row.average_reference_best_score) || 0) * count;
      weightedCandidateBest += (Number(row.average_candidate_best_score) || 0) * count;
    }
    return {
      compared_sources: compared,
      agreements,
      agreement_fraction: compared ? agreements / compared : 0,
      mean_reference_loss_if_candidate_used: compared ? weightedLoss / compared : 0,
      average_reference_best_score: compared ? weightedReferenceBest / compared : 0,
      average_candidate_best_score: compared ? weightedCandidateBest / compared : 0,
    };
  }

  function agreementTimeFs(row) {
    const fsRaw = window.ReebViewerCommon.formatFsFromLabel(row?.source_label, TIMESTEP_LABEL_OPTIONS);
    const fs = Number(fsRaw);
    if (Number.isFinite(fs)) return fs;
    return Number(row?.source_timestep_index) || 0;
  }

  function agreementPointKey(row) {
    return `${row?.metricKey}:${Number(row?.source_timestep_index)}:${Number(row?.target_timestep_index)}`;
  }

  function selectedAgreementPointKeySet(panel) {
    ensurePanelAnalysis(panel);
    return new Set((panel.analysis.selectedAgreementPointKeys || []).filter(Boolean));
  }

  function toggleAgreementPointSelection(panel, row) {
    ensurePanelAnalysis(panel);
    const key = agreementPointKey(row);
    if (!key) return;
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.highlight = null;
    const selected = panel.analysis.selectedAgreementPointKeys || [];
    const alreadySelected = selected.includes(key);
    panel.analysis.selectedAgreementPointKeys = alreadySelected ? [] : [key];
    if (!alreadySelected) queueAnalysisFocusPulse(panel, {
      start: Number(row.source_timestep_index),
      end: Number(row.target_timestep_index),
    });
    renderAll();
  }

  function agreementGraphTooltip(row) {
    return `
      <strong>${escapeHtml(row.label)}: ${escapeHtml(intervalTimestepPrimary(row, "source"))} -> ${escapeHtml(intervalTimestepPrimary(row, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Agreement</div><div>${escapeHtml(formatScore(100 * Number(row.agreement_fraction || 0)))}%</div>
        <div>Compared sources</div><div>${escapeHtml(row.compared_sources)}</div>
        <div>Agreements</div><div>${escapeHtml(row.agreements)}</div>
        <div>Loss</div><div>${escapeHtml(formatScore(row.mean_reference_loss_if_candidate_used))}</div>
        <div>Avg reference best</div><div>${escapeHtml(formatScore(row.average_reference_best_score))}</div>
        <div>Avg candidate best</div><div>${escapeHtml(formatScore(row.average_candidate_best_score))}</div>
        <div>Avg candidate combined</div><div>${escapeHtml(formatScore(row.average_candidate_reference_score))}</div>
      </div>`;
  }

  function renderMetricAgreementSummaries(container, panel, selectedMetrics) {
    const summary = container.append("div").attr("class", "analysis-metric-summary");
    selectedMetrics.forEach((metricKey, index) => {
      const info = agreementMetricInfo(metricKey);
      if (!info) return;
      const rows = agreementRowsInVisibleRange(metricKey);
      const totals = summarizeAgreementRows(rows);
      const card = summary.append("div").attr("class", "analysis-metric-card");
      const header = card.append("header");
      const title = header.append("span").attr("class", "analysis-metric-title");
      title.append("span")
        .attr("class", "analysis-metric-color")
        .style("background", agreementMetricColor(metricKey, index));
      title.append("span").text(info.label);
      header.append("button")
        .attr("type", "button")
        .text("Remove")
        .on("click", () => {
          panel.analysis.selectedAgreementMetrics = (panel.analysis.selectedAgreementMetrics || []).filter(key => key !== metricKey);
          panel.analysis.selectedAgreementPointKeys = (panel.analysis.selectedAgreementPointKeys || []).filter(key => !key.startsWith(`${metricKey}:`));
          renderAll();
        });
      card.append("div").text(`agreement ${formatScore(100 * totals.agreement_fraction)}%`);
      card.append("div").text(`loss ${formatScore(totals.mean_reference_loss_if_candidate_used)}`);
      card.append("div").text(`compared sources ${totals.compared_sources}`);
    });
  }

  function renderMetricAgreementGraph(container, panel, selectedMetrics) {
    const series = selectedMetrics
      .map((metricKey, index) => ({
        metricKey,
        info: agreementMetricInfo(metricKey),
        color: agreementMetricColor(metricKey, index),
        rows: agreementRowsInVisibleRange(metricKey),
      }))
      .filter(item => item.info && item.rows.length);
    if (!series.length) {
      container.append("div").attr("class", "analysis-hint").text("No metric agreement rows in the selected timestep range.");
      return;
    }

    const allRows = series.flatMap(item => item.rows);
    const visiblePointKeys = new Set(allRows.map(agreementPointKey));
    panel.analysis.selectedAgreementPointKeys = (panel.analysis.selectedAgreementPointKeys || []).filter(key => visiblePointKeys.has(key));
    const width = 760;
    const height = clampIntervalGraphHeight(panel.analysis.agreementGraphHeight);
    panel.analysis.agreementGraphHeight = height;
    const margin = { top: 12, right: 18, bottom: 46, left: 46 };
    const innerWidth = width - margin.left - margin.right;
    const innerHeight = Math.max(1, height - margin.top - margin.bottom);
    let xDomain = d3.extent(allRows, agreementTimeFs);
    if (xDomain[0] === xDomain[1]) xDomain = [xDomain[0] - 1, xDomain[1] + 1];
    const yDomain = [0, 100];
    const xBase = d3.scaleLinear().domain(xDomain).range([0, innerWidth]);
    const yBase = d3.scaleLinear().domain(yDomain).range([innerHeight, 0]);
    const graphKey = `agreement:${selectedMetrics.join("|")}:${allRows.map(row => `${row.metricKey}:${row.source_timestep_index}:${row.target_timestep_index}`).join("|")}`;
    if (panel.analysis.agreementGraphKey !== graphKey) {
      panel.analysis.agreementGraphKey = graphKey;
      panel.analysis.agreementGraphZoomScale = 1;
      panel.analysis.agreementGraphFocus = null;
    }
    const centerFocus = { x: innerWidth / 2, y: innerHeight / 2 };
    const initialFocus = panel.analysis.agreementGraphFocus &&
      Number.isFinite(Number(panel.analysis.agreementGraphFocus.x)) &&
      Number.isFinite(Number(panel.analysis.agreementGraphFocus.y))
        ? { x: Number(panel.analysis.agreementGraphFocus.x), y: Number(panel.analysis.agreementGraphFocus.y) }
        : centerFocus;
    const initialZoom = Number.isFinite(Number(panel.analysis.agreementGraphZoomScale))
      ? Number(panel.analysis.agreementGraphZoomScale)
      : 1;

    const graph = container.append("div").attr("class", "analysis-graph");
    const svg = graph.append("svg")
      .attr("viewBox", `0 0 ${width} ${height}`)
      .style("height", `${height}px`);
    const clipId = `analysis-agreement-clip-${String(panel.id).replace(/[^a-zA-Z0-9_-]/g, "")}`;
    svg.append("defs")
      .append("clipPath")
      .attr("id", clipId)
      .append("rect")
      .attr("width", innerWidth)
      .attr("height", innerHeight);
    const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const grid = g.append("g").attr("class", "analysis-graph-grid");
    const xAxis = g.append("g")
      .attr("class", "analysis-graph-axis")
      .attr("transform", `translate(0,${innerHeight})`);
    const yAxis = g.append("g").attr("class", "analysis-graph-axis");
    const marks = g.append("g").attr("clip-path", `url(#${clipId})`);
    const lineLayer = marks.append("g");
    const dotLayer = marks.append("g");
    const hitLayer = marks.append("g");

    g.append("text")
      .attr("class", "analysis-graph-label")
      .attr("x", innerWidth / 2)
      .attr("y", innerHeight + 38)
      .attr("text-anchor", "middle")
      .text("time (fs)");
    g.append("text")
      .attr("class", "analysis-graph-label")
      .attr("x", -innerHeight / 2)
      .attr("y", -34)
      .attr("text-anchor", "middle")
      .attr("transform", "rotate(-90)")
      .text("agreement (%)");
    g.insert("rect", ":first-child")
      .attr("class", "analysis-graph-zoom")
      .attr("width", innerWidth)
      .attr("height", innerHeight);

    function viewState(next = null) {
      const focus = next?.viewFocus || graphCamera?.getViewFocus?.() || centerFocus;
      const zoomScale = Math.max(1e-6, Number(next?.zoomScale ?? graphCamera?.getZoomScale?.() ?? initialZoom) || 1);
      return { focus, zoomScale };
    }
    function screenXFromWorld(worldX, stateForDraw) {
      return innerWidth / 2 + (worldX - stateForDraw.focus.x) * stateForDraw.zoomScale;
    }
    function screenYFromWorld(worldY, stateForDraw) {
      return innerHeight / 2 + (worldY - stateForDraw.focus.y) * stateForDraw.zoomScale;
    }
    function draw(next = null) {
      const selectedPointKeys = selectedAgreementPointKeySet(panel);
      const stateForDraw = viewState(next);
      const visibleLeft = stateForDraw.focus.x - innerWidth / (2 * stateForDraw.zoomScale);
      const visibleRight = stateForDraw.focus.x + innerWidth / (2 * stateForDraw.zoomScale);
      const visibleTop = stateForDraw.focus.y - innerHeight / (2 * stateForDraw.zoomScale);
      const visibleBottom = stateForDraw.focus.y + innerHeight / (2 * stateForDraw.zoomScale);
      const xAxisScale = d3.scaleLinear()
        .domain([xBase.invert(visibleLeft), xBase.invert(visibleRight)])
        .range([0, innerWidth]);
      const yAxisScale = d3.scaleLinear()
        .domain([yBase.invert(visibleBottom), yBase.invert(visibleTop)])
        .range([innerHeight, 0]);
      grid.call(d3.axisLeft(yAxisScale).ticks(4).tickSize(-innerWidth).tickFormat(""));
      xAxis.call(d3.axisBottom(xAxisScale).ticks(8).tickFormat(value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits)));
      yAxis.call(d3.axisLeft(yAxisScale).ticks(5).tickFormat(value => `${formatScore(value)}%`));

      lineLayer.selectAll("path.analysis-agreement-line")
        .data(series, item => item.metricKey)
        .join("path")
        .attr("class", "analysis-agreement-line")
        .attr("stroke", item => item.color)
        .attr("opacity", 1)
        .attr("stroke-opacity", 1)
        .style("opacity", 1)
        .style("stroke-opacity", 1)
        .style("mix-blend-mode", "normal")
        .attr("d", item => d3.line()
          .x(row => screenXFromWorld(xBase(agreementTimeFs(row)), stateForDraw))
          .y(row => screenYFromWorld(yBase(100 * Number(row.agreement_fraction || 0)), stateForDraw))(item.rows));

      dotLayer.selectAll("circle.analysis-agreement-dot")
        .data(allRows, agreementPointKey)
        .join("circle")
        .attr("class", row => `analysis-agreement-dot${selectedPointKeys.has(agreementPointKey(row)) ? " active" : ""}`)
        .attr("fill", row => agreementMetricColor(row.metricKey))
        .attr("opacity", 1)
        .attr("fill-opacity", 1)
        .style("opacity", 1)
        .style("fill-opacity", 1)
        .style("mix-blend-mode", "normal")
        .attr("cx", row => screenXFromWorld(xBase(agreementTimeFs(row)), stateForDraw))
        .attr("cy", row => screenYFromWorld(yBase(100 * Number(row.agreement_fraction || 0)), stateForDraw))
        .attr("r", row => selectedPointKeys.has(agreementPointKey(row)) ? ANALYSIS_DOT_SELECTED_RADIUS : ANALYSIS_DOT_RADIUS);

      hitLayer.selectAll("circle.analysis-agreement-hit")
        .data(allRows, agreementPointKey)
        .join("circle")
        .attr("class", "analysis-agreement-hit")
        .attr("cx", row => screenXFromWorld(xBase(agreementTimeFs(row)), stateForDraw))
        .attr("cy", row => screenYFromWorld(yBase(100 * Number(row.agreement_fraction || 0)), stateForDraw))
        .attr("r", ANALYSIS_DOT_HIT_RADIUS)
        .on("mouseenter", (event, row) => updateTooltip(agreementGraphTooltip(row), event.clientX, event.clientY))
        .on("mousemove", (event, row) => updateTooltip(agreementGraphTooltip(row), event.clientX, event.clientY))
        .on("mouseleave", hideTooltip)
        .on("click", (event, row) => {
          event.stopPropagation();
          hideTooltip();
          toggleAgreementPointSelection(panel, row);
        });
    }

    const graphCamera = window.ReebViewerCommon.createCameraController({
      zoomMin: 0.1,
      zoomMax: Math.max(20, Math.min(80, allRows.length * 2)),
      zoomStep: ZOOM_STEP,
      panDragThreshold: PAN_DRAG_THRESHOLD,
      initialZoomScale: initialZoom,
      initialViewFocus: initialFocus,
      applyTransform: next => {
        panel.analysis.agreementGraphZoomScale = next.zoomScale;
        panel.analysis.agreementGraphFocus = next.viewFocus
          ? { x: next.viewFocus.x, y: next.viewFocus.y }
          : null;
        draw(next);
      }
    });
    draw({ zoomScale: graphCamera.getZoomScale(), viewFocus: graphCamera.getViewFocus() || centerFocus });
    graphCamera.bindPanAndWheel(svg.node(), {
      cursorTarget: svg.node(),
      isPanTarget: target => !target.closest(".analysis-agreement-hit, .analysis-agreement-dot, button, input, select, label"),
      ensureFocus: () => graphCamera.getViewFocus() || centerFocus,
      onPanState: active => graph.classed("dragging", active)
    });

    const handle = container.append("div")
      .attr("class", "panel-resizer analysis-graph-resizer")
      .attr("title", "Drag to resize metric-agreement plot");
    const handleNode = handle.node();
    let resizeState = null;
    const finishResize = event => {
      if (!resizeState) return;
      resizeState = null;
      handle.classed("active", false);
      try {
        if (handleNode.hasPointerCapture(event.pointerId)) handleNode.releasePointerCapture(event.pointerId);
      } catch (_) {}
      renderAll();
    };
    handleNode.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      resizeState = { startY: event.clientY, startHeight: panel.analysis.agreementGraphHeight };
      handle.classed("active", true);
      handleNode.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    handleNode.addEventListener("pointermove", event => {
      if (!resizeState) return;
      const nextHeight = clampIntervalGraphHeight(resizeState.startHeight + event.clientY - resizeState.startY);
      panel.analysis.agreementGraphHeight = nextHeight;
      svg.attr("viewBox", `0 0 ${width} ${nextHeight}`).style("height", `${nextHeight}px`);
      event.preventDefault();
    });
    handleNode.addEventListener("pointerup", finishResize);
    handleNode.addEventListener("pointercancel", finishResize);
  }

  function nodeKeyFromDatum(d) {
    return `${Number(d.timestep_index)}:${Number(d.sheet_id)}`;
  }

  function linkKeyFromDatum(d) {
    return `${Number(d.source_timestep_index)}:${Number(d.source_sheet_id)}->${Number(d.target_timestep_index)}:${Number(d.target_sheet_id)}`;
  }

  function combinedHighlight(items, label) {
    const nodes = new Set();
    const links = new Set();
    let start = Number.POSITIVE_INFINITY;
    let end = Number.NEGATIVE_INFINITY;
    for (const item of items || []) {
      for (const key of item?.highlight?.nodes || item?.nodes || []) nodes.add(key);
      for (const key of item?.highlight?.links || item?.links || []) links.add(key);
      const itemStart = Number(item?.source_timestep_index ?? item?.start_timestep_index ?? item?.start);
      const itemEnd = Number(item?.target_timestep_index ?? item?.end_timestep_index ?? item?.end);
      if (Number.isFinite(itemStart)) start = Math.min(start, itemStart);
      if (Number.isFinite(itemEnd)) end = Math.max(end, itemEnd);
    }
    return {
      label,
      nodes: [...nodes],
      links: [...links],
      start: Number.isFinite(start) ? start : null,
      end: Number.isFinite(end) ? end : null,
    };
  }

  function selectedIntervalKeySet(panel) {
    ensurePanelAnalysis(panel);
    return new Set((panel.analysis.selectedIntervalKeys || []).filter(Boolean));
  }

  function selectedTrackKeySet(panel) {
    ensurePanelAnalysis(panel);
    return new Set((panel.analysis.selectedTrackKeys || []).filter(Boolean));
  }

  function aggregateSelectedIntervalHighlight(panel) {
    ensurePanelAnalysis(panel);
    const selectedKeys = selectedIntervalKeySet(panel);
    if (!selectedKeys.size) return null;
    const selected = analysisIntervals(panel)
      .filter(item => selectedKeys.has(intervalKeyFromItem(item)))
      .map(item => intervalHighlightPayload(item, panel));
    if (!selected.length) return null;
    const label = `${selected.length} selected interval${selected.length === 1 ? "" : "s"}`;
    return {
      ...combinedHighlight(selected, label),
      id: "selected-intervals",
      intervalKeys: selected.map(item => item.intervalKey).filter(Boolean),
    };
  }

  function aggregateSelectedTrackHighlight(panel) {
    ensurePanelAnalysis(panel);
    const selectedKeys = selectedTrackKeySet(panel);
    if (!selectedKeys.size) return null;
    const selected = analysisTracks(panel)
      .filter(item => selectedKeys.has(trackKeyFromItem(item)))
      .map(item => trackHighlightPayload(item, panel));
    if (!selected.length) return null;
    const label = `${selected.length} selected continuing feature${selected.length === 1 ? "" : "s"}`;
    return {
      ...combinedHighlight(selected, label),
      id: "selected-tracks",
      trackKeys: selected.map(item => item.trackKey).filter(Boolean),
    };
  }

  function toggleIntervalGraphSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const payload = intervalHighlightPayload(item, panel);
    const key = payload.intervalKey;
    if (!key) return;
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    const selected = panel.analysis.selectedIntervalKeys || [];
    const alreadySelected = selected.includes(key);
    panel.analysis.selectedIntervalKeys = alreadySelected
      ? selected.filter(entry => entry !== key)
      : [...selected, key];
    panel.analysis.highlight = aggregateSelectedIntervalHighlight(panel);
    if (!alreadySelected) queueAnalysisFocusPulse(panel, payload);
    expandRangesForHighlight(panel.analysis.highlight);
    renderAll();
  }

  function toggleTrackGraphSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const payload = trackHighlightPayload(item, panel);
    const key = payload.trackKey;
    if (!key) return;
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    const selected = panel.analysis.selectedTrackKeys || [];
    const alreadySelected = selected.includes(key);
    panel.analysis.selectedTrackKeys = alreadySelected
      ? selected.filter(entry => entry !== key)
      : [...selected, key];
    panel.analysis.highlight = aggregateSelectedTrackHighlight(panel);
    if (!alreadySelected) queueAnalysisFocusPulse(panel, { start: payload.start, end: payload.start });
    expandRangesForHighlight(panel.analysis.highlight);
    renderAll();
  }

  function queueAnalysisFocusPulse(panel, highlight) {
    const start = Number(highlight?.start);
    const end = Number(highlight?.end);
    if (!panel || !Number.isFinite(start) || !Number.isFinite(end)) return;
    state.analysisFocusPulse = {
      panelId: panel.id,
      start: Math.min(start, end),
      end: Math.max(start, end),
      key: `${panel.id}:${start}:${end}:${Date.now()}`
    };
  }

  function expandRangesForHighlight(highlight) {
    if (!highlight || !Number.isFinite(Number(highlight.start)) || !Number.isFinite(Number(highlight.end))) return;
    const pad = 1;
    const highlightStart = clamp(Math.floor(Math.min(Number(highlight.start), Number(highlight.end))) - pad, 0, timestepMax);
    const highlightEnd = clamp(Math.ceil(Math.max(Number(highlight.start), Number(highlight.end))) + pad, 0, timestepMax);
    const ranges = normalizedRanges();
    if (!ranges.length) return;

    const currentStart = d3.min(ranges, range => Number(range.start));
    const currentEnd = d3.max(ranges, range => Number(range.end));
    if (!Number.isFinite(currentStart) || !Number.isFinite(currentEnd)) return;

    const nextStart = Math.min(currentStart, highlightStart);
    const nextEnd = Math.max(currentEnd, highlightEnd);
    if (nextStart === currentStart && nextEnd === currentEnd) return;

    state.ranges = [{ start: nextStart, end: nextEnd }];
    state.selectedRangeIndex = 0;
  }

  function clearAnalysisSelectionsOnly(panel) {
    if (!panel?.analysis) return;
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.highlight = null;
    if (state.analysisFocusPulse?.panelId === panel.id) state.analysisFocusPulse = null;
  }

  function setAnalysisHighlight(panel, highlight, focusRange = true) {
    ensurePanelAnalysis(panel);
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    state.analysisFocusPulse = null;
    panel.analysis.highlight = highlight;
    if (focusRange && highlight && Number.isFinite(Number(highlight.start)) && Number.isFinite(Number(highlight.end))) {
      const pad = 1;
      const start = clamp(Math.floor(Number(highlight.start)) - pad, 0, timestepMax);
      const end = clamp(Math.ceil(Number(highlight.end)) + pad, 0, timestepMax);
      state.ranges = [{ start, end: Math.max(start, end) }];
      state.selectedRangeIndex = 0;
    } else {
      expandRangesForHighlight(highlight);
    }
    renderAll();
  }

  function clearAnalysisHighlight(panel) {
    ensurePanelAnalysis(panel);
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.selectedAgreementPointKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    state.analysisFocusPulse = null;
    panel.analysis.highlight = null;
    renderAll();
  }

  function highlightedNodeSet(panel) {
    return new Set(panel?.analysis?.highlight?.nodes || []);
  }

  function highlightedLinkSet(panel) {
    return new Set(panel?.analysis?.highlight?.links || []);
  }

  function renderAnalysisPanel(container, panel) {
    ensurePanelAnalysis(panel);
    const box = container.append("div").attr("class", "analysis-box");
    const toolbar = box.append("div").attr("class", "analysis-toolbar");
    toolbar.append("div").attr("class", "analysis-title").text("Analysis");
    const actions = toolbar.append("div").attr("class", "analysis-actions");

    if (!hasEmbeddedAnalysisData) {
      box.append("div").attr("class", "analysis-hint").text("Runtime interval and continuing-feature analysis is available. Run Stage 5B to populate sensitivity and metric-agreement summaries.");
    }

    const thetaSelect = actions.append("label");
    thetaSelect.append("span").text("theta ");
    const theta = thetaSelect.append("select");
    analysisThresholds.forEach(value => {
      theta.append("option")
        .attr("value", value)
        .property("selected", Math.abs(Number(value) - Number(panel.analysis.theta)) < 1e-12)
        .text(formatScore(value));
    });
    theta.on("change", event => {
      panel.analysis.theta = Number(event.target.value);
      panel.analysis.selectedIntervalKeys = [];
      panel.analysis.selectedTrackKeys = [];
      panel.analysis.selectedAgreementPointKeys = [];
      panel.analysis.selectedDisagreementKeys = [];
      state.analysisFocusPulse = null;
      panel.analysis.highlight = null;
      renderAll();
    });

    actions.append("button")
      .attr("type", "button")
      .text("Clear highlight")
      .on("click", () => clearAnalysisHighlight(panel));

    const tabs = [
      ["intervals", "Intervals"],
      ["tracks", "Continuing features"],
      ["sensitivity", "Sensitivity"],
      ["agreement", "Metric agreement"],
      ["disagreement", "Domain/range disagreement"],
    ];
    const tabRow = box.append("div").attr("class", "analysis-tabs");
    tabs.forEach(([id, label]) => {
      tabRow.append("button")
        .attr("type", "button")
        .attr("class", `analysis-tab${panel.analysis.tab === id ? " active" : ""}`)
        .text(label)
        .on("click", () => {
          if (panel.analysis.tab !== id) {
            panel.analysis.selectedAgreementPointKeys = [];
            if (id !== "disagreement") panel.analysis.selectedDisagreementKeys = [];
            if (id === "agreement" || id === "disagreement") {
              panel.analysis.selectedIntervalKeys = [];
              panel.analysis.selectedTrackKeys = [];
              panel.analysis.highlight = null;
            }
            state.analysisFocusPulse = null;
          }
          panel.analysis.tab = id;
          renderAll();
        });
    });

    const content = box.append("div").attr("class", "analysis-content");
    if (panel.analysis.tab === "intervals") {
      const rows = analysisIntervals(panel);
      const visibleRows = rows.slice(0, Math.min(panel.analysis.topIntervals, rows.length));
      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top intervals out of ${rows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 1)
        .attr("max", Math.max(1, rows.length))
        .property("value", Math.min(panel.analysis.topIntervals, Math.max(1, rows.length)));
      countInput.on("change", event => {
        const maxCount = Math.max(1, rows.length);
        panel.analysis.topIntervals = clamp(Math.floor(Number(event.target.value) || 1), 1, maxCount);
        renderAll();
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => setAnalysisHighlight(panel, combinedHighlight(visibleRows.map(item => intervalHighlightPayload(item, panel)), `Top ${visibleRows.length} intervals`), false));

      renderIntervalScoreGraph(content, panel, visibleRows);

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text("No interval analysis for this theta.");
      return;
    }

    if (panel.analysis.tab === "tracks") {
      const rows = analysisTracks(panel);
      const visibleRows = rows.slice(0, Math.min(panel.analysis.topFeatures, rows.length));
      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top continuing features out of ${rows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 1)
        .attr("max", Math.max(1, rows.length))
        .property("value", Math.min(panel.analysis.topFeatures, Math.max(1, rows.length)));
      countInput.on("change", event => {
        const maxCount = Math.max(1, rows.length);
        panel.analysis.topFeatures = clamp(Math.floor(Number(event.target.value) || 1), 1, maxCount);
        renderAll();
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => setAnalysisHighlight(panel, combinedHighlight(visibleRows, `Top ${visibleRows.length} continuing features`), false));

      renderTrackScoreGraph(content, panel, visibleRows);

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text("No continuing-feature analysis for this theta.");
      return;
    }

    if (panel.analysis.tab === "sensitivity") {
      const sensitivityRows = computeRuntimeSensitivity(panel);
      content.append("div")
        .attr("class", "analysis-hint")
        .text(`Sensitivity for ${modeLabel(panel.dataMode)} / ${metricLabel(panel.dataMode, panel.metricId)}`);
      const table = content.append("table").attr("class", "analysis-table");
      const header = table.append("thead").append("tr");
      ["theta", "mean event", "max event", "top interval", "max life", "median life", ""].forEach(label => header.append("th").text(label));
      const body = table.append("tbody");
      sensitivityRows.forEach(row => {
        const tr = body.append("tr");
        tr.append("td").text(formatScore(row.threshold));
        tr.append("td").text(formatScore(row.mean_event_score));
        tr.append("td").text(formatScore(row.max_event_score));
        tr.append("td").text(row.top_event_pair_label || "-");
        tr.append("td").text(row.max_lifetime ?? 0);
        tr.append("td").text(formatScore(row.median_lifetime));
        tr.append("td").append("button").attr("type", "button").text("Use")
          .on("click", () => {
            panel.analysis.theta = Number(row.threshold);
            panel.analysis.tab = "intervals";
            panel.analysis.selectedIntervalKeys = [];
            panel.analysis.selectedTrackKeys = [];
            panel.analysis.selectedAgreementPointKeys = [];
            panel.analysis.selectedDisagreementKeys = [];
            state.analysisFocusPulse = null;
            panel.analysis.highlight = null;
            renderAll();
          });
      });
      return;
    }

    if (panel.analysis.tab === "agreement") {
      const options = agreementMetricOptions();
      const validKeys = new Set(options.map(option => option.key));
      panel.analysis.selectedAgreementMetrics = (panel.analysis.selectedAgreementMetrics || []).filter(key => validKeys.has(key));

      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text("Add metrics to compare over the selected timestep range");
      const metricSelect = controls.append("select");
      options.forEach(option => {
        metricSelect.append("option")
          .attr("value", option.key)
          .text(`${option.label} (${option.scope})`);
      });
      controls.append("button")
        .attr("type", "button")
        .text("Add metric")
        .on("click", () => {
          const key = metricSelect.node()?.value;
          if (!key) return;
          if (!(panel.analysis.selectedAgreementMetrics || []).includes(key)) {
            panel.analysis.selectedAgreementMetrics = [...(panel.analysis.selectedAgreementMetrics || []), key];
          }
          renderAll();
        });
      controls.append("button")
        .attr("type", "button")
        .text("Clear metrics")
        .on("click", () => {
          panel.analysis.selectedAgreementMetrics = [];
          panel.analysis.selectedAgreementPointKeys = [];
          panel.analysis.selectedDisagreementKeys = [];
          renderAll();
        });

      const selected = panel.analysis.selectedAgreementMetrics || [];
      if (!selected.length) {
        content.append("div").attr("class", "analysis-hint").text("No metrics selected for plotting.");
        return;
      }
      renderMetricAgreementSummaries(content, panel, selected);
      renderMetricAgreementGraph(content, panel, selected);
      return;
    }

    if (panel.analysis.tab === "disagreement") {
      const disagreementData = domainRangeDisagreementData();
      const rankedRows = disagreementData.rankedSummary || [];
      const visibleRows = rankedRows.slice(0, Math.min(panel.analysis.topDisagreements, rankedRows.length));
      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top disagreement timestep pairs out of ${rankedRows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 1)
        .attr("max", Math.max(1, rankedRows.length))
        .property("value", Math.min(panel.analysis.topDisagreements, Math.max(1, rankedRows.length)));
      countInput.on("change", event => {
        const maxCount = Math.max(1, rankedRows.length);
        panel.analysis.topDisagreements = clamp(Math.floor(Number(event.target.value) || 1), 1, maxCount);
        renderAll();
      });
      controls.append("button")
        .attr("type", "button")
        .text("Highlight")
        .on("click", () => setAnalysisHighlight(
          panel,
          combinedHighlight(visibleRows.map(disagreementHighlightPayload), `Top ${visibleRows.length} domain/range disagreement pairs`),
          false
        ));

      renderDisagreementScoreGraph(content, panel, visibleRows);

      if (!rankedRows.length) {
        content.append("div").attr("class", "analysis-hint").text("No domain/range disagreement examples were found.");
        return;
      }

      const list = content.append("div").attr("class", "analysis-list");
      visibleRows.slice(0, 12).forEach((item, index) => {
        const strongest = item.strongest_disagreement || {};
        const row = list.append("button").attr("type", "button").attr("class", "analysis-row");
        row.append("strong").text(`${index + 1}. ${item.source_label} -> ${item.target_label}: ${item.disagreement_count}/${item.compared_sources} disagree`);
        row.append("span").text(`max score ${formatScore(item.max_disagreement_score)}, strongest S${strongest.source_sheet_id ?? "-"}: range S${strongest.shape_target_sheet_id ?? "-"}, domain S${strongest.overlap_target_sheet_id ?? "-"}`);
        row.on("click", () => toggleDisagreementGraphSelection(panel, item));
      });
      return;
    }
  }



  function panelTheta(panel) {
    ensurePanelAnalysis(panel);
    const theta = Number(panel?.analysis?.theta);
    return Number.isFinite(theta) ? theta : defaultAnalysisTheta();
  }

  function continuationScore(match, panel) {
    if (!match) return null;
    return combinedShapeScore(match.metrics || {}, panel?.shapeWeights || cloneDefaultShapeWeights());
  }

  function continuationStatus(score, theta) {
    if (!Number.isFinite(Number(score))) return "N/A";
    return Number(score) >= Number(theta) ? "strong" : "weak";
  }

  function formatContinuation(score, theta) {
    if (!Number.isFinite(Number(score))) return "N/A";
    return `${formatScore(score)} (${continuationStatus(score, theta)} at theta ${formatScore(theta)})`;
  }

  function continuationLinkInfo(link, panel) {
    const key = linkKeyFromDatum(link);
    const match = shapeMatchLookup.get(key) || (link?.metrics?.shape_iou !== undefined ? link : null);
    const theta = panelTheta(panel);
    const score = continuationScore(match, panel);
    return {
      theta,
      score,
      status: continuationStatus(score, theta),
      text: formatContinuation(score, theta),
    };
  }

  function bestContinuationForNode(node, direction, panel) {
    if (!node) return { theta: panelTheta(panel), score: null, text: "N/A" };
    const key = nodeKeyFromDatum(node);
    const matches = direction === "incoming"
      ? (shapeIncomingByNode.get(key) || [])
      : (shapeOutgoingByNode.get(key) || []);
    const theta = panelTheta(panel);
    let best = null;
    let bestScore = null;
    for (const match of matches) {
      const score = continuationScore(match, panel);
      if (!Number.isFinite(Number(score))) continue;
      if (bestScore === null || score > bestScore) {
        best = match;
        bestScore = score;
      }
    }
    if (!best) return { theta, score: null, text: "N/A" };
    const otherSheet = direction === "incoming" ? best.source_sheet_id : best.target_sheet_id;
    const otherLabel = direction === "incoming" ? best.source_label : best.target_label;
    return {
      theta,
      score: bestScore,
      status: continuationStatus(bestScore, theta),
      text: `${formatContinuation(bestScore, theta)} ${direction === "incoming" ? "from" : "to"} S${otherSheet} (${otherLabel})`,
      match: best,
    };
  }

  function ensurePanelMetric(panel) {
    ensurePanelShapeWeights(panel);
    ensurePanelHybridConfig(panel);
    panel.panelHeight = clampPanelHeight(panel.panelHeight);
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
    if (mode === "centroid_position") {
      return node.centroid_color || centroidColorFromCentroid(node.centroid);
    }
    if (mode === "centroid_axis_diagonal") {
      return centroidAxisDiagonalColorFromCentroid(node.centroid);
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

  function scoreOpacity(value, maxScore) {
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return 0.18 + ratio * 0.64;
  }

  function linkWidth(value, maxScore) {
    const ratio = maxScore > 0 ? clamp(value / maxScore, 0, 1) : 0;
    return linkMin + ratio * (linkMax - linkMin);
  }

  function shapeWeightLabel(metricId) {
    const labels = {
      shape_iou: "Shape",
      area_ratio: "Area",
      bbox_iou: "BBox",
      centroid_similarity: "Center"
    };
    return labels[metricId] || metricId;
  }

  function formatWeight(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "0";
    return (Math.round(n * 1000) / 1000).toString();
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

  function nodeImageLabel(node, kind, role = "") {
    const prefix = role ? `${role} ` : "";
    const timestep = node?.timestep_label || node?.stem || "timestep";
    return `${prefix}${kind} sheet ${node?.sheet_id ?? ""} (${timestep})`;
  }

  function zoomDataAttributes(src, label, pair = null) {
    if (!src) return "";
    if (pair) {
      return ` data-zoom-title="${escapeHtml(pair.title || label)}" data-zoom-left-src="${escapeHtml(pair.leftSrc || "")}" data-zoom-left-label="${escapeHtml(pair.leftLabel || "Source")}" data-zoom-right-src="${escapeHtml(pair.rightSrc || "")}" data-zoom-right-label="${escapeHtml(pair.rightLabel || "Target")}"`;
    }
    return ` data-zoom-src="${escapeHtml(src)}" data-zoom-label="${escapeHtml(label)}" data-zoom-title="${escapeHtml(label)}"`;
  }

  function nodeMediaStack(node, thumbClass = "", linkedPair = null) {
    if (!node) return "";
    const imageClass = thumbClass ? `${thumbClass} zoomable-image` : "";
    const classAttr = imageClass ? ` class="${imageClass}"` : "";
    const sheetLabel = nodeImageLabel(node, "Sheet image", linkedPair?.role || "");
    const fiberLabel = nodeImageLabel(node, "Fiber surface", linkedPair?.role || "");
    const sheetPair = linkedPair ? {
      title: "Sheet images",
      leftSrc: linkedPair.left?.thumbnail || "",
      leftLabel: nodeImageLabel(linkedPair.left, "Sheet image", "Source"),
      rightSrc: linkedPair.right?.thumbnail || "",
      rightLabel: nodeImageLabel(linkedPair.right, "Sheet image", "Target")
    } : null;
    const fiberPair = linkedPair ? {
      title: "Fiber surface images",
      leftSrc: linkedPair.left?.fiber_surface_image || "",
      leftLabel: nodeImageLabel(linkedPair.left, "Fiber surface", "Source"),
      rightSrc: linkedPair.right?.fiber_surface_image || "",
      rightLabel: nodeImageLabel(linkedPair.right, "Fiber surface", "Target")
    } : null;
    const sheetImage = node.thumbnail
      ? `<img${classAttr}${zoomDataAttributes(node.thumbnail, sheetLabel, sheetPair)} src="${escapeHtml(node.thumbnail)}" alt="${escapeHtml(sheetLabel)}">`
      : "";
    const fiberImage = node.fiber_surface_image
      ? `<img${classAttr}${zoomDataAttributes(node.fiber_surface_image, fiberLabel, fiberPair)} src="${escapeHtml(node.fiber_surface_image)}" alt="${escapeHtml(fiberLabel)}">`
      : "";
    if (!sheetImage && !fiberImage) return "";
    return `<div class="media-stack">${sheetImage}${fiberImage}</div>`;
  }

  function nodeTooltip(node, panel) {
    const image = nodeMediaStack(node);
    const incoming = bestContinuationForNode(node, "incoming", panel);
    const outgoing = bestContinuationForNode(node, "outgoing", panel);
    const imageFile = node.thumbnail ? imageFilename(node.thumbnail) : "N/A";
    const fiberImageFile = node.fiber_surface_image ? imageFilename(node.fiber_surface_image) : "N/A";
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
        <div>Best incoming continuation</div><div>${escapeHtml(incoming.text)}</div>
        <div>Best outgoing continuation</div><div>${escapeHtml(outgoing.text)}</div>
        <div>Centroid color</div><div>${colorSwatch(node.centroid_color)}</div>
        <div>RSI</div><div>${escapeHtml(rsiFile)}</div>
        <div>RSI JSON</div><div>${escapeHtml(rsijsonFile)}</div>
        <div>Image</div><div>${escapeHtml(imageFile || "N/A")}</div>
        <div>Fiber image</div><div>${escapeHtml(fiberImageFile || "N/A")}</div>
      </div>
      ${image}
    `;
  }

  function linkTooltip(link, panel) {
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const sourceImage = nodeMediaStack(sourceNode);
    const targetImage = nodeMediaStack(targetNode);
    const sourceRsi = pathFilename(link.source_rsi_file || sourceNode?.rsi_file) || "N/A";
    const targetRsi = pathFilename(link.target_rsi_file || targetNode?.rsi_file) || "N/A";
    const sourceRsijson = pathFilename(link.source_rsijson_file || sourceNode?.rsijson_file) || "N/A";
    const targetRsijson = pathFilename(link.target_rsijson_file || targetNode?.rsijson_file) || "N/A";
    const metricValueNow = metricValue(link, panel, panel.metricId);
    const continuation = continuationLinkInfo(link, panel);
    const metricMax = metricMaxForPanel(panel, panel.metricId);
    const metricRatio = metricMax > 0 ? clamp(metricValueNow / metricMax, 0, 1) : 0;
    const scoreRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, panel, metric.id)))}</div>`)
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
        <div>Continuation score</div><div>${escapeHtml(continuation.text)}</div>
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

  function colorSwatch(color) {
    const safe = /^#[0-9a-fA-F]{6}$/.test(String(color || "")) ? String(color) : "#6f9ed4";
    return `<span class="color-swatch" style="background:${safe}"></span>${escapeHtml(safe)}`;
  }

  function showNodeDetails(node, panel) {
    if (!detailsContent) return;
    const image = nodeMediaStack(node, "thumb");
    const incoming = bestContinuationForNode(node, "incoming", panel);
    const outgoing = bestContinuationForNode(node, "outgoing", panel);
    const imageFile = node.thumbnail ? imageFilename(node.thumbnail) : "N/A";
    const fiberImageFile = node.fiber_surface_image ? imageFilename(node.fiber_surface_image) : "N/A";
    const rawTable = scalarMetadataTable({
      node_id: node.node_id || "",
      sheet_id: node.sheet_id,
      rank: node.rank,
      timestep_index: node.timestep_index,
      timestep_label: node.timestep_label || "",
      stem: node.stem || "",
      area: node.area,
      num_vertices: node.num_vertices,
      analysis_theta: panelTheta(panel),
      best_incoming_continuation: incoming.text,
      best_outgoing_continuation: outgoing.text,
      rsi_file: node.rsi_file || "",
      rsijson_file: node.rsijson_file || "",
      thumbnail: node.thumbnail || "",
      fiber_surface_image: node.fiber_surface_image || "",
      bbox: formatArrayValue(node.bbox),
      centroid: formatArrayValue(node.centroid),
      centroid_color: node.centroid_color || "",
      centroid_color_position: formatArrayValue(node.centroid_color_position)
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
        <div>Best incoming continuation</div><div>${escapeHtml(incoming.text)}</div>
        <div>Best outgoing continuation</div><div>${escapeHtml(outgoing.text)}</div>
        <div>Centroid color</div><div>${colorSwatch(node.centroid_color)}</div>
        <div>RSI file</div><div>${escapeHtml(node.rsi_file || "N/A")}</div>
        <div>RSI JSON file</div><div>${escapeHtml(node.rsijson_file || "N/A")}</div>
        <div>Image file</div><div>${escapeHtml(imageFile)}</div>
        <div>Fiber image file</div><div>${escapeHtml(fiberImageFile)}</div>
      </div>
      ${rawTable}
    `;
  }

  function showLinkDetails(link, panel) {
    if (!detailsContent) return;
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const linkedPair = { left: sourceNode, right: targetNode };
    const sourceImage = nodeMediaStack(sourceNode, "thumb", { ...linkedPair, role: "Source" }) || "<p>No image</p>";
    const targetImage = nodeMediaStack(targetNode, "thumb", { ...linkedPair, role: "Target" }) || "<p>No image</p>";
    const selectedMetricLabel = metricLabel(panel.dataMode, panel.metricId);
    const selectedMetricValue = metricValue(link, panel, panel.metricId);
    const continuation = continuationLinkInfo(link, panel);
    const sourceImageFile = sourceNode?.thumbnail ? imageFilename(sourceNode.thumbnail) : "N/A";
    const targetImageFile = targetNode?.thumbnail ? imageFilename(targetNode.thumbnail) : "N/A";
    const sourceFiberImageFile = sourceNode?.fiber_surface_image ? imageFilename(sourceNode.fiber_surface_image) : "N/A";
    const targetFiberImageFile = targetNode?.fiber_surface_image ? imageFilename(targetNode.fiber_surface_image) : "N/A";
    const sourceRsi = link.source_rsi_file || sourceNode?.rsi_file || "";
    const targetRsi = link.target_rsi_file || targetNode?.rsi_file || "";
    const sourceRsijson = link.source_rsijson_file || sourceNode?.rsijson_file || "";
    const targetRsijson = link.target_rsijson_file || targetNode?.rsijson_file || "";
    const metricRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, panel, metric.id)))}</div>`)
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
      source_fiber_surface_image: sourceNode?.fiber_surface_image || "",
      target_fiber_surface_image: targetNode?.fiber_surface_image || "",
      selected_metric: selectedMetricLabel,
      selected_metric_value: selectedMetricValue,
      analysis_theta: continuation.theta,
      continuation_score: continuation.score,
      continuation_status: continuation.status,
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
        <div>Source fiber image file</div><div>${escapeHtml(sourceFiberImageFile)}</div>
        <div>Target fiber image file</div><div>${escapeHtml(targetFiberImageFile)}</div>
        <div>${escapeHtml(selectedMetricLabel)}</div><div>${escapeHtml(formatScore(selectedMetricValue))}</div>
        <div>Continuation score</div><div>${escapeHtml(continuation.text)}</div>
      </div>
      <div class="thumb-row link-media-row">
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
    const pairs = pairsForMode(panel.dataMode, panel);
    const ranges = normalizedRanges();
    const threshold = clamp(Number(thresholdPercent) || 0, 0, 100) / 100;
    const metricMax = metricMaxForPanel(panel, panel.metricId);
    const visible = new Set();
    for (const range of ranges.length ? ranges : [{ start: 0, end: timestepMax }]) {
      for (let t = range.start; t <= range.end; t += 1) visible.add(t);
    }

    for (const pair of pairs) {
      if (!visible.has(pair.source_timestep_index) || !visible.has(pair.target_timestep_index)) continue;
      for (const match of pair.matches) {
        const score = metricValue(match, panel, panel.metricId);
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
          width: linkWidth(score, metricMax),
          opacity: scoreOpacity(score, metricMax)
        });
      }
    }

    if (state.layoutControls.strongestOutgoingOnly) {
      const bestBySource = new Map();
      for (const edge of edges) {
        const sourceKey = `${edge.source_timestep_index}:${edge.source_sheet_id}`;
        const current = bestBySource.get(sourceKey);
        if (!current) {
          bestBySource.set(sourceKey, edge);
          continue;
        }
        const edgeScore = Number(edge.score) || 0;
        const currentScore = Number(current.score) || 0;
        if (edgeScore > currentScore + 1e-12) {
          bestBySource.set(sourceKey, edge);
          continue;
        }
        if (Math.abs(edgeScore - currentScore) <= 1e-12) {
          const edgeRank = Number.isFinite(+edge.target_rank) ? +edge.target_rank : Number.POSITIVE_INFINITY;
          const currentRank = Number.isFinite(+current.target_rank) ? +current.target_rank : Number.POSITIVE_INFINITY;
          if (edgeRank < currentRank) {
            bestBySource.set(sourceKey, edge);
            continue;
          }
          if (edgeRank === currentRank) {
            const edgeSheet = Number.isFinite(+edge.target_sheet_id) ? +edge.target_sheet_id : Number.POSITIVE_INFINITY;
            const currentSheet = Number.isFinite(+current.target_sheet_id) ? +current.target_sheet_id : Number.POSITIVE_INFINITY;
            if (edgeSheet < currentSheet) {
              bestBySource.set(sourceKey, edge);
            }
          }
        }
      }
      return edges.filter(edge => {
        const sourceKey = `${edge.source_timestep_index}:${edge.source_sheet_id}`;
        return bestBySource.get(sourceKey) === edge;
      });
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
    if (!Number.isFinite(n)) return DEFAULT_TOP_SHEETS;
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
      const metricMax = metricMaxForPanel(panel, panel.metricId);
      const hideIsolated = Boolean(state.layoutControls.hideIsolated);
      if (!hideIsolated) {
        if (view.nodeSelection) view.nodeSelection.style("display", null);
        view.linkSelection
          .style("display", d => {
            const normalized = metricMax > 0 ? metricValue(d, panel, panel.metricId) / metricMax : 0;
            return normalized >= threshold ? null : "none";
          })
          .style("pointer-events", d => {
            const normalized = metricMax > 0 ? metricValue(d, panel, panel.metricId) / metricMax : 0;
            return normalized >= threshold ? "all" : "none";
          });
        return;
      }

      const incidentNodes = new Set();
      view.linkSelection.each(function(d) {
        const normalized = metricMax > 0 ? metricValue(d, panel, panel.metricId) / metricMax : 0;
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

  function timestepFocusBounds(layout, start, end) {
    if (!layout?.visibleNodes?.length) return null;
    const minTime = Math.min(Number(start), Number(end));
    const maxTime = Math.max(Number(start), Number(end));
    if (!Number.isFinite(minTime) || !Number.isFinite(maxTime)) return null;
    const nodes = (layout.visibleNodes || []).filter(node => {
      const timestep = Number(node.timestep_index);
      return Number.isFinite(timestep) && timestep >= minTime && timestep <= maxTime;
    });
    if (!nodes.length) return null;
    const allNodes = layout.visibleNodes || [];
    const padX = 24;
    const padY = 20;
    const minX = (d3.min(nodes, d => Number(d.x0)) ?? 0) - padX;
    const maxX = (d3.max(nodes, d => Number(d.x1)) ?? 0) + padX;
    const minY = Math.max(0, (d3.min(allNodes, d => Number(d.y0)) ?? 0) - padY);
    const maxY = (d3.max(allNodes, d => Number(d.y1)) ?? layout.height ?? 0) + padY;
    if (![minX, maxX, minY, maxY].every(Number.isFinite)) return null;
    return { minX, maxX, minY, maxY };
  }

  function drawQueuedAnalysisFocusPulse(panel, layout, root, layoutBounds) {
    const pulse = state.analysisFocusPulse;
    if (!pulse || pulse.panelId !== panel?.id) return;
    const bounds = timestepFocusBounds(layout, pulse.start, pulse.end);
    state.analysisFocusPulse = null;
    if (!bounds) return;

    const currentFocus = camera?.getViewFocus?.();
    const fallbackY = ((layoutBounds?.minY ?? bounds.minY) + (layoutBounds?.maxY ?? bounds.maxY)) / 2;
    camera?.setViewFocus({
      x: (bounds.minX + bounds.maxX) / 2,
      y: currentFocus?.y ?? fallbackY
    });

    root.append("rect")
      .attr("class", "analysis-timestep-focus")
      .attr("x", bounds.minX)
      .attr("y", bounds.minY)
      .attr("width", Math.max(1, bounds.maxX - bounds.minX))
      .attr("height", Math.max(1, bounds.maxY - bounds.minY));
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

  function bindPanelResizeHandle(panel, canvas, svg, layoutBounds) {
    const handle = panel.container.append("div")
      .attr("class", "panel-resizer")
      .attr("title", "Drag to resize panel");
    const handleNode = handle.node();
    const canvasNode = canvas.node();
    if (!handleNode || !canvasNode) return;

    let dragState = null;
    const applyHeight = nextHeight => {
      panel.panelHeight = clampPanelHeight(nextHeight);
      canvas.style("height", `${panel.panelHeight}px`);
      if (svg && layoutBounds) {
        const minSvgHeight = Math.max(1, (layoutBounds.maxY - layoutBounds.minY) + 140);
        svg.attr("height", Math.max(minSvgHeight, panel.panelHeight));
      }
      scheduleViewportUpdate();
    };

    const finishDrag = event => {
      if (!dragState) return;
      dragState = null;
      handle.classed("active", false);
      try {
        if (handleNode.hasPointerCapture(event.pointerId)) {
          handleNode.releasePointerCapture(event.pointerId);
        }
      } catch (_) {}
    };

    handleNode.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      state.activePanelId = panel.id;
      const startFitZoom = layoutBounds ? fitZoomForBounds(layoutBounds, canvasNode) : null;
      dragState = {
        startY: event.clientY,
        startHeight: panel.panelHeight,
        startZoom: camera?.getZoomScale() ?? 1,
        startFitZoom: Number.isFinite(startFitZoom) && startFitZoom > 0 ? startFitZoom : null
      };
      handle.classed("active", true);
      handleNode.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    handleNode.addEventListener("pointermove", event => {
      if (!dragState) return;
      const dy = event.clientY - dragState.startY;
      applyHeight(dragState.startHeight + dy);
      if (layoutBounds && dragState.startFitZoom && camera) {
        const currentFit = fitZoomForBounds(layoutBounds, canvasNode);
        if (Number.isFinite(currentFit) && currentFit > 0) {
          const scaledZoom = dragState.startZoom * (currentFit / dragState.startFitZoom);
          camera.setZoomScale(scaledZoom);
        }
      }
      event.preventDefault();
    });

    handleNode.addEventListener("pointerup", finishDrag);
    handleNode.addEventListener("pointercancel", finishDrag);
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
      clearAnalysisSelectionsOnly(panel);
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
      clearAnalysisSelectionsOnly(panel);
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

    if (panel.dataMode === "shape" || panel.dataMode === "hybrid") {
      ensurePanelShapeWeights(panel);
      const weightControls = controls.append("div").attr("class", "shape-weight-controls");
      shapeScoreComponentIds.forEach((metricId, metricIndex) => {
        const item = weightControls.append("label").attr("class", "shape-weight-item");
        const labelText = panel.dataMode === "hybrid" && metricIndex === 0
          ? `Shape metric: combined | ${shapeWeightLabel(metricId)}`
          : shapeWeightLabel(metricId);
        item.append("span").text(labelText);
        const input = item.append("input")
          .attr("type", "number")
          .attr("min", 0)
          .attr("step", 0.01)
          .property("value", formatWeight(panel.shapeWeights[metricId]));
        window.ReebViewerCommon.bindCommittedNumberInput(input.node(), raw => {
          const value = Number(raw);
          if (!Number.isFinite(value) || value < 0) {
            input.property("value", formatWeight(panel.shapeWeights[metricId]));
            return;
          }
          panel.shapeWeights[metricId] = value;
          panel.shapeWeights = sanitizeShapeWeights(panel.shapeWeights);
          renderAll();
        });
      });
    }

    if (panel.dataMode === "hybrid") {
      ensurePanelShapeWeights(panel);
      ensurePanelHybridConfig(panel);
      const hybridControls = controls.append("div").attr("class", "hybrid-controls");

      const overlapMetricItem = hybridControls.append("label").attr("class", "hybrid-item");
      overlapMetricItem.append("span").text("Vertex overlap metric");
      const overlapMetricSelect = overlapMetricItem.append("select");
      overlapMetricIds.forEach(metricId => {
        overlapMetricSelect.append("option")
          .attr("value", metricId)
          .property("selected", metricId === panel.hybridVertexMetric)
          .text(metricLabel("overlap", metricId));
      });
      overlapMetricSelect.on("change", event => {
        panel.hybridVertexMetric = sanitizeHybridVertexMetric(event.target.value);
        renderAll();
      });

      const vertexWeightItem = hybridControls.append("label").attr("class", "hybrid-item");
      vertexWeightItem.append("span").text("Vertex overlap weight");
      const vertexWeightInput = vertexWeightItem.append("input")
        .attr("type", "number")
        .attr("min", 0)
        .attr("step", 0.01)
        .property("value", formatWeight(panel.hybridWeights.vertex_overlap));
      window.ReebViewerCommon.bindCommittedNumberInput(vertexWeightInput.node(), raw => {
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 0) {
          vertexWeightInput.property("value", formatWeight(panel.hybridWeights.vertex_overlap));
          return;
        }
        panel.hybridWeights.vertex_overlap = value;
        panel.hybridWeights = sanitizeHybridWeights(panel.hybridWeights);
        renderAll();
      });

      const shapeWeightItem = hybridControls.append("label").attr("class", "hybrid-item");
      shapeWeightItem.append("span").text("Shape combined weight");
      const shapeWeightInput = shapeWeightItem.append("input")
        .attr("type", "number")
        .attr("min", 0)
        .attr("step", 0.01)
        .property("value", formatWeight(panel.hybridWeights.shape_combined));
      window.ReebViewerCommon.bindCommittedNumberInput(shapeWeightInput.node(), raw => {
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 0) {
          shapeWeightInput.property("value", formatWeight(panel.hybridWeights.shape_combined));
          return;
        }
        panel.hybridWeights.shape_combined = value;
        panel.hybridWeights = sanitizeHybridWeights(panel.hybridWeights);
        renderAll();
      });
    }

    renderAnalysisPanel(container, panel);

    const canvas = container.append("div")
      .attr("class", "panel-canvas")
      .style("height", `${panel.panelHeight}px`);
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
      if (state.analysisFocusPulse?.panelId === panel.id) state.analysisFocusPulse = null;
      canvas.append("div").attr("class", "panel-empty").text("No timesteps in the selected range.");
      bindPanelResizeHandle(panel, canvas, null, null);
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
      linkSelection: null,
      nodeSelection: null
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
          ...TIMESTEP_LABEL_OPTIONS
        });
      });

    const activeLinkHighlights = highlightedLinkSet(panel);
    const activeNodeHighlights = highlightedNodeSet(panel);

    const linkSelection = root.append("g")
      .selectAll("path")
      .data(links, d => `${d.source_timestep_index}:${d.source_sheet_id}->${d.target_timestep_index}:${d.target_sheet_id}`)
      .join("path")
      .attr("class", "link global-link")
      .classed("analysis-highlight", d => activeLinkHighlights.has(linkKeyFromDatum(d)))
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
      .classed("analysis-highlight", d => activeNodeHighlights.has(nodeKeyFromDatum(d)))
      .on("mouseenter", function(event, d) {
        d3.select(this).classed("hover", true);
        updateTooltip(nodeTooltip(d, panel), event.clientX, event.clientY);
      })
      .on("mousemove", (event, d) => updateTooltip(nodeTooltip(d, panel), event.clientX, event.clientY))
      .on("mouseleave", function() {
        d3.select(this).classed("hover", false);
        hideTooltip();
      })
      .on("click", (_, d) => showNodeDetails(d, panel));

    state.panelViews.get(panel.id).nodeSelection = node;

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

    drawQueuedAnalysisFocusPulse(panel, layout, root, layoutBounds);

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

    bindPanelResizeHandle(panel, canvas, svg, layoutBounds);
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
    const strongestOutgoingNode = document.getElementById("strongestOutgoingOnly");

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

    drawCentroidColorLegend();
    updateCentroidColorLegendVisibility();

    if (nodeColorNode) {
      nodeColorNode.value = state.layoutControls.nodeColorMode;
      nodeColorNode.addEventListener("change", event => {
        state.layoutControls.nodeColorMode = event.target.value;
        drawCentroidColorLegend();
        updateCentroidColorLegendVisibility();
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

    if (strongestOutgoingNode) {
      strongestOutgoingNode.checked = Boolean(state.layoutControls.strongestOutgoingOnly);
      strongestOutgoingNode.addEventListener("change", event => {
        state.layoutControls.strongestOutgoingOnly = Boolean(event.target.checked);
        scheduleRenderAll();
      });
    }
  }

  function addPanel() {
    const activePanel = getPanelById(state.activePanelId);
    state.panels.push({
      id: state.nextPanelId++,
      dataMode: "shape",
      metricId: "combined",
      threshold: 0,
      shapeWeights: cloneDefaultShapeWeights(),
      analysis: activePanel?.analysis ? { ...activePanel.analysis, selectedIntervalKeys: [], selectedTrackKeys: [], selectedAgreementPointKeys: [], selectedDisagreementKeys: [], highlight: null } : null,
      panelHeight: clampPanelHeight(activePanel?.panelHeight ?? PANEL_HEIGHT_DEFAULT)
    });
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
  bindImageZoomViewer();
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

    if not TRACKING_DATA_FILE.exists():
        build_unified_sankey_data_stage()

    if UNIFIED_VIEWER_DIR.exists():
        shutil.rmtree(UNIFIED_VIEWER_DIR)
    UNIFIED_VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    link_sheet_images(UNIFIED_VIEWER_DIR)
    link_fiber_surface_images(UNIFIED_VIEWER_DIR)

    data = load_tracking_data()
    data["analysis"] = load_analysis_for_viewer()
    data_path = write_viewer_data_json(data)
    index_path = write_index_html()
    js_path = write_viewer_js()
    css_path = write_style_css()
    common_js_path = write_viewer_common_js(UNIFIED_VIEWER_DIR)

    print(f"Wrote unified sankey viewer: {UNIFIED_VIEWER_DIR}")
    for artifact in (data_path, index_path, js_path, css_path, common_js_path):
      print(f"  {artifact.name}")
    print("\nOpen with:")
    print(f"  cd {UNIFIED_VIEWER_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


def main() -> int:
    build_unified_sankey_viewer_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
