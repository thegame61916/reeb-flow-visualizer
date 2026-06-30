#!/usr/bin/env python3

"""Estimate pipeline and viewer performance from existing artifacts.

This script does not rerun the Reeb-space pipeline.  It inspects generated
files, embedded viewer data, and artifact modification-time spans to produce
ballpark performance numbers suitable for paper reporting.  Modification-time
spans are approximate: they are useful when a stage was generated in one pass,
but can be inflated or compressed if files were copied, regenerated
incrementally, or restored from cache.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_DATASET_ROOT = Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets")
DEFAULT_DATASETS = ("torus", "MVK_s1", "MVK_s2", "stilbene")
TIMED_LINE_RE = re.compile(r"::\s*([0-9]+(?:[.][0-9]+)?)\s+s\b")
PROGRESS_RE = re.compile(r"\[(\d+)m:(\d+)s\]\s+Augmenting Reeb space")


@dataclass
class ArtifactStats:
    name: str
    count: int
    bytes_total: int
    first_mtime: float | None
    last_mtime: float | None

    @property
    def span_seconds(self) -> float | None:
        if self.first_mtime is None or self.last_mtime is None:
            return None
        return max(0.0, self.last_mtime - self.first_mtime)


@dataclass
class LoggedTimingStats:
    count: int
    total_seconds: float
    median_seconds: float
    p90_seconds: float


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num_bytes} B"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 1:
        return f"{seconds:.2f} s"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours = minutes / 60
    return f"{hours:.2f} h"


def iter_files(base: Path, patterns: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path for path in base.glob(pattern) if path.is_file())
    return sorted(set(files))


def artifact_stats(name: str, base: Path, patterns: Iterable[str]) -> ArtifactStats:
    files = iter_files(base, patterns)
    mtimes: list[float] = []
    total = 0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        mtimes.append(stat.st_mtime)
    return ArtifactStats(
        name=name,
        count=len(mtimes),
        bytes_total=total,
        first_mtime=min(mtimes) if mtimes else None,
        last_mtime=max(mtimes) if mtimes else None,
    )


def parse_fv99_log_seconds(path: Path) -> float | None:
    """Estimate one fv99 run duration from its log text."""

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    timed_sum = sum(float(match.group(1)) for match in TIMED_LINE_RE.finditer(text))
    augment_seconds = 0.0
    for match in PROGRESS_RE.finditer(text):
        augment_seconds = max(augment_seconds, int(match.group(1)) * 60 + int(match.group(2)))
    total = timed_sum + augment_seconds
    return total if total > 0 else None


def logged_fv99_stats(dataset_dir: Path) -> LoggedTimingStats | None:
    values: list[float] = []
    for path in sorted((dataset_dir / "sankey").glob("*.fv99.log")):
        name = path.name
        if ".fiber." in name or ".perturb_" in name:
            continue
        seconds = parse_fv99_log_seconds(path)
        if seconds is not None:
            values.append(seconds)
    if not values:
        return None
    values.sort()
    p90_index = min(len(values) - 1, max(0, int(round(0.9 * (len(values) - 1)))))
    return LoggedTimingStats(
        count=len(values),
        total_seconds=sum(values),
        median_seconds=statistics.median(values),
        p90_seconds=values[p90_index],
    )


def load_viewer_data(dataset_dir: Path) -> dict:
    data_path = dataset_dir / "sankey" / "unified_sankey_viewer" / "data.json"
    if not data_path.exists():
        raise FileNotFoundError(f"Missing viewer data: {data_path}")
    return json.loads(data_path.read_text(encoding="utf-8"))


def count_links(data: dict, mode: str, stride: str = "1") -> int:
    key = "shape_pairs_by_stride" if mode == "range" else "overlap_pairs_by_stride"
    return sum(len(pair.get("matches", [])) for pair in data.get(key, {}).get(stride, []))


def metric_max(data: dict, metric: str) -> float:
    return float(data.get("meta", {}).get("metric_maxima", {}).get(metric, 0.0) or 0.0)


def stage_stats(dataset_dir: Path) -> list[ArtifactStats]:
    return [
        artifact_stats(
            "Input VTU files",
            dataset_dir,
            ["downsampledGrids/*.vtu"],
        ),
        artifact_stats(
            "Reeb-space outputs",
            dataset_dir,
            ["reebSpaces/*.rs", "sheetInfo/*.rsi", "sankey/rsi_json/*.rsijson"],
        ),
        artifact_stats(
            "Range sheet descriptors",
            dataset_dir,
            [
                "compareSheetShapesCache/cache/timesteps/*.json",
                "compareSheetShapesCache/cache/timesteps/*.npz",
                "compareSheetShapesCache/cache/vtp/*.vtp",
            ],
        ),
        artifact_stats(
            "Range pair matching",
            dataset_dir,
            [
                "compareSheetShapesCache/cache/matches/*.json",
                "compareSheetShapesCache/results/*.json",
            ],
        ),
        artifact_stats(
            "Domain overlap and analysis",
            dataset_dir,
            [
                "sankey/sheet_overlaps.json",
                "sankey/tracking_data.json",
                "sankey/tracking_analysis/*.csv",
                "sankey/tracking_analysis/*.json",
            ],
        ),
        artifact_stats(
            "Sheet and fiber images",
            dataset_dir,
            [
                "sankey/unified_sankey_viewer/sheet_images/**/*.png",
                "sankey/unified_sankey_viewer/fiber_surface_images/**/*.png",
            ],
        ),
        artifact_stats(
            "Unified viewer export",
            dataset_dir,
            [
                "sankey/unified_sankey_viewer/data.json",
                "sankey/unified_sankey_viewer/viewer.js",
                "sankey/unified_sankey_viewer/viewer_common.js",
                "sankey/unified_sankey_viewer/style.css",
                "sankey/unified_sankey_viewer/index.html",
            ],
        ),
    ]


def visible_link_comment(data: dict) -> str:
    range_links = count_links(data, "range")
    domain_links = count_links(data, "domain")
    timesteps = len(data.get("timesteps", []))
    if timesteps <= 120:
        return (
            "Full-sequence interaction should usually be responsive because the "
            f"adjacent range graph has {range_links:,} links and the domain graph "
            f"has {domain_links:,} links."
        )
    return (
        "Full-sequence interaction can become heavy because the adjacent range "
        f"graph has {range_links:,} links and the domain graph has {domain_links:,} "
        "links.  Interactive inspection is expected to be smoother when the "
        "visible timestep window is restricted."
    )


def summarize_dataset(dataset_dir: Path) -> str:
    data = load_viewer_data(dataset_dir)
    dataset = data.get("analysis", {}).get("dataset") or dataset_dir.name
    timesteps = len(data.get("timesteps", []))
    top_n = sorted({ts.get("top_n_sheets") for ts in data.get("timesteps", [])})
    shape_pairs = len(data.get("shape_pairs_by_stride", {}).get("1", []))
    domain_pairs = len(data.get("overlap_pairs_by_stride", {}).get("1", []))
    range_links = count_links(data, "range")
    domain_links = count_links(data, "domain")
    data_path = dataset_dir / "sankey" / "unified_sankey_viewer" / "data.json"
    data_size = data_path.stat().st_size if data_path.exists() else 0
    longest = 0
    for rows in data.get("analysis", {}).get("tracks_by_threshold", {}).values():
        for row in rows or []:
            longest = max(longest, int(row.get("length", 0) or 0))
    logged = logged_fv99_stats(dataset_dir)

    lines = [
        f"## {dataset}",
        "",
        "| Quantity | Value |",
        "|---|---:|",
        f"| Timesteps | {timesteps:,} |",
        f"| Stored top sheets per timestep | {', '.join(map(str, top_n)) if top_n else 'n/a'} |",
        f"| Adjacent range pairs | {shape_pairs:,} |",
        f"| Adjacent domain pairs | {domain_pairs:,} |",
        f"| Adjacent range links | {range_links:,} |",
        f"| Adjacent domain links | {domain_links:,} |",
        f"| Viewer data size | {human_size(data_size)} |",
        f"| Max raw domain overlap | {metric_max(data, 'overlap_vertices'):,.0f} vertices |",
        f"| Longest continuing feature at exported thresholds | {longest:,} timesteps |",
    ]
    if logged:
        parallel_wall = logged.total_seconds / max(1, min(logged.count, timesteps))
        lines.extend(
            [
                f"| Logged fv99 runs | {logged.count:,} |",
                f"| Logged fv99 median per timestep | {human_duration(logged.median_seconds)} |",
                f"| Logged fv99 p90 per timestep | {human_duration(logged.p90_seconds)} |",
                f"| Logged fv99 summed serial time | {human_duration(logged.total_seconds)} |",
                f"| Approx. fv99 wall time with one job per timestep | {human_duration(parallel_wall)} |",
            ]
        )
    lines.extend(
        [
            "",
            "### Artifact-derived timing estimates",
            "",
            "| Stage/artifact group | Files | Size | mtime span |",
            "|---|---:|---:|---:|",
        ]
    )

    for stats in stage_stats(dataset_dir):
        lines.append(
            f"| {stats.name} | {stats.count:,} | {human_size(stats.bytes_total)} | "
            f"{human_duration(stats.span_seconds)} |"
        )

    lines.extend(
        [
            "",
            "### Interactivity estimate",
            "",
            visible_link_comment(data),
            "",
            "Expected viewer behavior:",
            "",
            "- Selecting a node or link mainly updates highlights and detail panes; it should feel immediate for the visible window.",
            "- Adjusting threshold or opacity updates existing link visibility/styles without recomputing the backend data.",
            "- Changing metric, support filter, top-sheet count, timestep window, or node ordering recomputes the visible client-side graph layout.",
            "- Crossing-reduced ordering is the most expensive layout option because it evaluates edge orderings over the visible links.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=DEFAULT_DATASET_ROOT,
        help=f"Dataset root. Default: {DEFAULT_DATASET_ROOT}",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        help="Dataset name to include. Can be repeated. Defaults to torus, MVK_s1, MVK_s2, stilbene.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional Markdown output file.",
    )
    args = parser.parse_args()

    dataset_root = args.dataset_root.expanduser().resolve()
    dataset_names = args.dataset or list(DEFAULT_DATASETS)

    sections = [
        "# Reeb Flow Visualizer Performance Estimate",
        "",
        "These numbers are derived from existing artifacts. No pipeline stage is rerun.",
        "Artifact mtime spans are ballpark estimates and should be treated as approximate wall-clock ranges only when the corresponding files were generated in one pass.",
        "",
    ]
    for name in dataset_names:
        dataset_dir = dataset_root / name
        if not dataset_dir.exists():
            sections.append(f"## {name}\n\nMissing dataset directory: `{dataset_dir}`\n")
            continue
        sections.append(summarize_dataset(dataset_dir))

    output = "\n".join(sections).rstrip() + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
