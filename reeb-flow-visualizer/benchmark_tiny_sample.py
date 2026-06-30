#!/usr/bin/env python3
"""Run a small real benchmark of the Reeb-flow pipeline.

The script builds a temporary mini dataset by symlinking a few VTU files from an
existing dataset, runs the core pipeline stages on that subset, and writes JSON,
Markdown, and LaTeX reports. It does not edit common.py or the source dataset.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1")


@dataclass
class StageTiming:
    name: str
    seconds: float
    status: str
    note: str = ""


def natural_key(path: Path) -> tuple:
    parts = re.split(r"(\d+)", path.stem)
    return tuple(int(part) if part.isdigit() else part for part in parts)


def format_seconds(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "-"
    if seconds < 1.0:
        return f"{seconds:.2f} s"
    if seconds < 60.0:
        return f"{seconds:.1f} s"
    if seconds < 3600.0:
        return f"{seconds / 60.0:.2f} min"
    return f"{seconds / 3600.0:.2f} h"


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{num_bytes} B"


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() or item.is_symlink():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def count_files(path: Path, pattern: str = "*") -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob(pattern) if item.is_file() or item.is_symlink())


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def machine_specs() -> dict:
    specs = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "cpu_count_logical": os.cpu_count(),
    }

    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        specs["memory_total_bytes"] = int(memory.total)
    except Exception:
        specs["memory_total_bytes"] = None
        try:
            for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    specs["memory_total_bytes"] = int(parts[1]) * 1024
                    break
        except Exception:
            pass

    cpu_model = None
    cpu_count_physical = None
    try:
        text = Path("/proc/cpuinfo").read_text(errors="ignore")
        for line in text.splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
        physical_ids = set()
        core_ids = set()
        current_physical = None
        for line in text.splitlines():
            if line.startswith("physical id"):
                current_physical = line.split(":", 1)[1].strip()
                physical_ids.add(current_physical)
            elif line.startswith("core id") and current_physical is not None:
                core_ids.add((current_physical, line.split(":", 1)[1].strip()))
        if core_ids:
            cpu_count_physical = len(core_ids)
    except Exception:
        pass

    specs["cpu_model"] = cpu_model
    specs["cpu_count_physical"] = cpu_count_physical
    return specs


def vtu_stats(vtu_files: list[Path]) -> dict:
    stats = {
        "files": len(vtu_files),
        "total_size_bytes": sum(path.stat().st_size for path in vtu_files),
        "points": None,
        "cells": None,
    }
    try:
        import vtk  # type: ignore

        total_points = 0
        total_cells = 0
        for path in vtu_files:
            reader = vtk.vtkXMLUnstructuredGridReader()
            reader.SetFileName(str(path))
            reader.Update()
            output = reader.GetOutput()
            total_points += int(output.GetNumberOfPoints())
            total_cells += int(output.GetNumberOfCells())
        stats["points"] = total_points
        stats["cells"] = total_cells
    except Exception as exc:
        stats["vtu_read_error"] = f"{type(exc).__name__}: {exc}"
    return stats


def dataset_key_for_path(path: Path) -> str:
    name = path.name.lower()
    if "stilbene" in name:
        return "stilbene"
    if "torus" in name:
        return "torus"
    if "mvk" in name:
        return "mvk"
    raise ValueError(f"Cannot infer dataset type from {path}")


def prepare_mini_dataset(source_base: Path, output_root: Path, timesteps: int, replace: bool) -> tuple[Path, list[Path]]:
    source_vtu_dir = source_base / "downsampledGrids"
    if not source_vtu_dir.exists():
        raise FileNotFoundError(f"VTU directory not found: {source_vtu_dir}")

    selected = sorted(source_vtu_dir.glob("*.vtu"), key=natural_key)[:timesteps]
    if len(selected) < 2:
        raise RuntimeError(f"Need at least two VTU files, found {len(selected)} in {source_vtu_dir}")

    dataset_key = dataset_key_for_path(source_base)
    mini_base = output_root / f"{source_base.name}_benchmark_{len(selected)}"
    if replace and mini_base.exists():
        shutil.rmtree(mini_base)
    mini_vtu_dir = mini_base / "downsampledGrids"
    mini_vtu_dir.mkdir(parents=True, exist_ok=True)

    for src in selected:
        dst = mini_vtu_dir / src.name
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)

    marker = mini_base / "BENCHMARK_SOURCE.json"
    marker.write_text(json.dumps({
        "source_base": str(source_base),
        "dataset_key": dataset_key,
        "timesteps": [path.name for path in selected],
    }, indent=2) + "\n")
    return mini_base, selected


def configure_common_for_dataset(base_dir: Path, *, top: int, max_stride: int, workers: int) -> None:
    import common

    dataset_key = dataset_key_for_path(base_dir)
    config = common.DATASET_CONFIGS[dataset_key]
    common.BASE_DIR = base_dir
    common.dataset_key = dataset_key
    common.config = config
    common.TIMESTEP_LABEL_TO_FS_DIVISOR = float(config.get("timestep_label_to_fs_divisor", 41.341374575751))
    common.TIMESTEP_TIME_UNIT = "fs"
    common.TIMESTEP_TIME_DIGITS = 2
    common.FIBER_SURFACE_FIELD_F_ISOVALUE = config["f_isovalue"]
    common.FIBER_SURFACE_FIELD_G_ISOVALUE = config["g_isovalue"]
    common.FIBER_SURFACE_MODE = config.get("fiber_surface_mode", "fixed")
    common.FIBER_SURFACE_ADAPTIVE_ENABLED = False
    common.TOP_N_SHEETS = int(top)
    common.VIEWER_DEFAULT_TOP_SHEETS = min(10, int(top))
    common.SANKEY_TIMESTEP_STRIDE_MAX = int(max_stride)
    common.RESERVE_CORES = max(0, (os.cpu_count() or workers) - max(1, int(workers)))
    common.SHAPE_MATCHING_WORKERS = max(1, int(workers))

    common.VTU_DIR = base_dir / "downsampledGrids"
    common.RS_DIR = base_dir / "reebSpaces"
    common.RSI_DIR = base_dir / "sheetInfo"
    common.OUTPUT_DIR = base_dir / "sankey"
    common.RSI_JSON_DIR = common.OUTPUT_DIR / "rsi_json"
    common.FV99_PERTURBED_VTU_DIR = common.OUTPUT_DIR / "fv99_perturbed_vtu"
    common.UNIFIED_VIEWER_DIR = common.OUTPUT_DIR / "unified_sankey_viewer"
    common.VIEWER_DIR = common.UNIFIED_VIEWER_DIR
    common.TRACKING_DATA_FILE = common.OUTPUT_DIR / "tracking_data.json"
    common.TRACKING_ANALYSIS_DIR = common.OUTPUT_DIR / "tracking_analysis"
    common.TRACKING_ANALYSIS_VIEWER_FILE = common.TRACKING_ANALYSIS_DIR / "viewer_analysis.json"
    common.SHEET_IMAGE_DIR = base_dir / "sheetRendering"
    common.FIBER_SURFACE_DIR = base_dir / "sheetFiberSurfaces"
    common.FIBER_SURFACE_LABELED_DIR = common.FIBER_SURFACE_DIR / "labeled"
    common.FIBER_SURFACE_ADAPTIVE_LABELED_DIR = common.FIBER_SURFACE_DIR / "adaptive_labeled"
    common.FIBER_SURFACE_IMAGE_DIR = base_dir / "sheetFiberSurfaceImages"
    common.FIBER_SURFACE_TEMP_DIR = common.OUTPUT_DIR / "_fiber_tmp"
    common.FIBER_SURFACE_ADAPTIVE_TEMP_DIR = common.FIBER_SURFACE_TEMP_DIR / "adaptive"
    common.FIBER_SURFACE_MOLECULAR_STRUCTURE_DIR = common.VTU_DIR / "molecularStructure"
    common.OVERLAP_FILE = common.OUTPUT_DIR / "sheet_overlaps.json"
    common.SHEET_VTP_CACHE_DIR = base_dir / "compareSheetShapesCache" / "cache" / "vtp"
    common.FV99_FAILED_LOG_FILE = common.OUTPUT_DIR / "fv99_failed_files.log"
    common.FV99_PARTIAL_LOG_FILE = common.OUTPUT_DIR / "fv99_partial_files.log"
    common.FV99_RECOVERED_LOG_FILE = common.OUTPUT_DIR / "fv99_recovered_files.log"
    common.RSI_JSON_WARNINGS_LOG_FILE = common.OUTPUT_DIR / "rsi_json_warnings.log"
    common.LOW_SCALAR_ORIGIN_FILTER_LOG_FILE = common.OUTPUT_DIR / "low_scalar_origin_filter.log"
    common.OVERLAP_WARNINGS_LOG_FILE = common.OUTPUT_DIR / "sheet_overlap_warnings.log"
    common.FIBER_SURFACE_FAILED_LOG_FILE = common.OUTPUT_DIR / "fiber_surface_failed_files.log"
    common.FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE = common.OUTPUT_DIR / "adaptive_fiber_surface_failed_files.log"
    common.SHAPE_MATCHING_SKIPPED_LOG_FILE = common.OUTPUT_DIR / "shape_matching_skipped_timesteps.log"


@contextlib.contextmanager
def stage_timer(timings: list[StageTiming], name: str, note: str = ""):
    start = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        timings.append(StageTiming(name=name, seconds=time.perf_counter() - start, status=status, note=note))


def run_benchmark(args: argparse.Namespace) -> dict:
    source_base = args.dataset_base.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    mini_base, selected_vtus = prepare_mini_dataset(
        source_base=source_base,
        output_root=output_root,
        timesteps=args.timesteps,
        replace=args.replace,
    )
    configure_common_for_dataset(
        mini_base,
        top=args.top,
        max_stride=args.max_stride,
        workers=args.workers,
    )

    timings: list[StageTiming] = []

    import stage_01_run_fv99 as stage1
    import stage_02_build_sankey_data as stage2
    import stage_03_compute_sheet_overlaps as stage3
    import stage_06_analyze_tracking_results as stage6
    from compareSheetShapes.compare_sheet_shapes import main as run_shape_matching
    from unified_sankey_viewer import stage_07_unified_sankey_viewer as stage7

    if args.skip_labeled_fiber_surfaces:
        stage1.FIBER_SURFACE_ADAPTIVE_ENABLED = True
        stage1.FIBER_SURFACE_MODE = "benchmark_skip_labeled_fiber_surfaces"

    with stage_timer(
        timings,
        "Stage 1: fv99 Reeb-space extraction",
        "Includes Reeb-space files, RSI files, and sheet VTP geometry. Fixed labeled fiber surfaces are skipped by default in this benchmark.",
    ):
        stage1.run_fv99_stage()

    with stage_timer(timings, "Stage 2: RSI to JSON conversion"):
        stage2.build_rsi_json_stage()

    with stage_timer(
        timings,
        "Stage 3A: range sheet matching",
        f"Top {args.top} sheets, strides 1..{args.max_stride}, {args.workers} worker(s).",
    ):
        exit_code = run_shape_matching([
            "--workers",
            str(args.workers),
            "--top",
            str(args.top),
            "--max-stride",
            str(args.max_stride),
            "--rebuild-cache",
        ])
        if exit_code:
            raise RuntimeError(f"range sheet matching failed with exit code {exit_code}")

    with stage_timer(timings, "Stage 3B: domain overlap and metric attachment"):
        stage3.compute_sheet_overlaps_stage()

    stage7.configure_dataset_paths(mini_base)
    with stage_timer(timings, "Stage 5A: unified viewer data export"):
        stage7.build_unified_sankey_data_stage()

    with stage_timer(timings, "Stage 5B: event and feature analysis"):
        exit_code = stage6.analyze_tracking_results_stage([
            "--base-dir",
            str(mini_base),
            "--thresholds",
            ",".join(str(value) for value in args.thresholds),
            "--preferred-threshold",
            str(args.preferred_threshold),
        ])
        if exit_code:
            raise RuntimeError(f"tracking analysis failed with exit code {exit_code}")

    stage7.configure_dataset_paths(mini_base)
    with stage_timer(timings, "Stage 5C: HTML viewer export"):
        stage7.build_unified_sankey_viewer_stage(rebuild_data=False)

    tracking_data = read_json(mini_base / "sankey" / "tracking_data.json")
    overlap_data = read_json(mini_base / "sankey" / "sheet_overlaps.json")
    shape_summary = read_json(mini_base / "compareSheetShapesCache" / "results" / "sheet_shape_summary.json")

    report = {
        "source_dataset": str(source_base),
        "benchmark_dataset": str(mini_base),
        "timesteps": len(selected_vtus),
        "top_sheets": int(args.top),
        "max_stride": int(args.max_stride),
        "workers": int(args.workers),
        "skip_labeled_fiber_surfaces": bool(args.skip_labeled_fiber_surfaces),
        "input_vtu_stats": vtu_stats(selected_vtus),
        "machine": machine_specs(),
        "timings": [timing.__dict__ for timing in timings],
        "outputs": output_summary(mini_base),
        "graph": {
            "nodes": len(tracking_data.get("nodes", [])),
            "range_pairs": len(tracking_data.get("shape_pairs", [])),
            "domain_pairs": len(tracking_data.get("overlap_pairs", [])),
            "domain_links": overlap_data.get("num_links"),
            "shape_pairs": shape_summary.get("num_pairs") or shape_summary.get("pair_count"),
        },
        "parallelization": parallelization_notes(),
    }

    write_reports(report, mini_base / "benchmark_report")
    return report


def output_summary(base_dir: Path) -> dict:
    paths = {
        "reeb_space_outputs": base_dir / "reebSpaces",
        "rsi_outputs": base_dir / "sheetInfo",
        "sheet_vtp_cache": base_dir / "compareSheetShapesCache" / "cache" / "vtp",
        "range_matching_cache": base_dir / "compareSheetShapesCache",
        "sankey_outputs": base_dir / "sankey",
        "viewer_export": base_dir / "sankey" / "unified_sankey_viewer",
    }
    return {
        key: {
            "files": count_files(path),
            "size_bytes": tree_size(path),
        }
        for key, path in paths.items()
    }


def parallelization_notes() -> list[dict]:
    return [
        {
            "stage": "fv99 Reeb-space extraction",
            "parallelism": "embarrassingly parallel over timesteps",
            "implementation": "ThreadPoolExecutor launches independent fv99 processes.",
        },
        {
            "stage": "Range sheet matching",
            "parallelism": "parallel over timestep pairs and strides",
            "implementation": "ProcessPoolExecutor workers compare independent sheet-pair groups.",
        },
        {
            "stage": "Domain overlap",
            "parallelism": "conceptually parallel over timestep pairs",
            "implementation": "Current script computes it serially because it is usually lightweight compared with fv99 and shape matching.",
        },
        {
            "stage": "Sheet and fiber-surface image rendering",
            "parallelism": "embarrassingly parallel over timesteps, sheets, and isovalues",
            "implementation": "Pipeline rendering stages expose worker counts; these optional image stages are not included in the default tiny benchmark.",
        },
        {
            "stage": "Tracking summaries and viewer export",
            "parallelism": "mostly serial and lightweight",
            "implementation": "Runs after the graph has been built and writes JSON/HTML artifacts.",
        },
    ]


def machine_text(machine: dict) -> str:
    cpu = machine.get("cpu_model") or machine.get("processor") or "CPU"
    physical = machine.get("cpu_count_physical")
    logical = machine.get("cpu_count_logical")
    memory = machine.get("memory_total_bytes")
    memory_text = human_size(memory) if isinstance(memory, int) else "unknown memory"
    if physical and logical:
        return f"{cpu} ({physical} cores, {logical} hardware threads) with {memory_text} RAM"
    if logical:
        return f"{cpu} ({logical} hardware threads) with {memory_text} RAM"
    return f"{cpu} with {memory_text} RAM"


def write_reports(report: dict, prefix: Path) -> None:
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    tex_path = prefix.with_suffix(".tex")
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    md_path.write_text(markdown_report(report))
    tex_path.write_text(latex_report(report))
    print(f"Benchmark JSON: {json_path}")
    print(f"Benchmark Markdown: {md_path}")
    print(f"Benchmark LaTeX: {tex_path}")


def markdown_report(report: dict) -> str:
    timings = report["timings"]
    lines = [
        "# Tiny-sample pipeline benchmark",
        "",
        f"Source dataset: `{report['source_dataset']}`",
        f"Benchmark dataset: `{report['benchmark_dataset']}`",
        f"Machine: {machine_text(report['machine'])}",
        f"Input: {report['timesteps']} timesteps, top {report['top_sheets']} sheets, stride max {report['max_stride']}, {report['workers']} worker(s).",
        f"Input VTU size: {human_size(report['input_vtu_stats']['total_size_bytes'])}",
    ]
    if report["input_vtu_stats"].get("points") is not None:
        lines.append(
            f"Input VTU elements: {report['input_vtu_stats']['points']:,} points, "
            f"{report['input_vtu_stats']['cells']:,} cells across the benchmark subset."
        )
    lines.extend(["", "## Timings", "", "| Stage | Time | Note |", "|---|---:|---|"])
    for timing in timings:
        lines.append(f"| {timing['name']} | {format_seconds(timing['seconds'])} | {timing.get('note', '')} |")
    lines.extend(["", "## Output sizes", "", "| Artifact group | Files | Size |", "|---|---:|---:|"])
    for name, item in report["outputs"].items():
        lines.append(f"| {name.replace('_', ' ')} | {item['files']:,} | {human_size(item['size_bytes'])} |")
    lines.extend(["", "## Parallelization", ""])
    for item in report["parallelization"]:
        lines.append(f"- {item['stage']}: {item['parallelism']}. {item['implementation']}")
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
    timings = report["timings"]
    input_stats = report["input_vtu_stats"]
    total_seconds = sum(float(item["seconds"]) for item in timings)
    rows = "\n".join(
        f"{latex_escape(item['name'])} & {format_seconds(float(item['seconds']))} \\\\"
        for item in timings
    )
    output_rows = "\n".join(
        f"{latex_escape(name.replace('_', ' '))} & {item['files']:,} & {human_size(item['size_bytes'])} \\\\"
        for name, item in report["outputs"].items()
    )
    points_text = ""
    if input_stats.get("points") is not None:
        points_text = (
            f" The subset contains {int(input_stats['points']):,} grid vertices and "
            f"{int(input_stats['cells']):,} cells in total."
        )
    fiber_note = (
        "Fixed labeled fiber-surface extraction was disabled in this benchmark so that "
        "the first row measures Reeb-space extraction and sheet-geometry export; the "
        "image-rendering stages are embarrassingly parallel and are reported separately "
        "when figures are generated."
        if report.get("skip_labeled_fiber_surfaces")
        else "Fixed labeled fiber-surface extraction was included in the first stage."
    )

    return f"""\\subsection{{Performance and Interactivity}}
