#!/usr/bin/env python3
"""Benchmark fixed fiber-surface computation and rendering on a mini dataset.

Run this after benchmark_tiny_sample.py. The script reuses the Reeb-space,
RSI, range-matching, and sheet-geometry artifacts already present in the mini
dataset and adds fixed +/-f and +/-g fiber-surface timings. Adaptive torus
fiber surfaces are intentionally not benchmarked here.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from benchmark_tiny_sample import (
    REPO_ROOT,
    configure_common_for_dataset,
    count_files,
    dataset_key_for_path,
    format_seconds,
    human_size,
    machine_specs,
    machine_text,
    tree_size,
)


@dataclass
class Timing:
    name: str
    seconds: float
    note: str = ""


def vtu_stems(base_dir: Path) -> list[str]:
    return sorted(path.stem for path in (base_dir / "downsampledGrids").glob("*.vtu"))


def configure_for_fiber(base_dir: Path, *, top: int, max_stride: int, workers: int) -> None:
    configure_common_for_dataset(base_dir, top=top, max_stride=max_stride, workers=workers)

    import common

    dataset_key = dataset_key_for_path(base_dir)
    config = common.DATASET_CONFIGS[dataset_key]
    common.config = config
    common.FIBER_SURFACE_MODE = config.get("fiber_surface_mode", "fixed")
    common.FIBER_SURFACE_ADAPTIVE_ENABLED = common.FIBER_SURFACE_MODE == "adaptive_f_range_change"
    common.FIBER_SURFACE_FIELD_F_ISOVALUE = config["f_isovalue"]
    common.FIBER_SURFACE_FIELD_G_ISOVALUE = config["g_isovalue"]
    common.FIBER_SURFACE_TOP_N_SHEETS = int(top)
    common.FIBER_SURFACE_WORKERS = int(workers)
    common.FIBER_SURFACE_REBUILD = True
    common.FIBER_SURFACE_RENDER_STATE_FILE = REPO_ROOT / config["state_file"]
    common.FIBER_SURFACE_RENDER_IMAGE_RESOLUTION = (1600, 1200)
    common.FIBER_SURFACE_RENDER_TIMEOUT_SECONDS = 300
    common.FIBER_SURFACE_RENDER_RETRIES = 2


def time_block(name: str, timings: list[Timing], note: str = ""):
    class _Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self

        def __exit__(self, exc_type, exc, tb):
            timings.append(Timing(name=name, seconds=time.perf_counter() - self.start, note=note))
            return False

    return _Timer()


def run_fixed_fiber_surfaces(base_dir: Path, stems: list[str], workers: int, timings: list[Timing]) -> None:
    import stage_01_run_fv99 as stage1
    import stage_04_compute_sheet_fiber_surfaces as stage4

    vtu_dir = base_dir / "downsampledGrids"
    rs_dir = base_dir / "reebSpaces"

    with time_block(
        "Fixed fiber-surface computation",
        timings,
        "Runs fv99 twice per timestep to produce labeled +/-f and +/-g fiber surfaces.",
    ):
        for stem in stems:
            vtu_file = vtu_dir / f"{stem}.vtu"
            rs_file = rs_dir / f"{stem}.rs"
            ok, details = stage1.generate_fiber_surfaces(vtu_file, rs_file)
            if not ok:
                failed = [str(item) for item in details if not item.get("ok")]
                raise RuntimeError(f"fiber-surface computation failed for {stem}: {failed}")

    with time_block(
        "Fiber-surface thresholding and rendering",
        timings,
        "Thresholds labeled surfaces to top sheets and renders PNGs with ParaView.",
    ):
        stage4.compute_sheet_fiber_surfaces_stage(
            selected_stems=set(stems),
            workers=workers,
            rebuild=True,
        )


def summarize_outputs(base_dir: Path) -> dict:
    labeled = base_dir / "sheetFiberSurfaces" / "labeled"
    images = base_dir / "sheetFiberSurfaceImages"
    return {
        "fixed_labeled_surfaces": {
            "files": count_files(labeled),
            "size_bytes": tree_size(labeled),
        },
        "rendered_images": {
            "files": count_files(images, "*.png"),
            "size_bytes": tree_size(images),
        },
    }


def write_reports(base_dir: Path, report: dict) -> None:
    prefix = base_dir / "fiber_benchmark_report"
    prefix.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    prefix.with_suffix(".md").write_text(markdown_report(report))
    prefix.with_suffix(".tex").write_text(latex_report(report))
    print(f"Fiber benchmark JSON: {prefix.with_suffix('.json')}")
    print(f"Fiber benchmark Markdown: {prefix.with_suffix('.md')}")
    print(f"Fiber benchmark LaTeX: {prefix.with_suffix('.tex')}")


def markdown_report(report: dict) -> str:
    lines = [
        "# Fiber-surface benchmark",
        "",
        f"Dataset: `{report['benchmark_dataset']}`",
        f"Machine: {machine_text(report['machine'])}",
        f"Timesteps: {report['timesteps']}",
        f"Top sheets: {report['top_sheets']}",
        f"Workers: {report['workers']}",
        "",
        "## Timings",
        "",
        "| Stage | Time | Note |",
        "|---|---:|---|",
    ]
    for item in report["timings"]:
        lines.append(f"| {item['name']} | {format_seconds(item['seconds'])} | {item.get('note', '')} |")
    lines.extend(["", "## Outputs", "", "| Artifact | Files | Size |", "|---|---:|---:|"])
    for name, item in report["outputs"].items():
        lines.append(f"| {name.replace('_', ' ')} | {item['files']:,} | {human_size(item['size_bytes'])} |")
    return "\n".join(lines) + "\n"


def latex_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\textbackslash{}")
        .replace("_", "\\_")
        .replace("&", "\\&")
        .replace("%", "\\%")
        .replace("#", "\\#")
    )


def latex_report(report: dict) -> str:
    rows = "\n".join(
        f"{latex_escape(item['name'])} & {format_seconds(float(item['seconds']))} \\\\"
        for item in report["timings"]
    )
    output_rows = "\n".join(
        f"{latex_escape(name.replace('_', ' '))} & {item['files']:,} & {human_size(item['size_bytes'])} \\\\"
        for name, item in report["outputs"].items()
    )
    return f"""\\begin{{table}}[t]
