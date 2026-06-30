#!/usr/bin/env python3

"""Build a unified Sankey dashboard for range and domain overlap."""

from __future__ import annotations

import json
import math
import re
import shutil
import time
import argparse
from pathlib import Path

from common import (
    ANALYSIS_PLOT_DEFAULT_COLOR,
    ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY,
    ANALYSIS_PLOT_SELECTED_COLOR,
    ANALYSIS_PLOT_SELECTED_STROKE_COLOR,
    BASE_DIR,
    CENTROID_COLOR_CORNERS,
    FIBER_SURFACE_IMAGE_DIR,
    OUTPUT_DIR,
    OVERLAP_FILE,
    SHEET_IMAGE_DIR,
    SHAPE_SCORE_DEFAULT_WEIGHTS,
    SANKEY_TIMESTEP_STRIDE_MAX,
    TRACKING_ANALYSIS_EVENT_SCORE_TERMS,
    TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
    TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT,
    TRACKING_ANALYSIS_THRESHOLDS,
    TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
    TRACKING_ANALYSIS_TOP_FEATURES,
    TRACKING_ANALYSIS_TOP_INTERVALS,
    TRACKING_ANALYSIS_VIEWER_FILE,
    TRACKING_DATA_FILE,
    TIMESTEP_LABEL_TO_FS_DIVISOR,
    TIMESTEP_TIME_DIGITS,
    TIMESTEP_TIME_UNIT,
    UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY,
    tracking_analysis_event_score_formula_text,
    VIEWER_DEFAULT_TOP_SHEETS,
    dataset_config_for_base_dir,
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
PAPER_EXPORT_DIR = OUTPUT_DIR / "paper_exports"
FIGURE_PRESET_DIR = PAPER_EXPORT_DIR / "presets"
FIGURE_IMAGE_DIR = PAPER_EXPORT_DIR / "images"

SHAPE_METRICS = [
    {"id": "shape_iou", "label": "Sheet IoU", "field": "shape_iou"},
    {"id": "area_ratio", "label": "Sheet area ratio", "field": "area_ratio"},
    {"id": "bbox_iou", "label": "Sheet bbox overlap", "field": "bbox_iou"},
    {"id": "centroid_similarity", "label": "Sheet centroid distance", "field": "centroid_similarity"},
    {"id": "combined", "label": "Combined", "field": "final_score"},
]

OVERLAP_METRICS = [
    {"id": "overlap_vertices", "label": "Domain vertex count", "field": "overlap_vertices"},
    {"id": "overlap_source_percent", "label": "Source domain %", "field": "source_percent"},
    {"id": "overlap_target_percent", "label": "Target domain %", "field": "target_percent"},
    {"id": "overlap_max_percent", "label": "Max domain %", "field": "max_percent"},
]


DATA_MODES = [
    {
        "id": "shape",
        "label": "Range overlap",
        "pair_field": "shape_pairs",
        "default_metric": "shape_iou",
        "metrics": SHAPE_METRICS,
    },
    {
        "id": "overlap",
        "label": "Domain overlap",
        "pair_field": "overlap_pairs",
        "default_metric": "overlap_vertices",
        "metrics": OVERLAP_METRICS,
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


TIMESTEP_NUMBER_PATTERN = re.compile(r"(\d+)(?!.*\d)")


def timestep_numeric_value(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)

    match = TIMESTEP_NUMBER_PATTERN.search(str(value))
    if not match:
        return None
    return int(match.group(1))


def timestep_cache_sort_key(item: dict) -> tuple[int, int | str, str]:
    for key in ("timestep_index", "index"):
        numeric_value = timestep_numeric_value(item.get(key))
        if numeric_value is not None:
            return (0, numeric_value, str(item.get("stem", "")))

    for key in ("label", "stem"):
        numeric_value = timestep_numeric_value(item.get(key))
        if numeric_value is not None:
            return (1, numeric_value, str(item.get("stem", "")))

    return (2, str(item.get("label", "")), str(item.get("stem", "")))


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


def refresh_media_paths(data: dict, viewer_dir: Path) -> int:
    changed = 0
    for timestep in data.get("timesteps", []):
        stem = str(timestep.get("stem", ""))
        if not stem:
            continue
        for sheet in timestep.get("sheets", []):
            sheet_id = safe_int(sheet.get("sheet_id"))
            thumbnail = find_sheet_image(stem, sheet_id, viewer_dir)
            fiber_surface_image = find_fiber_surface_image(stem, sheet_id, viewer_dir)
            if sheet.get("thumbnail") != thumbnail:
                sheet["thumbnail"] = thumbnail
                changed += 1
            if sheet.get("fiber_surface_image") != fiber_surface_image:
                sheet["fiber_surface_image"] = fiber_surface_image
                changed += 1
    return changed


def load_timestep_cache(viewer_dir: Path) -> tuple[list[dict], float, int, tuple[float, float, float, float]]:
    if not TIMESTEP_CACHE_DIR.exists():
        raise FileNotFoundError(f"Timestep cache directory missing: {TIMESTEP_CACHE_DIR}")

    cache_items = [json.loads(path.read_text()) for path in TIMESTEP_CACHE_DIR.glob("*.json")]
    cache_items.sort(key=timestep_cache_sort_key)
    centroid_color_bounds: tuple[float, float, float, float] | None = None
    for data in cache_items:
        centroid_color_bounds = merge_bounds(centroid_color_bounds, safe_bounds(data.get("global_bounds")))
    if centroid_color_bounds is None:
        centroid_color_bounds = bounds_from_centroids(cache_items)
    centroid_color_bounds = origin_symmetric_bounds(centroid_color_bounds) or (-0.5, -0.5, 0.5, 0.5)

    timesteps: list[dict] = []
    max_area = 0.0
    max_vertices = 0

    for timestep_index, data in enumerate(cache_items):
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
                "timestep_index": timestep_index,
                "source_timestep_index": safe_int(data.get("timestep_index")),
                "label": str(data.get("label", "")),
                "stem": stem,
                "sheets": sheets,
            }
        )

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

    max_vertices = 0
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
            if overlap_node:
                sheet["num_vertices"] = safe_int(overlap_node.get("num_vertices"))
            max_vertices = max(max_vertices, safe_int(sheet.get("num_vertices")))
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
    timestep_index_by_stem = {
        str(item.get("stem", "")): safe_int(item.get("timestep_index"))
        for item in timesteps
        if item.get("stem")
    }
    timestep_index_by_label = {
        str(item.get("label", "")): safe_int(item.get("timestep_index"))
        for item in timesteps
        if item.get("label") != ""
    }

    def canonical_pair_index(pair: dict, side: str) -> int:
        stem = str(pair.get(f"{side}_stem", ""))
        if stem and stem in timestep_index_by_stem:
            return timestep_index_by_stem[stem]
        label = str(pair.get(f"{side}_label", ""))
        if label and label in timestep_index_by_label:
            return timestep_index_by_label[label]
        return safe_int(pair.get(f"{side}_timestep_index"))

    def build_shape_pairs(pairwise_matches):
        built_pairs = []
        for pair in pairwise_matches or []:
            source_timestep_index = canonical_pair_index(pair, "source")
            target_timestep_index = canonical_pair_index(pair, "target")
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

                source_num_vertices = safe_int(source_node.get("num_vertices")) if source_node else safe_int(match.get("source_num_vertices"))
                target_num_vertices = safe_int(target_node.get("num_vertices")) if target_node else safe_int(match.get("target_num_vertices"))
                matches.append(
                    {
                        "source_sheet_id": source_sheet_id,
                        "target_sheet_id": target_sheet_id,
                        "source_rank": safe_int(match.get("source_rank")),
                        "target_rank": safe_int(match.get("target_rank")),
                        "source_area": safe_float(match.get("source_area")),
                        "target_area": safe_float(match.get("target_area")),
                        "source_num_vertices": source_num_vertices,
                        "target_num_vertices": target_num_vertices,
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

            built_pairs.append(
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
        return built_pairs

    raw_shape_pairs_by_stride = match_data.get("pairwise_matches_by_stride")
    if isinstance(raw_shape_pairs_by_stride, dict):
        shape_pairs_by_stride = {
            str(safe_int(stride_key, 1)): build_shape_pairs(pairs or [])
            for stride_key, pairs in raw_shape_pairs_by_stride.items()
        }
    else:
        shape_pairs_by_stride = {"1": build_shape_pairs(match_data.get("pairwise_matches", []))}
    shape_pairs = shape_pairs_by_stride.get("1", [])

    overlap_node_index = {
        node.get("id"): node
        for node in overlap_data.get("nodes", [])
    }

    def build_overlap_pairs(overlap_links):
        pairs_by_key: dict[tuple[int, int], dict] = {}
        for link in overlap_links or []:
            source_index = safe_int(link.get("source_timestep_index"))
            target_index = safe_int(link.get("target_timestep_index"))
            key = (source_index, target_index)
            pair = pairs_by_key.get(key)
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
                pairs_by_key[key] = pair

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

        return sorted(
            pairs_by_key.values(),
            key=lambda item: (item["source_timestep_index"], item["target_timestep_index"]),
        )

    raw_overlap_links_by_stride = overlap_data.get("links_by_stride")
    if isinstance(raw_overlap_links_by_stride, dict):
        overlap_pairs_by_stride = {
            str(safe_int(stride_key, 1)): build_overlap_pairs(links or [])
            for stride_key, links in raw_overlap_links_by_stride.items()
        }
    else:
        overlap_pairs_by_stride = {"1": build_overlap_pairs(overlap_data.get("links", []))}
    overlap_pairs = overlap_pairs_by_stride.get("1", [])

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
            "tracking_analysis_thresholds": list(TRACKING_ANALYSIS_THRESHOLDS),
            "timestep_strides": list(range(1, max(1, safe_int(SANKEY_TIMESTEP_STRIDE_MAX, 1)) + 1)),
            "default_timestep_stride": 1,
            "max_timestep_stride": max(1, safe_int(SANKEY_TIMESTEP_STRIDE_MAX, 1)),
            "tracking_analysis_preferred_threshold": TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
            "tracking_analysis_top_intervals": TRACKING_ANALYSIS_TOP_INTERVALS,
            "tracking_analysis_top_features": TRACKING_ANALYSIS_TOP_FEATURES,
            "tracking_analysis_top_disagreements": TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
            "tracking_analysis_split_merge_weight": TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT,
            "tracking_analysis_event_score_terms": list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS),
            "timestep_label_to_fs_divisor": TIMESTEP_LABEL_TO_FS_DIVISOR,
            "timestep_time_unit": TIMESTEP_TIME_UNIT,
            "timestep_time_digits": TIMESTEP_TIME_DIGITS,
            "analysis_plot_default_color": ANALYSIS_PLOT_DEFAULT_COLOR,
            "analysis_plot_selected_color": ANALYSIS_PLOT_SELECTED_COLOR,
            "analysis_plot_selected_stroke_color": ANALYSIS_PLOT_SELECTED_STROKE_COLOR,
            "analysis_plot_deemphasis_transparency": ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY,
            "unsupported_link_default_transparency": UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY,
            "tracking_analysis_event_score_formula": tracking_analysis_event_score_formula_text(),
            "global_area_max": max_area,
            "global_vertex_max": max_vertices,
            "centroid_color_bounds": list(centroid_color_bounds),
            "centroid_color_corners": CENTROID_COLOR_CORNERS,
            "default_ranges": DEFAULT_RANGES,
            "viewer_default_top_sheets": max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10)),
            "node_height_fixed": 18,
            "link_thickness_min": 1.4,
            "link_thickness_max": 16,
        },
        "timesteps": timesteps,
        "shape_pairs": shape_pairs,
        "overlap_pairs": overlap_pairs,
        "shape_pairs_by_stride": shape_pairs_by_stride,
        "overlap_pairs_by_stride": overlap_pairs_by_stride,
        "viewer": {
            "generated_from": {
                "matches": str(MATCHES_FILE),
                "overlap": str(OVERLAP_FILE),
            },
            "sheet_image_dir": str(SHEET_IMAGE_DIR),
            "fiber_surface_image_dir": str(FIBER_SURFACE_IMAGE_DIR),
        },
    }


def write_json_file(data: dict, path: Path, *, compact: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False))
    else:
        path.write_text(json.dumps(data, indent=2, allow_nan=False))
    return path


def write_tracking_data_json(data: dict) -> Path:
    return write_json_file(data, TRACKING_DATA_FILE)


def compact_viewer_match(match: dict) -> dict:
    compact = {
        "source_sheet_id": safe_int(match.get("source_sheet_id")),
        "target_sheet_id": safe_int(match.get("target_sheet_id")),
        "source_rank": safe_int(match.get("source_rank")),
        "target_rank": safe_int(match.get("target_rank")),
        "metrics": match.get("metrics", {}),
    }
    return compact


def compact_viewer_pair(pair: dict) -> dict:
    compact = {
        "source_timestep_index": safe_int(pair.get("source_timestep_index")),
        "source_label": str(pair.get("source_label", "")),
        "source_stem": str(pair.get("source_stem", "")),
        "source_rsijson_file": str(pair.get("source_rsijson_file", "")),
        "source_rsi_file": str(pair.get("source_rsi_file", "")),
        "target_timestep_index": safe_int(pair.get("target_timestep_index")),
        "target_label": str(pair.get("target_label", "")),
        "target_stem": str(pair.get("target_stem", "")),
        "target_rsijson_file": str(pair.get("target_rsijson_file", "")),
        "target_rsi_file": str(pair.get("target_rsi_file", "")),
        "pair_count": safe_int(pair.get("pair_count")),
        "matches": [compact_viewer_match(match) for match in pair.get("matches", [])],
    }
    if pair.get("global_bounds"):
        compact["global_bounds"] = pair.get("global_bounds", [])
    return compact


def compact_viewer_data(data: dict) -> dict:
    compact = {
        key: value
        for key, value in data.items()
        if key not in {"shape_pairs", "overlap_pairs", "shape_pairs_by_stride", "overlap_pairs_by_stride"}
    }
    for field in ("shape_pairs_by_stride", "overlap_pairs_by_stride"):
        by_stride = data.get(field)
        if isinstance(by_stride, dict):
            compact[field] = {
                str(stride): [compact_viewer_pair(pair) for pair in pairs or []]
                for stride, pairs in by_stride.items()
            }
    return compact


def write_viewer_data_json(data: dict) -> Path:
    return write_json_file(
        compact_viewer_data(data),
        UNIFIED_VIEWER_DIR / "data.json",
        compact=True,
    )


def configure_dataset_paths(base_dir: Path) -> None:
    global BASE_DIR
    global OUTPUT_DIR
    global STORAGE_ROOT
    global TIMESTEP_CACHE_DIR
    global MATCHES_FILE
    global UNIFIED_VIEWER_DIR
    global TRACKING_DATA_FILE
    global TRACKING_ANALYSIS_VIEWER_FILE
    global SHEET_IMAGE_DIR
    global FIBER_SURFACE_IMAGE_DIR
    global OVERLAP_FILE
    global PAPER_EXPORT_DIR
    global FIGURE_PRESET_DIR
    global FIGURE_IMAGE_DIR
    global TIMESTEP_LABEL_TO_FS_DIVISOR
    global TIMESTEP_TIME_UNIT
    global TIMESTEP_TIME_DIGITS

    BASE_DIR = base_dir
    dataset_config = dataset_config_for_base_dir(BASE_DIR)
    TIMESTEP_LABEL_TO_FS_DIVISOR = float(dataset_config.get("timestep_label_to_fs_divisor", 41.341374575751))
    TIMESTEP_TIME_UNIT = str(dataset_config.get("timestep_time_unit", "fs"))
    TIMESTEP_TIME_DIGITS = int(dataset_config.get("timestep_time_digits", 2))
    OUTPUT_DIR = BASE_DIR / "sankey"
    STORAGE_ROOT = BASE_DIR / "compareSheetShapesCache"
    TIMESTEP_CACHE_DIR = STORAGE_ROOT / "cache" / "timesteps"
    MATCHES_FILE = STORAGE_ROOT / "results" / "sheet_shape_matches.json"
    UNIFIED_VIEWER_DIR = OUTPUT_DIR / "unified_sankey_viewer"
    TRACKING_DATA_FILE = OUTPUT_DIR / "tracking_data.json"
    TRACKING_ANALYSIS_VIEWER_FILE = OUTPUT_DIR / "tracking_analysis" / "viewer_analysis.json"
    SHEET_IMAGE_DIR = BASE_DIR / "sheetRendering"
    FIBER_SURFACE_IMAGE_DIR = BASE_DIR / "sheetFiberSurfaceImages"
    OVERLAP_FILE = OUTPUT_DIR / "sheet_overlaps.json"
    PAPER_EXPORT_DIR = OUTPUT_DIR / "paper_exports"
    FIGURE_PRESET_DIR = PAPER_EXPORT_DIR / "presets"
    FIGURE_IMAGE_DIR = PAPER_EXPORT_DIR / "images"


def dataset_arg_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def ensure_paper_export_dirs() -> None:
    FIGURE_PRESET_DIR.mkdir(parents=True, exist_ok=True)
    FIGURE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)




def apply_current_tracking_analysis_meta(data: dict) -> dict:
    meta = data.setdefault("meta", {})
    meta["data_modes"] = DATA_MODES
    meta["shape_score_components"] = list(SHAPE_SCORE_DEFAULT_WEIGHTS.keys())
    meta["shape_score_default_weights"] = SHAPE_SCORE_DEFAULT_WEIGHTS
    for removed_key in ("hybrid_score_default_weights", "hybrid_vertex_metric_default"):
        meta.pop(removed_key, None)
    meta["centroid_color_corners"] = CENTROID_COLOR_CORNERS
    meta["viewer_default_top_sheets"] = max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10))
    meta["tracking_analysis_thresholds"] = list(TRACKING_ANALYSIS_THRESHOLDS)
    available_stride_keys = set()
    for field in ("shape_pairs_by_stride", "overlap_pairs_by_stride"):
        by_stride = data.get(field)
        if isinstance(by_stride, dict):
            available_stride_keys.update(str(key) for key in by_stride.keys())
    if available_stride_keys:
        timestep_strides = sorted({max(1, safe_int(key, 1)) for key in available_stride_keys})
    else:
        timestep_strides = [1]
    meta["timestep_strides"] = timestep_strides
    meta["default_timestep_stride"] = 1
    meta["max_timestep_stride"] = max(timestep_strides)
    meta["tracking_analysis_preferred_threshold"] = TRACKING_ANALYSIS_PREFERRED_THRESHOLD
    meta["tracking_analysis_top_intervals"] = TRACKING_ANALYSIS_TOP_INTERVALS
    meta["tracking_analysis_top_features"] = TRACKING_ANALYSIS_TOP_FEATURES
    meta["tracking_analysis_top_disagreements"] = TRACKING_ANALYSIS_TOP_DISAGREEMENTS
    meta["tracking_analysis_split_merge_weight"] = TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT
    meta["tracking_analysis_event_score_terms"] = list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS)
    meta["tracking_analysis_event_score_formula"] = tracking_analysis_event_score_formula_text()
    meta["timestep_label_to_fs_divisor"] = TIMESTEP_LABEL_TO_FS_DIVISOR
    meta["timestep_time_unit"] = TIMESTEP_TIME_UNIT
    meta["timestep_time_digits"] = TIMESTEP_TIME_DIGITS
    meta["analysis_plot_default_color"] = ANALYSIS_PLOT_DEFAULT_COLOR
    meta["analysis_plot_selected_color"] = ANALYSIS_PLOT_SELECTED_COLOR
    meta["analysis_plot_selected_stroke_color"] = ANALYSIS_PLOT_SELECTED_STROKE_COLOR
    meta["analysis_plot_deemphasis_transparency"] = ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY
    meta["unsupported_link_default_transparency"] = UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY
    meta["paper_export_dirs"] = {
        "presets": str(FIGURE_PRESET_DIR),
        "images": str(FIGURE_IMAGE_DIR),
    }
    return data


def load_tracking_data() -> dict:
    if TRACKING_DATA_FILE.exists():
        data = json.loads(TRACKING_DATA_FILE.read_text())
    else:
        data = prepare_data(UNIFIED_VIEWER_DIR)
    refresh_media_paths(data, UNIFIED_VIEWER_DIR)
    return apply_current_tracking_analysis_meta(data)


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
    ensure_paper_export_dirs()
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
  <title>Reeb flow visualizer</title>
  <link rel="stylesheet" href="style.css?v={asset_version}">