We evaluated the implementation on a small real subset of the {latex_escape(Path(report['source_dataset']).name)} dataset to provide an order-of-magnitude indication of runtime. The benchmark was run on {latex_escape(machine_text(report['machine']))}. The subset contains {report['timesteps']} consecutive timesteps, stores the top {report['top_sheets']} sheets per timestep, and uses direct timestep-pair comparisons up to stride {report['max_stride']}. The input VTU files occupy {human_size(input_stats['total_size_bytes'])}.{points_text} {fiber_note}

\\begin{{table}}[t]
\\centering
\\caption{{Tiny-sample benchmark timings.}}
\\label{{tab:tiny-benchmark-timings}}
\\begin{{tabular}}{{lr}}
\\toprule
Stage & Time \\\\
\\midrule
{rows}
\\midrule
Total & {format_seconds(total_seconds)} \\\\
\\bottomrule
\\end{{tabular}}
\\end{{table}}

\\begin{{table}}[t]
\\centering
\\caption{{Artifacts produced by the tiny-sample benchmark.}}
\\label{{tab:tiny-benchmark-artifacts}}
\\begin{{tabular}}{{lrr}}
\\toprule
Artifact group & Files & Size \\\\
\\midrule
{output_rows}
\\bottomrule
\\end{{tabular}}
\\end{{table}}

