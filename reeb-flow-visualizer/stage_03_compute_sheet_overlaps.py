#!/usr/bin/env python3

import json
import re
from pathlib import Path

from common import (
    BASE_DIR,
    OUTPUT_DIR,
    OVERLAP_FILE,
    OVERLAP_WARNINGS_LOG_FILE,
    RANGE_SCORE_DEFAULT_WEIGHTS,
    RSI_JSON_DIR,
    SANKEY_TIMESTEP_STRIDE_MAX,
)

SHAPE_MATCHES_FILE = (
    BASE_DIR
    / "compareSheetShapesCache"
    / "results"
    / "sheet_shape_matches.json"
)

def configured_strides(max_stride=None):
    value = SANKEY_TIMESTEP_STRIDE_MAX if max_stride is None else max_stride
    try:
        count = int(value)
    except Exception:
        count = 1
    count = max(1, count)
    return list(range(1, count + 1))


RANGE_METRIC_FIELDS = (
    "range_combined_score",
    "range_shape_iou",
    "range_geometry_iou",
    "range_area_ratio",
    "range_bbox_iou",
    "range_centroid_similarity",
)

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


def safe_stem(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return Path(text).stem
    except Exception:
        return ""


def read_rsi_json(rsijson_file):
    data = json.loads(rsijson_file.read_text())
    data["rsijson_file"] = str(rsijson_file)
    return data


def timestep_number(path):
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else None


def timestep_label(path):
    number = timestep_number(path)
    return str(number) if number is not None else path.stem


def timestep_sort_key(path):
    number = timestep_number(path)
    return number if number is not None else path.stem


def make_node_id(timestep_index, sheet_id):
    return f"t{timestep_index}_sheet{sheet_id}"


def prepare_sheet(sheet):
    vertices = sheet.get("vertices", [])

    prepared = dict(sheet)
    prepared["_vertex_set"] = set(vertices)

    return prepared


def make_node(timestep_index, timestep_label, rsijson_file, rsi_file, sheet):
    return {
        "id": make_node_id(timestep_index, sheet["sheet_id"]),
        "timestep_index": timestep_index,
        "timestep_label": timestep_label,
        "rsijson_file": str(rsijson_file),
        "rsi_file": rsi_file,
        "sheet_id": sheet["sheet_id"],
        "rank": sheet["rank"],
        "area": sheet["area"],
        "num_vertices": sheet["num_vertices"],
    }


def make_timestep(timestep_index, rsijson_file, data):
    label = timestep_label(rsijson_file)

    sheets = [
        prepare_sheet(sheet)
        for sheet in data.get("top_sheets", [])
    ]

    nodes = [
        make_node(
            timestep_index,
            label,
            rsijson_file,
            data.get("rsi_file"),
            sheet,
        )
        for sheet in sheets
    ]

    return {
        "index": timestep_index,
        "label": label,
        "stem": rsijson_file.stem,
        "rsijson_file": str(rsijson_file),
        "rsi_file": data.get("rsi_file"),
        "num_vertices": data.get("num_vertices"),
        "num_singular_vertices": data.get("num_singular_vertices"),
        "num_regular_vertices": data.get("num_regular_vertices"),
        "top_n_sheets": data.get("top_n_sheets"),
        "nodes": nodes,
        "sheets": sheets,
    }


def compute_link(source_timestep, target_timestep, source_sheet, target_sheet):
    source_vertices = source_sheet["_vertex_set"]
    target_vertices = target_sheet["_vertex_set"]

    overlap_vertices = len(source_vertices & target_vertices)

    source_count = len(source_vertices)
    target_count = len(target_vertices)

    source_percent = (
        100.0 * overlap_vertices / source_count
        if source_count else 0.0
    )

    target_percent = (
        100.0 * overlap_vertices / target_count
        if target_count else 0.0
    )

    return {
        "source": make_node_id(source_timestep["index"], source_sheet["sheet_id"]),
        "target": make_node_id(target_timestep["index"], target_sheet["sheet_id"]),
        "source_timestep_index": source_timestep["index"],
        "target_timestep_index": target_timestep["index"],
        "source_timestep_label": source_timestep["label"],
        "target_timestep_label": target_timestep["label"],
        "source_stem": source_timestep.get("stem"),
        "target_stem": target_timestep.get("stem"),
        "source_rsijson_file": source_timestep["rsijson_file"],
        "target_rsijson_file": target_timestep["rsijson_file"],
        "source_rsi_file": source_timestep["rsi_file"],
        "target_rsi_file": target_timestep["rsi_file"],
        "source_sheet_id": source_sheet["sheet_id"],
        "target_sheet_id": target_sheet["sheet_id"],
        "source_rank": source_sheet["rank"],
        "target_rank": target_sheet["rank"],
        "source_area": source_sheet["area"],
        "target_area": target_sheet["area"],
        "source_num_vertices": source_count,
        "target_num_vertices": target_count,
        "overlap_vertices": overlap_vertices,
        "source_percent": source_percent,
        "target_percent": target_percent,
    }


def compute_links_by_stride(timesteps, max_stride=None):
    links_by_stride = {}

    for stride in configured_strides(max_stride):
        links = []
        if len(timesteps) <= stride:
            links_by_stride[str(stride)] = links
            continue
        for index in range(0, len(timesteps) - stride):
            source_timestep = timesteps[index]
            target_timestep = timesteps[index + stride]
            for source_sheet in source_timestep["sheets"]:
                for target_sheet in target_timestep["sheets"]:
                    link = compute_link(
                        source_timestep,
                        target_timestep,
                        source_sheet,
                        target_sheet,
                    )

                    if link["overlap_vertices"] > 0:
                        links.append(link)

        links_by_stride[str(stride)] = links

    return links_by_stride


def compute_links(timesteps):
    return compute_links_by_stride(timesteps, 1).get("1", [])


def load_shape_match_index(warning_lines):
    if not SHAPE_MATCHES_FILE.exists():
        warning_lines.append(
            f"shape matches missing: {SHAPE_MATCHES_FILE} "
            "(range metrics will be zero in overlap links)"
        )
        return {}, {
            field: 0.0
            for field in RANGE_METRIC_FIELDS
        }, 0, 0

    try:
        payload = json.loads(SHAPE_MATCHES_FILE.read_text())
    except Exception as exc:
        warning_lines.append(
            f"failed to read shape matches: {SHAPE_MATCHES_FILE}: {exc}"
        )
        return {}, {
            field: 0.0
            for field in RANGE_METRIC_FIELDS
        }, 0, 0

    match_index_by_stem = {}
    match_index_by_label = {}
    match_index_by_index = {}
    metric_maxima = {
        field: 0.0
        for field in RANGE_METRIC_FIELDS
    }
    pair_count = 0
    match_count = 0

    raw_pairs_by_stride = payload.get("pairwise_matches_by_stride")
    if isinstance(raw_pairs_by_stride, dict):
        pair_groups = [
            pair
            for pairs in raw_pairs_by_stride.values()
            for pair in (pairs or [])
        ]
    else:
        pair_groups = payload.get("pairwise_matches", [])

    for pair in pair_groups:
        pair_count += 1
        source_stem = str(pair.get("source_stem", "")).strip()
        target_stem = str(pair.get("target_stem", "")).strip()
        source_label = str(pair.get("source_label", "")).strip()
        target_label = str(pair.get("target_label", "")).strip()
        source_timestep_index = safe_int(pair.get("source_timestep_index"))
        target_timestep_index = safe_int(pair.get("target_timestep_index"))

        for match in pair.get("matches", []):
            source_sheet_id = safe_int(match.get("source_sheet_id"))
            target_sheet_id = safe_int(match.get("target_sheet_id"))

            metrics = {
                "range_combined_score": safe_float(match.get("final_score")),
                "range_shape_iou": safe_float(match.get("shape_iou")),
                "range_geometry_iou": safe_float(match.get("geometry_iou")),
                "range_area_ratio": safe_float(match.get("area_ratio")),
                "range_bbox_iou": safe_float(match.get("bbox_iou")),
                "range_centroid_similarity": safe_float(match.get("centroid_similarity")),
            }

            for key, value in metrics.items():
                metric_maxima[key] = max(metric_maxima[key], value)

            if source_stem and target_stem:
                match_index_by_stem[
                    (
                        source_stem,
                        target_stem,
                        source_sheet_id,
                        target_sheet_id,
                    )
                ] = metrics

            if source_label and target_label:
                match_index_by_label[
                    (
                        source_label,
                        target_label,
                        source_sheet_id,
                        target_sheet_id,
                    )
                ] = metrics

            match_index_by_index[
                (
                    source_timestep_index,
                    target_timestep_index,
                    source_sheet_id,
                    target_sheet_id,
                )
            ] = metrics
            match_count += 1

    return {
        "by_stem": match_index_by_stem,
        "by_label": match_index_by_label,
        "by_index": match_index_by_index,
    }, metric_maxima, pair_count, match_count


def attach_range_metrics(links, match_index):
    attached_count = 0
    lookup_counts = {
        "stem": 0,
        "label": 0,
        "index": 0,
        "miss": 0,
    }

    by_stem = match_index.get("by_stem", {})
    by_label = match_index.get("by_label", {})
    by_index = match_index.get("by_index", {})

    for link in links:
        source_sheet_id = safe_int(link.get("source_sheet_id"))
        target_sheet_id = safe_int(link.get("target_sheet_id"))
        source_stem = (
            str(link.get("source_stem", "")).strip()
            or safe_stem(link.get("source_rsijson_file"))
            or safe_stem(link.get("source_rsi_file"))
        )
        target_stem = (
            str(link.get("target_stem", "")).strip()
            or safe_stem(link.get("target_rsijson_file"))
            or safe_stem(link.get("target_rsi_file"))
        )
        source_label = str(link.get("source_timestep_label", "")).strip()
        target_label = str(link.get("target_timestep_label", "")).strip()

        index_key = (
            safe_int(link.get("source_timestep_index")),
            safe_int(link.get("target_timestep_index")),
            source_sheet_id,
            target_sheet_id,
        )

        stem_key = (
            source_stem,
            target_stem,
            source_sheet_id,
            target_sheet_id,
        )
        label_key = (
            source_label,
            target_label,
            source_sheet_id,
            target_sheet_id,
        )

        metrics = None
        lookup_mode = None
        if source_stem and target_stem:
            metrics = by_stem.get(stem_key)
            if metrics is not None:
                lookup_mode = "stem"

        if metrics is None and source_label and target_label:
            metrics = by_label.get(label_key)
            if metrics is not None:
                lookup_mode = "label"

        if metrics is None:
            metrics = by_index.get(index_key)
            if metrics is not None:
                lookup_mode = "index"

        if metrics:
            link.update(metrics)
            link["has_range_metrics"] = True
            attached_count += 1
            lookup_counts[lookup_mode] += 1
        else:
            for field in RANGE_METRIC_FIELDS:
                link[field] = 0.0
            link["has_range_metrics"] = False
            lookup_counts["miss"] += 1

    return attached_count, lookup_counts


def find_warnings(timesteps):
    warnings = []

    for timestep in timesteps:
        if not timestep["sheets"]:
            warnings.append(f"{timestep['rsijson_file']}: no top sheets found")

        for sheet in timestep["sheets"]:
            vertices = sheet.get("vertices", [])

            if sheet.get("num_vertices", 0) != len(vertices):
                warnings.append(
                    f"{timestep['rsijson_file']}: sheet {sheet.get('sheet_id')} "
                    "num_vertices does not match vertex list length"
                )

    return warnings


def public_timestep_info(timestep):
    return {
        "index": timestep["index"],
        "label": timestep["label"],
        "stem": timestep.get("stem"),
        "rsijson_file": timestep["rsijson_file"],
        "rsi_file": timestep["rsi_file"],
        "num_vertices": timestep["num_vertices"],
        "num_singular_vertices": timestep["num_singular_vertices"],
        "num_regular_vertices": timestep["num_regular_vertices"],
        "top_n_sheets": timestep["top_n_sheets"],
        "node_ids": [node["id"] for node in timestep["nodes"]],
    }


def compute_sheet_overlaps_stage():
    if not RSI_JSON_DIR.exists():
        raise FileNotFoundError(f"RSI JSON directory not found: {RSI_JSON_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rsijson_files = sorted(
        RSI_JSON_DIR.glob("*.rsijson"),
        key=timestep_sort_key,
    )

    print(f"Reading {len(rsijson_files)} RSI JSON files")

    timesteps = []
    warning_lines = []

    for index, rsijson_file in enumerate(rsijson_files):
        try:
            data = read_rsi_json(rsijson_file)
            timestep = make_timestep(index, rsijson_file, data)
            timesteps.append(timestep)

            print(
                f"[{index + 1}/{len(rsijson_files)}] done: {rsijson_file.name}",
                flush=True,
            )

        except Exception as exc:
            warning_lines.append(f"{rsijson_file}: failed to read: {exc}")

            print(
                f"[{index + 1}/{len(rsijson_files)}] failed: {rsijson_file.name}",
                flush=True,
            )

    links_by_stride = compute_links_by_stride(timesteps, SANKEY_TIMESTEP_STRIDE_MAX)
    links = links_by_stride.get("1", [])
    all_links = [link for stride_links in links_by_stride.values() for link in stride_links]
    warning_lines.extend(find_warnings(timesteps))
    (
        shape_match_index,
        range_metric_maxima,
        shape_pair_count,
        shape_match_count,
    ) = load_shape_match_index(warning_lines)
    range_metrics_attached, range_metric_lookup_counts = attach_range_metrics(
        all_links,
        shape_match_index,
    )

    nodes = [
        node
        for timestep in timesteps
        for node in timestep["nodes"]
    ]

    output = {
        "rsijson_directory": str(RSI_JSON_DIR),
        "shape_matches_file": str(SHAPE_MATCHES_FILE),
        "num_timesteps": len(timesteps),
        "num_nodes": len(nodes),
        "num_links": len(links),
        "max_timestep_stride": SANKEY_TIMESTEP_STRIDE_MAX,
        "timestep_strides": configured_strides(SANKEY_TIMESTEP_STRIDE_MAX),
        "range_metric_fields": list(RANGE_METRIC_FIELDS),
        "range_score_default_weights": RANGE_SCORE_DEFAULT_WEIGHTS,
        "range_metric_maxima": range_metric_maxima,
        "range_metric_lookup_counts": range_metric_lookup_counts,
        "num_shape_pairs": shape_pair_count,
        "num_shape_matches": shape_match_count,
        "num_links_with_range_metrics": range_metrics_attached,
        "timesteps": [
            public_timestep_info(timestep)
            for timestep in timesteps
        ],
        "nodes": nodes,
        "links": links,
        "links_by_stride": links_by_stride,
    }

    OVERLAP_FILE.write_text(json.dumps(output, indent=2, allow_nan=False))
    OVERLAP_WARNINGS_LOG_FILE.write_text("\n".join(warning_lines))

    print(f"Overlap data: {OVERLAP_FILE}")
    print(f"Warnings: {len(warning_lines)}")
    print(f"Warning log: {OVERLAP_WARNINGS_LOG_FILE}")
    print(f"Nodes: {len(nodes)}")
    print(f"Links: {len(links)}")
    print("Links by stride: " + ", ".join(f"{stride}={len(stride_links)}" for stride, stride_links in links_by_stride.items()))
    print(f"Links with range metrics: {range_metrics_attached}")
    print(f"Range metric lookup: {range_metric_lookup_counts}")