</head>
<body>
  <header>
    <div class="title-block">
      <h1>Reeb flow visualizer</h1>
      <p>Explore time-varying reeb-space sheets with linked sankey, image, and analysis views.</p>
    </div>
    <div class="header-actions">
      <button id="zoomOut">Zoom out</button>
      <button id="zoomIn">Zoom in</button>
      <button id="centerView">Center sankey</button>
      <button id="saveFigurePreset">Save figure preset</button>
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
        <h2>Layout</h2>
        <label>
          Sorting
          <select id="orderingMode">
            <option value="crossings" selected>Weighted crossing-reduced</option>
            <option value="area">Sheet area</option>
            <option value="vertices">Domain vertex count</option>
          </select>
        </label>
        <label>
          Node height basis
          <select id="nodeSizeMode">
            <option value="vertices" selected>Domain vertex count</option>
          </select>
        </label>
        <label>
          Top sheets
          <input id="topSheets" type="number" min="1" step="1" value="{max(1, safe_int(VIEWER_DEFAULT_TOP_SHEETS, 10))}">
        </label>
        <label>
          Timestep sampling
          <select id="timestepStride"></select>
        </label>
        <label>
          Node color
          <select id="nodeColorMode">
            <option value="solid" selected>Solid</option>
            <option value="area">Sheet area</option>
            <option value="vertices">Domain vertex count</option>
            <option value="centroid_position">Sheet centroid</option>
          </select>
        </label>
        <div id="centroidColorLegend" class="centroid-color-legend" hidden>
          <div class="centroid-color-title">2D Centroid color</div>
          <div id="centroidYMax" class="centroid-axis-label"></div>
          <div class="centroid-color-row">
            <div id="centroidXMin" class="centroid-axis-label"></div>
            <canvas id="centroidColorCanvas" width="112" height="112" aria-label="Centroid color space"></canvas>
            <div id="centroidXMax" class="centroid-axis-label"></div>
          </div>
          <div id="centroidYMin" class="centroid-axis-label"></div>
        </div>
        <label>
          Link darkness
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
        <label class="inline">
          <input id="hideSheetLabels" type="checkbox">
          Hide sheet labels
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
html {
  zoom: 0.8;
}
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
#detailsContent .media-item {
  display: grid;
  gap: 4px;
}
#detailsContent .media-label {
  color: #5b6673;
  font-size: 11px;
  font-weight: 600;
}
#detailsContent .media-missing {
  align-items: center;
  background: #f5f7fa;
  border: 1px dashed #bdc7d2;
  border-radius: 6px;
  color: #6d7784;
  display: flex;
  font-size: 12px;
  min-height: 46px;
  padding: 8px;
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
.panel-controls .control-label {
  color: #5b6673;
  font-size: 12px;
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
  grid-template-columns: repeat(auto-fit, minmax(92px, 1fr));
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
.link.domain-support-strong {
  stroke: rgba(21, 128, 61, 0.82);
  stroke-width: 1.3px;
}
.link.domain-support-weak {
  stroke: rgba(220, 38, 38, 0.72);
  stroke-width: 1.2px;
  stroke-dasharray: 5 4;
}
.link.domain-support-missing {
  stroke: rgba(100, 116, 139, 0.68);
  stroke-width: 1.1px;
  stroke-dasharray: 2 4;
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
.analysis-toolbar .analysis-actions {
  align-items: flex-end;
}
.analysis-actions label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.panel-controls label.range-control,
.analysis-actions label.range-control {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
}
.range-control-header {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  font-size: 12px;
  color: #000;
  white-space: nowrap;
}
.range-control-body {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
}
.analysis-actions input[type="color"] {
  width: 34px;
  height: 26px;
  padding: 1px;
}
.analysis-actions input[type="range"] {
  width: 110px;
}
.analysis-title {
  font-weight: 700;
  color: #233040;
}
.analysis-subtitle {
  margin: 10px 0 4px;
  font-size: 12px;
  font-weight: 700;
  color: #475569;
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
  border-radius: 5px;
  background: #fff;
  padding: 8px 10px 4px;
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
.analysis-graph-axis path {
  stroke: #334155;
  stroke-width: 1.35px;
  shape-rendering: crispEdges;
}
.analysis-graph-axis line {
  stroke: none;
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
.analysis-graph-dot-count {
  fill: #fff;
  font-size: 10px;
  font-weight: 700;
  pointer-events: none;
}
.analysis-graph-hit {
  fill: transparent;
  cursor: pointer;
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
.analysis-row.active {
  border-color: #ef4444;
  background: #fef2f2;
}
.track-feature-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  align-items: start;
}
.track-feature-layout.has-chooser {
  grid-template-columns: minmax(0, 1fr) minmax(240px, 320px);
}
.track-feature-plot {
  min-width: 0;
}
.track-feature-chooser {
  border: 1px solid #d9e2ec;
  border-radius: 5px;
  background: #f8fafc;
  margin: 7px 0 8px;
  padding: 8px;
}
.track-feature-chooser-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 7px;
}
.track-feature-chooser-header strong {
  color: #1f2937;
  font-size: 12px;
}
.track-feature-chooser-header span {
  color: #536273;
  font-size: 12px;
}
.track-feature-chooser-list {
  display: grid;
  gap: 6px;
  max-height: 360px;
  overflow: auto;
}
.track-feature-chooser .analysis-row {
  width: 100%;
}
@media (max-width: 1500px) {
  main {
    grid-template-columns: 260px minmax(0, 1fr) 300px;
  }
  aside {
    padding: 10px;
  }
  #details h2 {
    padding: 10px 12px 7px;
  }
  #detailsContent {
    padding: 10px 12px;
  }
  #panelList {
    padding: 10px;
  }
  .panel {
    padding: 8px 8px 10px;
  }
  .panel-header {
    gap: 8px;
    align-items: flex-start;
  }
  .panel-controls {
    gap: 7px;
  }
  .panel-controls input[type="range"] {
    width: 120px;
  }
  .range-control-body {
    gap: 4px;
  }
  .range-control-body input[type="number"] {
    width: 62px;
  }
  .analysis-actions input[type="range"] {
    width: 96px;
  }
  .shape-weight-controls {
    grid-template-columns: repeat(auto-fit, minmax(78px, 1fr));
  }
}
@media (max-width: 1400px) {
  header {
    padding: 10px 12px;
  }
  .title-block p {
    display: none;
  }
  .header-actions {
    gap: 6px;
  }
  .header-actions button {
    padding: 5px 8px;
  }
  main {
    grid-template-columns: 240px minmax(0, 1fr) 270px;
  }
  .panel-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }
  .panel-title {
    justify-content: space-between;
  }
  .panel-controls {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: end;
    width: 100%;
  }
  .panel-controls > select,
  .panel-controls > input,
  .panel-controls > label {
    min-width: 0;
    width: 100%;
  }
  .panel-controls select,
  .panel-controls input[type="number"] {
    max-width: 100%;
  }
  .panel-controls input[type="range"] {
    width: 100%;
  }
  .range-control-body input[type="range"] {
    min-width: 80px;
    width: 100%;
  }
  .shape-weight-controls {
    grid-column: 1 / -1;
  }
}
@media (max-width: 1180px) {
  main {
    grid-template-columns: 220px minmax(0, 1fr) 250px;
  }
  .title-block {
    max-width: 280px;
  }
  .header-actions button {
    padding: 5px 7px;
  }
  .track-feature-layout.has-chooser {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 900px) {
  header {
    height: auto;
    min-height: 72px;
    flex-wrap: wrap;
  }
  main {
    height: auto;
    min-height: calc(100vh - 72px);
    grid-template-columns: minmax(0, 1fr);
  }
  #controls,
  #details {
    max-height: 260px;
  }
  .track-feature-layout {
    grid-template-columns: minmax(0, 1fr);
  }
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
  function defaultPanelHeightForViewport() {
    if (window.innerWidth > 1400) return 560;
    return Math.max(400, Math.min(560, Math.round(window.innerHeight * 0.58)));
  }
  function defaultAnalysisGraphHeightForViewport() {
    if (window.innerWidth > 1400) return 240;
    return Math.max(170, Math.min(240, Math.round(window.innerHeight * 0.25)));
  }
  const PANEL_HEIGHT_DEFAULT = defaultPanelHeightForViewport();
  const ANALYSIS_PLOT_DEFAULT_COLOR = String(data?.meta?.analysis_plot_default_color || "#6b7280");
  const ANALYSIS_PLOT_SELECTED_COLOR = String(data?.meta?.analysis_plot_selected_color || "#ef4444");
  const ANALYSIS_PLOT_SELECTED_STROKE_COLOR = String(data?.meta?.analysis_plot_selected_stroke_color || "#991b1b");
  const ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY = clamp(
    Number(data?.meta?.analysis_plot_deemphasis_transparency) || 0,
    0,
    100
  );
  const UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY = clamp(
    Number(data?.meta?.unsupported_link_default_transparency) || 0,
    0,
    100
  );
  const DEFAULT_TOP_SHEETS = Math.max(
    1,
    Math.floor(Number(data?.meta?.viewer_default_top_sheets) || 10)
  );
  const rawTimestepStrideOptions = Array.isArray(data?.meta?.timestep_strides) && data.meta.timestep_strides.length
    ? data.meta.timestep_strides
    : [1];
  function hasPairDataForStride(stride) {
    const key = String(Math.max(1, Math.floor(Number(stride) || 1)));
    if (key === "1" && (Array.isArray(data?.shape_pairs) || Array.isArray(data?.overlap_pairs))) return true;
    return Boolean(
      (data?.shape_pairs_by_stride && Array.isArray(data.shape_pairs_by_stride[key])) ||
      (data?.overlap_pairs_by_stride && Array.isArray(data.overlap_pairs_by_stride[key]))
    );
  }
  const timestepStrideOptions = [...new Set(rawTimestepStrideOptions
    .map(value => Math.max(1, Math.floor(Number(value) || 1)))
    .filter(value => Number.isFinite(value) && hasPairDataForStride(value)))]
    .sort((a, b) => a - b);
  if (!timestepStrideOptions.length) timestepStrideOptions.push(1);
  const defaultTimestepStride = timestepStrideOptions.includes(Number(data?.meta?.default_timestep_stride))
    ? Number(data.meta.default_timestep_stride)
    : timestepStrideOptions[0];
  const state = {
    ranges: (data.meta.default_ranges && data.meta.default_ranges.length ? data.meta.default_ranges : [{start: 0, end: 20}]).map(r => ({...r})),
    selectedRangeIndex: 0,
    timestepStride: defaultTimestepStride,
    panels: [{
      id: 1,
      dataMode: "shape",
      metricId: "shape_iou",
      threshold: 0,
      domainFilterMetricId: "overlap_vertices",
      rangeSupportMetricId: "shape_iou",
      domainSupportFilterMode: "all",
      rangeSupportFilterMode: "all",
      unsupportedLinkTransparency: UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY,
      panelHeight: PANEL_HEIGHT_DEFAULT
    }],
    nextPanelId: 2,
    rangeDrag: null,
    viewportDrag: null,
    tooltipLocked: false,
    detailsSelection: null,
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
      strongestOutgoingOnly: false,
      hideSheetLabels: false
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
  const configuredSplitMergeWeight = Number(data.meta?.tracking_analysis_split_merge_weight);
  const splitMergeWeight = Number.isFinite(configuredSplitMergeWeight) ? configuredSplitMergeWeight : 1;
  const metaEventScoreTerms = Array.isArray(data.meta?.tracking_analysis_event_score_terms) && data.meta.tracking_analysis_event_score_terms.length
    ? data.meta.tracking_analysis_event_score_terms
    : [
        { component: "source_weak_count", weight: 1 },
        { component: "target_weak_count", weight: 1 },
        { component: "possible_splits", weight: splitMergeWeight },
        { component: "possible_merges", weight: splitMergeWeight },
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
        split_merge_weight: splitMergeWeight,
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
  const VERTEX_THETA_QUANTILE = 0.5;
  const VERTEX_THETA_OPTION_QUANTILES = [0.25, 0.5, 0.75, 0.9];
  const vertexThetaCache = new Map();
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
  const vertexMetricDefault = overlapMetricIds.includes("overlap_max_percent")
    ? "overlap_max_percent"
    : (overlapMetricIds[0] || "overlap_max_percent");
  const domainFilterDefaultMetric = overlapMetricIds.includes("overlap_vertices")
    ? "overlap_vertices"
    : vertexMetricDefault;
  const rangeSupportDefaultMetric = shapeMetricIds.includes("shape_iou")
    ? "shape_iou"
    : (shapeMetricIds[0] || "shape_iou");
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
  const linkMin = data.meta.link_thickness_min || 1.4;
  const linkMax = data.meta.link_thickness_max || 16;
  const timestepMax = timestepLookup.maxIndex;
  const VIEWPORT_ANCHOR_Y = 0.50;
  const ZOOM_MIN = 0.005;
  const ZOOM_MAX = 20;
  const ZOOM_STEP = 1.2;
  const PAN_DRAG_THRESHOLD = 4;
  const PANEL_HEIGHT_MIN = 360;
  const PANEL_HEIGHT_MAX = 1400;
  const INTERVAL_GRAPH_HEIGHT_DEFAULT = defaultAnalysisGraphHeightForViewport();
  const INTERVAL_GRAPH_HEIGHT_MIN = 160;
  const INTERVAL_GRAPH_HEIGHT_MAX = 680;
  const IMAGE_ZOOM_MIN = 0.03;
  const IMAGE_ZOOM_MAX = 20;
  const timestepTimeDivisor = Number(data.meta?.timestep_label_to_fs_divisor);
  const timestepTimeDigits = Number(data.meta?.timestep_time_digits);
  const TIMESTEP_LABEL_OPTIONS = {
    divisor: Number.isFinite(timestepTimeDivisor) && timestepTimeDivisor > 0 ? timestepTimeDivisor : 41.341374575751,
    digits: Number.isFinite(timestepTimeDigits) ? timestepTimeDigits : 2,
    unit: data.meta?.timestep_time_unit || "fs",
  };
  const AGREEMENT_SERIES_COLORS = ["#1f6feb", "#dc2626", "#16a34a", "#f59e0b", "#7c3aed", "#0891b2", "#db2777", "#4b5563"];
  const ANALYSIS_DOT_RADIUS = 5.6;
  const ANALYSIS_DOT_SELECTED_RADIUS = 7.0;
  const ANALYSIS_DOT_HIT_RADIUS = 12;
  const TRACK_GRAPH_GROUP_PIXEL_SIZE = 18;

  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const shapePairLookup = new Map();
  const overlapPairLookup = new Map();
  const analysisRuntimeCache = new Map();
  const shapeMatchLookup = new Map();
  const overlapMatchLookup = new Map();
  const shapeOutgoingByNode = new Map();
  const shapeIncomingByNode = new Map();
  const overlapOutgoingByNode = new Map();
  const overlapIncomingByNode = new Map();

  function selectedStride() {
    const value = Math.max(1, Math.floor(Number(state.timestepStride) || 1));
    return timestepStrideOptions.includes(value) ? value : timestepStrideOptions[0];
  }

  function pairArrayForField(field, stride = selectedStride()) {
    const key = String(Math.max(1, Math.floor(Number(stride) || 1)));
    const byStride = data?.[`${field}_by_stride`];
    if (byStride && Array.isArray(byStride[key])) return byStride[key];
    if (key === "1" && Array.isArray(data?.[field])) return data[field];
    return [];
  }

  function clearPairLookups() {
    shapePairLookup.clear();
    overlapPairLookup.clear();
    shapeMatchLookup.clear();
    overlapMatchLookup.clear();
    shapeOutgoingByNode.clear();
    shapeIncomingByNode.clear();
    overlapOutgoingByNode.clear();
    overlapIncomingByNode.clear();
  }

  function populatePairLookupsForMode(modeId, pairLookup, matchLookup, outgoingByNode, incomingByNode) {
    for (const pair of pairsForMode(modeId)) {
      pairLookup.set(`${pair.source_timestep_index}:${pair.target_timestep_index}`, pair);
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
        matchLookup.set(key, enriched);
        const sourceKey = `${pair.source_timestep_index}:${match.source_sheet_id}`;
        const targetKey = `${pair.target_timestep_index}:${match.target_sheet_id}`;
        if (!outgoingByNode.has(sourceKey)) outgoingByNode.set(sourceKey, []);
        if (!incomingByNode.has(targetKey)) incomingByNode.set(targetKey, []);
        outgoingByNode.get(sourceKey).push(enriched);
        incomingByNode.get(targetKey).push(enriched);
      }
    }
  }

  function refreshPairLookups() {
    clearPairLookups();
    populatePairLookupsForMode("overlap", overlapPairLookup, overlapMatchLookup, overlapOutgoingByNode, overlapIncomingByNode);
    populatePairLookupsForMode("shape", shapePairLookup, shapeMatchLookup, shapeOutgoingByNode, shapeIncomingByNode);
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

    const clickedItem = target.closest(".media-item");
    const mediaKind = target.dataset.mediaKind || clickedItem?.dataset.mediaKind || "";
    if (mediaKind) {
      const title = mediaKind === "fiber" ? "Spatial context images" : "Sheet images";
      const images = Array.from(row.querySelectorAll(".media-stack")).map((item, index) => {
        const img = item.querySelector(`img.zoomable-image[data-media-kind="${mediaKind}"]`);
        if (!img) return null;
        return {
          src: img.currentSrc || img.src,
          label: img.dataset.zoomLabel || img.alt || (index === 0 ? "Source" : "Target")
        };
      }).filter(Boolean);
      return { images: images.length > 1 ? images : [], title };
    }

    const clickedStackImages = Array.from(stack.querySelectorAll("img.zoomable-image"));
    const mediaIndex = clickedStackImages.indexOf(target);
    if (mediaIndex < 0) return { images: [], title: "" };

    const title = mediaIndex === 0 ? "Sheet images" : "Spatial context images";
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
        const rgb = centroidRgbFromUnit(tx, ty);
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
      title.textContent = "2D centroid color";
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
    legend.hidden = state.layoutControls.nodeColorMode !== "centroid_position";
  }

  function sanitizeShapeWeights(weights) {
    const source = weights && typeof weights === "object" ? weights : shapeScoreDefaultWeightsRaw;
    const next = {};
    for (const metricId of shapeScoreComponentIds) {
      const fallback = Number(shapeScoreDefaultWeightsRaw?.[metricId]);
      const raw = Number(source?.[metricId]);
      const value = Number.isFinite(raw)
        ? raw
        : (Number.isFinite(fallback) ? fallback : (metricId === "shape_iou" ? 1 : 0));
      next[metricId] = Math.max(0, value);
    }
    return next;
  }

  function cloneDefaultShapeWeights() {
    return sanitizeShapeWeights(shapeScoreDefaultWeightsRaw);
  }

  function ensurePanelShapeWeights(panel) {
    if (!panel || panel.dataMode !== "shape") return;
    panel.shapeWeights = sanitizeShapeWeights(panel.shapeWeights);
  }

  function singleActiveShapeWeightId(weights) {
    let activeMetricId = "";
    for (const metricId of shapeScoreComponentIds) {
      const weight = Math.max(0, Number(weights?.[metricId]) || 0);
      if (weight <= 0) continue;
      if (activeMetricId) return "";
      activeMetricId = metricId;
    }
    return activeMetricId;
  }

  function combinedShapeScore(metrics, weights) {
    const singleMetricId = singleActiveShapeWeightId(weights);
    if (singleMetricId) return Math.max(0, Number(metrics?.[singleMetricId]) || 0);
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
    return Number(link.metrics?.[id] ?? 0);
  }

  function metricMaxForPanel(panel, metricId = null) {
    const id = metricId || panel?.metricId || "";
    if (panel?.dataMode === "shape" && id === "combined") {
      const singleMetricId = singleActiveShapeWeightId(panel.shapeWeights || cloneDefaultShapeWeights());
      if (singleMetricId) return metricMaxima[singleMetricId] || 1;
      const pairs = pairsForMode("shape");
      let maxValue = 0;
      for (const pair of pairs) {
        for (const match of pair.matches || []) {
          maxValue = Math.max(maxValue, combinedShapeScore(match.metrics || {}, panel.shapeWeights || cloneDefaultShapeWeights()));
        }
      }
      return maxValue > 0 ? maxValue : 1;
    }
    return metricMaxima[id] || 1;
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

  function overlapSourceRetention(match) {
    const metrics = match?.metrics || match || {};
    const value = Number(metrics.overlap_source_percent ?? match?.source_percent ?? 0);
    return Number.isFinite(value) ? clamp(value / 100, 0, 1) : 0;
  }

  function overlapTargetInheritance(match) {
    const metrics = match?.metrics || match || {};
    const value = Number(metrics.overlap_target_percent ?? match?.target_percent ?? 0);
    return Number.isFinite(value) ? clamp(value / 100, 0, 1) : 0;
  }

  function bidirectionalDomainSupport(match) {
    return Math.min(overlapSourceRetention(match), overlapTargetInheritance(match));
  }

  function domainSupportForKey(key) {
    const match = overlapMatchLookup.get(key);
    if (!match) {
      return { match: null, sourceRetention: 0, targetInheritance: 0, bidirectional: 0, any: 0 };
    }
    const sourceRetention = overlapSourceRetention(match);
    const targetInheritance = overlapTargetInheritance(match);
    return {
      match,
      sourceRetention,
      targetInheritance,
      bidirectional: Math.min(sourceRetention, targetInheritance),
      any: Math.max(sourceRetention, targetInheritance),
    };
  }

  function domainSupportForLink(link) {
    return domainSupportForKey(linkKeyFromDatum(link));
  }

  function domainSupportClass(link, panel) {
    if (panel?.dataMode === "overlap") return "";
    const support = domainSupportForLink(link);
    if (!support.match) {
      return state.layoutControls.showMissingDomainSupport ? "domain-support-missing" : "";
    }
    if (support.bidirectional >= panelTheta(panel)) {
      return state.layoutControls.showStrongDomainSupport ? "domain-support-strong" : "";
    }
    return state.layoutControls.showWeakDomainSupport ? "domain-support-weak" : "";
  }

  function formatDomainSupport(support) {
    if (!support?.match) return "N/A";
    return `source ${formatScore(100 * support.sourceRetention)}%, target ${formatScore(100 * support.targetInheritance)}%`;
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

  function visibleTimestepIndexSet() {
    const ranges = normalizedRanges();
    const visible = new Set();
    const stride = selectedStride();
    for (const range of ranges.length ? ranges : [{ start: 0, end: timestepMax }]) {
      for (let t = range.start; t <= range.end; t += stride) visible.add(t);
    }
    return visible;
  }

  function visibleTimesteps() {
    const ranges = normalizedRanges();
    const active = ranges.length ? ranges : [{ start: 0, end: timestepMax }];
    const visible = [];
    const seen = new Set();
    const stride = selectedStride();
    for (const range of active) {
      for (let t = range.start; t <= range.end; t += stride) {
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

  function pairsForMode(modeId, _panel = null) {
    const field = pairFieldForMode(modeId);
    if (!field) return [];
    return pairArrayForField(field, selectedStride());
  }

  function metricLabel(modeId, metricId) {
    return (metricsForMode(modeId).find(metric => metric.id === metricId) || { label: metricId }).label;
  }

  function normalizeDomainFilterMetricId(metricId) {
    const id = String(metricId || "");
    if (overlapMetricIds.includes(id)) return id;
    return domainFilterDefaultMetric;
  }

  function normalizeRangeSupportMetricId(metricId) {
    const id = String(metricId || "");
    if (shapeMetricIds.includes(id)) return id;
    return rangeSupportDefaultMetric;
  }

  function normalizeBoolean(value) {
    return value === true || value === "true" || value === 1 || value === "1";
  }

  function normalizeHexColor(value, fallback = ANALYSIS_PLOT_DEFAULT_COLOR) {
    const text = String(value || "").trim();
    return /^#[0-9a-fA-F]{6}$/.test(text) ? text : fallback;
  }

  function normalizeSupportFilterMode(value, legacyBest = false) {
    const mode = String(value || "");
    if (["all", "outgoing", "incoming", "both"].includes(mode)) return mode;
    if (legacyBest || mode === "best_domain_support" || mode === "one_to_one") return "both";
    return "all";
  }

  function ensurePanelSupportFilters(panel) {
    if (!panel) return;
    panel.domainFilterMetricId = normalizeDomainFilterMetricId(panel.domainFilterMetricId);
    panel.rangeSupportMetricId = normalizeRangeSupportMetricId(panel.rangeSupportMetricId);
    panel.domainSupportFilterMode = normalizeSupportFilterMode(
      panel.domainSupportFilterMode,
      normalizeBoolean(panel.useBestDomainSupport) ||
        panel.domainFilterMode === "best_domain_support" ||
        panel.domainFilterMode === "one_to_one"
    );
    panel.rangeSupportFilterMode = normalizeSupportFilterMode(
      panel.rangeSupportFilterMode,
      normalizeBoolean(panel.useBestRangeSupport)
    );
    panel.unsupportedLinkTransparency = clamp(
      Number(panel.unsupportedLinkTransparency ?? UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY),
      0,
      100
    );
  }

  function thresholdKey(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return String(value ?? "");
    return numeric.toFixed(6).replace(/0+$/, "").replace(/[.]$/, "");
  }

  function overlapScoreForTheta(match, metricId) {
    const metrics = match?.metrics || match || {};
    const sourcePercent = Number(metrics.overlap_source_percent ?? match?.source_percent ?? 0) || 0;
    const targetPercent = Number(metrics.overlap_target_percent ?? match?.target_percent ?? 0) || 0;
    const fallback = Math.max(sourcePercent, targetPercent);
    const raw = Number(metrics[metricId] ?? metrics.overlap_max_percent ?? fallback);
    if (!Number.isFinite(raw)) return 0;
    if (String(metricId).includes("percent")) return clamp(raw / 100, 0, 1);
    const maxValue = Math.max(1e-12, Number(metricMaxima?.[metricId]) || 0);
    return maxValue > 0 ? clamp(raw / maxValue, 0, 1) : raw;
  }

  function domainFilterScoreForLink(link, panel) {
    const domainMatch = overlapMatchLookup.get(linkKeyFromDatum(link));
    if (!domainMatch) return Number.NEGATIVE_INFINITY;
    const value = Number(domainMatch?.metrics?.[panel.domainFilterMetricId] ?? 0);
    return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
  }

  function rangeSupportScoreForLink(link, panel) {
    const rangeMatch = shapeMatchLookup.get(linkKeyFromDatum(link));
    if (!rangeMatch) return Number.NEGATIVE_INFINITY;
    const metrics = rangeMatch?.metrics || {};
    if (panel.rangeSupportMetricId === "combined") {
      return combinedShapeScore(metrics, panel.shapeWeights || cloneDefaultShapeWeights());
    }
    const value = Number(metrics[panel.rangeSupportMetricId] ?? 0);
    return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
  }

  function rankedSupportedEdges(edges, supportScoreFn) {
    const supportedEdges = edges.filter(edge => supportScoreFn(edge) > Number.NEGATIVE_INFINITY);
    return supportedEdges.slice().sort((a, b) => {
      const supportDiff = supportScoreFn(b) - supportScoreFn(a);
      if (Math.abs(supportDiff) > 1e-12) return supportDiff;
      const rangeDiff = (Number(b.score) || 0) - (Number(a.score) || 0);
      if (Math.abs(rangeDiff) > 1e-12) return rangeDiff;
      const aSourceRank = Number.isFinite(Number(a.source_rank)) ? Number(a.source_rank) : Number.POSITIVE_INFINITY;
      const bSourceRank = Number.isFinite(Number(b.source_rank)) ? Number(b.source_rank) : Number.POSITIVE_INFINITY;
      const aTargetRank = Number.isFinite(Number(a.target_rank)) ? Number(a.target_rank) : Number.POSITIVE_INFINITY;
      const bTargetRank = Number.isFinite(Number(b.target_rank)) ? Number(b.target_rank) : Number.POSITIVE_INFINITY;
      const sourceRankDiff = aSourceRank - bSourceRank;
      if (sourceRankDiff) return sourceRankDiff;
      const targetRankDiff = aTargetRank - bTargetRank;
      if (targetRankDiff) return targetRankDiff;
      const sourceIdDiff = (Number(a.source_sheet_id) || 0) - (Number(b.source_sheet_id) || 0);
      if (sourceIdDiff) return sourceIdDiff;
      return (Number(a.target_sheet_id) || 0) - (Number(b.target_sheet_id) || 0);
    });
  }

  function bestOutgoingSupportedEdges(edges, supportScoreFn) {
    const bestBySource = new Map();
    for (const edge of rankedSupportedEdges(edges, supportScoreFn)) {
      const sourceKey = `${edge.source_timestep_index}:${edge.source_sheet_id}`;
      if (!bestBySource.has(sourceKey)) bestBySource.set(sourceKey, edge);
    }
    return edges.filter(edge => bestBySource.get(`${edge.source_timestep_index}:${edge.source_sheet_id}`) === edge);
  }

  function bestIncomingSupportedEdges(edges, supportScoreFn) {
    const bestByTarget = new Map();
    for (const edge of rankedSupportedEdges(edges, supportScoreFn)) {
      const targetKey = `${edge.target_timestep_index}:${edge.target_sheet_id}`;
      if (!bestByTarget.has(targetKey)) bestByTarget.set(targetKey, edge);
    }
    return edges.filter(edge => bestByTarget.get(`${edge.target_timestep_index}:${edge.target_sheet_id}`) === edge);
  }

  function bestBothSupportedEdges(edges, supportScoreFn) {
    const supportedEdges = rankedSupportedEdges(edges, supportScoreFn);
    const groups = d3.group(supportedEdges, edge => `${edge.source_timestep_index}:${edge.target_timestep_index}`);
    const allowed = new Set();
    for (const groupEdges of groups.values()) {
      const usedSources = new Set();
      const usedTargets = new Set();
      for (const edge of groupEdges) {
        const sourceKey = `${edge.source_timestep_index}:${edge.source_sheet_id}`;
        const targetKey = `${edge.target_timestep_index}:${edge.target_sheet_id}`;
        if (usedSources.has(sourceKey) || usedTargets.has(targetKey)) continue;
        usedSources.add(sourceKey);
        usedTargets.add(targetKey);
        allowed.add(linkKeyFromDatum(edge));
      }
    }
    return edges.filter(edge => allowed.has(linkKeyFromDatum(edge)));
  }

  function filterSupportedEdges(edges, supportScoreFn, mode) {
    if (mode === "outgoing") return bestOutgoingSupportedEdges(edges, supportScoreFn);
    if (mode === "incoming") return bestIncomingSupportedEdges(edges, supportScoreFn);
    if (mode === "both") return bestBothSupportedEdges(edges, supportScoreFn);
    return edges;
  }

  function applySupportFilter(edges, panel) {
    ensurePanelSupportFilters(panel);
    if (panel?.dataMode === "shape") {
      return filterSupportedEdges(edges, edge => domainFilterScoreForLink(edge, panel), panel.domainSupportFilterMode);
    }
    if (panel?.dataMode === "overlap") {
      return filterSupportedEdges(edges, edge => rangeSupportScoreForLink(edge, panel), panel.rangeSupportFilterMode);
    }
    return edges;
  }

  function hasActiveSupportFilter(panel) {
    ensurePanelSupportFilters(panel);
    if (panel?.dataMode === "shape") return panel.domainSupportFilterMode !== "all";
    if (panel?.dataMode === "overlap") return panel.rangeSupportFilterMode !== "all";
    return false;
  }

  function appendUnsupportedLinkTransparencyControl(controls, panel) {
    ensurePanelSupportFilters(panel);
    const label = controls.append("label")
      .attr("class", "range-control")
      .attr("title", "Transparency for links rejected by the active support filter");
    const header = label.append("span").attr("class", "range-control-header");
    header.append("span").text("Unsupported link transparency");
    const body = label.append("span").attr("class", "range-control-body");
    const slider = body.append("input")
      .attr("type", "range")
      .attr("min", 0)
      .attr("max", 100)
      .attr("step", 1)
      .property("value", panel.unsupportedLinkTransparency);
    const valueLabel = body.append("span")
      .text(`${panel.unsupportedLinkTransparency}%`);
    slider.on("change", event => {
      panel.unsupportedLinkTransparency = clamp(Number(event.target.value) || 0, 0, 100);
      valueLabel.text(`${panel.unsupportedLinkTransparency}%`);
      scheduleRenderAll();
    });
  }

  function vertexThetaStats(metricId = vertexMetricDefault) {
    const id = overlapMetricIds.includes(metricId) ? metricId : vertexMetricDefault;
    const cacheId = `${selectedStride()}:${id}`;
    if (vertexThetaCache.has(cacheId)) return vertexThetaCache.get(cacheId);
    const scores = [];
    for (const pair of pairsForMode("overlap")) {
      const sourceIndex = Number(pair.source_timestep_index);
      const sourceSheets = timestepByIndex.get(sourceIndex)?.sheets || [];
      const bySource = groupMatchesByField(pair.matches || [], "source_sheet_id");
      for (const sheet of sourceSheets) {
        const matches = bySource.get(Number(sheet.sheet_id)) || [];
        let best = 0;
        for (const match of matches) best = Math.max(best, overlapScoreForTheta(match, id));
        scores.push(best);
      }
    }
    const sorted = scores.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    const quantile = fraction => {
      if (!sorted.length) return 0.5;
      const index = Math.max(0, Math.min(sorted.length - 1, Math.round(fraction * (sorted.length - 1))));
      return sorted[index];
    };
    const options = VERTEX_THETA_OPTION_QUANTILES
      .map(quantile)
      .filter(Number.isFinite)
      .filter((value, index, values) => values.findIndex(other => Math.abs(other - value) < 1e-6) === index)
      .sort((a, b) => a - b);
    const result = {
      metricId: id,
      count: sorted.length,
      defaultTheta: quantile(VERTEX_THETA_QUANTILE),
      options: options.length ? options : [0.5],
    };
    vertexThetaCache.set(cacheId, result);
    return result;
  }

  function analysisThresholdOptions(panel) {
    if (panel?.dataMode === "overlap") return vertexThetaStats(panel.metricId || vertexMetricDefault).options;
    return analysisThresholds.length ? analysisThresholds : [0.5];
  }

  function defaultAnalysisTheta(panel = null) {
    if (panel?.dataMode === "overlap") return vertexThetaStats(panel.metricId || vertexMetricDefault).defaultTheta;
    const preferred = Number(analysisData?.preferred_threshold);
    if (Number.isFinite(preferred)) return preferred;
    return analysisThresholds.length ? analysisThresholds[0] : 0.5;
  }

  function ensurePanelAnalysis(panel) {
    if (!panel.analysis) {
      panel.analysis = {
        tab: "intervals",
        theta: defaultAnalysisTheta(panel),
        topIntervals: 0,
        topBestSupportedIntervals: Math.min(5, Math.max(1, Number(analysisData?.top_intervals) || 5)),
        topFeatures: Math.min(5, Math.max(1, Number(analysisData?.top_features) || 5)),
        topDomainStability: Math.min(12, Math.max(1, Number(analysisData?.top_intervals) || 12)),
        topDisagreements: Math.min(12, Math.max(1, Number(analysisData?.top_disagreements ?? data.meta?.tracking_analysis_top_disagreements) || 12)),
        plotColor: ANALYSIS_PLOT_DEFAULT_COLOR,
        deEmphasisTransparency: ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY,
        intervalGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        intervalGraphZoomScale: 1,
        intervalGraphFocus: null,
        intervalGraphKey: "",
        trackGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        trackGraphZoomScale: 1,
        trackGraphFocus: null,
        trackGraphKey: "",
        trackChooserGroupKey: "",
        domainStabilityGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        domainStabilityGraphZoomScale: 1,
        domainStabilityGraphFocus: null,
        domainStabilityGraphKey: "",
        disagreementGraphHeight: INTERVAL_GRAPH_HEIGHT_DEFAULT,
        disagreementGraphZoomScale: 1,
        disagreementGraphFocus: null,
        disagreementGraphKey: "",
        selectedDisagreementKeys: [],
        selectedDomainStabilityKeys: [],
        selectedIntervalKeys: [],
        selectedTrackKeys: [],
        highlight: null,
      };
    }
    const thetaOptions = analysisThresholdOptions(panel);
    if (!thetaOptions.some(value => Math.abs(value - Number(panel.analysis.theta)) < 1e-6)) {
      panel.analysis.theta = defaultAnalysisTheta(panel);
    }
    panel.analysis.topIntervals = Math.max(0, Math.floor(Number(panel.analysis.topIntervals) || 0));
    panel.analysis.topBestSupportedIntervals = Math.max(1, Math.floor(Number(panel.analysis.topBestSupportedIntervals) || panel.analysis.topIntervals || 1));
    panel.analysis.topFeatures = Math.max(1, Math.floor(Number(panel.analysis.topFeatures) || 1));
    panel.analysis.topDomainStability = Math.max(1, Math.floor(Number(panel.analysis.topDomainStability) || 1));
    panel.analysis.topDisagreements = Math.max(1, Math.floor(Number(panel.analysis.topDisagreements) || 1));
    panel.analysis.plotColor = normalizeHexColor(panel.analysis.plotColor);
    panel.analysis.deEmphasisTransparency = clamp(
      Number(panel.analysis.deEmphasisTransparency ?? ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY),
      0,
      100
    );
    panel.analysis.intervalGraphHeight = clampIntervalGraphHeight(panel.analysis.intervalGraphHeight);
    panel.analysis.intervalGraphZoomScale = Number.isFinite(Number(panel.analysis.intervalGraphZoomScale))
      ? Number(panel.analysis.intervalGraphZoomScale)
      : 1;
    panel.analysis.trackGraphHeight = clampIntervalGraphHeight(panel.analysis.trackGraphHeight);
    panel.analysis.trackGraphZoomScale = Number.isFinite(Number(panel.analysis.trackGraphZoomScale))
      ? Number(panel.analysis.trackGraphZoomScale)
      : 1;
    panel.analysis.domainStabilityGraphHeight = clampIntervalGraphHeight(panel.analysis.domainStabilityGraphHeight);
    panel.analysis.domainStabilityGraphZoomScale = Number.isFinite(Number(panel.analysis.domainStabilityGraphZoomScale))
      ? Number(panel.analysis.domainStabilityGraphZoomScale)
      : 1;
    panel.analysis.disagreementGraphHeight = clampIntervalGraphHeight(panel.analysis.disagreementGraphHeight);
    panel.analysis.disagreementGraphZoomScale = Number.isFinite(Number(panel.analysis.disagreementGraphZoomScale))
      ? Number(panel.analysis.disagreementGraphZoomScale)
      : 1;
    if (!Array.isArray(panel.analysis.selectedDisagreementKeys)) panel.analysis.selectedDisagreementKeys = [];
    if (!Array.isArray(panel.analysis.selectedDomainStabilityKeys)) panel.analysis.selectedDomainStabilityKeys = [];
    if (!Array.isArray(panel.analysis.selectedIntervalKeys)) {
      panel.analysis.selectedIntervalKeys = Array.isArray(panel.analysis.selectedIntervals)
        ? panel.analysis.selectedIntervals.map(item => item?.intervalKey).filter(Boolean)
        : [];
    }
    if (!Array.isArray(panel.analysis.selectedTrackKeys)) panel.analysis.selectedTrackKeys = [];
    if (typeof panel.analysis.trackChooserGroupKey !== "string") panel.analysis.trackChooserGroupKey = "";
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
    if (mode === "shape" && metric === "combined") {
      const weights = sanitizeShapeWeights(panel?.shapeWeights);
      const singleMetricId = singleActiveShapeWeightId(weights);
      if (singleMetricId) return `${mode}:${singleMetricId}`;
      const weightKey = shapeScoreComponentIds
        .map(metricId => `${metricId}=${formatWeight(weights[metricId])}`)
        .join(",");
      return `${mode}:${metric}:${weightKey}`;
    }
    return `${mode}:${metric}`;
  }

  function analysisCacheKey(kind, panel, thetaOverride = null) {
    ensurePanelAnalysis(panel);
    const theta = thetaOverride === null || thetaOverride === undefined ? panel.analysis.theta : thetaOverride;
    return `${kind}:stride${selectedStride()}:${analysisMetricKey(panel)}:${thresholdKey(theta)}`;
  }

  function supportFilterAnalysisKey(panel) {
    ensurePanelSupportFilters(panel);
    if (panel?.dataMode === "shape") {
      return `domain-support:${panel.domainSupportFilterMode}:${panel.domainFilterMetricId}`;
    }
    if (panel?.dataMode === "overlap") {
      return `range-support:${panel.rangeSupportFilterMode}:${panel.rangeSupportMetricId}`;
    }
    return "support:none";
  }

  function supportFilterDescription(panel) {
    ensurePanelSupportFilters(panel);
    if (panel?.dataMode === "shape") {
      return `Domain support: ${supportFilterModeLabel(panel.domainSupportFilterMode)} / ${metricLabel("overlap", panel.domainFilterMetricId)}`;
    }
    if (panel?.dataMode === "overlap") {
      return `Range support: ${supportFilterModeLabel(panel.rangeSupportFilterMode)} / ${metricLabel("shape", panel.rangeSupportMetricId)}`;
    }
    return "No support filter";
  }

  function supportFilterModeLabel(mode) {
    if (mode === "outgoing") return "Best supported outgoing links";
    if (mode === "incoming") return "Best supported incoming links";
    if (mode === "both") return "Best supported incoming and outgoing links";
    return "All links";
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
      return analysisCombinedScore(match, panel);
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

  function analysisEdgesForPair(panel, pair, supportFiltered = false) {
    const edges = (pair?.matches || []).map(match => ({
      ...match,
      source_timestep_index: pair.source_timestep_index,
      source_label: pair.source_label,
      source_stem: pair.source_stem || "",
      target_timestep_index: pair.target_timestep_index,
      target_label: pair.target_label,
      target_stem: pair.target_stem || "",
      score: panelAnalysisScore(match, panel),
    }));
    return supportFiltered ? applySupportFilter(edges, panel) : edges;
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

  function analysisCombinedScore(match, panel = null) {
    const metrics = match?.metrics || match || {};
    return combinedShapeScore(metrics, panel?.shapeWeights || cloneDefaultShapeWeights());
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

  function normalizedOverlapMaxScore(match) {
    return clamp(overlapMaxPercent(match) / 100, 0, 1);
  }

  function overlapPercentForDirection(match, direction) {
    const metrics = match?.metrics || {};
    const value = direction === "backward"
      ? Number(metrics.overlap_target_percent ?? match?.target_percent ?? 0)
      : Number(metrics.overlap_source_percent ?? match?.source_percent ?? 0);
    return Number.isFinite(value) ? value : 0;
  }

  function normalizedOverlapScoreForDirection(match, direction) {
    return clamp(overlapPercentForDirection(match, direction) / 100, 0, 1);
  }

  function bestDirectionalOverlapMatch(matches, direction) {
    let best = null;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (const match of matches || []) {
      const score = overlapPercentForDirection(match, direction);
      if (score > bestScore) {
        best = match;
        bestScore = score;
      }
    }
    return best;
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
    const cacheKey = `domain-range-complementarity:stride${selectedStride()}`;
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const examples = [];
    const summary = [];
    const collectDirectionalDisagreements = (shapePair, overlapPair, direction) => {
      const sourceIndex = Number(shapePair.source_timestep_index);
      const targetIndex = Number(shapePair.target_timestep_index);
      const groupField = direction === "forward" ? "source_sheet_id" : "target_sheet_id";
      const choiceField = direction === "forward" ? "target_sheet_id" : "source_sheet_id";
      const comparedRole = direction === "forward" ? "source" : "target";
      const choiceRole = direction === "forward" ? "target" : "source";
      const shapeGroups = groupMatchesByField(shapePair.matches || [], groupField);
      const overlapGroups = groupMatchesByField(overlapPair.matches || [], groupField);
      let compared = 0;
      let agreements = 0;
      const directionExamples = [];

      for (const [comparedSheetIdRaw, overlapMatches] of overlapGroups.entries()) {
        const comparedSheetId = Number(comparedSheetIdRaw);
        const shapeMatches = shapeGroups.get(comparedSheetIdRaw) || shapeGroups.get(comparedSheetId) || [];
        const shapeBest = bestAnalysisMatch(shapeMatches);
        const overlapBest = bestDirectionalOverlapMatch(overlapMatches || [], direction);
        if (!shapeBest || !overlapBest) continue;

        compared += 1;
        const shapeChoice = Number(shapeBest?.[choiceField]);
        const overlapChoice = Number(overlapBest?.[choiceField]);
        const shapeScore = analysisCombinedScore(shapeBest);
        const overlapPercent = overlapPercentForDirection(overlapBest, direction);
        const overlapMax = overlapMaxPercent(overlapBest);
        const overlapScore = normalizedOverlapScoreForDirection(overlapBest, direction);
        if (shapeChoice === overlapChoice) {
          agreements += 1;
          continue;
        }

        const shapeForDomainChoice = shapeMatches.find(match => Number(match?.[choiceField]) === overlapChoice) || null;
        const overlapForRangeChoice = (overlapMatches || []).find(match => Number(match?.[choiceField]) === shapeChoice) || null;
        const shapeScoreForDomainChoice = shapeForDomainChoice ? analysisCombinedScore(shapeForDomainChoice) : 0;
        const overlapScoreForRangeChoice = overlapForRangeChoice ? normalizedOverlapScoreForDirection(overlapForRangeChoice, direction) : 0;
        const shapeLoss = Math.max(0, shapeScore - shapeScoreForDomainChoice);
        const overlapLoss = Math.max(0, overlapScore - overlapScoreForRangeChoice);
        const confidence = Math.min(shapeScore, overlapScore);
        const disagreementScore = 0.5 * (shapeLoss + overlapLoss) * confidence;
        const directionFields = direction === "forward"
          ? {
              source_sheet_id: comparedSheetId,
              shape_target_sheet_id: shapeChoice,
              overlap_target_sheet_id: overlapChoice,
              source_node: sheetKey(sourceIndex, comparedSheetId),
              shape_target_node: sheetKey(targetIndex, shapeChoice),
              overlap_target_node: sheetKey(targetIndex, overlapChoice),
              shape_link: linkKeyParts(sourceIndex, comparedSheetId, targetIndex, shapeChoice),
              overlap_link: linkKeyParts(sourceIndex, comparedSheetId, targetIndex, overlapChoice),
              highlight: {
                nodes: [
                  sheetKey(sourceIndex, comparedSheetId),
                  sheetKey(targetIndex, shapeChoice),
                  sheetKey(targetIndex, overlapChoice),
                ],
                links: [
                  linkKeyParts(sourceIndex, comparedSheetId, targetIndex, shapeChoice),
                  linkKeyParts(sourceIndex, comparedSheetId, targetIndex, overlapChoice),
                ],
              },
            }
          : {
              target_sheet_id: comparedSheetId,
              shape_source_sheet_id: shapeChoice,
              overlap_source_sheet_id: overlapChoice,
              target_node: sheetKey(targetIndex, comparedSheetId),
              shape_source_node: sheetKey(sourceIndex, shapeChoice),
              overlap_source_node: sheetKey(sourceIndex, overlapChoice),
              shape_link: linkKeyParts(sourceIndex, shapeChoice, targetIndex, comparedSheetId),
              overlap_link: linkKeyParts(sourceIndex, overlapChoice, targetIndex, comparedSheetId),
              highlight: {
                nodes: [
                  sheetKey(targetIndex, comparedSheetId),
                  sheetKey(sourceIndex, shapeChoice),
                  sheetKey(sourceIndex, overlapChoice),
                ],
                links: [
                  linkKeyParts(sourceIndex, shapeChoice, targetIndex, comparedSheetId),
                  linkKeyParts(sourceIndex, overlapChoice, targetIndex, comparedSheetId),
                ],
              },
            };
        const example = {
          id: `disagreement:${direction}:${sourceIndex}:${targetIndex}:${comparedSheetId}`,
          direction,
          source_timestep_index: sourceIndex,
          target_timestep_index: targetIndex,
          source_label: shapePair.source_label,
          target_label: shapePair.target_label,
          compared_sheet_role: comparedRole,
          compared_sheet_id: comparedSheetId,
          range_choice_role: choiceRole,
          domain_choice_role: choiceRole,
          range_choice_sheet_id: shapeChoice,
          domain_choice_sheet_id: overlapChoice,
          shape_score: shapeScore,
          domain_overlap_metric: direction === "backward" ? "overlap_target_percent" : "overlap_source_percent",
          domain_overlap_percent: overlapPercent,
          overlap_max_percent: overlapMax,
          overlap_score: overlapScore,
          shape_score_for_domain_choice: shapeScoreForDomainChoice,
          overlap_score_for_range_choice: overlapScoreForRangeChoice,
          shape_score_for_domain_target: shapeScoreForDomainChoice,
          overlap_score_for_range_target: overlapScoreForRangeChoice,
          shape_loss: shapeLoss,
          overlap_loss: overlapLoss,
          confidence,
          disagreement_score: disagreementScore,
          ...directionFields,
        };
        directionExamples.push(example);
      }

      directionExamples.sort((a, b) => Number(b.disagreement_score) - Number(a.disagreement_score));
      return {
        examples: directionExamples,
        compared,
        agreements,
        disagreements: directionExamples.length,
        maxScore: directionExamples.length ? Number(directionExamples[0].disagreement_score) || 0 : 0,
      };
    };

    for (const shapePair of pairsForMode("shape")) {
      const sourceIndex = Number(shapePair.source_timestep_index);
      const targetIndex = Number(shapePair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;
      const pairKey = `${sourceIndex}:${targetIndex}`;
      const overlapPair = overlapPairLookup.get(pairKey);
      if (!overlapPair) continue;

      const forward = collectDirectionalDisagreements(shapePair, overlapPair, "forward");
      const backward = collectDirectionalDisagreements(shapePair, overlapPair, "backward");
      const pairExamples = [...forward.examples, ...backward.examples];

      pairExamples.sort((a, b) => Number(b.disagreement_score) - Number(a.disagreement_score));
      const scores = pairExamples.map(item => Number(item.disagreement_score) || 0);
      const shapeLosses = pairExamples.map(item => Number(item.shape_loss) || 0);
      const overlapLosses = pairExamples.map(item => Number(item.overlap_loss) || 0);
      if (!pairExamples.length) continue;
      const compared = forward.compared + backward.compared;
      const agreements = forward.agreements + backward.agreements;
      summary.push({
        id: `disagreement_pair:${sourceIndex}:${targetIndex}`,
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: shapePair.source_label,
        target_label: shapePair.target_label,
        pair_label: `${shapePair.source_label || sourceIndex}->${shapePair.target_label || targetIndex}`,
        compared_count: compared,
        compared_sources: compared,
        forward_compared_sources: forward.compared,
        backward_compared_targets: backward.compared,
        agreement_count: agreements,
        forward_agreement_count: forward.agreements,
        backward_agreement_count: backward.agreements,
        disagreement_count: pairExamples.length,
        forward_disagreement_count: forward.disagreements,
        backward_disagreement_count: backward.disagreements,
        agreement_fraction: compared ? agreements / compared : 0,
        disagreement_fraction: compared ? pairExamples.length / compared : 0,
        forward_max_disagreement_score: forward.maxScore,
        backward_max_disagreement_score: backward.maxScore,
        max_disagreement_score: scores.length ? Math.max(...scores) : 0,
        mean_disagreement_score: meanNumber(scores),
        max_shape_loss: shapeLosses.length ? Math.max(...shapeLosses) : 0,
        mean_shape_loss: meanNumber(shapeLosses),
        max_overlap_loss: overlapLosses.length ? Math.max(...overlapLosses) : 0,
        mean_overlap_loss: meanNumber(overlapLosses),
        strongest_disagreement: pairExamples[0] || null,
      });
      examples.push(...pairExamples);
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

  function computeRuntimeIntervals(panel, thetaOverride = null, supportFiltered = false) {
    ensurePanelAnalysis(panel);
    const theta = Number(thetaOverride === null || thetaOverride === undefined ? panel.analysis.theta : thetaOverride);
    const cacheKey = supportFiltered
      ? `${analysisCacheKey("best-supported-intervals", panel, theta)}:${supportFilterAnalysisKey(panel)}`
      : analysisCacheKey("intervals", panel, theta);
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const rows = [];
    for (const pair of selectedAnalysisPairs(panel)) {
      const sourceIndex = Number(pair.source_timestep_index);
      const targetIndex = Number(pair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;

      const sourceSheets = timestepByIndex.get(sourceIndex)?.sheets || [];
      const targetSheets = timestepByIndex.get(targetIndex)?.sheets || [];
      const analysisEdges = analysisEdgesForPair(panel, pair, supportFiltered);
      const bySource = groupMatchesByField(analysisEdges, "source_sheet_id");
      const byTarget = groupMatchesByField(analysisEdges, "target_sheet_id");
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
      const supportKey = supportFiltered ? supportFilterAnalysisKey(panel) : "all-links";

      rows.push({
        id: `${supportFiltered ? "best_supported_interval_runtime" : "interval_runtime"}:${analysisMetricKey(panel)}:${supportKey}:${thresholdKey(theta)}:${sourceIndex}:${targetIndex}`,
        threshold: theta,
        analysis_mode: panel.dataMode,
        analysis_metric_id: panel.metricId,
        analysis_metric_label: metricLabel(panel.dataMode, panel.metricId),
        support_filtered: supportFiltered,
        support_filter_key: supportKey,
        support_filter_label: supportFiltered ? supportFilterDescription(panel) : "All links",
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: pair.source_label,
        target_label: pair.target_label,
        source_stem: pair.source_stem || "",
        target_stem: pair.target_stem || "",
        pair_label: `${pair.source_label || sourceIndex}->${pair.target_label || targetIndex}`,
        source_sheet_count: sourceSheetCount,
        target_sheet_count: targetSheets.length,
        candidate_match_count: supportFiltered ? analysisEdges.length : (Number(pair.pair_count ?? (pair.matches || []).length) || 0),
        raw_candidate_match_count: Number(pair.pair_count ?? (pair.matches || []).length) || 0,
        filtered_candidate_match_count: analysisEdges.length,
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

  function bestSupportedIntervals(panel) {
    if (!hasActiveSupportFilter(panel)) return [];
    return computeRuntimeIntervals(panel, null, true).ranked;
  }

  function activeIntervalRows(panel) {
    return panel?.analysis?.tab === "best-supported-intervals"
      ? bestSupportedIntervals(panel)
      : analysisIntervals(panel);
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
    const cacheKey = `sensitivity:stride${selectedStride()}:${analysisMetricKey(panel)}`;
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);
    const rows = analysisThresholdOptions(panel).map(threshold => {
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
        top_event_source_label: topEvent.source_label || "",
        top_event_target_label: topEvent.target_label || "",
        track_count: tracks.length,
        max_lifetime: lengths.length ? Math.max(...lengths) : 0,
        median_lifetime: quantileNumber(lengths, 0.5),
        mean_lifetime: meanNumber(lengths),
      };
    });
    analysisRuntimeCache.set(cacheKey, rows);
    return rows;
  }

  function sensitivityTopIntervalTimeLabel(row) {
    const sourceIndex = Number(row?.top_event_source_timestep_index);
    const targetIndex = Number(row?.top_event_target_timestep_index);
    if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) return "-";
    return formatIntervalFsRange({
      source_timestep_index: sourceIndex,
      target_timestep_index: targetIndex,
      source_label: row?.top_event_source_label || timestepLookup.labelAt(sourceIndex, String(sourceIndex)),
      target_label: row?.top_event_target_label || timestepLookup.labelAt(targetIndex, String(targetIndex)),
    }) || "-";
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

    const analysisEdges = analysisEdgesForPair(panel, pair, Boolean(item?.support_filtered));
    const bySource = groupMatchesByField(analysisEdges, "source_sheet_id");
    const byTarget = groupMatchesByField(analysisEdges, "target_sheet_id");
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

  function intervalRoleTimeFs(item, role = "source") {
    const index = Number(role === "target" ? item?.target_timestep_index : item?.source_timestep_index);
    const fsRaw = window.ReebViewerCommon.formatFsFromLabel(intervalTimestepLabel(item, role), TIMESTEP_LABEL_OPTIONS);
    const fs = Number(fsRaw);
    if (Number.isFinite(fs)) return fs;
    return Number.isFinite(index) ? index : 0;
  }

  function intervalTimeFs(item) {
    return intervalRoleTimeFs(item, "source");
  }

  function formatIntervalFs(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${numeric.toFixed(TIMESTEP_LABEL_OPTIONS.digits)} ${TIMESTEP_LABEL_OPTIONS.unit}` : "";
  }

  function formatIntervalFsRange(item) {
    const sourceTime = formatIntervalFs(intervalRoleTimeFs(item, "source"));
    const targetTime = formatIntervalFs(intervalRoleTimeFs(item, "target"));
    if (!sourceTime && !targetTime) return "";
    return `${sourceTime || "-"} -> ${targetTime || "-"}`;
  }

  function intervalGraphTooltip(item, panel) {
    const theta = panelTheta(panel);
    const scoreLabel = item?.support_filtered ? "Best supported event score" : "Event score";
    const candidateLabel = item?.support_filtered
      ? `${item.filtered_candidate_match_count ?? item.candidate_match_count ?? 0} / ${item.raw_candidate_match_count ?? item.candidate_match_count ?? 0}`
      : String(item.candidate_match_count ?? 0);
    return `
      <strong>${escapeHtml(intervalTimestepPrimary(item, "source"))} -> ${escapeHtml(intervalTimestepPrimary(item, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Time</div><div>${escapeHtml(formatIntervalFs(intervalTimeFs(item)))}</div>
        <div>${escapeHtml(scoreLabel)}</div><div>${escapeHtml(formatScore(item.event_score))}</div>
        <div>Analysis metric</div><div>${escapeHtml(item.analysis_metric_label || metricLabel(panel.dataMode, panel.metricId))}</div>
        ${item?.support_filtered ? `<div>Support filter</div><div>${escapeHtml(item.support_filter_label || supportFilterDescription(panel))}</div>` : ""}
        <div>Candidate links</div><div>${escapeHtml(candidateLabel)}</div>
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



  function trackGraphItemSort(a, b) {
    return Number(b.length) - Number(a.length) ||
      Number(b.mean_continuation_score) - Number(a.mean_continuation_score) ||
      Number(b.min_continuation_score) - Number(a.min_continuation_score) ||
      Number(a.track_id) - Number(b.track_id);
  }

  function formatTrackGroupCount(count) {
    return String(Math.max(0, Math.floor(Number(count) || 0)));
  }

  function formatTrackGroupRange(items, accessor, formatter = formatScore) {
    const values = (items || [])
      .map(accessor)
      .map(Number)
      .filter(Number.isFinite);
    if (!values.length) return "-";
    const minValue = d3.min(values);
    const maxValue = d3.max(values);
    if (Math.abs(minValue - maxValue) < 1e-12) return formatter(minValue);
    return `${formatter(minValue)}-${formatter(maxValue)}`;
  }

  function trackGraphGroupRows(rows, context) {
    const groups = new Map();
    (rows || []).forEach(item => {
      const x = Number(context.xValue(item));
      const y = Number(context.yValue(item));
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      const screenX = Number(context.xBase(x));
      const screenY = Number(context.yBase(y));
      const bucketX = Math.round(screenX / TRACK_GRAPH_GROUP_PIXEL_SIZE);
      const bucketY = Math.round(screenY / TRACK_GRAPH_GROUP_PIXEL_SIZE);
      const groupId = `${bucketX}:${bucketY}`;
      let group = groups.get(groupId);
      if (!group) {
        group = { groupKey: `track-group:${groupId}`, items: [], xSum: 0, ySum: 0 };
        groups.set(groupId, group);
      }
      group.items.push(item);
      group.xSum += x;
      group.ySum += y;
    });
    return Array.from(groups.values())
      .map(group => {
        group.items.sort(trackGraphItemSort);
        const count = group.items.length;
        const primary = group.items[0] || null;
        return {
          groupKey: group.groupKey,
          items: group.items,
          count,
          primary,
          length: count ? group.xSum / count : 0,
          mean_continuation_score: count ? group.ySum / count : 0,
        };
      })
      .sort((a, b) => Number(a.length) - Number(b.length) || Number(b.mean_continuation_score) - Number(a.mean_continuation_score));
  }

  function trackPlotKey(item) {
    return item?.groupKey || trackKeyFromItem(item);
  }

  function trackPlotIsActive(item, activeKeys) {
    const items = Array.isArray(item?.items) ? item.items : [item];
    return items.some(entry => activeKeys.has(trackKeyFromItem(entry)));
  }

  function trackPlotRadius(item, active) {
    const count = Math.max(1, Number(item?.count) || 1);
    if (count <= 1) return active ? ANALYSIS_DOT_SELECTED_RADIUS : ANALYSIS_DOT_RADIUS;
    const base = Math.min(20, ANALYSIS_DOT_RADIUS + Math.log2(count + 1));
    return active ? Math.max(base + 1.5, ANALYSIS_DOT_SELECTED_RADIUS) : base;
  }

  function trackPlotHitRadius(item) {
    return Math.max(ANALYSIS_DOT_HIT_RADIUS, trackPlotRadius(item, false) + 7);
  }

  function trackPlotTooltip(item, panel) {
    const items = Array.isArray(item?.items) ? item.items : [item];
    if (items.length <= 1) return trackGraphTooltip(items[0], panel);
    const theta = panelTheta(panel);
    return `
      <strong>${escapeHtml(items.length)} overlapping continuing features</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Feature length</div><div>${escapeHtml(formatTrackGroupRange(items, entry => entry.length, value => String(Math.round(value))))}</div>
        <div>Analysis metric</div><div>${escapeHtml(items[0]?.analysis_metric_label || metricLabel(panel.dataMode, panel.metricId))}</div>
        <div>Mean score range</div><div>${escapeHtml(formatTrackGroupRange(items, entry => entry.mean_continuation_score))}</div>
        <div>Min score range</div><div>${escapeHtml(formatTrackGroupRange(items, entry => entry.min_continuation_score))}</div>
        <div>Theta</div><div>${escapeHtml(formatScore(theta))}</div>
        <div>Action</div><div>Click to choose a feature</div>
      </div>`;
  }

  function openTrackPlotSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const items = Array.isArray(item?.items) ? item.items : [item];
    if (items.length <= 1) {
      panel.analysis.trackChooserGroupKey = "";
      toggleTrackGraphSelection(panel, items[0]);
      return;
    }
    panel.analysis.trackChooserGroupKey = item.groupKey || "";
    renderAll();
  }

  function trackChooserGroupFromPlotRows(panel, plotRows) {
    ensurePanelAnalysis(panel);
    const groupKey = panel.analysis.trackChooserGroupKey;
    if (!groupKey) return null;
    const group = (plotRows || []).find(item => item?.groupKey === groupKey && Number(item?.count) > 1);
    if (!group) {
      panel.analysis.trackChooserGroupKey = "";
      return null;
    }
    return group;
  }

  function renderTrackFeatureChooser(container, panel, group) {
    const items = Array.isArray(group?.items) ? group.items : [];
    if (items.length <= 1) return;
    const selectedKeys = selectedTrackKeySet(panel);
    const chooser = container.append("div").attr("class", "track-feature-chooser");
    const header = chooser.append("div").attr("class", "track-feature-chooser-header");
    const title = header.append("div");
    title.append("strong").text(`${items.length} Overlapping continuing features`);
    title.append("span")
      .style("display", "block")
      .text(`Length ${formatTrackGroupRange(items, entry => entry.length, value => String(Math.round(value)))} | Mean ${formatTrackGroupRange(items, entry => entry.mean_continuation_score)}`);
    header.append("button")
      .attr("type", "button")
      .text("Close")
      .on("click", () => {
        panel.analysis.trackChooserGroupKey = "";
        renderAll();
      });

    const list = chooser.append("div").attr("class", "track-feature-chooser-list");
    const rows = list.selectAll("button")
      .data(items, trackKeyFromItem)
      .join(enter => {
        const button = enter.append("button").attr("type", "button");
        button.append("strong");
        button.append("span");
        return button;
      });
    rows
      .attr("class", item => `analysis-row${selectedKeys.has(trackKeyFromItem(item)) ? " active" : ""}`)
      .on("mouseenter", (event, item) => updateTooltip(trackGraphTooltip(item, panel), event.clientX, event.clientY))
      .on("mousemove", (event, item) => updateTooltip(trackGraphTooltip(item, panel), event.clientX, event.clientY))
      .on("mouseleave", hideTooltip)
      .on("click", (event, item) => {
        event.stopPropagation();
        hideTooltip();
        toggleTrackGraphSelection(panel, item);
      });
    rows.select("strong")
      .text(item => `Feature ${item.track_id}: S${item.start_sheet_id} ${item.start_label} -> ${item.end_label}`);
    rows.select("span")
      .text(item => `Length ${item.length} | mean ${formatScore(item.mean_continuation_score)} | min ${formatScore(item.min_continuation_score)} | rank ${item.rank_min}/${item.rank_max}`);
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
    if (!valid.length) return null;

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
    const groupContext = { xBase, yBase, xValue, yValue };
    const plotRows = typeof options.groupRows === "function"
      ? options.groupRows(valid, groupContext)
      : valid;
    const plotKeyFn = options.plotKeyFn || keyFn;
    const plotXValue = options.plotXValue || xValue;
    const plotYValue = options.plotYValue || yValue;
    const plotTooltip = options.plotTooltip || options.tooltip;
    const plotClick = options.onPlotClick || options.onClick;
    const plotIsActive = options.isPlotRowActive || ((item, activeKeys) => activeKeys.has(keyFn(item)));
    const dotRadius = options.dotRadius || ((item, active) => active ? ANALYSIS_DOT_SELECTED_RADIUS : ANALYSIS_DOT_RADIUS);
    const hitRadius = options.hitRadius || (() => ANALYSIS_DOT_HIT_RADIUS);
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

    const xAxis = g.append("g")
      .attr("class", "analysis-graph-axis")
      .attr("transform", `translate(0,${innerHeight})`);
    const yAxis = g.append("g").attr("class", "analysis-graph-axis");
    const marks = g.append("g").attr("clip-path", `url(#${clipId})`);
    const lineLayer = options.drawLine ? marks.append("g").attr("class", "analysis-graph-lines") : null;
    const dotLayer = marks.append("g");
    const labelLayer = marks.append("g");
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
      const plotColor = normalizeHexColor(panel.analysis.plotColor);
      const deEmphasisOpacity = 1 - clamp(
        Number(panel.analysis.deEmphasisTransparency) || 0,
        0,
        100
      ) / 100;
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

      xAxis.call(d3.axisBottom(xAxisScale)
        .ticks(Math.min(8, valid.length))
        .tickSizeInner(0)
        .tickSizeOuter(0)
        .tickPadding(6)
        .tickFormat(options.xTickFormat || (value => Number(value).toFixed(2))));
      yAxis.call(d3.axisLeft(yAxisScale)
        .ticks(4)
        .tickSizeInner(0)
        .tickSizeOuter(0)
        .tickPadding(6)
        .tickFormat(options.yTickFormat || undefined));

      if (lineLayer) {
        lineLayer.selectAll("path.analysis-graph-line")
          .data(d3.pairs(valid), pair => `${keyFn(pair[0])}->${keyFn(pair[1])}`)
          .join("path")
          .attr("class", pair => {
            const active = activeKeys.has(keyFn(pair[0])) && activeKeys.has(keyFn(pair[1]));
            return `analysis-graph-line${active ? " active" : ""}`;
          })
          .attr("d", pair => d3.line()
            .x(d => screenXFromWorld(xBase(xValue(d)), stateForDraw))
            .y(d => screenYFromWorld(yBase(yValue(d)), stateForDraw))(pair))
          .style("stroke", plotColor)
          .style("opacity", pair => (
            activeKeys.has(keyFn(pair[0])) && activeKeys.has(keyFn(pair[1]))
              ? 1
              : deEmphasisOpacity
          ));
      }

      dotLayer.selectAll("circle.analysis-graph-dot")
        .data(plotRows, plotKeyFn)
        .join("circle")
        .attr("class", d => `analysis-graph-dot${plotIsActive(d, activeKeys) ? " active" : ""}${Array.isArray(d?.items) && Number(d?.count) > 1 ? " grouped" : ""}`)
        .attr("cx", d => screenXFromWorld(xBase(plotXValue(d)), stateForDraw))
        .attr("cy", d => screenYFromWorld(yBase(plotYValue(d)), stateForDraw))
        .attr("r", d => dotRadius(d, plotIsActive(d, activeKeys)))
        .style("fill", d => plotIsActive(d, activeKeys) ? ANALYSIS_PLOT_SELECTED_COLOR : plotColor)
        .style("stroke", d => plotIsActive(d, activeKeys) ? ANALYSIS_PLOT_SELECTED_STROKE_COLOR : "#ffffff")
        .style("opacity", d => plotIsActive(d, activeKeys) ? 1 : deEmphasisOpacity);

      labelLayer.selectAll("text.analysis-graph-dot-count")
        .data(plotRows.filter(d => Array.isArray(d?.items) && Number(d?.count) > 1), plotKeyFn)
        .join("text")
        .attr("class", "analysis-graph-dot-count")
        .attr("x", d => screenXFromWorld(xBase(plotXValue(d)), stateForDraw))
        .attr("y", d => screenYFromWorld(yBase(plotYValue(d)), stateForDraw) + 3.5)
        .attr("text-anchor", "middle")
        .style("opacity", d => plotIsActive(d, activeKeys) ? 1 : deEmphasisOpacity)
        .text(d => formatTrackGroupCount(d.count));

      hitLayer.selectAll("circle.analysis-graph-hit")
        .data(plotRows, plotKeyFn)
        .join("circle")
        .attr("class", "analysis-graph-hit")
        .attr("cx", d => screenXFromWorld(xBase(plotXValue(d)), stateForDraw))
        .attr("cy", d => screenYFromWorld(yBase(plotYValue(d)), stateForDraw))
        .attr("r", d => hitRadius(d))
        .on("mouseenter", (event, d) => updateTooltip(plotTooltip(d, panel), event.clientX, event.clientY))
        .on("mousemove", (event, d) => updateTooltip(plotTooltip(d, panel), event.clientX, event.clientY))
        .on("mouseleave", hideTooltip)
        .on("click", (event, d) => {
          event.stopPropagation();
          hideTooltip();
          plotClick(panel, d);
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
      isPanTarget: target => !target.closest(".analysis-graph-hit, .analysis-graph-dot, .analysis-graph-dot-count, button, input, select, label"),
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
    return { rows: valid, plotRows };
  }

  function renderIntervalScoreGraph(container, panel, rows, options = {}) {
    const graphId = options.id || "intervals";
    renderAnalysisPointGraph(container, panel, rows, {
      id: graphId,
      heightKey: "intervalGraphHeight",
      zoomKey: "intervalGraphZoomScale",
      focusKey: "intervalGraphFocus",
      graphKey: "intervalGraphKey",
      selectedKeysKey: "selectedIntervalKeys",
      keyFn: intervalKeyFromItem,
      stateKey: options.stateKey || (panel => analysisCacheKey("interval-graph", panel)),
      xValue: intervalTimeFs,
      yValue: item => Number(item.event_score),
      sort: (a, b) => intervalTimeFs(a) - intervalTimeFs(b),
      xLabel: "Time (fs)",
      yLabel: options.yLabel || "Event score",
      xTickFormat: value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits),
      drawLine: true,
      tooltip: options.tooltip || intervalGraphTooltip,
      selectedKeySet: selectedIntervalKeySet,
      aggregateHighlight: aggregateSelectedIntervalHighlight,
      onClick: toggleIntervalGraphSelection,
      resizeTitle: "Drag to resize interval plot"
    });
  }

  function renderTrackScoreGraph(container, panel, rows) {
    const layout = container.append("div").attr("class", "track-feature-layout");
    const plotPane = layout.append("div").attr("class", "track-feature-plot");
    const chooserPane = layout.append("div").attr("class", "track-feature-chooser-pane");
    const graphInfo = renderAnalysisPointGraph(plotPane, panel, rows, {
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
      xLabel: "Feature length",
      yLabel: "Mean score",
      xTickFormat: value => String(Math.round(Number(value))),
      yTickFormat: value => formatScore(value),
      drawLine: false,
      tooltip: trackGraphTooltip,
      plotTooltip: trackPlotTooltip,
      selectedKeySet: selectedTrackKeySet,
      aggregateHighlight: aggregateSelectedTrackHighlight,
      onClick: toggleTrackGraphSelection,
      onPlotClick: openTrackPlotSelection,
      groupRows: trackGraphGroupRows,
      plotKeyFn: trackPlotKey,
      plotXValue: item => Number(item.length),
      plotYValue: item => Number(item.mean_continuation_score),
      isPlotRowActive: trackPlotIsActive,
      dotRadius: trackPlotRadius,
      hitRadius: trackPlotHitRadius,
      resizeTitle: "Drag to resize continuing-feature plot"
    });
    const chooserGroup = trackChooserGroupFromPlotRows(panel, graphInfo?.plotRows || []);
    layout.classed("has-chooser", Boolean(chooserGroup));
    if (chooserGroup) {
      renderTrackFeatureChooser(chooserPane, panel, chooserGroup);
    } else {
      chooserPane.remove();
    }
  }

  function computeDomainStabilityRows(panel) {
    ensurePanelAnalysis(panel);
    const theta = panelTheta(panel);
    const cacheKey = `domain-stability:stride${selectedStride()}:${thresholdKey(theta)}`;
    if (analysisRuntimeCache.has(cacheKey)) return analysisRuntimeCache.get(cacheKey);

    const series = [];
    for (const pair of pairsForMode("overlap")) {
      const sourceIndex = Number(pair.source_timestep_index);
      const targetIndex = Number(pair.target_timestep_index);
      if (!Number.isFinite(sourceIndex) || !Number.isFinite(targetIndex)) continue;

      const sourceSheets = timestepByIndex.get(sourceIndex)?.sheets || [];
      const targetSheets = timestepByIndex.get(targetIndex)?.sheets || [];
      const bySource = groupMatchesByField(pair.matches || [], "source_sheet_id");
      const byTarget = groupMatchesByField(pair.matches || [], "target_sheet_id");
      const bestSourceRetentions = [];
      const bestTargetInheritances = [];
      const weakSourceNodes = [];
      const weakTargetNodes = [];
      const splitSourceNodes = [];
      const mergeTargetNodes = [];
      const highlightNodes = new Set();
      const highlightLinks = new Set();

      for (const sheet of sourceSheets) {
        const sourceSheetId = Number(sheet.sheet_id);
        const matches = bySource.get(sourceSheetId) || [];
        const best = bestByScore(matches, overlapSourceRetention);
        const score = best ? overlapSourceRetention(best) : 0;
        bestSourceRetentions.push(score);
        if (score < theta) {
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
        const matches = byTarget.get(targetSheetId) || [];
        const best = bestByScore(matches, overlapTargetInheritance);
        const score = best ? overlapTargetInheritance(best) : 0;
        bestTargetInheritances.push(score);
        if (score < theta) {
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

      let domainSplits = 0;
      for (const [sourceSheetId, matches] of bySource.entries()) {
        const above = (matches || []).filter(match => overlapSourceRetention(match) >= theta);
        if (above.length >= 2) {
          domainSplits += 1;
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

      let domainMerges = 0;
      for (const [targetSheetId, matches] of byTarget.entries()) {
        const above = (matches || []).filter(match => overlapTargetInheritance(match) >= theta);
        if (above.length >= 2) {
          domainMerges += 1;
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

      const meanSourceRetention = meanNumber(bestSourceRetentions);
      const meanTargetInheritance = meanNumber(bestTargetInheritances);
      const churnScore = clamp(1 - 0.5 * (meanSourceRetention + meanTargetInheritance), 0, 1);
      const sourceWeakCount = bestSourceRetentions.filter(value => value < theta).length;
      const targetWeakCount = bestTargetInheritances.filter(value => value < theta).length;
      const domainChangeScore = sourceWeakCount + targetWeakCount + splitMergeWeight * domainSplits + splitMergeWeight * domainMerges + churnScore * Math.max(sourceSheets.length, 1);

      series.push({
        id: `domain_stability:${thresholdKey(theta)}:${sourceIndex}:${targetIndex}`,
        threshold: theta,
        source_timestep_index: sourceIndex,
        target_timestep_index: targetIndex,
        source_label: pair.source_label,
        target_label: pair.target_label,
        source_stem: pair.source_stem || "",
        target_stem: pair.target_stem || "",
        pair_label: `${pair.source_label || sourceIndex}->${pair.target_label || targetIndex}`,
        source_sheet_count: sourceSheets.length,
        target_sheet_count: targetSheets.length,
        overlap_link_count: Number(pair.pair_count ?? (pair.matches || []).length) || 0,
        mean_source_retention: meanSourceRetention,
        mean_target_inheritance: meanTargetInheritance,
        churn_score: churnScore,
        domain_change_score: domainChangeScore,
        source_weak_count: sourceWeakCount,
        target_weak_count: targetWeakCount,
        domain_splits: domainSplits,
        domain_merges: domainMerges,
        highlight: {
          nodes: [...highlightNodes].sort(),
          links: [...highlightLinks].sort(),
          weak_source_nodes: weakSourceNodes,
          weak_target_nodes: weakTargetNodes,
          split_source_nodes: splitSourceNodes,
          merge_target_nodes: mergeTargetNodes,
        },
      });
    }

    const result = {
      series: series.slice().sort((a, b) => Number(a.source_timestep_index) - Number(b.source_timestep_index) || Number(a.target_timestep_index) - Number(b.target_timestep_index)),
      ranked: series.slice().sort((a, b) =>
        Number(b.domain_change_score || 0) - Number(a.domain_change_score || 0) ||
        Number(b.churn_score || 0) - Number(a.churn_score || 0)
      ),
    };
    analysisRuntimeCache.set(cacheKey, result);
    return result;
  }

  function domainStabilityRows(panel) {
    return computeDomainStabilityRows(panel).ranked;
  }

  function domainStabilityKeyFromItem(item) {
    return String(item?.id || `domain_stability:${Number(item?.source_timestep_index)}:${Number(item?.target_timestep_index)}`);
  }

  function domainStabilityHighlightPayload(item) {
    return {
      id: item?.id || `domain-stability:${domainStabilityKeyFromItem(item)}`,
      domainStabilityKey: domainStabilityKeyFromItem(item),
      label: `Domain stability ${item?.source_label || item?.source_timestep_index} -> ${item?.target_label || item?.target_timestep_index}`,
      nodes: item?.highlight?.nodes || [],
      links: item?.highlight?.links || [],
      start: Number(item?.source_timestep_index),
      end: Number(item?.target_timestep_index),
    };
  }

  function selectedDomainStabilityKeySet(panel) {
    ensurePanelAnalysis(panel);
    return new Set((panel.analysis.selectedDomainStabilityKeys || []).filter(Boolean));
  }

  function aggregateSelectedDomainStabilityHighlight(panel) {
    ensurePanelAnalysis(panel);
    const selectedKeys = selectedDomainStabilityKeySet(panel);
    if (!selectedKeys.size) return null;
    const selected = domainStabilityRows(panel)
      .filter(item => selectedKeys.has(domainStabilityKeyFromItem(item)))
      .map(domainStabilityHighlightPayload);
    if (!selected.length) return null;
    const label = `${selected.length} selected domain-change interval${selected.length === 1 ? "" : "s"}`;
    return {
      ...combinedHighlight(selected, label),
      id: "selected-domain-stability",
      domainStabilityKeys: selected.map(item => item.domainStabilityKey).filter(Boolean),
    };
  }

  function toggleDomainStabilityGraphSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const payload = domainStabilityHighlightPayload(item);
    const key = payload.domainStabilityKey;
    if (!key) return;
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
    const selected = panel.analysis.selectedDomainStabilityKeys || [];
    const alreadySelected = selected.includes(key);
    panel.analysis.selectedDomainStabilityKeys = alreadySelected
      ? selected.filter(entry => entry !== key)
      : [...selected, key];
    panel.analysis.highlight = aggregateSelectedDomainStabilityHighlight(panel);
    if (!alreadySelected) queueAnalysisFocusPulse(panel, payload);
    expandRangesForHighlight(panel.analysis.highlight);
    renderAll();
  }

  function domainStabilityGraphTooltip(item, panel) {
    const theta = panelTheta(panel);
    return `
      <strong>${escapeHtml(intervalTimestepPrimary(item, "source"))} -> ${escapeHtml(intervalTimestepPrimary(item, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Time</div><div>${escapeHtml(formatIntervalFs(intervalTimeFs(item)))}</div>
        <div>Domain change score</div><div>${escapeHtml(formatScore(item.domain_change_score))}</div>
        <div>Domain churn</div><div>${escapeHtml(formatScore(100 * Number(item.churn_score || 0)))}%</div>
        <div>Mean source retention</div><div>${escapeHtml(formatScore(100 * Number(item.mean_source_retention || 0)))}%</div>
        <div>Mean target inheritance</div><div>${escapeHtml(formatScore(100 * Number(item.mean_target_inheritance || 0)))}%</div>
        <div>Weak source/target</div><div>${escapeHtml(item.source_weak_count)} / ${escapeHtml(item.target_weak_count)}</div>
        <div>Domain splits / merges</div><div>${escapeHtml(item.domain_splits)} / ${escapeHtml(item.domain_merges)}</div>
        <div>Theta</div><div>${escapeHtml(formatScore(theta))}</div>
      </div>`;
  }

  function renderDomainStabilityGraph(container, panel, rows) {
    renderAnalysisPointGraph(container, panel, rows, {
      id: "domain-stability",
      heightKey: "domainStabilityGraphHeight",
      zoomKey: "domainStabilityGraphZoomScale",
      focusKey: "domainStabilityGraphFocus",
      graphKey: "domainStabilityGraphKey",
      selectedKeysKey: "selectedDomainStabilityKeys",
      keyFn: domainStabilityKeyFromItem,
      stateKey: panel => `domain-stability:${thresholdKey(panelTheta(panel))}`,
      xValue: intervalTimeFs,
      yValue: item => Number(item.domain_change_score),
      sort: (a, b) => intervalTimeFs(a) - intervalTimeFs(b),
      xLabel: "Time (fs)",
      yLabel: "Domain change score",
      xTickFormat: value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits),
      yTickFormat: value => formatScore(value),
      drawLine: true,
      tooltip: domainStabilityGraphTooltip,
      selectedKeySet: selectedDomainStabilityKeySet,
      aggregateHighlight: aggregateSelectedDomainStabilityHighlight,
      onClick: toggleDomainStabilityGraphSelection,
      resizeTitle: "Drag to resize domain-stability plot"
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
      label: "Domain/range complementarity",
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
    const label = `${selected.length} selected domain/range complementarity${selected.length === 1 ? "" : "s"}`;
    return {
      ...combinedHighlight(selected, label),
      id: "selected-domain-range-complementarity",
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
    panel.analysis.selectedDomainStabilityKeys = [];
    panel.analysis.selectedDisagreementKeys = alreadySelected ? [] : [key];
    panel.analysis.highlight = aggregateSelectedDisagreementHighlight(panel);
    if (!alreadySelected) queueAnalysisFocusPulse(panel, payload);
    expandRangesForHighlight(panel.analysis.highlight);
    renderAll();
  }

  function disagreementGraphTooltip(item) {
    const strongest = item?.strongest_disagreement || {};
    const direction = strongest.direction || "forward";
    const isBackward = direction === "backward";
    const comparedSheet = strongest.compared_sheet_id ?? (isBackward ? strongest.target_sheet_id : strongest.source_sheet_id);
    const rangeChoice = strongest.range_choice_sheet_id ?? (isBackward ? strongest.shape_source_sheet_id : strongest.shape_target_sheet_id);
    const domainChoice = strongest.domain_choice_sheet_id ?? (isBackward ? strongest.overlap_source_sheet_id : strongest.overlap_target_sheet_id);
    const comparedLabel = isBackward ? "Common target sheet" : "Common source sheet";
    const rangeChoiceLabel = isBackward ? "Range predecessor" : "Range target";
    const domainChoiceLabel = isBackward ? "Domain predecessor" : "Domain target";
    const domainPercent = strongest.domain_overlap_percent ?? strongest.overlap_max_percent;
    const domainPercentLabel = isBackward ? "target %" : "source %";
    const comparedCount = item.compared_count ?? item.compared_sources ?? 0;
    return `
      <strong>${escapeHtml(intervalTimestepPrimary(item, "source"))} -> ${escapeHtml(intervalTimestepPrimary(item, "target"))}</strong>
      <div class="tooltip-grid" style="margin-top:8px;">
        <div>Time</div><div>${escapeHtml(formatIntervalFs(intervalTimeFs(item)))}</div>
        <div>Max bidirectional score</div><div>${escapeHtml(formatScore(item.max_disagreement_score))}</div>
        <div>Forward / backward max</div><div>${escapeHtml(formatScore(item.forward_max_disagreement_score))} / ${escapeHtml(formatScore(item.backward_max_disagreement_score))}</div>
        <div>Mean disagreement score</div><div>${escapeHtml(formatScore(item.mean_disagreement_score))}</div>
        <div>Disagreements / compared</div><div>${escapeHtml(item.disagreement_count)} / ${escapeHtml(comparedCount)}</div>
        <div>Disagreement fraction</div><div>${escapeHtml(formatScore(100 * Number(item.disagreement_fraction || 0)))}%</div>
        <div>Strongest direction</div><div>${escapeHtml(isBackward ? "Backward" : "Forward")}</div>
        <div>${comparedLabel}</div><div>S${escapeHtml(comparedSheet ?? "-")}</div>
        <div>${rangeChoiceLabel}</div><div>S${escapeHtml(rangeChoice ?? "-")} (${escapeHtml(formatScore(strongest.shape_score))})</div>
        <div>${domainChoiceLabel}</div><div>S${escapeHtml(domainChoice ?? "-")} (${escapeHtml(formatScore(domainPercent))}% ${escapeHtml(domainPercentLabel)})</div>
        <div>Range / domain normalized loss</div><div>${escapeHtml(formatScore(strongest.shape_loss))} / ${escapeHtml(formatScore(strongest.overlap_loss))}</div>
      </div>`;
  }

  function renderDisagreementScoreGraph(container, panel, rows) {
    renderAnalysisPointGraph(container, panel, rows, {
      id: "domain-range-complementarity",
      heightKey: "disagreementGraphHeight",
      zoomKey: "disagreementGraphZoomScale",
      focusKey: "disagreementGraphFocus",
      graphKey: "disagreementGraphKey",
      selectedKeysKey: "selectedDisagreementKeys",
      keyFn: disagreementSummaryKey,
      xValue: intervalTimeFs,
      yValue: item => Number(item.max_disagreement_score),
      sort: (a, b) => intervalTimeFs(a) - intervalTimeFs(b),
      xLabel: "Time (fs)",
      yLabel: "Max bidirectional disagreement score",
      xTickFormat: value => Number(value).toFixed(TIMESTEP_LABEL_OPTIONS.digits),
      yTickFormat: value => formatScore(value),
      drawLine: false,
      tooltip: disagreementGraphTooltip,
      selectedKeySet: selectedDisagreementKeySet,
      aggregateHighlight: aggregateSelectedDisagreementHighlight,
      onClick: toggleDisagreementGraphSelection,
      resizeTitle: "Drag to resize domain/range complementarity plot"
    });
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

  function visibleRowsWithSelected(rows, visibleRows, selectedKeys, keyFn) {
    const seen = new Set((visibleRows || []).map(keyFn));
    const merged = [...(visibleRows || [])];
    for (const row of rows || []) {
      const key = keyFn(row);
      if (!selectedKeys.has(key) || seen.has(key)) continue;
      merged.push(row);
      seen.add(key);
    }
    return merged;
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
    const selected = activeIntervalRows(panel)
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

  function trackNodeKeys(item) {
    const nodes = item?.highlight?.nodes || item?.nodes || [];
    return Array.isArray(nodes) ? nodes : [];
  }

  function trackLength(item) {
    const explicit = Number(item?.length);
    if (Number.isFinite(explicit)) return explicit;
    return trackNodeKeys(item).length;
  }

  function visibleTrackNodeMeta() {
    const nodeMeta = new Map();
    for (const timestep of (data.timesteps || [])) {
      const timestepIndex = Number(timestep.timestep_index);
      for (const sheet of (timestep.sheets || [])) {
        nodeMeta.set(sheetKey(timestepIndex, sheet.sheet_id), { timestep, sheet });
      }
    }
    return nodeMeta;
  }

  function trackRowFromVisibleNodePath(panel, nodes, edgeByLinkKey, trackId) {
    const nodeMeta = visibleTrackNodeMeta();
    const links = [];
    const scores = [];
    for (let index = 0; index + 1 < nodes.length; index += 1) {
      const [sourceIndex, sourceSheetId] = nodes[index].split(":").map(Number);
      const [targetIndex, targetSheetId] = nodes[index + 1].split(":").map(Number);
      const linkKey = linkKeyParts(sourceIndex, sourceSheetId, targetIndex, targetSheetId);
      links.push(linkKey);
      const edge = edgeByLinkKey.get(linkKey);
      if (edge) scores.push(Number(edge.score) || 0);
    }

    const sheets = nodes.map(key => nodeMeta.get(key)?.sheet).filter(Boolean);
    const ranks = sheets.map(sheet => Number(sheet.rank) || 0);
    const areas = sheets.map(sheet => Number(sheet.area) || 0);
    const firstNode = nodes[0]?.split(":").map(Number) || [0, 0];
    const lastNode = nodes[nodes.length - 1]?.split(":").map(Number) || firstNode;
    const firstTs = timestepByIndex.get(firstNode[0]);
    const lastTs = timestepByIndex.get(lastNode[0]);
    const supportKey = panel?.dataMode === "shape"
      ? panel.domainSupportFilterMode
      : panel.rangeSupportFilterMode;

    return {
      id: `visible_track:${analysisMetricKey(panel)}:${supportKey}:${trackId}`,
      threshold: panelTheta(panel),
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
    };
  }

  function longestVisibleLinkTracksThroughNode(panel, node) {
    if (!hasActiveSupportFilter(panel)) return null;
    const nodeKey = nodeKeyFromDatum(node);
    const activeThreshold = clamp(Number(panel.threshold) || 0, 0, 100);
    const edges = gatherVisibleMatchEdges(panel, activeThreshold, { includeUnsupported: false });
    const outgoing = new Map();
    const incoming = new Map();
    const edgeByLinkKey = new Map();
    for (const edge of edges) {
      const sourceKey = sheetKey(edge.source_timestep_index, edge.source_sheet_id);
      const targetKey = sheetKey(edge.target_timestep_index, edge.target_sheet_id);
      if (!outgoing.has(sourceKey)) outgoing.set(sourceKey, []);
      if (!incoming.has(targetKey)) incoming.set(targetKey, []);
      outgoing.get(sourceKey).push({ ...edge, sourceKey, targetKey });
      incoming.get(targetKey).push({ ...edge, sourceKey, targetKey });
      edgeByLinkKey.set(linkKeyParts(edge.source_timestep_index, edge.source_sheet_id, edge.target_timestep_index, edge.target_sheet_id), edge);
    }

    const maxPathsPerSide = 32;
    const keepLongest = paths => {
      if (!paths.length) return [];
      const maxLength = Math.max(...paths.map(path => path.length));
      return paths.filter(path => path.length === maxLength).slice(0, maxPathsPerSide);
    };
    const backwardMemo = new Map();
    const forwardMemo = new Map();
    const backwardPaths = key => {
      if (backwardMemo.has(key)) return backwardMemo.get(key);
      const parents = incoming.get(key) || [];
      if (!parents.length) {
        const result = [[key]];
        backwardMemo.set(key, result);
        return result;
      }
      const paths = [];
      for (const edge of parents) {
        for (const path of backwardPaths(edge.sourceKey)) paths.push([...path, key]);
      }
      const result = keepLongest(paths);
      backwardMemo.set(key, result);
      return result;
    };
    const forwardPaths = key => {
      if (forwardMemo.has(key)) return forwardMemo.get(key);
      const children = outgoing.get(key) || [];
      if (!children.length) {
        const result = [[key]];
        forwardMemo.set(key, result);
        return result;
      }
      const paths = [];
      for (const edge of children) {
        for (const path of forwardPaths(edge.targetKey)) paths.push([key, ...path]);
      }
      const result = keepLongest(paths);
      forwardMemo.set(key, result);
      return result;
    };

    const combinedPaths = [];
    for (const prefix of backwardPaths(nodeKey)) {
      for (const suffix of forwardPaths(nodeKey)) {
        combinedPaths.push([...prefix, ...suffix.slice(1)]);
      }
    }
    const longestPaths = keepLongest(combinedPaths);
    return longestPaths
      .map((path, index) => trackRowFromVisibleNodePath(panel, path, edgeByLinkKey, index + 1))
      .sort(trackGraphItemSort);
  }

  function longestTracksThroughNode(panel, node) {
    const visibleTracks = longestVisibleLinkTracksThroughNode(panel, node);
    if (visibleTracks) return visibleTracks;
    const key = nodeKeyFromDatum(node);
    const rows = analysisTracks(panel)
      .filter(item => trackNodeKeys(item).includes(key));
    if (!rows.length) return [];
    const maxLength = Math.max(...rows.map(trackLength));
    return rows
      .filter(item => Math.abs(trackLength(item) - maxLength) < 1e-9)
      .sort(trackGraphItemSort);
  }

  function highlightLongestTracksThroughNode(panel, node) {
    ensurePanelAnalysis(panel);
    const tracks = longestTracksThroughNode(panel, node);
    if (!tracks.length) return tracks;
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedTrackKeys = [...new Set(tracks.map(trackKeyFromItem).filter(Boolean))];
    panel.analysis.highlight = combinedHighlight(
      tracks.map(item => trackHighlightPayload(item, panel)),
      `${tracks.length} longest continuing feature${tracks.length === 1 ? "" : "s"} through selected sheet`
    );
    if (state.analysisFocusPulse?.panelId === panel.id) state.analysisFocusPulse = null;
    renderAll();
    return tracks;
  }

  function toggleIntervalGraphSelection(panel, item) {
    ensurePanelAnalysis(panel);
    const payload = intervalHighlightPayload(item, panel);
    const key = payload.intervalKey;
    if (!key) return;
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
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
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
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

  function selectTopIntervalRows(panel, rows) {
    ensurePanelAnalysis(panel);
    const selectedRows = (rows || []).filter(Boolean);
    panel.analysis.selectedIntervalKeys = selectedRows.map(intervalKeyFromItem).filter(Boolean);
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
    panel.analysis.highlight = aggregateSelectedIntervalHighlight(panel);
    expandRangesForHighlight(panel.analysis.highlight);

    const firstRow = selectedRows[0];
    if (firstRow) {
      queueAnalysisFocusPulse(panel, intervalHighlightPayload(firstRow, panel));
    } else if (state.analysisFocusPulse?.panelId === panel.id) {
      state.analysisFocusPulse = null;
    }
    renderAll();
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
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
    panel.analysis.highlight = null;
    if (state.analysisFocusPulse?.panelId === panel.id) state.analysisFocusPulse = null;
  }

  function setAnalysisHighlight(panel, highlight, focusRange = true) {
    ensurePanelAnalysis(panel);
    panel.analysis.selectedIntervalKeys = [];
    panel.analysis.selectedTrackKeys = [];
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
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
    panel.analysis.trackChooserGroupKey = "";
    panel.analysis.selectedDisagreementKeys = [];
    panel.analysis.selectedDomainStabilityKeys = [];
    panel.analysis.topIntervals = 0;
    state.analysisFocusPulse = null;
    panel.analysis.highlight = null;
    renderAll();
  }

  function appendClearHighlightButton(controls, panel) {
    controls.append("button")
      .attr("type", "button")
      .text("Clear highlight")
      .on("click", () => clearAnalysisHighlight(panel));
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
      box.append("div").attr("class", "analysis-hint").text("Runtime interval, continuing-feature, and sensitivity analysis is available.");
    }

    const thetaSelect = actions.append("label");
    thetaSelect.append("span").text("Theta ");
    const theta = thetaSelect.append("select");
    const thetaOptions = analysisThresholdOptions(panel);
    thetaOptions.forEach(value => {
      theta.append("option")
        .attr("value", value)
        .property("selected", Math.abs(Number(value) - Number(panel.analysis.theta)) < 1e-12)
        .text(formatScore(value));
    });
    theta.on("change", event => {
      panel.analysis.theta = Number(event.target.value);
      panel.analysis.selectedIntervalKeys = [];
      panel.analysis.selectedTrackKeys = [];
      panel.analysis.selectedDisagreementKeys = [];
      panel.analysis.selectedDomainStabilityKeys = [];
      state.analysisFocusPulse = null;
      panel.analysis.highlight = null;
      renderAll();
    });

    const plotColorLabel = actions.append("label");
    plotColorLabel.append("span").text("Plot color ");
    plotColorLabel.append("input")
      .attr("type", "color")
      .attr("title", "Analysis plot color")
      .property("value", panel.analysis.plotColor)
      .on("input", event => {
        panel.analysis.plotColor = normalizeHexColor(event.target.value);
        scheduleRenderAll();
      });

    const plotTransparencyLabel = actions.append("label")
      .attr("class", "range-control")
      .attr("title", "Transparency for unselected analysis points and edges");
    const plotTransparencyHeader = plotTransparencyLabel.append("span").attr("class", "range-control-header");
    plotTransparencyHeader.append("span").text("Plot transparency");
    const plotTransparencyBody = plotTransparencyLabel.append("span").attr("class", "range-control-body");
    const plotTransparency = plotTransparencyBody.append("input")
      .attr("type", "range")
      .attr("min", 0)
      .attr("max", 100)
      .attr("step", 1)
      .property("value", panel.analysis.deEmphasisTransparency);
    const plotTransparencyValue = plotTransparencyBody.append("span")
      .text(`${panel.analysis.deEmphasisTransparency}%`);
    plotTransparency.on("change", event => {
      panel.analysis.deEmphasisTransparency = clamp(Number(event.target.value) || 0, 0, 100);
      plotTransparencyValue.text(`${panel.analysis.deEmphasisTransparency}%`);
      scheduleRenderAll();
    });

    const tabs = panel.dataMode === "overlap"
      ? [
          ["intervals", "Interesting domain intervals"],
          ["tracks", "Domain continuing features"],
          ["sensitivity", "Domain sensitivity"],
        ]
      : [
          ["intervals", "Interesting range intervals"],
          ["tracks", "Range continuing features"],
          ["sensitivity", "Range sensitivity"],
        ];
    if (!tabs.some(([id]) => id === panel.analysis.tab)) {
      clearAnalysisSelectionsOnly(panel);
      panel.analysis.tab = tabs[0][0];
    }
    const tabRow = box.append("div").attr("class", "analysis-tabs");
    tabs.forEach(([id, label]) => {
      tabRow.append("button")
        .attr("type", "button")
        .attr("class", `analysis-tab${panel.analysis.tab === id ? " active" : ""}`)
        .text(label)
        .on("click", () => {
          if (panel.analysis.tab !== id) {
            const intervalTabs = new Set(["intervals", "best-supported-intervals"]);
            if (intervalTabs.has(panel.analysis.tab) || intervalTabs.has(id)) {
              panel.analysis.selectedIntervalKeys = [];
              panel.analysis.highlight = null;
            }
            panel.analysis.trackChooserGroupKey = "";
            if (id !== "disagreement") {
              panel.analysis.selectedDisagreementKeys = [];
            }
            if (id === "disagreement") {
              panel.analysis.selectedIntervalKeys = [];
              panel.analysis.selectedTrackKeys = [];
              panel.analysis.selectedDomainStabilityKeys = [];
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
      const intervalKind = panel.dataMode === "overlap" ? "interesting domain intervals" : "interesting range intervals";
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top ${intervalKind} out of ${rows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 0)
        .attr("max", Math.max(1, rows.length))
        .property("value", Math.min(panel.analysis.topIntervals, rows.length));
      countInput.on("change", event => {
        const maxCount = Math.max(0, rows.length);
        panel.analysis.topIntervals = clamp(Math.floor(Number(event.target.value) || 0), 0, maxCount);
        selectTopIntervalRows(panel, rows.slice(0, Math.min(panel.analysis.topIntervals, rows.length)));
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => selectTopIntervalRows(panel, visibleRows));
      appendClearHighlightButton(controls, panel);

      renderIntervalScoreGraph(content, panel, rows);

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text(`No ${intervalKind} analysis for this theta.`);
      return;
    }

    if (panel.analysis.tab === "best-supported-intervals") {
      const intervalKind = panel.dataMode === "overlap" ? "best supported domain intervals" : "best supported range intervals";
      if (!hasActiveSupportFilter(panel)) {
        content.append("div")
          .attr("class", "analysis-hint")
          .text("Select a support filter to compute best-supported intervals.");
        return;
      }

      const rows = bestSupportedIntervals(panel);
      const visibleRows = rows.slice(0, Math.min(panel.analysis.topBestSupportedIntervals, rows.length));
      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top ${intervalKind} out of ${rows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 1)
        .attr("max", Math.max(1, rows.length))
        .property("value", Math.min(panel.analysis.topBestSupportedIntervals, Math.max(1, rows.length)));
      countInput.on("change", event => {
        const maxCount = Math.max(1, rows.length);
        panel.analysis.topBestSupportedIntervals = clamp(Math.floor(Number(event.target.value) || 1), 1, maxCount);
        selectTopIntervalRows(panel, rows.slice(0, Math.min(panel.analysis.topBestSupportedIntervals, rows.length)));
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => selectTopIntervalRows(panel, visibleRows));
      appendClearHighlightButton(controls, panel);

      content.append("div")
        .attr("class", "analysis-hint")
        .text(`Filtered by ${supportFilterDescription(panel)}.`);
      renderIntervalScoreGraph(content, panel, rows, {
        id: "best-supported-intervals",
        yLabel: "Best supported event score",
        stateKey: panel => `${analysisCacheKey("best-supported-interval-graph", panel)}:${supportFilterAnalysisKey(panel)}`,
      });

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text(`No ${intervalKind} analysis for this theta.`);
      return;
    }

    if (panel.analysis.tab === "tracks") {
      const rows = analysisTracks(panel);
      const topRows = rows.slice(0, Math.min(panel.analysis.topFeatures, rows.length));
      const visibleRows = visibleRowsWithSelected(rows, topRows, selectedTrackKeySet(panel), trackKeyFromItem);
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
        panel.analysis.trackChooserGroupKey = "";
        renderAll();
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => setAnalysisHighlight(panel, combinedHighlight(topRows, `Top ${topRows.length} continuing features`), false));
      appendClearHighlightButton(controls, panel);

      renderTrackScoreGraph(content, panel, visibleRows);

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text("No continuing-feature analysis for this theta.");
      return;
    }

    if (panel.analysis.tab === "domain") {
      const rows = domainStabilityRows(panel);
      const visibleRows = rows.slice(0, Math.min(panel.analysis.topDomainStability, rows.length));
      const controls = content.append("div").attr("class", "analysis-actions");
      controls.append("span").attr("class", "analysis-hint").text(`Show/highlight top domain-change intervals out of ${rows.length}`);
      const countInput = controls.append("input")
        .attr("type", "number")
        .attr("min", 1)
        .attr("max", Math.max(1, rows.length))
        .property("value", Math.min(panel.analysis.topDomainStability, Math.max(1, rows.length)));
      countInput.on("change", event => {
        const maxCount = Math.max(1, rows.length);
        panel.analysis.topDomainStability = clamp(Math.floor(Number(event.target.value) || 1), 1, maxCount);
        renderAll();
      });
      controls.append("button").attr("type", "button").text("Highlight")
        .on("click", () => setAnalysisHighlight(panel, combinedHighlight(visibleRows.map(domainStabilityHighlightPayload), `Top ${visibleRows.length} domain-change intervals`), false));
      appendClearHighlightButton(controls, panel);

      renderDomainStabilityGraph(content, panel, visibleRows);

      if (!rows.length) content.append("div").attr("class", "analysis-hint").text("No domain-stability analysis for this theta.");
      return;
    }

    if (panel.analysis.tab === "sensitivity") {
      const sensitivityRows = computeRuntimeSensitivity(panel);
      content.append("div")
        .attr("class", "analysis-hint")
        .text(`Sensitivity for ${modeLabel(panel.dataMode)} / ${metricLabel(panel.dataMode, panel.metricId)}`);
      const table = content.append("table").attr("class", "analysis-table");
      const header = table.append("thead").append("tr");
      ["Theta", "Mean event", "Max event", "Top interval time", "Max life", "Median life", ""].forEach(label => header.append("th").text(label));
      const body = table.append("tbody");
      sensitivityRows.forEach(row => {
        const tr = body.append("tr");
        tr.append("td").text(formatScore(row.threshold));
        tr.append("td").text(formatScore(row.mean_event_score));
        tr.append("td").text(formatScore(row.max_event_score));
        tr.append("td").text(sensitivityTopIntervalTimeLabel(row));
        tr.append("td").text(row.max_lifetime ?? 0);
        tr.append("td").text(formatScore(row.median_lifetime));
        tr.append("td").append("button").attr("type", "button").text("Use")
          .on("click", () => {
            panel.analysis.theta = Number(row.threshold);
            panel.analysis.tab = "intervals";
            panel.analysis.selectedIntervalKeys = [];
            panel.analysis.selectedTrackKeys = [];
            panel.analysis.trackChooserGroupKey = "";
            panel.analysis.selectedDisagreementKeys = [];
            panel.analysis.selectedDomainStabilityKeys = [];
            state.analysisFocusPulse = null;
            panel.analysis.highlight = null;
            renderAll();
          });
      });
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
          combinedHighlight(visibleRows.map(disagreementHighlightPayload), `Top ${visibleRows.length} domain/range complementarity pairs`),
          false
        ));
      appendClearHighlightButton(controls, panel);

      content.append("div")
        .attr("class", "analysis-subtitle")
        .text("Highest local complementarity intervals");
      renderDisagreementScoreGraph(content, panel, visibleRows);
      if (!rankedRows.length) {
        content.append("div").attr("class", "analysis-hint").text("No domain/range complementarity examples were found.");
      }
      return;
    }
  }



  function panelTheta(panel) {
    ensurePanelAnalysis(panel);
    const theta = Number(panel?.analysis?.theta);
    return Number.isFinite(theta) ? theta : defaultAnalysisTheta(panel);
  }

  function panelMatchForKey(key, panel) {
    const mode = panel?.dataMode || "shape";
    if (mode === "overlap") return overlapMatchLookup.get(key) || null;
    return shapeMatchLookup.get(key) || null;
  }

  function panelMatchesForNode(node, direction, panel) {
    const key = nodeKeyFromDatum(node);
    const mode = panel?.dataMode || "shape";
    if (mode === "overlap") {
      return direction === "incoming"
        ? (overlapIncomingByNode.get(key) || [])
        : (overlapOutgoingByNode.get(key) || []);
    }
    return direction === "incoming"
      ? (shapeIncomingByNode.get(key) || [])
      : (shapeOutgoingByNode.get(key) || []);
  }

  function continuationScore(match, panel) {
    if (!match) return null;
    return panelAnalysisScore(match, panel);
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
    const match = panelMatchForKey(key, panel) || link;
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
    const matches = panelMatchesForNode(node, direction, panel);
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
    ensurePanelSupportFilters(panel);
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
    if (mode === "centroid_position" || mode === "centroid_axis_diagonal") {
      return node.centroid_color || centroidColorFromCentroid(node.centroid);
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

  function linkFillColor(opacity, hover, visibility = 1) {
    const darkness = clamp(state.layoutControls.linkDarkness, 0, 100) / 100;
    const shade = Math.round(140 - darkness * 110);
    const blue = Math.round(168 - darkness * 124);
    const alphaBase = hover
      ? clamp(0.20 + opacity * 0.90, 0, 0.98)
      : clamp(0.14 + opacity * 0.80, 0, 0.92);
    const alpha = clamp(alphaBase * clamp(Number(visibility) || 0, 0, 1), 0, 0.98);
    return `rgba(${shade}, ${shade}, ${blue}, ${alpha})`;
  }

  function supportLinkVisibility(link, panel) {
    if (!link?.supportFilteredOut) return 1;
    const transparency = clamp(
      Number(panel?.unsupportedLinkTransparency ?? UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY),
      0,
      100
    );
    return 1 - transparency / 100;
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
      shape_iou: "Sheet IoU",
      area_ratio: "Sheet area ratio",
      bbox_iou: "Sheet bbox overlap",
      centroid_similarity: "Sheet centroid distance"
    };
    return labels[metricId] || metricId;
  }

  function formatWeight(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "0";
    return (Math.round(n * 1000) / 1000).toString();
  }

  function renderShapeWeightControls(controls, panel) {
    if (panel.dataMode !== "shape" || panel.metricId !== "combined") return;
    ensurePanelShapeWeights(panel);
    const group = controls.append("div")
      .attr("class", "shape-weight-controls")
      .attr("title", "Weights used by the Combined range-overlap metric");
    for (const metricId of shapeScoreComponentIds) {
      const item = group.append("label").attr("class", "shape-weight-item");
      item.append("span").text(shapeWeightLabel(metricId));
      item.append("input")
        .attr("type", "number")
        .attr("min", 0)
        .attr("step", 0.05)
        .property("value", formatWeight(panel.shapeWeights?.[metricId]))
        .on("change", event => {
          const next = sanitizeShapeWeights(panel.shapeWeights);
          next[metricId] = Math.max(0, Number(event.target.value) || 0);
          panel.shapeWeights = next;
          event.target.value = formatWeight(next[metricId]);
          clearAnalysisSelectionsOnly(panel);
          scheduleRenderAll();
        });
    }
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
    const includeMissingSlots = Boolean(thumbClass);
    const sheetLabel = nodeImageLabel(node, "Sheet image", linkedPair?.role || "");
    const fiberLabel = nodeImageLabel(node, "Spatial context", linkedPair?.role || "");
    const sheetPair = linkedPair ? {
      title: "Sheet images",
      leftSrc: linkedPair.left?.thumbnail || "",
      leftLabel: nodeImageLabel(linkedPair.left, "Sheet image", "Source"),
      rightSrc: linkedPair.right?.thumbnail || "",
      rightLabel: nodeImageLabel(linkedPair.right, "Sheet image", "Target")
    } : null;
    const fiberPair = linkedPair ? {
      title: "Spatial context images",
      leftSrc: linkedPair.left?.fiber_surface_image || "",
      leftLabel: nodeImageLabel(linkedPair.left, "Spatial context", "Source"),
      rightSrc: linkedPair.right?.fiber_surface_image || "",
      rightLabel: nodeImageLabel(linkedPair.right, "Spatial context", "Target")
    } : null;
    const sheetImage = node.thumbnail
      ? `<img${classAttr} data-media-kind="sheet"${zoomDataAttributes(node.thumbnail, sheetLabel, sheetPair)} src="${escapeHtml(node.thumbnail)}" alt="${escapeHtml(sheetLabel)}">`
      : includeMissingSlots
        ? `<div class="media-missing">No sheet image</div>`
        : "";
    const fiberImage = node.fiber_surface_image
      ? `<img${classAttr} data-media-kind="fiber"${zoomDataAttributes(node.fiber_surface_image, fiberLabel, fiberPair)} src="${escapeHtml(node.fiber_surface_image)}" alt="${escapeHtml(fiberLabel)}">`
      : includeMissingSlots
        ? `<div class="media-missing">No spatial context image</div>`
        : "";
    const sheetItem = sheetImage ? `<div class="media-item" data-media-kind="sheet"><div class="media-label">Sheet image</div>${sheetImage}</div>` : "";
    const fiberItem = fiberImage ? `<div class="media-item" data-media-kind="fiber"><div class="media-label">Spatial context</div>${fiberImage}</div>` : "";
    if (!sheetItem && !fiberItem) return "";
    return `<div class="media-stack">${sheetItem}${fiberItem}</div>`;
  }

  function nodeTooltip(node, panel) {
    const image = nodeMediaStack(node);
    const incoming = bestContinuationForNode(node, "incoming", panel);
    const outgoing = bestContinuationForNode(node, "outgoing", panel);
    return `
      <h3>Sheet ${escapeHtml(node.sheet_id)}</h3>
      <div class="meta-list">
        <div>Timestep</div><div>${escapeHtml(node.timestep_label)}</div>
        <div>Rank</div><div>${escapeHtml(node.rank)}</div>
        <div>Area</div><div>${escapeHtml(formatScore(node.area))}</div>
        <div>Domain vertices</div><div>${escapeHtml(node.num_vertices)}</div>
        <div>Best incoming continuation</div><div>${escapeHtml(incoming.text)}</div>
        <div>Best outgoing continuation</div><div>${escapeHtml(outgoing.text)}</div>
      </div>
      ${image}
    `;
  }

  function linkTooltip(link, panel) {
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const sourceImage = nodeMediaStack(sourceNode);
    const targetImage = nodeMediaStack(targetNode);
    const metricValueNow = metricValue(link, panel, panel.metricId);
    const continuation = continuationLinkInfo(link, panel);
    const scoreRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, panel, metric.id)))}</div>`)
      .join("");
    return `
      <h3>Sheet ${escapeHtml(link.source_sheet_id)} → ${escapeHtml(link.target_sheet_id)}</h3>
      <div class="meta-list">
        <div>Source timestep</div><div>${escapeHtml(link.source_label)}</div>
        <div>Target timestep</div><div>${escapeHtml(link.target_label)}</div>
        <div>Source sheet</div><div>S${escapeHtml(link.source_sheet_id)}${Number.isFinite(Number(link.source_rank)) ? `, rank ${escapeHtml(link.source_rank)}` : ""}</div>
        <div>Target sheet</div><div>S${escapeHtml(link.target_sheet_id)}${Number.isFinite(Number(link.target_rank)) ? `, rank ${escapeHtml(link.target_rank)}` : ""}</div>
        <div>${escapeHtml(metricLabel(panel.dataMode, panel.metricId))}</div><div>${escapeHtml(formatScore(metricValueNow))}</div>
        <div>Continuation score</div><div>${escapeHtml(continuation.text)}</div>
        <div>Domain support</div><div>${escapeHtml(formatDomainSupport(domainSupportForLink(link)))}</div>
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

  function showNodeDetails(node, panel, recordSelection = true) {
    if (!detailsContent) return;
    if (recordSelection) {
      state.detailsSelection = {
        type: "node",
        panelId: panel?.id ?? state.activePanelId,
        key: nodeKeyFromDatum(node)
      };
    }
    const image = nodeMediaStack(node, "thumb");
    const incoming = bestContinuationForNode(node, "incoming", panel);
    const outgoing = bestContinuationForNode(node, "outgoing", panel);
    const longestTracks = longestTracksThroughNode(panel, node);
    const longestTrackText = longestTracks.length
      ? `${longestTracks.length} feature${longestTracks.length === 1 ? "" : "s"}, length ${formatTrackGroupCount(trackLength(longestTracks[0]))}`
      : "None at current theta";
    detailsContent.innerHTML = `
      <h3>Sheet ${escapeHtml(node.sheet_id)}</h3>
      ${image}
      <div class="meta-list">
        <div>Timestep</div><div>${escapeHtml(node.timestep_label)}</div>
        <div>Rank</div><div>${escapeHtml(node.rank)}</div>
        <div>Area</div><div>${escapeHtml(formatScore(node.area))}</div>
        <div>Domain vertices</div><div>${escapeHtml(node.num_vertices)}</div>
        <div>Best incoming continuation</div><div>${escapeHtml(incoming.text)}</div>
        <div>Best outgoing continuation</div><div>${escapeHtml(outgoing.text)}</div>
        <div>Longest continuing feature</div><div>${escapeHtml(longestTrackText)}</div>
        <div>Centroid</div><div>${escapeHtml(formatArrayValue(node.centroid))}</div>
      </div>
    `;
  }

  function showLinkDetails(link, panel, recordSelection = true) {
    if (!detailsContent) return;
    if (recordSelection) {
      state.detailsSelection = {
        type: "link",
        panelId: panel?.id ?? state.activePanelId,
        key: linkKeyFromDatum(link)
      };
    }
    const sourceNode = sheetByTimestepAndId(link.source_timestep_index, link.source_sheet_id);
    const targetNode = sheetByTimestepAndId(link.target_timestep_index, link.target_sheet_id);
    const linkedPair = { left: sourceNode, right: targetNode };
    const sourceImage = nodeMediaStack(sourceNode, "thumb", { ...linkedPair, role: "Source" }) || "<p>No image</p>";
    const targetImage = nodeMediaStack(targetNode, "thumb", { ...linkedPair, role: "Target" }) || "<p>No image</p>";
    const selectedMetricLabel = metricLabel(panel.dataMode, panel.metricId);
    const selectedMetricValue = metricValue(link, panel, panel.metricId);
    const continuation = continuationLinkInfo(link, panel);
    const domainSupport = domainSupportForLink(link);
    const metricRows = metricsForMode(panel.dataMode)
      .map(metric => `<div>${escapeHtml(metric.label)}</div><div>${escapeHtml(formatScore(metricValue(link, panel, metric.id)))}</div>`)
      .join("");
    detailsContent.innerHTML = `
      <h3>Link S${escapeHtml(link.source_sheet_id)} → S${escapeHtml(link.target_sheet_id)}</h3>
      <div class="meta-list">
        <div>Mode</div><div>${escapeHtml(modeLabel(panel.dataMode))}</div>
        <div>Source timestep</div><div>${escapeHtml(link.source_label)}</div>
        <div>Target timestep</div><div>${escapeHtml(link.target_label)}</div>
        <div>Source sheet</div><div>S${escapeHtml(link.source_sheet_id)}${Number.isFinite(Number(link.source_rank)) ? `, rank ${escapeHtml(link.source_rank)}` : ""}</div>
        <div>Target sheet</div><div>S${escapeHtml(link.target_sheet_id)}${Number.isFinite(Number(link.target_rank)) ? `, rank ${escapeHtml(link.target_rank)}` : ""}</div>
        <div>${escapeHtml(selectedMetricLabel)}</div><div>${escapeHtml(formatScore(selectedMetricValue))}</div>
        <div>Continuation score</div><div>${escapeHtml(continuation.text)}</div>
        <div>Domain support</div><div>${escapeHtml(formatDomainSupport(domainSupport))}</div>
      </div>
      <div class="thumb-row link-media-row">
        <div>${sourceImage}</div>
        <div>${targetImage}</div>
      </div>
      <div class="meta-list">${metricRows}</div>
    `;
  }

  function cloneJson(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function sanitizeFilenamePart(value) {
    const cleaned = String(value || "figure")
      .trim()
      .replace(/[^a-zA-Z0-9._-]+/g, "_")
      .replace(/^_+|_+$/g, "");
    return cleaned || "figure";
  }

  function datasetFigureLabel() {
    const fromMeta = data.meta?.dataset_name || data.meta?.base_dir || "";
    const raw = fromMeta ? String(fromMeta).split("/").filter(Boolean).pop() : "";
    if (raw) return raw;
    const tokens = String(location.pathname || "").split("/").filter(Boolean);
    const sankeyIndex = tokens.lastIndexOf("sankey");
    if (sankeyIndex > 0) return tokens[sankeyIndex - 1];
    return tokens[tokens.length - 2] || "reeb_viewer";
  }

  function serializeFigurePanel(panel) {
    return cloneJson({
      id: panel.id,
      dataMode: panel.dataMode,
      metricId: panel.metricId,
      threshold: panel.threshold,
      domainFilterMetricId: panel.domainFilterMetricId,
      rangeSupportMetricId: panel.rangeSupportMetricId,
      domainSupportFilterMode: panel.domainSupportFilterMode,
      rangeSupportFilterMode: panel.rangeSupportFilterMode,
      unsupportedLinkTransparency: panel.unsupportedLinkTransparency,
      shapeWeights: panel.shapeWeights || null,
      analysis: panel.analysis || null,
      panelHeight: panel.panelHeight
    });
  }

  function serializeFigurePreset(options = {}) {
    const activePanel = getPanelById(state.activePanelId) || state.panels[0] || null;
    return {
      schema_version: 1,
      viewer: "unified_sankey_viewer",
      name: options.name || "",
      dataset: datasetFigureLabel(),
      created_at: new Date().toISOString(),
      recommended_target: options.target || "active-panel",
      state: {
        ranges: cloneJson(state.ranges),
        selectedRangeIndex: state.selectedRangeIndex,
        activePanelId: activePanel?.id ?? state.activePanelId,
        panels: state.panels.map(serializeFigurePanel),
        layoutControls: cloneJson(state.layoutControls),
        detailsSelection: cloneJson(state.detailsSelection),
        camera: {
          zoomScale: camera?.getZoomScale?.() ?? 1,
          viewFocus: camera?.getViewFocus?.() || null
        }
      }
    };
  }

  function downloadTextFile(filename, text, mimeType = "application/json") {
    const blob = new Blob([text], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function saveFigurePreset() {
    const defaultName = `${datasetFigureLabel()}_${new Date().toISOString().replace(/[:.]/g, "-")}`;
    const requested = window.prompt("Figure preset name", defaultName);
    if (requested === null) return;
    const name = sanitizeFilenamePart(requested || defaultName);
    const preset = serializeFigurePreset({ name });
    downloadTextFile(`${name}.figure_preset.json`, JSON.stringify(preset, null, 2));
  }

  function normalizePresetPanel(raw, fallbackId) {
    const domainFilterMetricId = normalizeDomainFilterMetricId(raw?.domainFilterMetricId);
    const rangeSupportMetricId = normalizeRangeSupportMetricId(raw?.rangeSupportMetricId);
    const panel = {
      id: Number(raw?.id) || fallbackId,
      dataMode: String(raw?.dataMode || "shape"),
      metricId: String(raw?.metricId || "shape_iou"),
      threshold: clamp(Number(raw?.threshold) || 0, 0, 100),
      domainFilterMetricId,
      rangeSupportMetricId,
      domainSupportFilterMode: normalizeSupportFilterMode(
        raw?.domainSupportFilterMode,
        normalizeBoolean(raw?.useBestDomainSupport) ||
          raw?.domainFilterMode === "best_domain_support" ||
          raw?.domainFilterMode === "one_to_one"
      ),
      rangeSupportFilterMode: normalizeSupportFilterMode(
        raw?.rangeSupportFilterMode,
        normalizeBoolean(raw?.useBestRangeSupport)
      ),
      unsupportedLinkTransparency: clamp(
        Number(raw?.unsupportedLinkTransparency ?? UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY),
        0,
        100
      ),
      shapeWeights: raw?.shapeWeights ? cloneJson(raw.shapeWeights) : cloneDefaultShapeWeights(),
      analysis: raw?.analysis ? cloneJson(raw.analysis) : null,
      panelHeight: clampPanelHeight(raw?.panelHeight ?? PANEL_HEIGHT_DEFAULT)
    };
    ensurePanelMetric(panel);
    ensurePanelAnalysis(panel);
    return panel;
  }

  function syncLayoutControlsToDom() {
    const orderingNode = document.getElementById("orderingMode");
    const topSheetsNode = document.getElementById("topSheets");
    const timestepStrideNode = document.getElementById("timestepStride");
    const nodeColorNode = document.getElementById("nodeColorMode");
    const darknessNode = document.getElementById("linkDarkness");
    const darknessValueNode = document.getElementById("linkDarknessValue");
    const hideIsolatedNode = document.getElementById("hideIsolated");
    const strongestOutgoingNode = document.getElementById("strongestOutgoingOnly");
    const hideSheetLabelsNode = document.getElementById("hideSheetLabels");
    if (orderingNode) orderingNode.value = state.layoutControls.orderingMode;
    if (topSheetsNode) topSheetsNode.value = String(normalizeTopSheets(state.layoutControls.topSheets));
    if (timestepStrideNode) timestepStrideNode.value = String(selectedStride());
    if (nodeColorNode) nodeColorNode.value = state.layoutControls.nodeColorMode;
    if (darknessNode) darknessNode.value = String(clamp(state.layoutControls.linkDarkness, 0, 100));
    if (darknessValueNode) darknessValueNode.textContent = `${clamp(state.layoutControls.linkDarkness, 0, 100)}%`;
    if (hideIsolatedNode) hideIsolatedNode.checked = Boolean(state.layoutControls.hideIsolated);
    if (strongestOutgoingNode) strongestOutgoingNode.checked = Boolean(state.layoutControls.strongestOutgoingOnly);
    if (hideSheetLabelsNode) hideSheetLabelsNode.checked = hideSheetLabels();
    drawCentroidColorLegend();
    updateCentroidColorLegendVisibility();
    refreshLinkDarkness();
  }

  function nodeFromKey(key) {
    const [timestepIndex, sheetId] = String(key || "").split(":").map(Number);
    if (!Number.isFinite(timestepIndex) || !Number.isFinite(sheetId)) return null;
    return sheetByTimestepAndId(timestepIndex, sheetId);
  }

  function linkFromKeyForPanel(key, panel) {
    if (!key || !panel) return null;
    const threshold = clamp(Number(panel.threshold) || 0, 0, 100);
    return gatherVisibleMatchEdges(panel, threshold).find(link => linkKeyFromDatum(link) === key) || null;
  }

  function restoreFigureDetailsSelection(selection) {
    if (!selection) return;
    const panel = getPanelById(selection.panelId) || getPanelById(state.activePanelId) || state.panels[0];
    if (!panel) return;
    if (selection.type === "node") {
      const node = nodeFromKey(selection.key);
      if (node) showNodeDetails(node, panel, false);
      return;
    }
    if (selection.type === "link") {
      const link = linkFromKeyForPanel(selection.key, panel);
      if (link) showLinkDetails(link, panel, false);
    }
  }

  function applyFigurePreset(preset) {
    const presetState = preset?.state || preset || {};
    if (Array.isArray(presetState.ranges)) {
      state.ranges = presetState.ranges.map(range => ({
        start: Number(range.start),
        end: Number(range.end)
      })).filter(range => Number.isFinite(range.start) && Number.isFinite(range.end));
    }
    state.selectedRangeIndex = Math.max(0, Math.floor(Number(presetState.selectedRangeIndex) || 0));
    if (Array.isArray(presetState.panels) && presetState.panels.length) {
      state.panels = presetState.panels.map((panel, index) => normalizePresetPanel(panel, index + 1));
      state.nextPanelId = Math.max(...state.panels.map(panel => Number(panel.id) || 0), 0) + 1;
    }
    if (presetState.layoutControls && typeof presetState.layoutControls === "object") {
      state.layoutControls = {
        ...state.layoutControls,
        ...cloneJson(presetState.layoutControls),
        nodeSizeMode: "vertices",
        nodeColorMode: presetState.layoutControls.nodeColorMode === "centroid_axis_diagonal"
          ? "centroid_position"
          : String(presetState.layoutControls.nodeColorMode || state.layoutControls.nodeColorMode),
        topSheets: normalizeTopSheets(presetState.layoutControls.topSheets)
      };
    }
    state.detailsSelection = presetState.detailsSelection ? cloneJson(presetState.detailsSelection) : null;
    const requestedActiveId = Number(presetState.activePanelId);
    state.activePanelId = state.panels.some(panel => panel.id === requestedActiveId)
      ? requestedActiveId
      : state.panels[0]?.id ?? 1;

    const presetCamera = presetState.camera || {};
    const zoomScale = Number(presetCamera.zoomScale);
    if (Number.isFinite(zoomScale)) camera?.setZoomScale(zoomScale);
    if (presetCamera.viewFocus && Number.isFinite(Number(presetCamera.viewFocus.x)) && Number.isFinite(Number(presetCamera.viewFocus.y))) {
      camera?.setViewFocus({ x: Number(presetCamera.viewFocus.x), y: Number(presetCamera.viewFocus.y) });
    } else {
      camera?.clearViewFocus();
    }

    hideTooltip();
    syncLayoutControlsToDom();
    renderAll();
    return new Promise(resolve => {
      requestAnimationFrame(() => {
        if (Number.isFinite(zoomScale)) camera?.setZoomScale(zoomScale);
        if (presetCamera.viewFocus && Number.isFinite(Number(presetCamera.viewFocus.x)) && Number.isFinite(Number(presetCamera.viewFocus.y))) {
          camera?.setViewFocus({ x: Number(presetCamera.viewFocus.x), y: Number(presetCamera.viewFocus.y) });
          camera?.applyNow?.();
        }
        restoreFigureDetailsSelection(state.detailsSelection);
        resolve(serializeFigurePreset({ name: preset?.name || "restored" }));
      });
    });
  }

  function installFigureExportApi() {
    window.ReebFigureExport = {
      ready: () => Boolean(camera),
      getPreset: serializeFigurePreset,
      applyPreset: applyFigurePreset,
      selectors: () => ({
        full: "body",
        main: "main",
        viewer: "#viewer",
        panels: "#panelList",
        activePanel: ".panel.active-panel",
        details: "#details",
        controls: "#controls",
        analysis: ".panel.active-panel .analysis-box"
      })
    };
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
      const stride = selectedStride();
      for (let t = range.start; t <= range.end; t += stride) {
        if (seen.has(t)) continue;
        const ts = timestepLookup.itemAt(t);
        if (!ts) continue;
        columns.push({ type: "timestep", timestep: ts });
        seen.add(t);
      }
    }
    return columns;
  }

  function strongestOutgoingEdges(edges) {
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

  function gatherVisibleMatchEdges(panel, thresholdPercent, options = {}) {
    const edges = [];
    const pairs = pairsForMode(panel.dataMode, panel);
    const ranges = normalizedRanges();
    const threshold = clamp(Number(thresholdPercent) || 0, 0, 100) / 100;
    const metricMax = metricMaxForPanel(panel, panel.metricId);
    const visible = visibleTimestepIndexSet();

    for (const pair of pairs) {
      if (!visible.has(pair.source_timestep_index) || !visible.has(pair.target_timestep_index)) continue;
      for (const match of pair.matches) {
        const edgeBase = {
          ...match,
          source_timestep_index: pair.source_timestep_index,
          target_timestep_index: pair.target_timestep_index,
        };
        const score = metricValue(match, panel, panel.metricId);
        const normalized = metricMax > 0 ? score / metricMax : 0;
        if (normalized < threshold) continue;
        edges.push({
          ...edgeBase,
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

    let supportedEdges = applySupportFilter(edges, panel);

    if (state.layoutControls.strongestOutgoingOnly) {
      supportedEdges = strongestOutgoingEdges(supportedEdges);
      return supportedEdges.map(edge => ({ ...edge, supportFilteredOut: false }));
    }

    const includeUnsupported = options.includeUnsupported !== false;
    const unsupportedTransparency = clamp(
      Number(panel?.unsupportedLinkTransparency ?? UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY),
      0,
      100
    );
    if (
      !hasActiveSupportFilter(panel)
      || !includeUnsupported
      || unsupportedTransparency >= 100
    ) {
      return supportedEdges.map(edge => ({ ...edge, supportFilteredOut: false }));
    }

    const supportedKeys = new Set(supportedEdges.map(linkKeyFromDatum));
    return edges.map(edge => ({
      ...edge,
      supportFilteredOut: !supportedKeys.has(linkKeyFromDatum(edge))
    }));
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

  function edgeOrderingWeight(edge) {
    const width = Number(edge?.width);
    if (Number.isFinite(width) && width > 0) return width;
    const score = Number(edge?.score);
    if (Number.isFinite(score) && score > 0) return score;
    return 1;
  }

  function computeCrossingOrder(columnNodesByTime, edgeList, fallbackComparator, candidateComparators = []) {
    const timeKeys = [...columnNodesByTime.keys()].sort((a, b) => a - b);
    const nodeKey = (t, s) => `${t}:${s}`;
    const baseColumns = new Map();

    for (const t of timeKeys) {
      const nodes = columnNodesByTime.get(t) || [];
      baseColumns.set(t, [...nodes]);
    }

    const incomingByNode = new Map();
    const outgoingByNode = new Map();
    const crossingEdges = [];
    for (const edge of edgeList || []) {
      const sourceKey = nodeKey(edge.source_timestep_index, edge.source_sheet_id);
      const targetKey = nodeKey(edge.target_timestep_index, edge.target_sheet_id);
      const weight = edgeOrderingWeight(edge);
      const sourceEntry = { key: sourceKey, weight };
      const targetEntry = { key: targetKey, weight };
      if (!incomingByNode.has(targetKey)) incomingByNode.set(targetKey, []);
      if (!outgoingByNode.has(sourceKey)) outgoingByNode.set(sourceKey, []);
      incomingByNode.get(targetKey).push(sourceEntry);
      outgoingByNode.get(sourceKey).push(targetEntry);
      crossingEdges.push({
        sourceKey,
        targetKey,
        weight,
        groupKey: `${edge.source_timestep_index}->${edge.target_timestep_index}`
      });
    }

    const cloneColumns = columns => {
      const clone = new Map();
      for (const [timestep, nodes] of columns.entries()) {
        clone.set(timestep, [...nodes]);
      }
      return clone;
    };

    const syncOrderByKey = state => {
      state.orderByKey.clear();
      for (const timestep of timeKeys) {
        const nodes = state.columns.get(timestep) || [];
        nodes.forEach((node, index) => {
          state.orderByKey.set(nodeKey(timestep, node.sheet_id), index);
        });
      }
    };

    const makeState = comparator => {
      const state = { columns: cloneColumns(baseColumns), orderByKey: new Map() };
      for (const timestep of timeKeys) {
        const nodes = state.columns.get(timestep) || [];
        nodes.sort(comparator);
        state.columns.set(timestep, nodes);
      }
      syncOrderByKey(state);
      return state;
    };

    const crossingStats = orderByKey => {
      const edgesByGap = new Map();
      for (const edge of crossingEdges) {
        const sourceOrder = orderByKey.get(edge.sourceKey);
        const targetOrder = orderByKey.get(edge.targetKey);
        if (!Number.isFinite(sourceOrder) || !Number.isFinite(targetOrder)) continue;
        if (!edgesByGap.has(edge.groupKey)) edgesByGap.set(edge.groupKey, []);
        edgesByGap.get(edge.groupKey).push({
          sourceOrder,
          targetOrder,
          weight: Math.max(1e-6, Number(edge.weight) || 1)
        });
      }

      let weighted = 0;
      let count = 0;
      for (const list of edgesByGap.values()) {
        for (let i = 0; i < list.length; i += 1) {
          for (let j = i + 1; j < list.length; j += 1) {
            const sourceDelta = list[i].sourceOrder - list[j].sourceOrder;
            const targetDelta = list[i].targetOrder - list[j].targetOrder;
            if (sourceDelta === 0 || targetDelta === 0) continue;
            if (sourceDelta * targetDelta < 0) {
              weighted += list[i].weight * list[j].weight;
              count += 1;
            }
          }
        }
      }
      return { weighted, count };
    };

    const statsBetter = (candidate, current) => {
      if (!current) return true;
      const epsilon = 1e-9;
      return (
        candidate.weighted < current.weighted - epsilon ||
        (Math.abs(candidate.weighted - current.weighted) <= epsilon && candidate.count < current.count)
      );
    };

    let bestColumns = null;
    let bestStats = null;
    const considerState = state => {
      const stats = crossingStats(state.orderByKey);
      if (statsBetter(stats, bestStats)) {
        bestStats = stats;
        bestColumns = cloneColumns(state.columns);
      }
    };

    const weightedBarycenter = (neighbors, orderByKey) => {
      let weightedSum = 0;
      let totalWeight = 0;
      for (const neighbor of neighbors || []) {
        const value = orderByKey.get(neighbor.key);
        if (!Number.isFinite(value)) continue;
        const weight = Math.max(1e-6, Number(neighbor.weight) || 1);
        weightedSum += value * weight;
        totalWeight += weight;
      }
      return totalWeight > 0 ? weightedSum / totalWeight : Number.POSITIVE_INFINITY;
    };

    const sweepSort = (state, neighborsByNode, timestep, tieComparator) => {
      const nodes = state.columns.get(timestep) || [];
      const withScores = nodes.map(node => {
        const key = nodeKey(timestep, node.sheet_id);
        const neighbors = neighborsByNode.get(key) || [];
        return {
          node,
          barycenter: weightedBarycenter(neighbors, state.orderByKey)
        };
      });

      withScores.sort((a, b) =>
        d3.ascending(a.barycenter, b.barycenter) ||
        tieComparator(a.node, b.node) ||
        fallbackComparator(a.node, b.node)
      );

      withScores.forEach((entry, index) => {
        state.orderByKey.set(nodeKey(timestep, entry.node.sheet_id), index);
      });
      state.columns.set(timestep, withScores.map(entry => entry.node));
    };

    const adjacentCrossingCost = (firstEntries, secondEntries, orderByKey) => {
      let beforeWeighted = 0;
      let afterWeighted = 0;
      let beforeCount = 0;
      let afterCount = 0;
      for (const first of firstEntries || []) {
        const firstOrder = orderByKey.get(first.key);
        if (!Number.isFinite(firstOrder)) continue;
        for (const second of secondEntries || []) {
          const secondOrder = orderByKey.get(second.key);
          if (!Number.isFinite(secondOrder) || first.key === second.key) continue;
          const pairWeight = Math.max(1e-6, Number(first.weight) || 1) * Math.max(1e-6, Number(second.weight) || 1);
          if (firstOrder > secondOrder) {
            beforeWeighted += pairWeight;
            beforeCount += 1;
          } else if (firstOrder < secondOrder) {
            afterWeighted += pairWeight;
            afterCount += 1;
          }
        }
      }
      return { beforeWeighted, afterWeighted, beforeCount, afterCount };
    };

    const swapImprovesCrossings = (firstKey, secondKey, orderByKey) => {
      const incoming = adjacentCrossingCost(incomingByNode.get(firstKey), incomingByNode.get(secondKey), orderByKey);
      const outgoing = adjacentCrossingCost(outgoingByNode.get(firstKey), outgoingByNode.get(secondKey), orderByKey);
      const beforeWeighted = incoming.beforeWeighted + outgoing.beforeWeighted;
      const afterWeighted = incoming.afterWeighted + outgoing.afterWeighted;
      const beforeCount = incoming.beforeCount + outgoing.beforeCount;
      const afterCount = incoming.afterCount + outgoing.afterCount;
      const epsilon = 1e-9;
      return (
        afterWeighted < beforeWeighted - epsilon ||
        (Math.abs(afterWeighted - beforeWeighted) <= epsilon && afterCount < beforeCount)
      );
    };

    const transposeRefinement = state => {
      let changed = false;
      for (const timestep of timeKeys) {
        const nodes = state.columns.get(timestep) || [];
        let index = 0;
        while (index < nodes.length - 1) {
          const firstKey = nodeKey(timestep, nodes[index].sheet_id);
          const secondKey = nodeKey(timestep, nodes[index + 1].sheet_id);
          if (swapImprovesCrossings(firstKey, secondKey, state.orderByKey)) {
            const tmp = nodes[index];
            nodes[index] = nodes[index + 1];
            nodes[index + 1] = tmp;
            state.orderByKey.set(nodeKey(timestep, nodes[index].sheet_id), index);
            state.orderByKey.set(nodeKey(timestep, nodes[index + 1].sheet_id), index + 1);
            changed = true;
            index = Math.max(0, index - 1);
          } else {
            index += 1;
          }
        }
        state.columns.set(timestep, nodes);
      }
      return changed;
    };

    const rawComparators = [fallbackComparator, ...candidateComparators].filter(fn => typeof fn === "function");
    const comparators = [];
    rawComparators.forEach(fn => {
      if (!comparators.includes(fn)) comparators.push(fn);
    });
    if (!comparators.length && typeof fallbackComparator === "function") comparators.push(fallbackComparator);
    for (const comparator of comparators) {
      const state = makeState(comparator);
      considerState(state);
      for (let iteration = 0; iteration < 8; iteration += 1) {
        for (let i = 1; i < timeKeys.length; i += 1) {
          const t = timeKeys[i];
          sweepSort(state, incomingByNode, t, comparator);
        }
        for (let i = timeKeys.length - 2; i >= 0; i -= 1) {
          const t = timeKeys[i];
          sweepSort(state, outgoingByNode, t, comparator);
        }
        for (let pass = 0; pass < 6; pass += 1) {
          if (!transposeRefinement(state)) break;
        }
        considerState(state);
      }
    }

    if (!bestColumns) {
      bestColumns = makeState(fallbackComparator).columns;
    }
    for (const timestep of timeKeys) {
      const nodes = bestColumns.get(timestep) || [];
      columnNodesByTime.set(timestep, [...nodes]);
    }
  }

  function adjacentVisibleOrder(node) {
    if (!node) return Number.POSITIVE_INFINITY;
    return ((Number(node.y0) || 0) + (Number(node.y1) || 0)) / 2;
  }

  function linkSortTieBreak(a, b) {
    return (
      d3.descending(Number(a.score) || 0, Number(b.score) || 0) ||
      d3.ascending(Number(a.target_rank) || 0, Number(b.target_rank) || 0) ||
      d3.ascending(Number(a.target_sheet_id) || 0, Number(b.target_sheet_id) || 0) ||
      d3.ascending(Number(a.source_rank) || 0, Number(b.source_rank) || 0) ||
      d3.ascending(Number(a.source_sheet_id) || 0, Number(b.source_sheet_id) || 0)
    );
  }

  function byTargetOrder(a, b) {
    return (
      d3.ascending(adjacentVisibleOrder(a.targetNode), adjacentVisibleOrder(b.targetNode)) ||
      linkSortTieBreak(a, b)
    );
  }

  function bySourceOrder(a, b) {
    return (
      d3.ascending(adjacentVisibleOrder(a.sourceNode), adjacentVisibleOrder(b.sourceNode)) ||
      linkSortTieBreak(a, b)
    );
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

    const areaComparator = nodeSortComparator("area");
    const vertexComparator = nodeSortComparator("vertices");
    const fallbackComparator = state.layoutControls.orderingMode === "vertices" ? vertexComparator : areaComparator;
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
      computeCrossingOrder(columnNodesByTime, edgeList, areaComparator, [areaComparator, vertexComparator]);
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

  function scheduleViewportUpdate(immediate = false) {
    if (!camera) return;
    if (immediate && typeof camera.applyNow === "function") {
      camera.applyNow();
      return;
    }
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

    for (const [key, list] of outgoing.entries()) {
      list.sort(byTargetOrder);
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
      list.sort(bySourceOrder);
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
    const visibleSet = visibleTimestepIndexSet();
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
      ["Sampling", `Every ${selectedStride()} timestep${selectedStride() === 1 ? "" : "s"}`],
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
      scheduleViewportUpdate(true);
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
          if (typeof camera.applyNow === "function") camera.applyNow();
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

    const thresholdLabel = controls.append("label")
      .attr("class", "range-control")
      .attr("title", "Hide links whose selected metric is below this percentage of the current metric maximum");
    const thresholdHeader = thresholdLabel.append("span").attr("class", "range-control-header");
    thresholdHeader.append("span").text("Link threshold");
    const thresholdBody = thresholdLabel.append("span").attr("class", "range-control-body");
    const thresholdRange = thresholdBody.append("input")
      .attr("type", "range")
      .attr("min", 0)
      .attr("max", 100)
      .attr("step", 0.5)
      .property("value", panel.threshold);
    const thresholdBox = thresholdBody.append("input")
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
        scheduleRenderAll();
      }
    });

    if (panel.dataMode === "shape" && overlapMetricIds.length) {
      ensurePanelSupportFilters(panel);
      controls.append("span")
        .attr("class", "control-label")
        .text("Domain support");

      const domainSupportModeSelect = controls.append("select")
        .attr("title", "Range-link domain support filter");
      [
        ["all", "All links"],
        ["outgoing", "Best supported outgoing links"],
        ["incoming", "Best supported incoming links"],
        ["both", "Best supported incoming and outgoing links"],
      ].forEach(([value, label]) => {
        domainSupportModeSelect.append("option")
          .attr("value", value)
          .property("selected", value === panel.domainSupportFilterMode)
          .text(label);
      });
      domainSupportModeSelect.on("change", event => {
        panel.domainSupportFilterMode = normalizeSupportFilterMode(event.target.value);
        clearAnalysisSelectionsOnly(panel);
        scheduleRenderAll();
      });

      const domainMetricSelect = controls.append("select")
        .attr("title", "Domain support metric for best-link selection");
      metricsForMode("overlap").forEach(metric => {
        domainMetricSelect.append("option")
          .attr("value", metric.id)
          .property("selected", metric.id === panel.domainFilterMetricId)
          .text(metric.label);
      });
      domainMetricSelect.on("change", event => {
        panel.domainFilterMetricId = normalizeDomainFilterMetricId(event.target.value);
        clearAnalysisSelectionsOnly(panel);
        scheduleRenderAll();
      });
      appendUnsupportedLinkTransparencyControl(controls, panel);
    }

    if (panel.dataMode === "overlap" && shapeMetricIds.length) {
      ensurePanelSupportFilters(panel);
      controls.append("span")
        .attr("class", "control-label")
        .text("Range support");

      const rangeSupportModeSelect = controls.append("select")
        .attr("title", "Domain-link range support filter");
      [
        ["all", "All links"],
        ["outgoing", "Best supported outgoing links"],
        ["incoming", "Best supported incoming links"],
        ["both", "Best supported incoming and outgoing links"],
      ].forEach(([value, label]) => {
        rangeSupportModeSelect.append("option")
          .attr("value", value)
          .property("selected", value === panel.rangeSupportFilterMode)
          .text(label);
      });
      rangeSupportModeSelect.on("change", event => {
        panel.rangeSupportFilterMode = normalizeSupportFilterMode(event.target.value);
        clearAnalysisSelectionsOnly(panel);
        scheduleRenderAll();
      });

      const rangeMetricSelect = controls.append("select")
        .attr("title", "Range support metric for best-link selection");
      metricsForMode("shape").forEach(metric => {
        rangeMetricSelect.append("option")
          .attr("value", metric.id)
          .property("selected", metric.id === panel.rangeSupportMetricId)
          .text(metric.label);
      });
      rangeMetricSelect.on("change", event => {
        panel.rangeSupportMetricId = normalizeRangeSupportMetricId(event.target.value);
        clearAnalysisSelectionsOnly(panel);
        scheduleRenderAll();
      });
      appendUnsupportedLinkTransparencyControl(controls, panel);
    }

    renderShapeWeightControls(controls, panel);


    renderAnalysisPanel(container, panel);

    const canvas = container.append("div")
      .attr("class", "panel-canvas")
      .style("height", `${panel.panelHeight}px`);
    const svg = canvas.append("svg").attr("class", "summary-chart");

    const columns = buildVisibleColumns();
    const activeThreshold = clamp(Number(panel.threshold) || 0, 0, 100);
    const edgeList = gatherVisibleMatchEdges(panel, activeThreshold);
    const layout = layoutForPanel(columns, panel, edgeList);
    const links = assignLinkOffsets(gatherVisiblePairs(panel, activeThreshold, layout.nodeByKey, edgeList));
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
      .classed("support-filtered-out", d => Boolean(d.supportFilteredOut))
      .classed("domain-support-strong", d => domainSupportClass(d, panel) === "domain-support-strong")
      .classed("domain-support-weak", d => domainSupportClass(d, panel) === "domain-support-weak")
      .classed("domain-support-missing", d => domainSupportClass(d, panel) === "domain-support-missing")
      .attr("d", ribbonPath)
      .attr("fill", d => linkFillColor(d.opacity, false, supportLinkVisibility(d, panel)))
      .on("mouseenter", function(event, d) {
        d3.select(this).classed("hover", true);
        d3.select(this).attr("fill", linkFillColor(d.opacity, true, supportLinkVisibility(d, panel)));
        updateTooltip(linkTooltip(d, panel), event.clientX, event.clientY);
      })
      .on("mousemove", (event, d) => updateTooltip(linkTooltip(d, panel), event.clientX, event.clientY))
      .on("mouseleave", function(event, d) {
        d3.select(this).classed("hover", false);
        d3.select(this).attr("fill", linkFillColor(d.opacity, false, supportLinkVisibility(d, panel)));
        hideTooltip();
      })
      .on("click", (event, d) => {
        event.stopPropagation();
        hideTooltip();
        showLinkDetails(d, panel);
      });

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
      .on("click", (event, d) => {
        event.stopPropagation();
        hideTooltip();
        highlightLongestTracksThroughNode(panel, d);
        showNodeDetails(d, panel);
      });

    state.panelViews.get(panel.id).nodeSelection = node;

    node.append("rect")
      .attr("x", d => d.x0)
      .attr("y", d => d.y0)
      .attr("width", d => d.x1 - d.x0)
      .attr("height", d => Math.max(2, d.height))
      .attr("fill", d => nodeColorFill(d));

    if (!hideSheetLabels()) {
      node.append("text")
        .attr("x", d => d.x1 + 5)
        .attr("y", d => d.y0 + Math.max(2, d.height) / 2)
        .attr("dominant-baseline", "middle")
        .text(d => `S${d.sheet_id} R${d.rank}`);
    }

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
      panelList.selectAll(".panel").classed("active-panel", d => d.id === panel.id);
      renderRangeBar();
    });

    bindPanelResizeHandle(panel, canvas, svg, layoutBounds);
    syncThresholdVisibility();
  }

  function renderPanels() {
    const panels = panelList.selectAll(".panel")
      .data(state.panels, d => d.id)
      .join(enter => enter.append("div").attr("class", "panel"));

    panels
      .classed("active-panel", d => d.id === state.activePanelId)
      .attr("data-panel-id", d => d.id);

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

  function hideSheetLabels() {
    return Boolean(state.layoutControls.hideSheetLabels) || state.layoutControls.showSheetLabels === false;
  }

  function bindLayoutControls() {
    const orderingNode = document.getElementById("orderingMode");
    const nodeSizeNode = document.getElementById("nodeSizeMode");
    const topSheetsNode = document.getElementById("topSheets");
    const timestepStrideNode = document.getElementById("timestepStride");
    const nodeColorNode = document.getElementById("nodeColorMode");
    const darknessNode = document.getElementById("linkDarkness");
    const darknessValueNode = document.getElementById("linkDarknessValue");
    const hideIsolatedNode = document.getElementById("hideIsolated");
    const strongestOutgoingNode = document.getElementById("strongestOutgoingOnly");
    const hideSheetLabelsNode = document.getElementById("hideSheetLabels");

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

    if (timestepStrideNode) {
      timestepStrideNode.innerHTML = "";
      timestepStrideOptions.forEach(stride => {
        const option = document.createElement("option");
        option.value = String(stride);
        const suffix = stride === 2 ? "nd" : (stride === 3 ? "rd" : "th");
        option.textContent = stride === 1 ? "Every timestep" : `Every ${stride}${suffix} timestep`;
        timestepStrideNode.appendChild(option);
      });
      timestepStrideNode.value = String(selectedStride());
      timestepStrideNode.addEventListener("change", event => {
        const next = Math.max(1, Math.floor(Number(event.target.value) || 1));
        state.timestepStride = timestepStrideOptions.includes(next) ? next : timestepStrideOptions[0];
        timestepStrideNode.value = String(state.timestepStride);
        analysisRuntimeCache.clear();
        vertexThetaCache.clear();
        refreshPairLookups();
        state.panels.forEach(panel => clearAnalysisSelectionsOnly(panel));
        state.analysisFocusPulse = null;
        renderAll();
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

    if (hideSheetLabelsNode) {
      hideSheetLabelsNode.checked = hideSheetLabels();
      hideSheetLabelsNode.addEventListener("change", event => {
        state.layoutControls.hideSheetLabels = Boolean(event.target.checked);
        delete state.layoutControls.showSheetLabels;
        scheduleRenderAll();
      });
    }
  }

  function addPanel() {
    const activePanel = getPanelById(state.activePanelId);
    state.panels.push({
      id: state.nextPanelId++,
      dataMode: "shape",
      metricId: "shape_iou",
      threshold: 0,
      domainFilterMetricId: normalizeDomainFilterMetricId(activePanel?.domainFilterMetricId),
      rangeSupportMetricId: normalizeRangeSupportMetricId(activePanel?.rangeSupportMetricId),
      domainSupportFilterMode: normalizeSupportFilterMode(activePanel?.domainSupportFilterMode),
      rangeSupportFilterMode: normalizeSupportFilterMode(activePanel?.rangeSupportFilterMode),
      unsupportedLinkTransparency: clamp(
        Number(activePanel?.unsupportedLinkTransparency ?? UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY),
        0,
        100
      ),
      shapeWeights: cloneDefaultShapeWeights(),
      analysis: activePanel?.analysis ? { ...activePanel.analysis, selectedIntervalKeys: [], selectedTrackKeys: [], selectedDisagreementKeys: [], selectedDomainStabilityKeys: [], trackChooserGroupKey: "", highlight: null } : null,
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
  document.getElementById("saveFigurePreset").addEventListener("click", () => saveFigurePreset());

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
  installFigureExportApi();
  bindImageZoomViewer();
  refreshPairLookups();
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


def build_unified_sankey_viewer_stage(*, rebuild_data: bool = False) -> None:
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"Expected match results at {MATCHES_FILE}")

    if rebuild_data or not TRACKING_DATA_FILE.exists():
        build_unified_sankey_data_stage()

    ensure_paper_export_dirs()

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
    print("Paper export folders:")
    print(f"  presets: {FIGURE_PRESET_DIR}")
    print(f"  images:  {FIGURE_IMAGE_DIR}")
    print("\nOpen with:")
    print(f"  cd {UNIFIED_VIEWER_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the unified Sankey dashboard for one dataset."
    )
    parser.add_argument(
        "--base-dir",
        type=dataset_arg_path,
        default=None,
        help="Dataset base directory. Defaults to common.BASE_DIR.",
    )
    parser.add_argument(
        "--rebuild-data",
        action="store_true",
        help="Regenerate sankey/tracking_data.json before writing viewer assets.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.base_dir is not None:
        configure_dataset_paths(args.base_dir)
    build_unified_sankey_viewer_stage(rebuild_data=args.rebuild_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
