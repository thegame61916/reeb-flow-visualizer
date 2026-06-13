#!/usr/bin/env python3

"""Generate paper-oriented diagnostics from existing tracking result JSON."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from common import (
    BASE_DIR,
    TRACKING_ANALYSIS_DIR,
    TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
    TRACKING_ANALYSIS_EVENT_SCORE_TERMS,
    TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT,
    TRACKING_ANALYSIS_THRESHOLDS,
    tracking_analysis_event_score,
    tracking_analysis_event_score_components,
    tracking_analysis_event_score_formula_text,
    TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
    TRACKING_ANALYSIS_TOP_FEATURES,
    TRACKING_ANALYSIS_TOP_INTERVALS,
    TRACKING_ANALYSIS_VIEWER_FILE,
    TRACKING_DATA_FILE,
)

SHAPE_METRICS = (
    "combined",
    "shape_iou",
    "area_ratio",
    "bbox_iou",
    "centroid_similarity",
)

OVERLAP_METRICS = (
    "overlap_vertices",
    "overlap_source_percent",
    "overlap_target_percent",
    "overlap_max_percent",
)

KNOWN_DATASET_DIRS = (
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1"),
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s2"),
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/stilbene"),
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus"),
)


def safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    return result if math.isfinite(result) else default


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def mean(values: Iterable[float]) -> float:
    cleaned = [value for value in values if math.isfinite(value)]
    return sum(cleaned) / len(cleaned) if cleaned else 0.0


def pstdev(values: Iterable[float]) -> float:
    cleaned = [value for value in values if math.isfinite(value)]
    return statistics.pstdev(cleaned) if len(cleaned) > 1 else 0.0


def quantile(values: Iterable[float], fraction: float) -> float:
    cleaned = sorted(value for value in values if math.isfinite(value))
    if not cleaned:
        return 0.0
    index = round(fraction * (len(cleaned) - 1))
    index = max(0, min(len(cleaned) - 1, index))
    return cleaned[index]


def correlation(xs: Iterable[float], ys: Iterable[float]) -> float:
    pairs = [
        (x, y)
        for x, y in zip(xs, ys)
        if math.isfinite(x) and math.isfinite(y)
    ]
    if len(pairs) < 2:
        return 0.0

    x_mean = mean(x for x, _ in pairs)
    y_mean = mean(y for _, y in pairs)
    x_var = sum((x - x_mean) ** 2 for x, _ in pairs)
    y_var = sum((y - y_mean) ** 2 for _, y in pairs)
    if x_var <= 0.0 or y_var <= 0.0:
        return 0.0

    cov = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    return cov / math.sqrt(x_var * y_var)


def rel_change(source: float, target: float) -> float:
    denom = max(abs(source), abs(target), 1e-12)
    return abs(target - source) / denom


def threshold_slug(value: float) -> str:
    return f"{value:g}".replace("-", "m").replace(".", "p")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def viewer_data_file(base_dir: Path) -> Path:
    if base_dir == BASE_DIR and TRACKING_DATA_FILE.exists():
        return TRACKING_DATA_FILE
    tracking_data = base_dir / "sankey" / "tracking_data.json"
    if tracking_data.exists():
        return tracking_data
    return base_dir / "sankey" / "unified_sankey_viewer" / "data.json"


def output_dir_for(base_dir: Path) -> Path:
    if base_dir == BASE_DIR:
        return TRACKING_ANALYSIS_DIR
    return base_dir / "sankey" / "tracking_analysis"


def viewer_analysis_file_for(base_dir: Path) -> Path:
    if base_dir == BASE_DIR:
        return TRACKING_ANALYSIS_VIEWER_FILE
    return base_dir / "sankey" / "tracking_analysis" / "viewer_analysis.json"


def node_key(timestep_index: int, sheet_id: int) -> str:
    return f"{safe_int(timestep_index)}:{safe_int(sheet_id)}"


def link_key(source_timestep_index: int, source_sheet_id: int, target_timestep_index: int, target_sheet_id: int) -> str:
    return f"{safe_int(source_timestep_index)}:{safe_int(source_sheet_id)}->{safe_int(target_timestep_index)}:{safe_int(target_sheet_id)}"


def get_shape_metrics(match: dict) -> dict[str, float]:
    metrics = match.get("metrics")
    if not isinstance(metrics, dict):
        metrics = match
    return {
        "combined": safe_float(metrics.get("combined", metrics.get("final_score"))),
        "shape_iou": safe_float(metrics.get("shape_iou")),
        "area_ratio": safe_float(metrics.get("area_ratio")),
        "bbox_iou": safe_float(metrics.get("bbox_iou")),
        "centroid_similarity": safe_float(metrics.get("centroid_similarity")),
    }


def get_overlap_metrics(match: dict) -> dict[str, float]:
    metrics = match.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}

    source_percent = safe_float(
        metrics.get("overlap_source_percent", match.get("source_percent"))
    )
    target_percent = safe_float(
        metrics.get("overlap_target_percent", match.get("target_percent"))
    )
    max_percent = safe_float(
        metrics.get("overlap_max_percent", max(source_percent, target_percent))
    )

    return {
        "overlap_vertices": safe_float(
            metrics.get("overlap_vertices", match.get("overlap_vertices"))
        ),
        "overlap_source_percent": source_percent,
        "overlap_target_percent": target_percent,
        "overlap_max_percent": max_percent,
    }


def metric_summary_row(
    dataset: str,
    scope: str,
    metric: str,
    values: list[float],
    combined_values: list[float] | None = None,
) -> dict:
    combined_values = combined_values or []
    return {
        "dataset": dataset,
        "scope": scope,
        "metric": metric,
        "count": len(values),
        "mean": mean(values),
        "std": pstdev(values),
        "min": min(values) if values else 0.0,
        "q05": quantile(values, 0.05),
        "q25": quantile(values, 0.25),
        "q50": quantile(values, 0.50),
        "q75": quantile(values, 0.75),
        "q95": quantile(values, 0.95),
        "max": max(values) if values else 0.0,
        "corr_with_shape_combined": (
            correlation(combined_values, values)
            if combined_values and len(combined_values) == len(values)
            else 0.0
        ),
    }


def collect_metric_summary(dataset: str, data: dict) -> list[dict]:
    shape_values = {metric: [] for metric in SHAPE_METRICS}
    for pair in data.get("shape_pairs", []):
        for match in pair.get("matches", []):
            metrics = get_shape_metrics(match)
            for metric in SHAPE_METRICS:
                shape_values[metric].append(metrics[metric])

    rows = [
        metric_summary_row(
            dataset,
            "shape",
            metric,
            values,
            shape_values["combined"],
        )
        for metric, values in shape_values.items()
    ]

    overlap_values = {metric: [] for metric in OVERLAP_METRICS}
    for pair in data.get("overlap_pairs", []):
        for match in pair.get("matches", []):
            metrics = get_overlap_metrics(match)
            for metric in OVERLAP_METRICS:
                overlap_values[metric].append(metrics[metric])

    rows.extend(
        metric_summary_row(dataset, "overlap", metric, values)
        for metric, values in overlap_values.items()
    )
    return rows


def shape_matches_by_pair(data: dict) -> dict[tuple[int, int], dict[int, list[dict]]]:
    result: dict[tuple[int, int], dict[int, list[dict]]] = {}
    for pair in data.get("shape_pairs", []):
        pair_key = (
            safe_int(pair.get("source_timestep_index")),
            safe_int(pair.get("target_timestep_index")),
        )
        groups: dict[int, list[dict]] = defaultdict(list)
        for match in pair.get("matches", []):
            groups[safe_int(match.get("source_sheet_id"))].append(match)
        result[pair_key] = groups
    return result


def overlap_matches_by_pair(data: dict) -> dict[tuple[int, int], dict[int, list[dict]]]:
    result: dict[tuple[int, int], dict[int, list[dict]]] = {}
    for pair in data.get("overlap_pairs", []):
        pair_key = (
            safe_int(pair.get("source_timestep_index")),
            safe_int(pair.get("target_timestep_index")),
        )
        groups: dict[int, list[dict]] = defaultdict(list)
        for match in pair.get("matches", []):
            groups[safe_int(match.get("source_sheet_id"))].append(match)
        result[pair_key] = groups
    return result


def best_shape_match(rows: list[dict], metric: str) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda item: get_shape_metrics(item).get(metric, 0.0))


def best_overlap_match(rows: list[dict], metric: str) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda item: get_overlap_metrics(item).get(metric, 0.0))


def collect_best_target_agreement(dataset: str, data: dict) -> list[dict]:
    rows: list[dict] = []
    shape_groups = shape_matches_by_pair(data)

    for metric in SHAPE_METRICS:
        if metric == "combined":
            continue

        totals = Counter()
        combined_losses: list[float] = []
        for pair_key, source_groups in shape_groups.items():
            for source_sheet_id, matches in source_groups.items():
                combined_best = best_shape_match(matches, "combined")
                metric_best = best_shape_match(matches, metric)
                if combined_best is None or metric_best is None:
                    continue

                totals["compared"] += 1
                if safe_int(combined_best.get("target_sheet_id")) == safe_int(
                    metric_best.get("target_sheet_id")
                ):
                    totals["agreements"] += 1

                combined_losses.append(
                    get_shape_metrics(combined_best)["combined"]
                    - get_shape_metrics(metric_best)["combined"]
                )

        compared = totals["compared"]
        rows.append(
            {
                "dataset": dataset,
                "candidate_scope": "shape",
                "candidate_metric": metric,
                "reference_scope": "shape",
                "reference_metric": "combined",
                "compared_sources": compared,
                "agreements": totals["agreements"],
                "agreement_fraction": (
                    totals["agreements"] / compared if compared else 0.0
                ),
                "mean_reference_loss_if_candidate_used": mean(combined_losses),
            }
        )

    overlap_groups = overlap_matches_by_pair(data)
    totals = Counter()
    combined_losses = []
    for pair_key, source_groups in overlap_groups.items():
        shape_source_groups = shape_groups.get(pair_key, {})
        for source_sheet_id, matches in source_groups.items():
            shape_matches = shape_source_groups.get(source_sheet_id, [])
            combined_best = best_shape_match(shape_matches, "combined")
            overlap_best = best_overlap_match(matches, "overlap_max_percent")
            if combined_best is None or overlap_best is None:
                continue

            totals["compared"] += 1
            if safe_int(combined_best.get("target_sheet_id")) == safe_int(
                overlap_best.get("target_sheet_id")
            ):
                totals["agreements"] += 1

            overlap_target = safe_int(overlap_best.get("target_sheet_id"))
            shape_for_overlap_target = next(
                (
                    match
                    for match in shape_matches
                    if safe_int(match.get("target_sheet_id")) == overlap_target
                ),
                None,
            )
            if shape_for_overlap_target is not None:
                combined_losses.append(
                    get_shape_metrics(combined_best)["combined"]
                    - get_shape_metrics(shape_for_overlap_target)["combined"]
                )

    compared = totals["compared"]
    rows.append(
        {
            "dataset": dataset,
            "candidate_scope": "overlap",
            "candidate_metric": "overlap_max_percent",
            "reference_scope": "shape",
            "reference_metric": "combined",
            "compared_sources": compared,
            "agreements": totals["agreements"],
            "agreement_fraction": totals["agreements"] / compared if compared else 0.0,
            "mean_reference_loss_if_candidate_used": mean(combined_losses),
        }
    )

    return rows


def sheet_lookup(data: dict) -> dict[tuple[int, int], dict]:
    lookup = {}
    for timestep in data.get("timesteps", []):
        timestep_index = safe_int(timestep.get("timestep_index"))
        for sheet in timestep.get("sheets", []):
            lookup[(timestep_index, safe_int(sheet.get("sheet_id")))] = sheet
    return lookup


def sheets_for_timestep(data: dict, timestep_index: int) -> list[dict]:
    timesteps = data.get("timesteps", [])
    if 0 <= timestep_index < len(timesteps):
        return list(timesteps[timestep_index].get("sheets", []))
    return []


def area_summaries(sheets: list[dict]) -> dict[str, float]:
    areas = sorted((safe_float(sheet.get("area")) for sheet in sheets), reverse=True)
    return {
        "top1_area": sum(areas[:1]),
        "top5_area": sum(areas[:5]),
        "top20_area": sum(areas[:20]),
    }


def pair_label(pair: dict) -> str:
    return f"{pair.get('source_label')}->{pair.get('target_label')}"


def pair_domain_shape_agreement(
    pair: dict,
    shape_groups: dict[tuple[int, int], dict[int, list[dict]]],
    overlap_groups: dict[tuple[int, int], dict[int, list[dict]]],
) -> dict:
    pair_key = (
        safe_int(pair.get("source_timestep_index")),
        safe_int(pair.get("target_timestep_index")),
    )
    shape_source_groups = shape_groups.get(pair_key, {})
    overlap_source_groups = overlap_groups.get(pair_key, {})

    compared = 0
    agreements = 0
    disagreements = 0

    for source_sheet_id, overlap_matches in overlap_source_groups.items():
        shape_matches = shape_source_groups.get(source_sheet_id, [])
        shape_best = best_shape_match(shape_matches, "combined")
        overlap_best = best_overlap_match(overlap_matches, "overlap_max_percent")
        if shape_best is None or overlap_best is None:
            continue

        compared += 1
        if safe_int(shape_best.get("target_sheet_id")) == safe_int(
            overlap_best.get("target_sheet_id")
        ):
            agreements += 1
        else:
            disagreements += 1

    return {
        "domain_shape_compared_sources": compared,
        "domain_shape_agreements": agreements,
        "domain_shape_disagreements": disagreements,
        "domain_shape_agreement_fraction": agreements / compared if compared else 0.0,
    }


def collect_event_scores(
    dataset: str,
    data: dict,
    thresholds: tuple[float, ...],
) -> list[dict]:
    rows: list[dict] = []
    shape_groups = shape_matches_by_pair(data)
    overlap_groups = overlap_matches_by_pair(data)

    for pair in data.get("shape_pairs", []):
        source_index = safe_int(pair.get("source_timestep_index"))
        target_index = safe_int(pair.get("target_timestep_index"))
        source_sheets = sheets_for_timestep(data, source_index)
        target_sheets = sheets_for_timestep(data, target_index)
        pair_key = (source_index, target_index)

        by_source = shape_groups.get(pair_key, {})
        by_target: dict[int, list[dict]] = defaultdict(list)
        for matches in by_source.values():
            for match in matches:
                by_target[safe_int(match.get("target_sheet_id"))].append(match)

        best_source_combined = []
        best_source_shape_iou = []
        best_target_combined = []
        for sheet in source_sheets:
            sheet_id = safe_int(sheet.get("sheet_id"))
            matches = by_source.get(sheet_id, [])
            combined_best = best_shape_match(matches, "combined")
            shape_iou_best = best_shape_match(matches, "shape_iou")
            best_source_combined.append(
                get_shape_metrics(combined_best)["combined"] if combined_best else 0.0
            )
            best_source_shape_iou.append(
                get_shape_metrics(shape_iou_best)["shape_iou"]
                if shape_iou_best
                else 0.0
            )

        for sheet in target_sheets:
            sheet_id = safe_int(sheet.get("sheet_id"))
            combined_best = best_shape_match(by_target.get(sheet_id, []), "combined")
            best_target_combined.append(
                get_shape_metrics(combined_best)["combined"] if combined_best else 0.0
            )

        source_areas = area_summaries(source_sheets)
        target_areas = area_summaries(target_sheets)
        domain_shape = pair_domain_shape_agreement(pair, shape_groups, overlap_groups)
        source_sheet_count = len(source_sheets)
        mean_best_combined = mean(best_source_combined)

        for threshold in thresholds:
            source_weak_count = sum(
                1 for value in best_source_combined if value < threshold
            )
            target_weak_count = sum(
                1 for value in best_target_combined if value < threshold
            )
            possible_splits = sum(
                1
                for matches in by_source.values()
                if sum(
                    1
                    for match in matches
                    if get_shape_metrics(match)["combined"] >= threshold
                )
                >= 2
            )
            possible_merges = sum(
                1
                for matches in by_target.values()
                if sum(
                    1
                    for match in matches
                    if get_shape_metrics(match)["combined"] >= threshold
                )
                >= 2
            )
            event_components = tracking_analysis_event_score_components(
                source_weak_count=source_weak_count,
                target_weak_count=target_weak_count,
                possible_splits=possible_splits,
                possible_merges=possible_merges,
                mean_best_combined=mean_best_combined,
                source_sheet_count=source_sheet_count,
            )
            event_score = tracking_analysis_event_score(event_components)

            rows.append(
                {
                    "dataset": dataset,
                    "source_timestep_index": source_index,
                    "target_timestep_index": target_index,
                    "source_label": pair.get("source_label"),
                    "target_label": pair.get("target_label"),
                    "source_stem": pair.get("source_stem"),
                    "target_stem": pair.get("target_stem"),
                    "pair_label": pair_label(pair),
                    "threshold": threshold,
                    "source_sheet_count": source_sheet_count,
                    "target_sheet_count": len(target_sheets),
                    "candidate_match_count": safe_int(pair.get("pair_count")),
                    "mean_best_combined": mean_best_combined,
                    "min_best_combined": min(best_source_combined)
                    if best_source_combined
                    else 0.0,
                    "mean_best_shape_iou": mean(best_source_shape_iou),
                    "source_weak_count": source_weak_count,
                    "target_weak_count": target_weak_count,
                    "possible_splits": possible_splits,
                    "possible_merges": possible_merges,
                    "event_score": event_score,
                    "source_top1_area": source_areas["top1_area"],
                    "target_top1_area": target_areas["top1_area"],
                    "top1_area_rel_change": rel_change(
                        source_areas["top1_area"], target_areas["top1_area"]
                    ),
                    "source_top5_area": source_areas["top5_area"],
                    "target_top5_area": target_areas["top5_area"],
                    "top5_area_rel_change": rel_change(
                        source_areas["top5_area"], target_areas["top5_area"]
                    ),
                    "source_top20_area": source_areas["top20_area"],
                    "target_top20_area": target_areas["top20_area"],
                    "top20_area_rel_change": rel_change(
                        source_areas["top20_area"], target_areas["top20_area"]
                    ),
                    **domain_shape,
                }
            )

    return rows


def build_edges(data: dict, threshold: float) -> dict[tuple[int, int], tuple[tuple[int, int], float]]:
    edges = {}
    for pair in data.get("shape_pairs", []):
        source_index = safe_int(pair.get("source_timestep_index"))
        target_index = safe_int(pair.get("target_timestep_index"))
        groups: dict[int, list[dict]] = defaultdict(list)
        for match in pair.get("matches", []):
            groups[safe_int(match.get("source_sheet_id"))].append(match)

        for source_sheet_id, matches in groups.items():
            best = best_shape_match(matches, "combined")
            if best is None:
                continue
            score = get_shape_metrics(best)["combined"]
            if score >= threshold:
                edges[(source_index, source_sheet_id)] = (
                    (target_index, safe_int(best.get("target_sheet_id"))),
                    score,
                )
    return edges


def collect_sheet_lifetimes(
    dataset: str,
    data: dict,
    thresholds: tuple[float, ...],
) -> list[dict]:
    rows: list[dict] = []
    node_meta = sheet_lookup(data)

    for threshold in thresholds:
        edges = build_edges(data, threshold)
        incoming: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for source, (target, _score) in edges.items():
            incoming[target].append(source)

        starts = sorted(node for node in node_meta if not incoming.get(node))
        used_starts = set()
        track_id = 0
        for start in starts:
            if start in used_starts:
                continue

            track_id += 1
            current = start
            seen = set()
            nodes = []
            scores = []

            while current in node_meta and current not in seen:
                seen.add(current)
                nodes.append(current)
                edge = edges.get(current)
                if edge is None:
                    break
                current, score = edge
                scores.append(score)

            used_starts.add(start)

            sheets = [node_meta[node] for node in nodes]
            areas = [safe_float(sheet.get("area")) for sheet in sheets]
            ranks = [safe_int(sheet.get("rank")) for sheet in sheets]
            timestep_labels = [
                str(data["timesteps"][node[0]].get("label"))
                for node in nodes
                if 0 <= node[0] < len(data.get("timesteps", []))
            ]

            node_path = [node_key(timestep, sheet_id) for timestep, sheet_id in nodes]
            link_path = [
                link_key(nodes[index][0], nodes[index][1], nodes[index + 1][0], nodes[index + 1][1])
                for index in range(max(0, len(nodes) - 1))
            ]

            rows.append(
                {
                    "dataset": dataset,
                    "threshold": threshold,
                    "track_id": track_id,
                    "length": len(nodes),
                    "start_timestep_index": nodes[0][0] if nodes else "",
                    "end_timestep_index": nodes[-1][0] if nodes else "",
                    "start_label": timestep_labels[0] if timestep_labels else "",
                    "end_label": timestep_labels[-1] if timestep_labels else "",
                    "start_sheet_id": nodes[0][1] if nodes else "",
                    "end_sheet_id": nodes[-1][1] if nodes else "",
                    "rank_min": min(ranks) if ranks else 0,
                    "rank_max": max(ranks) if ranks else 0,
                    "area_mean": mean(areas),
                    "area_first": areas[0] if areas else 0.0,
                    "area_last": areas[-1] if areas else 0.0,
                    "mean_continuation_score": mean(scores),
                    "min_continuation_score": min(scores) if scores else 0.0,
                    "sheet_path": " ".join(str(node[1]) for node in nodes),
                    "node_path": " ".join(node_path),
                    "link_path": " ".join(link_path),
                }
            )

    return rows


def find_sheet_image(base_dir: Path, stem: str, sheet_id: int) -> str | None:
    folder = base_dir / "sheetRendering" / stem
    preferred = folder / f"sheet_{sheet_id}.png"
    if preferred.exists():
        return str(preferred)

    legacy = sorted(folder.glob(f"{sheet_id}_*.png")) if folder.exists() else []
    return str(legacy[0]) if legacy else None


def find_fiber_image(base_dir: Path, stem: str, sheet_id: int) -> str | None:
    image = base_dir / "sheetFiberSurfaceImages" / stem / f"sheet_{sheet_id}.png"
    return str(image) if image.exists() else None


def interesting_match_record(
    base_dir: Path,
    pair: dict,
    match: dict,
    source_kind: str,
) -> dict:
    source_sheet_id = safe_int(match.get("source_sheet_id"))
    target_sheet_id = safe_int(match.get("target_sheet_id"))
    source_stem = str(pair.get("source_stem") or "")
    target_stem = str(pair.get("target_stem") or "")
    return {
        "kind": source_kind,
        "source_sheet_id": source_sheet_id,
        "target_sheet_id": target_sheet_id,
        "source_rank": safe_int(match.get("source_rank")),
        "target_rank": safe_int(match.get("target_rank")),
        "source_area": safe_float(match.get("source_area")),
        "target_area": safe_float(match.get("target_area")),
        "source_num_vertices": safe_int(match.get("source_num_vertices")),
        "target_num_vertices": safe_int(match.get("target_num_vertices")),
        "metrics": get_shape_metrics(match),
        "source_sheet_image": find_sheet_image(base_dir, source_stem, source_sheet_id),
        "target_sheet_image": find_sheet_image(base_dir, target_stem, target_sheet_id),
        "source_fiber_image": find_fiber_image(base_dir, source_stem, source_sheet_id),
        "target_fiber_image": find_fiber_image(base_dir, target_stem, target_sheet_id),
    }


def collect_interesting_intervals(
    dataset: str,
    base_dir: Path,
    data: dict,
    event_rows: list[dict],
    preferred_threshold: float,
    top_n: int,
) -> dict:
    event_candidates = [
        row for row in event_rows if row["threshold"] == preferred_threshold
    ]
    event_candidates.sort(
        key=lambda row: (
            safe_float(row.get("event_score")),
            safe_float(row.get("top1_area_rel_change")),
            safe_float(row.get("domain_shape_disagreements")),
        ),
        reverse=True,
    )
    event_candidates = event_candidates[:top_n]

    pair_index = {
        (
            safe_int(pair.get("source_timestep_index")),
            safe_int(pair.get("target_timestep_index")),
        ): pair
        for pair in data.get("shape_pairs", [])
    }

    intervals = []
    for row in event_candidates:
        pair_key = (
            safe_int(row.get("source_timestep_index")),
            safe_int(row.get("target_timestep_index")),
        )
        pair = pair_index.get(pair_key)
        if pair is None:
            continue

        source_groups: dict[int, list[dict]] = defaultdict(list)
        for match in pair.get("matches", []):
            source_groups[safe_int(match.get("source_sheet_id"))].append(match)

        best_per_source = [
            best
            for matches in source_groups.values()
            if (best := best_shape_match(matches, "combined")) is not None
        ]
        best_per_source.sort(key=lambda match: get_shape_metrics(match)["combined"])
        weakest = best_per_source[:5]
        strongest = list(reversed(best_per_source[-5:]))

        intervals.append(
            {
                "dataset": dataset,
                "source_label": row["source_label"],
                "target_label": row["target_label"],
                "source_stem": row["source_stem"],
                "target_stem": row["target_stem"],
                "threshold": preferred_threshold,
                "selection_metrics": row,
                "source_full_sheet_image": str(
                    base_dir
                    / "sheetRendering"
                    / str(row["source_stem"])
                    / f"{row['source_stem']}.png"
                ),
                "target_full_sheet_image": str(
                    base_dir
                    / "sheetRendering"
                    / str(row["target_stem"])
                    / f"{row['target_stem']}.png"
                ),
                "weakest_best_source_matches": [
                    interesting_match_record(base_dir, pair, match, "weak_best_source")
                    for match in weakest
                ],
                "strongest_best_source_matches": [
                    interesting_match_record(base_dir, pair, match, "strong_best_source")
                    for match in strongest
                ],
            }
        )

    return {
        "dataset": dataset,
        "base_dir": str(base_dir),
        "preferred_threshold": preferred_threshold,
        "event_score_formula": tracking_analysis_event_score_formula_text(),
        "event_score_terms": list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS),
        "intervals": intervals,
    }


def shape_pair_index(data: dict) -> dict[tuple[int, int], dict]:
    return {
        (
            safe_int(pair.get("source_timestep_index")),
            safe_int(pair.get("target_timestep_index")),
        ): pair
        for pair in data.get("shape_pairs", [])
    }


def interval_viewer_record(data: dict, pair: dict, row: dict, threshold: float) -> dict:
    source_index = safe_int(row.get("source_timestep_index"))
    target_index = safe_int(row.get("target_timestep_index"))

    by_source: dict[int, list[dict]] = defaultdict(list)
    by_target: dict[int, list[dict]] = defaultdict(list)
    for match in pair.get("matches", []):
        by_source[safe_int(match.get("source_sheet_id"))].append(match)
        by_target[safe_int(match.get("target_sheet_id"))].append(match)

    source_sheets = sheets_for_timestep(data, source_index)
    target_sheets = sheets_for_timestep(data, target_index)
    highlight_nodes: set[str] = set()
    highlight_links: set[str] = set()
    weak_source_nodes: list[str] = []
    weak_target_nodes: list[str] = []
    split_source_nodes: list[str] = []
    merge_target_nodes: list[str] = []

    for sheet in source_sheets:
        source_sheet_id = safe_int(sheet.get("sheet_id"))
        best = best_shape_match(by_source.get(source_sheet_id, []), "combined")
        best_score = get_shape_metrics(best)["combined"] if best else 0.0
        if best_score < threshold:
            key = node_key(source_index, source_sheet_id)
            weak_source_nodes.append(key)
            highlight_nodes.add(key)
            if best:
                target_sheet_id = safe_int(best.get("target_sheet_id"))
                highlight_nodes.add(node_key(target_index, target_sheet_id))
                highlight_links.add(link_key(source_index, source_sheet_id, target_index, target_sheet_id))

    for sheet in target_sheets:
        target_sheet_id = safe_int(sheet.get("sheet_id"))
        best = best_shape_match(by_target.get(target_sheet_id, []), "combined")
        best_score = get_shape_metrics(best)["combined"] if best else 0.0
        if best_score < threshold:
            key = node_key(target_index, target_sheet_id)
            weak_target_nodes.append(key)
            highlight_nodes.add(key)
            if best:
                source_sheet_id = safe_int(best.get("source_sheet_id"))
                highlight_nodes.add(node_key(source_index, source_sheet_id))
                highlight_links.add(link_key(source_index, source_sheet_id, target_index, target_sheet_id))

    for source_sheet_id, matches in by_source.items():
        above = [match for match in matches if get_shape_metrics(match)["combined"] >= threshold]
        if len(above) >= 2:
            split_source_nodes.append(node_key(source_index, source_sheet_id))
            highlight_nodes.add(node_key(source_index, source_sheet_id))
            for match in above:
                target_sheet_id = safe_int(match.get("target_sheet_id"))
                highlight_nodes.add(node_key(target_index, target_sheet_id))
                highlight_links.add(link_key(source_index, source_sheet_id, target_index, target_sheet_id))

    for target_sheet_id, matches in by_target.items():
        above = [match for match in matches if get_shape_metrics(match)["combined"] >= threshold]
        if len(above) >= 2:
            merge_target_nodes.append(node_key(target_index, target_sheet_id))
            highlight_nodes.add(node_key(target_index, target_sheet_id))
            for match in above:
                source_sheet_id = safe_int(match.get("source_sheet_id"))
                highlight_nodes.add(node_key(source_index, source_sheet_id))
                highlight_links.add(link_key(source_index, source_sheet_id, target_index, target_sheet_id))

    return {
        "id": f"interval:{threshold_slug(threshold)}:{source_index}:{target_index}",
        "threshold": threshold,
        "source_timestep_index": source_index,
        "target_timestep_index": target_index,
        "source_label": row.get("source_label"),
        "target_label": row.get("target_label"),
        "source_stem": row.get("source_stem"),
        "target_stem": row.get("target_stem"),
        "pair_label": row.get("pair_label"),
        "event_score": safe_float(row.get("event_score")),
        "mean_best_combined": safe_float(row.get("mean_best_combined")),
        "min_best_combined": safe_float(row.get("min_best_combined")),
        "source_weak_count": safe_int(row.get("source_weak_count")),
        "target_weak_count": safe_int(row.get("target_weak_count")),
        "possible_splits": safe_int(row.get("possible_splits")),
        "possible_merges": safe_int(row.get("possible_merges")),
        "domain_shape_agreement_fraction": safe_float(row.get("domain_shape_agreement_fraction")),
        "highlight": {
            "nodes": sorted(highlight_nodes),
            "links": sorted(highlight_links),
            "weak_source_nodes": weak_source_nodes,
            "weak_target_nodes": weak_target_nodes,
            "split_source_nodes": split_source_nodes,
            "merge_target_nodes": merge_target_nodes,
        },
    }


def collect_viewer_intervals(data: dict, event_rows: list[dict], thresholds: tuple[float, ...], top_n: int) -> dict[str, list[dict]]:
    pair_index = shape_pair_index(data)
    result: dict[str, list[dict]] = {}
    for threshold in thresholds:
        rows = [row for row in event_rows if safe_float(row.get("threshold")) == threshold]
        rows.sort(key=lambda row: safe_float(row.get("event_score")), reverse=True)
        records = []
        for row in rows[:top_n]:
            pair = pair_index.get((safe_int(row.get("source_timestep_index")), safe_int(row.get("target_timestep_index"))))
            if pair is None:
                continue
            records.append(interval_viewer_record(data, pair, row, threshold))
        result[str(threshold)] = records
    return result


def collect_viewer_tracks(lifetime_rows: list[dict], thresholds: tuple[float, ...], top_n: int) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for threshold in thresholds:
        rows = [row for row in lifetime_rows if safe_float(row.get("threshold")) == threshold]
        rows.sort(
            key=lambda row: (
                safe_int(row.get("length")),
                safe_float(row.get("mean_continuation_score")),
                safe_float(row.get("min_continuation_score")),
            ),
            reverse=True,
        )
        records = []
        for row in rows[:top_n]:
            node_path = [item for item in str(row.get("node_path", "")).split() if item]
            link_path = [item for item in str(row.get("link_path", "")).split() if item]
            records.append(
                {
                    "id": f"track:{threshold_slug(threshold)}:{safe_int(row.get('track_id'))}",
                    "threshold": threshold,
                    "track_id": safe_int(row.get("track_id")),
                    "length": safe_int(row.get("length")),
                    "start_timestep_index": safe_int(row.get("start_timestep_index")),
                    "end_timestep_index": safe_int(row.get("end_timestep_index")),
                    "start_label": row.get("start_label"),
                    "end_label": row.get("end_label"),
                    "start_sheet_id": safe_int(row.get("start_sheet_id")),
                    "end_sheet_id": safe_int(row.get("end_sheet_id")),
                    "rank_min": safe_int(row.get("rank_min")),
                    "rank_max": safe_int(row.get("rank_max")),
                    "area_mean": safe_float(row.get("area_mean")),
                    "mean_continuation_score": safe_float(row.get("mean_continuation_score")),
                    "min_continuation_score": safe_float(row.get("min_continuation_score")),
                    "highlight": {"nodes": node_path, "links": link_path},
                }
            )
        result[str(threshold)] = records
    return result


def collect_sensitivity_summary(event_rows: list[dict], lifetime_rows: list[dict], thresholds: tuple[float, ...]) -> list[dict]:
    rows = []
    for threshold in thresholds:
        events = [row for row in event_rows if safe_float(row.get("threshold")) == threshold]
        tracks = [row for row in lifetime_rows if safe_float(row.get("threshold")) == threshold]
        top_event = max(events, key=lambda row: safe_float(row.get("event_score")), default={})
        lengths = sorted(safe_int(row.get("length")) for row in tracks)
        rows.append(
            {
                "threshold": threshold,
                "event_count": len(events),
                "mean_event_score": mean(safe_float(row.get("event_score")) for row in events),
                "max_event_score": safe_float(top_event.get("event_score")),
                "top_event_pair_label": top_event.get("pair_label", ""),
                "top_event_source_timestep_index": safe_int(top_event.get("source_timestep_index")),
                "top_event_target_timestep_index": safe_int(top_event.get("target_timestep_index")),
                "track_count": len(tracks),
                "max_lifetime": max(lengths) if lengths else 0,
                "median_lifetime": quantile(lengths, 0.5) if lengths else 0,
                "mean_lifetime": mean(lengths),
            }
        )
    return rows


def collect_domain_shape_disagreements(data: dict) -> tuple[list[dict], list[dict]]:
    shape_groups = shape_matches_by_pair(data)
    overlap_groups = overlap_matches_by_pair(data)
    examples: list[dict] = []
    summaries: dict[tuple[int, int], dict] = {}

    for pair in data.get("shape_pairs", []):
        source_index = safe_int(pair.get("source_timestep_index"))
        target_index = safe_int(pair.get("target_timestep_index"))
        pair_key = (source_index, target_index)
        shape_source_groups = shape_groups.get(pair_key, {})
        overlap_source_groups = overlap_groups.get(pair_key, {})

        compared = 0
        agreements = 0
        pair_examples: list[dict] = []

        for source_sheet_id, overlap_matches in overlap_source_groups.items():
            shape_matches = shape_source_groups.get(source_sheet_id, [])
            shape_best = best_shape_match(shape_matches, "combined")
            overlap_best = best_overlap_match(overlap_matches, "overlap_max_percent")
            if shape_best is None or overlap_best is None:
                continue

            compared += 1
            shape_target = safe_int(shape_best.get("target_sheet_id"))
            overlap_target = safe_int(overlap_best.get("target_sheet_id"))
            shape_score = get_shape_metrics(shape_best)["combined"]
            overlap_score = get_overlap_metrics(overlap_best)["overlap_max_percent"]
            if shape_target == overlap_target:
                agreements += 1
                continue

            shape_for_overlap_target = next(
                (
                    match
                    for match in shape_matches
                    if safe_int(match.get("target_sheet_id")) == overlap_target
                ),
                None,
            )
            overlap_for_shape_target = next(
                (
                    match
                    for match in overlap_matches
                    if safe_int(match.get("target_sheet_id")) == shape_target
                ),
                None,
            )
            shape_score_for_domain_target = (
                get_shape_metrics(shape_for_overlap_target)["combined"]
                if shape_for_overlap_target
                else 0.0
            )
            overlap_score_for_range_target = (
                get_overlap_metrics(overlap_for_shape_target)["overlap_max_percent"]
                if overlap_for_shape_target
                else 0.0
            )
            shape_loss = max(0.0, shape_score - shape_score_for_domain_target)
            overlap_loss = max(0.0, overlap_score - overlap_score_for_range_target)
            confidence = min(shape_score, overlap_score)
            disagreement_score = 0.5 * (shape_loss + overlap_loss) * confidence

            example = {
                "id": f"disagreement:{source_index}:{target_index}:{source_sheet_id}",
                "source_timestep_index": source_index,
                "target_timestep_index": target_index,
                "source_label": pair.get("source_label"),
                "target_label": pair.get("target_label"),
                "source_sheet_id": source_sheet_id,
                "shape_target_sheet_id": shape_target,
                "overlap_target_sheet_id": overlap_target,
                "shape_score": shape_score,
                "overlap_max_percent": overlap_score,
                "shape_score_for_domain_target": shape_score_for_domain_target,
                "overlap_score_for_range_target": overlap_score_for_range_target,
                "shape_loss": shape_loss,
                "overlap_loss": overlap_loss,
                "confidence": confidence,
                "disagreement_score": disagreement_score,
                "source_node": node_key(source_index, source_sheet_id),
                "shape_target_node": node_key(target_index, shape_target),
                "overlap_target_node": node_key(target_index, overlap_target),
                "shape_link": link_key(source_index, source_sheet_id, target_index, shape_target),
                "overlap_link": link_key(source_index, source_sheet_id, target_index, overlap_target),
                "highlight": {
                    "nodes": sorted({node_key(source_index, source_sheet_id), node_key(target_index, shape_target), node_key(target_index, overlap_target)}),
                    "links": sorted({link_key(source_index, source_sheet_id, target_index, shape_target), link_key(source_index, source_sheet_id, target_index, overlap_target)}),
                },
            }
            pair_examples.append(example)
            examples.append(example)

        if pair_examples:
            pair_examples.sort(key=lambda item: safe_float(item.get("disagreement_score")), reverse=True)
            scores = [safe_float(item.get("disagreement_score")) for item in pair_examples]
            shape_losses = [safe_float(item.get("shape_loss")) for item in pair_examples]
            overlap_losses = [safe_float(item.get("overlap_loss")) for item in pair_examples]
            summaries[pair_key] = {
                "id": f"disagreement_pair:{source_index}:{target_index}",
                "source_timestep_index": source_index,
                "target_timestep_index": target_index,
                "source_label": pair.get("source_label"),
                "target_label": pair.get("target_label"),
                "pair_label": f"{pair.get('source_label')}->{pair.get('target_label')}",
                "compared_sources": compared,
                "agreement_count": agreements,
                "disagreement_count": len(pair_examples),
                "agreement_fraction": agreements / compared if compared else 0.0,
                "disagreement_fraction": len(pair_examples) / compared if compared else 0.0,
                "max_disagreement_score": max(scores) if scores else 0.0,
                "mean_disagreement_score": mean(scores),
                "max_shape_loss": max(shape_losses) if shape_losses else 0.0,
                "mean_shape_loss": mean(shape_losses),
                "max_overlap_loss": max(overlap_losses) if overlap_losses else 0.0,
                "mean_overlap_loss": mean(overlap_losses),
                "strongest_disagreement": pair_examples[0] if pair_examples else None,
            }

    examples.sort(key=lambda item: safe_float(item.get("disagreement_score")), reverse=True)
    summary_rows = sorted(
        summaries.values(),
        key=lambda item: (
            safe_float(item.get("max_disagreement_score")),
            safe_float(item.get("disagreement_fraction")),
            safe_int(item.get("disagreement_count")),
        ),
        reverse=True,
    )
    return examples, summary_rows


def collect_domain_shape_disagreement_examples(data: dict, limit: int = 200) -> list[dict]:
    examples, _summary = collect_domain_shape_disagreements(data)
    return examples[:limit]


def collect_domain_shape_disagreement_summary(data: dict, limit: int | None = None) -> list[dict]:
    _examples, summary = collect_domain_shape_disagreements(data)
    return summary if limit is None else summary[:limit]


def build_viewer_analysis(
    dataset: str,
    base_dir: Path,
    data: dict,
    metric_rows: list[dict],
    agreement_rows: list[dict],
    event_rows: list[dict],
    lifetime_rows: list[dict],
    thresholds: tuple[float, ...],
    preferred_threshold: float,
    top_intervals: int,
    top_features: int,
    top_disagreements: int,
) -> dict:
    return {
        "dataset": dataset,
        "base_dir": str(base_dir),
        "thresholds": list(thresholds),
        "preferred_threshold": preferred_threshold,
        "top_intervals": top_intervals,
        "top_features": top_features,
        "top_disagreements": top_disagreements,
        "split_merge_weight": TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT,
        "event_score_terms": list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS),
        "event_score_formula": tracking_analysis_event_score_formula_text(),
        "metric_summary": metric_rows,
        "best_target_agreement": agreement_rows,
        "sensitivity": collect_sensitivity_summary(event_rows, lifetime_rows, thresholds),
        "intervals_by_threshold": collect_viewer_intervals(data, event_rows, thresholds, top_intervals),
        "tracks_by_threshold": collect_viewer_tracks(lifetime_rows, thresholds, top_features),
        "domain_shape_disagreement_summary": collect_domain_shape_disagreement_summary(data, top_disagreements),
        "domain_shape_disagreements": collect_domain_shape_disagreement_examples(data),
        "notes": {
            "interval_score": "Higher event score means weaker or more ambiguous sheet continuation between adjacent timesteps.",
            "track_score": "Continuing features are greedy best-combined-score tracks at the selected theta.",
            "disagreement": "Domain-vs-range disagreements compare overlap_max_percent best targets against combined range-shape best targets.",
        },
    }


def plot_outputs(
    output_dir: Path,
    event_rows: list[dict],
    lifetime_rows: list[dict],
    preferred_threshold: float,
) -> list[str]:
    warnings = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return [f"matplotlib unavailable; skipped plots: {exc}"]

    plot_dir = output_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    threshold_rows = [
        row for row in event_rows if row["threshold"] == preferred_threshold
    ]
    threshold_rows.sort(key=lambda row: safe_int(row["source_timestep_index"]))

    def save_current(name: str) -> None:
        for suffix in (".png", ".pdf"):
            plt.savefig(plot_dir / f"{name}{suffix}", bbox_inches="tight", dpi=180)
        plt.close()

    if threshold_rows:
        labels = [str(row["source_label"]) for row in threshold_rows]
        x = list(range(len(threshold_rows)))
        tick_step = max(1, len(x) // 12)

        plt.figure(figsize=(11, 4))
        plt.plot(x, [safe_float(row["event_score"]) for row in threshold_rows])
        plt.title(f"Event score (threshold {preferred_threshold:g})")
        plt.xlabel("Adjacent timestep pair")
        plt.ylabel("Event score")
        plt.xticks(x[::tick_step], labels[::tick_step], rotation=45, ha="right")
        save_current(f"event_score_threshold_{threshold_slug(preferred_threshold)}")

        plt.figure(figsize=(11, 4))
        plt.plot(
            x,
            [safe_float(row["mean_best_combined"]) for row in threshold_rows],
            label="mean best combined",
        )
        plt.plot(
            x,
            [safe_float(row["mean_best_shape_iou"]) for row in threshold_rows],
            label="mean best shape IoU",
        )
        plt.title("Mean best continuation scores")
        plt.xlabel("Adjacent timestep pair")
        plt.ylabel("Score")
        plt.xticks(x[::tick_step], labels[::tick_step], rotation=45, ha="right")
        plt.legend()
        save_current("mean_best_scores")

        plt.figure(figsize=(11, 4))
        plt.plot(x, [safe_float(row["source_top1_area"]) for row in threshold_rows], label="top 1")
        plt.plot(x, [safe_float(row["source_top5_area"]) for row in threshold_rows], label="top 5")
        plt.plot(x, [safe_float(row["source_top20_area"]) for row in threshold_rows], label="top 20")
        plt.title("Sheet area summaries")
        plt.xlabel("Timestep")
        plt.ylabel("Area")
        plt.xticks(x[::tick_step], labels[::tick_step], rotation=45, ha="right")
        plt.legend()
        save_current("sheet_area_summaries")

        plt.figure(figsize=(11, 4))
        plt.plot(
            x,
            [
                safe_float(row["domain_shape_agreement_fraction"])
                for row in threshold_rows
            ],
        )
        plt.title("Domain-vs-shape best-target agreement")
        plt.xlabel("Adjacent timestep pair")
        plt.ylabel("Agreement fraction")
        plt.xticks(x[::tick_step], labels[::tick_step], rotation=45, ha="right")
        save_current("domain_shape_agreement")

    threshold_lifetimes = [
        row for row in lifetime_rows if row["threshold"] == preferred_threshold
    ]
    if threshold_lifetimes:
        plt.figure(figsize=(7, 4))
        plt.hist(
            [safe_int(row["length"]) for row in threshold_lifetimes],
            bins=30,
        )
        plt.title(f"Sheet track lifetimes (threshold {preferred_threshold:g})")
        plt.xlabel("Track length in timesteps")
        plt.ylabel("Count")
        save_current(f"lifetime_hist_threshold_{threshold_slug(preferred_threshold)}")

    return warnings


def analyze_dataset(
    base_dir: Path,
    thresholds: tuple[float, ...],
    preferred_threshold: float,
    top_intervals: int,
    top_features: int,
    top_disagreements: int,
) -> dict:
    dataset = base_dir.name
    data_file = viewer_data_file(base_dir)
    if not data_file.exists():
        raise FileNotFoundError(f"viewer data missing: {data_file}")

    data = read_json(data_file)
    output_dir = output_dir_for(base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metric_rows = collect_metric_summary(dataset, data)
    agreement_rows = collect_best_target_agreement(dataset, data)
    event_rows = collect_event_scores(dataset, data, thresholds)
    lifetime_rows = collect_sheet_lifetimes(dataset, data, thresholds)
    intervals = collect_interesting_intervals(
        dataset,
        base_dir,
        data,
        event_rows,
        preferred_threshold,
        top_intervals,
    )
    viewer_analysis = build_viewer_analysis(
        dataset,
        base_dir,
        data,
        metric_rows,
        agreement_rows,
        event_rows,
        lifetime_rows,
        thresholds,
        preferred_threshold,
        top_intervals,
        top_features,
        top_disagreements,
    )

    write_csv(
        output_dir / "metric_summary.csv",
        metric_rows,
        [
            "dataset",
            "scope",
            "metric",
            "count",
            "mean",
            "std",
            "min",
            "q05",
            "q25",
            "q50",
            "q75",
            "q95",
            "max",
            "corr_with_shape_combined",
        ],
    )
    write_csv(
        output_dir / "best_target_agreement.csv",
        agreement_rows,
        [
            "dataset",
            "candidate_scope",
            "candidate_metric",
            "reference_scope",
            "reference_metric",
            "compared_sources",
            "agreements",
            "agreement_fraction",
            "mean_reference_loss_if_candidate_used",
        ],
    )
    write_csv(
        output_dir / "event_scores.csv",
        event_rows,
        [
            "dataset",
            "source_timestep_index",
            "target_timestep_index",
            "source_label",
            "target_label",
            "source_stem",
            "target_stem",
            "pair_label",
            "threshold",
            "source_sheet_count",
            "target_sheet_count",
            "candidate_match_count",
            "mean_best_combined",
            "min_best_combined",
            "mean_best_shape_iou",
            "source_weak_count",
            "target_weak_count",
            "possible_splits",
            "possible_merges",
            "event_score",
            "source_top1_area",
            "target_top1_area",
            "top1_area_rel_change",
            "source_top5_area",
            "target_top5_area",
            "top5_area_rel_change",
            "source_top20_area",
            "target_top20_area",
            "top20_area_rel_change",
            "domain_shape_compared_sources",
            "domain_shape_agreements",
            "domain_shape_disagreements",
            "domain_shape_agreement_fraction",
        ],
    )
    write_csv(
        output_dir / "sheet_lifetimes.csv",
        lifetime_rows,
        [
            "dataset",
            "threshold",
            "track_id",
            "length",
            "start_timestep_index",
            "end_timestep_index",
            "start_label",
            "end_label",
            "start_sheet_id",
            "end_sheet_id",
            "rank_min",
            "rank_max",
            "area_mean",
            "area_first",
            "area_last",
            "mean_continuation_score",
            "min_continuation_score",
            "sheet_path",
            "node_path",
            "link_path",
        ],
    )

    (output_dir / "interesting_intervals.json").write_text(
        json.dumps(intervals, indent=2, allow_nan=False)
    )
    viewer_analysis_path = viewer_analysis_file_for(base_dir)
    viewer_analysis_path.parent.mkdir(parents=True, exist_ok=True)
    viewer_analysis_path.write_text(json.dumps(viewer_analysis, indent=2, allow_nan=False))

    plot_warnings = plot_outputs(
        output_dir,
        event_rows,
        lifetime_rows,
        preferred_threshold,
    )

    metadata = {
        "dataset": dataset,
        "base_dir": str(base_dir),
        "viewer_data_file": str(data_file),
        "output_dir": str(output_dir),
        "thresholds": list(thresholds),
        "preferred_threshold": preferred_threshold,
        "top_intervals": top_intervals,
        "top_features": top_features,
        "split_merge_weight": TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT,
        "event_score_terms": list(TRACKING_ANALYSIS_EVENT_SCORE_TERMS),
        "event_score_formula": tracking_analysis_event_score_formula_text(),
        "num_timesteps": len(data.get("timesteps", [])),
        "num_shape_pairs": len(data.get("shape_pairs", [])),
        "num_overlap_pairs": len(data.get("overlap_pairs", [])),
        "num_metric_rows": len(metric_rows),
        "num_agreement_rows": len(agreement_rows),
        "num_event_rows": len(event_rows),
        "num_lifetime_rows": len(lifetime_rows),
        "plot_warnings": plot_warnings,
        "outputs": {
            "metric_summary": str(output_dir / "metric_summary.csv"),
            "best_target_agreement": str(output_dir / "best_target_agreement.csv"),
            "event_scores": str(output_dir / "event_scores.csv"),
            "sheet_lifetimes": str(output_dir / "sheet_lifetimes.csv"),
            "interesting_intervals": str(output_dir / "interesting_intervals.json"),
            "viewer_analysis": str(viewer_analysis_path),
            "plots": str(output_dir / "plots"),
        },
    }
    (output_dir / "analysis_metadata.json").write_text(
        json.dumps(metadata, indent=2, allow_nan=False)
    )

    return metadata


def parse_thresholds(value: str | None) -> tuple[float, ...]:
    if not value:
        return tuple(float(item) for item in TRACKING_ANALYSIS_THRESHOLDS)
    thresholds = tuple(
        float(item.strip())
        for item in value.split(",")
        if item.strip()
    )
    if not thresholds:
        raise ValueError("at least one threshold is required")
    return thresholds


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=BASE_DIR,
        help="Dataset base directory to analyze. Defaults to common.BASE_DIR.",
    )
    parser.add_argument(
        "--all-known",
        action="store_true",
        help="Analyze MVK_s1, MVK_s2, stilbene, and torus.",
    )
    parser.add_argument(
        "--thresholds",
        help="Comma-separated continuity thresholds, e.g. 0.3,0.4,0.5,0.6,0.7.",
    )
    parser.add_argument(
        "--preferred-threshold",
        type=float,
        default=TRACKING_ANALYSIS_PREFERRED_THRESHOLD,
        help="Threshold used for interesting interval ranking and plots.",
    )
    parser.add_argument(
        "--top-intervals",
        type=int,
        default=TRACKING_ANALYSIS_TOP_INTERVALS,
        help="Number of highest-scoring intervals to store in interesting_intervals.json and viewer_analysis.json.",
    )
    parser.add_argument(
        "--top-features",
        type=int,
        default=TRACKING_ANALYSIS_TOP_FEATURES,
        help="Number of longest continuing features to store in viewer_analysis.json.",
    )
    parser.add_argument(
        "--top-disagreements",
        type=int,
        default=TRACKING_ANALYSIS_TOP_DISAGREEMENTS,
        help="Number of highest-scoring domain/range disagreement timestep pairs to store in viewer_analysis.json.",
    )
    return parser.parse_args(argv)


def analyze_tracking_results_stage(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    thresholds = parse_thresholds(args.thresholds)

    if args.preferred_threshold not in thresholds:
        thresholds = tuple(sorted((*thresholds, args.preferred_threshold)))

    dataset_dirs = KNOWN_DATASET_DIRS if args.all_known else (args.base_dir,)

    metadata = []
    for base_dir in dataset_dirs:
        print(f"Analyzing tracking results: {base_dir}", flush=True)
        result = analyze_dataset(
            base_dir=base_dir,
            thresholds=thresholds,
            preferred_threshold=args.preferred_threshold,
            top_intervals=max(1, args.top_intervals),
            top_features=max(1, args.top_features),
            top_disagreements=max(1, args.top_disagreements),
        )
        metadata.append(result)
        print(f"Analysis output: {result['output_dir']}", flush=True)

    if args.all_known:
        combined_output = dataset_dirs[0].parent / "tracking_analysis_all_known_summary.json"
        combined_output.write_text(json.dumps(metadata, indent=2, allow_nan=False))
        print(f"Combined summary: {combined_output.resolve()}", flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    return analyze_tracking_results_stage(argv)


if __name__ == "__main__":
    raise SystemExit(main())