\\centering
\\caption{{Fiber-surface computation and rendering benchmark for {latex_escape(Path(report['benchmark_dataset']).name)}.}}
\\label{{tab:fiber-surface-benchmark-{latex_escape(Path(report['benchmark_dataset']).name)}}}
\\begin{{tabular}}{{lr}}
\\toprule
Stage & Time \\\\
\\midrule
{rows}
\\bottomrule
\\end{{tabular}}
\\end{{table}}

\\begin{{table}}[t]
\\centering
\\caption{{Fiber-surface artifacts produced for {latex_escape(Path(report['benchmark_dataset']).name)}.}}
\\label{{tab:fiber-surface-artifacts-{latex_escape(Path(report['benchmark_dataset']).name)}}}
\\begin{{tabular}}{{lrr}}
\\toprule
Artifact & Files & Size \\\\
\\midrule
{output_rows}
\\bottomrule
\\end{{tabular}}
\\end{{table}}
"""


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-base",
        type=Path,
        required=True,
        help="Mini dataset created by benchmark_tiny_sample.py.",
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-stride", type=int, default=4)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    base_dir = args.benchmark_base.expanduser().resolve()
    if not (base_dir / "downsampledGrids").exists():
        raise FileNotFoundError(f"benchmark dataset does not contain downsampledGrids: {base_dir}")

    configure_for_fiber(base_dir, top=args.top, max_stride=args.max_stride, workers=args.workers)

    import common

    if common.FIBER_SURFACE_ADAPTIVE_ENABLED:
        raise RuntimeError(
            "This benchmark intentionally covers only the fixed +/-f and +/-g "
            "fiber-surface path. The selected dataset uses adaptive fiber surfaces."
        )

    stems = vtu_stems(base_dir)
    timings: list[Timing] = []
    run_fixed_fiber_surfaces(base_dir, stems, args.workers, timings)

    report = {
        "benchmark_dataset": str(base_dir),
        "mode": "fixed",
        "timesteps": len(stems),
        "top_sheets": int(args.top),
        "workers": int(args.workers),
        "machine": machine_specs(),
        "timings": [timing.__dict__ for timing in timings],
        "outputs": summarize_outputs(base_dir),
    }
    write_reports(base_dir, report)
    print("Total fiber benchmark time:", format_seconds(sum(item.seconds for item in timings)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