The most expensive stages are the external Reeb-space computation and the range-space sheet matching. Both are naturally parallel: Reeb spaces can be computed independently for each timestep, and range comparisons can be distributed over timestep pairs. Sheet and fiber-surface image rendering are also embarrassingly parallel over timesteps, sheets, and selected isovalues. Domain-overlap construction is pairwise over adjacent timesteps and can also be parallelized, although in our implementation it is typically much cheaper than Reeb-space extraction and range matching. The tracking summaries and viewer export are lightweight post-processing stages.

The browser interface loads precomputed JSON and image artifacts. Selecting nodes or links updates highlights and detail panels without recomputing correspondences. Threshold, opacity, and support-filter controls operate on the already loaded graph. The heaviest interactive operations are changes that rebuild the visible Sankey layout, especially the crossing-reduced ordering, because they evaluate the currently visible links. For the smaller torus and MVK graphs the full sequence remains interactive; for the 704-timestep stilbene graph, restricting the visible timestep window gives smoother interaction.
"""


def parse_thresholds(text: str) -> list[float]:
    values = []
    for part in text.split(","):
        part = part.strip()
        if part:
            values.append(float(part))
    return values or [0.3, 0.4, 0.5, 0.6, 0.7]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-base", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/reeb_flow_tiny_benchmark"))
    parser.add_argument("--timesteps", type=int, default=3)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-stride", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--thresholds", type=parse_thresholds, default=parse_thresholds("0.3,0.4,0.5,0.6,0.7"))
    parser.add_argument("--preferred-threshold", type=float, default=0.5)
    parser.add_argument("--replace", action="store_true", help="Delete any previous benchmark dataset at the output path.")
    parser.add_argument(
        "--include-labeled-fiber-surfaces",
        dest="skip_labeled_fiber_surfaces",
        action="store_false",
        help="Also run Stage 1 fixed labeled fiber-surface extraction for datasets where it is configured.",
    )
    parser.set_defaults(skip_labeled_fiber_surfaces=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_benchmark(args)
    print()
    print("Total benchmark time:", format_seconds(sum(float(item["seconds"]) for item in report["timings"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
