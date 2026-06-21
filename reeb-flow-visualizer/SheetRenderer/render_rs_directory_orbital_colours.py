#!/usr/bin/env python3
"""
Batch render Reeb-space overview images with distinct arrange-and-traverse
sheet colors. Individual sheet images use the configured uniform sheet color.

Uses project paths from common.py, then run:

    python3 scripts/render_rs_directory_orbital_colours.py

For every .rs file in RS_DIRECTORY, this script looks for matching .rsi and
Stage 1 cached sheet geometry VTP files by stem, and writes only PNG files:

    OUTPUT_DIRECTORY/<rs-stem>/<rs-stem>.png
    OUTPUT_DIRECTORY/<rs-stem>/sheet_<sheet-id>.png

Cached VTP behavior:
  - sheet VTPs are owned by Stage 1
  - this renderer only reads the Stage 1 cache and never calls fv99
  - --rebuild-cache/--clean-cache are kept for CLI compatibility only

When SHEET_RENDERER_USE_GLOBAL_BOUNDS is enabled in common.py, all output PNGs
use the same global 2D sheet-space frame and fixed canvas size.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    RSI_DIR,
    RS_DIR,
    SHEET_IMAGE_DIR,
    SHEET_RENDERER_GLOBAL_PADDING,
    SHEET_RENDERER_IMAGE_SIZE,
    SHEET_RENDERER_REPLACE_EXISTING_IMAGES,
    SHEET_RENDERER_RENDER_TIMEOUT_SECONDS,
    SHEET_RENDERER_TEMP_DIR,
    SHEET_RENDERER_UNIFORM_SHEET_COLOR,
    SHEET_RENDERER_USE_GLOBAL_BOUNDS,
    SHEET_VTP_CACHE_DIR,
    SHEET_RENDERER_WORKERS,
    TOP_N_SHEETS,
    PVPYTHON,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTK_LIB_DIR,
    VTU_DIR,
)
from render_rs_sheets import (  # noqa: E402
    read_sheet_vtp,
    sheet_areas,
)


# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

RS_DIRECTORY = RS_DIR
RSI_DIRECTORY = RSI_DIR
VTU_DIRECTORY = VTU_DIR
OUTPUT_DIRECTORY = SHEET_IMAGE_DIR

LD_LIBRARY_PATH = os.pathsep.join(
    str(path)
    for path in (VTK_LIB_DIR, TTK_BUILD_LIB_DIR, TTK_INSTALL_LIB_DIR)
    if path.exists()
)

# The exported sheet geometry can be hundreds of MB per time step. Keep a
# dedicated temp directory under the configured output root.
TEMP_DIRECTORY = SHEET_RENDERER_TEMP_DIR
TEMP_PREFIX = "rs-render-"
STALE_TEMP_MAX_AGE_HOURS = 6
MIN_TEMP_FREE_GB = 50
MIN_OUTPUT_FREE_GB = 5
VTP_CACHE_DIR = SHEET_VTP_CACHE_DIR
PARAVIEW_SHEET_RENDER_HELPER = REPO_ROOT / "render_sheet_surface_state.py"
SHEET_RENDER_LOG_DIR = OUTPUT_DIRECTORY / "logs"
SHEET_OVERVIEW_OPACITY = 0.50
SHEET_OVERVIEW_BASE_OPACITY = 0.50
SHEET_SELECTED_OPACITY = 0.97
SHEET_CONTEXT_OPACITY = 0.4
SHEET_CONTEXT_BOUNDARY_OPACITY = 0.25
SHEET_CONTEXT_BOUNDARY_WIDTH = 1.0
SHEET_CONTEXT_COLOR = (0.86, 0.86, 0.84)
SHEET_CONTEXT_BOUNDARY_COLOR = (0.38, 0.38, 0.36)

RENDER_ENV = os.environ.copy()
RENDER_ENV.setdefault("QT_QPA_PLATFORM", "offscreen")

UNIFORM_SHEET_COLOR = SHEET_RENDERER_UNIFORM_SHEET_COLOR
# High-contrast qualitative palette used for full Reeb-space overviews.
# Rank 0 is the largest sheet, rank 1 the next, and the palette wraps for
# later ranks.
PAPER_SHEET_COLORS: tuple[tuple[float, float, float], ...] = (
    (0.121, 0.466, 0.705),  # blue
    (1.000, 0.498, 0.054),  # orange
    (0.173, 0.627, 0.173),  # green
    (0.839, 0.153, 0.157),  # red
    (0.580, 0.404, 0.741),  # purple
    (0.549, 0.337, 0.294),  # brown
    (0.890, 0.467, 0.761),  # pink
    (0.498, 0.498, 0.498),  # gray
    (0.737, 0.741, 0.133),  # olive
    (0.090, 0.745, 0.811),  # cyan
    (0.682, 0.780, 0.909),  # light blue
    (1.000, 0.733, 0.471),  # light orange
    (0.596, 0.875, 0.541),  # light green
    (1.000, 0.596, 0.588),  # light red
    (0.773, 0.690, 0.835),  # light purple
    (0.769, 0.612, 0.580),  # light brown
    (0.969, 0.714, 0.824),  # light pink
    (0.780, 0.780, 0.780),  # light gray
    (0.859, 0.859, 0.553),  # light olive
    (0.620, 0.855, 0.898),  # light cyan
)


@dataclass(frozen=True)
class RsiData:
    is_vertex_singular: list[bool]
    sheet_area: dict[int, float]
    sheet_vertices: dict[int, list[int]]


class BinaryReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        self.offset = 0

    def _read(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.offset + size > len(self.data):
            raise ValueError(f"Unexpected end of file while reading {self.path}")
        value = struct.unpack_from(fmt, self.data, self.offset)
        self.offset += size
        return value[0] if len(value) == 1 else value

    def int32(self) -> int:
        return self._read("<i")

    def uint8(self) -> int:
        return self._read("<B")

    def float64(self) -> float:
        return self._read("<d")

    def size(self) -> int:
        # The C++ writer serializes size_t on 64-bit Linux.
        return self._read("<Q")


def read_rsi(path: Path) -> RsiData:
    reader = BinaryReader(path)

    is_vertex_singular = [bool(reader.uint8()) for _ in range(reader.size())]

    sheet_area: dict[int, float] = {}
    for _ in range(reader.size()):
        sheet_id = reader.int32()
        area = reader.float64()
        sheet_area[sheet_id] = area

    sheet_vertices: dict[int, list[int]] = {}
    for _ in range(reader.size()):
        sheet_id = reader.int32()
        vertices = [reader.int32() for _ in range(reader.size())]
        sheet_vertices[sheet_id] = vertices

    return RsiData(
        is_vertex_singular=is_vertex_singular,
        sheet_area=sheet_area,
        sheet_vertices=sheet_vertices,
    )


def free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def require_free_space(path: Path, min_free_gb: float, label: str) -> None:
    available = free_gb(path)
    if available < min_free_gb:
        raise RuntimeError(
            f"{label} has only {available:.1f} GiB free at {path}; "
            f"need at least {min_free_gb:.1f} GiB."
        )


def cleanup_stale_temp_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STALE_TEMP_MAX_AGE_HOURS * 3600

    for child in path.iterdir():
        if not child.is_dir() or not child.name.startswith(TEMP_PREFIX):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                print(f"Removed stale temp directory {child}")
        except FileNotFoundError:
            continue


def sheet_polygons_bounds(sheet_polygons) -> tuple[float, float, float, float] | None:
    finite_points = [
        (x, y)
        for x, y in sheet_polygons.points
        if math.isfinite(x) and math.isfinite(y)
    ]
    if not finite_points:
        return None

    xs = [point[0] for point in finite_points]
    ys = [point[1] for point in finite_points]
    return min(xs), min(ys), max(xs), max(ys)


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


def expand_bounds_to_canvas(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = bounds
    width = xmax - xmin
    height = ymax - ymin

    if width <= 0.0:
        xmin -= 0.5
        xmax += 0.5
        width = xmax - xmin
    if height <= 0.0:
        ymin -= 0.5
        ymax += 0.5
        height = ymax - ymin

    pad = max(0.0, float(SHEET_RENDERER_GLOBAL_PADDING))
    xmin -= width * pad
    xmax += width * pad
    ymin -= height * pad
    ymax += height * pad
    width = xmax - xmin
    height = ymax - ymin

    pixel_width, pixel_height = SHEET_RENDERER_IMAGE_SIZE
    target_aspect = float(pixel_width) / float(pixel_height) if pixel_height else 1.0
    current_aspect = width / height if height else target_aspect
    cx = (xmin + xmax) * 0.5
    cy = (ymin + ymax) * 0.5

    if current_aspect > target_aspect:
        height = width / target_aspect
    else:
        width = height * target_aspect

    return (
        cx - width * 0.5,
        cy - height * 0.5,
        cx + width * 0.5,
        cy + height * 0.5,
    )


def paper_sheet_color_for_rank(rank: int) -> tuple[float, float, float]:
    return PAPER_SHEET_COLORS[rank % len(PAPER_SHEET_COLORS)]


def sheet_colors_by_area_rank(rsi: RsiData, sheet_polygons) -> dict[int, tuple[float, float, float]]:
    sheet_ids = set(sheet_polygons.sheet_ids)
    ranked_sheet_ids = sorted(
        sheet_ids,
        key=lambda sheet_id: (-float(rsi.sheet_area.get(sheet_id, 0.0)), sheet_id),
    )
    return {
        sheet_id: paper_sheet_color_for_rank(rank)
        for rank, sheet_id in enumerate(ranked_sheet_ids)
    }


def color_list(rgb: tuple[float, float, float]) -> list[float]:
    return [float(rgb[0]), float(rgb[1]), float(rgb[2])]


def render_sheet_images_with_paraview(
    stem: str,
    vtp_path: Path,
    output_dir: Path,
    sheets: list[int],
    colors_by_sheet: dict[int, tuple[float, float, float]],
    render_bounds: tuple[float, float, float, float] | None,
) -> None:
    images: list[dict] = [
        {
            "mode": "overview",
            "output": str(output_dir / f"{stem}.png"),
            "colors_by_sheet": {str(sheet_id): color_list(rgb) for sheet_id, rgb in colors_by_sheet.items()},
            "default_color": [0.85, 0.85, 0.85],
            "opacity": SHEET_OVERVIEW_OPACITY,
            "base_sheet_ids": [int(next(iter(colors_by_sheet)))] if colors_by_sheet else [],
            "base_opacity": SHEET_OVERVIEW_BASE_OPACITY,
        }
    ]

    for sheet_id in sheets:
        images.append(
            {
                "mode": "selected",
                "output": str(output_dir / f"sheet_{sheet_id}.png"),
                "selected_sheet": int(sheet_id),
                "selected_color": color_list(UNIFORM_SHEET_COLOR),
                "selected_opacity": SHEET_SELECTED_OPACITY,
                "context_default_color": color_list(SHEET_CONTEXT_COLOR),
                "context_opacity": SHEET_CONTEXT_OPACITY,
                "boundary_default_color": color_list(SHEET_CONTEXT_BOUNDARY_COLOR),
                "boundary_opacity": SHEET_CONTEXT_BOUNDARY_OPACITY,
                "boundary_width": SHEET_CONTEXT_BOUNDARY_WIDTH,
            }
        )

    spec = {
        "vtp": str(vtp_path),
        "bounds": list(render_bounds) if render_bounds is not None else None,
        "image_resolution": list(SHEET_RENDERER_IMAGE_SIZE),
        "background": [1.0, 1.0, 1.0],
        "images": images,
    }

    SHEET_RENDER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SHEET_RENDER_LOG_DIR / f"{stem}.sheet_render.log"

    with tempfile.TemporaryDirectory(prefix=f"{TEMP_PREFIX}{stem}-pv-", dir=TEMP_DIRECTORY) as tmp_name:
        spec_path = Path(tmp_name) / "render_spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        command = [
            str(PVPYTHON),
            "--force-offscreen-rendering",
            str(PARAVIEW_SHEET_RENDER_HELPER),
            "--spec",
            str(spec_path),
        ]
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                subprocess.run(
                    command,
                    check=True,
                    env=RENDER_ENV,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=SHEET_RENDERER_RENDER_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ParaView sheet render timeout after {SHEET_RENDERER_RENDER_TIMEOUT_SECONDS}s; log={log_path}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"ParaView sheet render failed returncode={exc.returncode}; log={log_path}"
            ) from exc


def matching_file(directory: Path, stem: str, suffix: str) -> Path:
    exact = directory / f"{stem}{suffix}"
    if exact.exists():
        return exact

    matches = sorted(directory.glob(f"**/{stem}{suffix}"))
    if matches:
        return matches[0]

    raise FileNotFoundError(f"No matching {suffix} file for {stem!r} under {directory}")


def sheets_to_render(rsi: RsiData, sheet_polygons) -> list[int]:
    drawable_areas = sheet_areas(sheet_polygons)
    available = set(drawable_areas) & set(rsi.sheet_area)

    def area(sheet_id: int) -> float:
        return rsi.sheet_area.get(sheet_id, drawable_areas.get(sheet_id, 0.0))

    sheets = sorted(available, key=area, reverse=True)
    if TOP_N_SHEETS is not None and TOP_N_SHEETS > 0:
        sheets = sheets[:TOP_N_SHEETS]
    return sheets


def expected_sheet_ids_for_stem(stem: str) -> list[int]:
    rsi_path = matching_file(RSI_DIRECTORY, stem, ".rsi")
    rsi = read_rsi(rsi_path)
    cached_vtp = VTP_CACHE_DIR / f"{stem}.sheets.vtp"
    if not cached_vtp.exists():
        raise FileNotFoundError(f"missing Stage 1 cached sheet VTP: {cached_vtp}")
    sheet_polygons = read_sheet_vtp(cached_vtp)
    return sheets_to_render(rsi, sheet_polygons)


def timestep_images_complete(rs_path: Path) -> bool:
    stem = rs_path.stem
    output_dir = OUTPUT_DIRECTORY / stem
    overview = output_dir / f"{stem}.png"
    if not overview.exists():
        return False

    try:
        expected_sheets = expected_sheet_ids_for_stem(stem)
    except Exception as exc:
        print(f"{stem}: cannot validate existing sheet images; rerendering. Error: {exc}", file=sys.stderr)
        return False

    missing = [sheet_id for sheet_id in expected_sheets if not (output_dir / f"sheet_{sheet_id}.png").exists()]
    if missing:
        preview = ", ".join(str(sheet_id) for sheet_id in missing[:12])
        suffix = "" if len(missing) <= 12 else f", ... ({len(missing)} total)"
        print(f"{stem}: incomplete sheet images; missing sheet(s) {preview}{suffix}", file=sys.stderr)
        return False

    return True


def should_render_timestep(rs_path: Path) -> bool:
    if SHEET_RENDERER_REPLACE_EXISTING_IMAGES:
        return True
    return not timestep_images_complete(rs_path)


def load_or_build_sheet_vtp(
    stem: str,
    vtu_path: Path,
    rs_path: Path,
    library_path: str,
    rebuild_cache: bool,
):
    del vtu_path, rs_path, library_path, rebuild_cache
    cached_vtp = VTP_CACHE_DIR / f"{stem}.sheets.vtp"
    cache_status = "reused"

    if not cached_vtp.exists():
        raise FileNotFoundError(
            f"missing Stage 1 cached sheet VTP: {cached_vtp}. "
            "Run Stage 1 before sheet rendering."
        )

    try:
        return read_sheet_vtp(cached_vtp), cache_status
    except Exception as exc:
        raise RuntimeError(
            f"Stage 1 cached VTP is unreadable: {cached_vtp}. "
            f"Regenerate it from Stage 1. Error: {exc}"
        ) from exc


def collect_bounds_one(
    rs_path: Path,
    library_path: str,
    rebuild_cache: bool,
) -> tuple[float, float, float, float] | None:
    stem = rs_path.stem
    vtu_path = matching_file(VTU_DIRECTORY, stem, ".vtu")
    sheet_polygons, _cache_status = load_or_build_sheet_vtp(
        stem=stem,
        vtu_path=vtu_path,
        rs_path=rs_path,
        library_path=library_path,
        rebuild_cache=rebuild_cache,
    )
    return sheet_polygons_bounds(sheet_polygons)


def collect_global_render_bounds(
    rs_files: list[Path],
    library_path: str,
    rebuild_cache: bool,
    workers: int,
) -> tuple[float, float, float, float]:
    print("Computing global sheet image bounds")
    bounds: tuple[float, float, float, float] | None = None

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(collect_bounds_one, rs_path, library_path, rebuild_cache): rs_path
            for rs_path in rs_files
        }

        for i, future in enumerate(as_completed(futures), start=1):
            rs_path = futures[future]
            try:
                local = future.result()
            except Exception as exc:
                print(f"Failed to collect bounds for {rs_path}: {exc}", file=sys.stderr)
                continue

            bounds = merge_bounds(bounds, local)
            print(f"[bounds {i}/{len(futures)}] {rs_path.stem}", flush=True)

    if bounds is None:
        raise RuntimeError("Could not compute global sheet image bounds from any timestep.")

    return expand_bounds_to_canvas(bounds)


def render_one(
    rs_path: Path,
    library_path: str,
    rebuild_cache: bool,
    render_bounds: tuple[float, float, float, float] | None = None,
) -> str:
    stem = rs_path.stem
    rsi_path = matching_file(RSI_DIRECTORY, stem, ".rsi")
    vtu_path = matching_file(VTU_DIRECTORY, stem, ".vtu")

    output_dir = OUTPUT_DIRECTORY / stem
    output_dir.mkdir(parents=True, exist_ok=True)
    require_free_space(TEMP_DIRECTORY, MIN_TEMP_FREE_GB, "Temporary filesystem")
    require_free_space(OUTPUT_DIRECTORY, MIN_OUTPUT_FREE_GB, "Output filesystem")

    rsi = read_rsi(rsi_path)
    sheet_polygons, cache_status = load_or_build_sheet_vtp(
        stem=stem,
        vtu_path=vtu_path,
        rs_path=rs_path,
        library_path=library_path,
        rebuild_cache=rebuild_cache,
    )

    sheets = sheets_to_render(rsi, sheet_polygons)
    colors_by_sheet = sheet_colors_by_area_rank(rsi, sheet_polygons)

    render_sheet_images_with_paraview(
        stem=stem,
        vtp_path=VTP_CACHE_DIR / f"{stem}.sheets.vtp",
        output_dir=output_dir,
        sheets=sheets,
        colors_by_sheet=colors_by_sheet,
        render_bounds=render_bounds,
    )

    return f"{stem}: {cache_status} VTP, wrote {1 + len(sheets)} image(s) to {output_dir}"


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Force rebuilding cached VTP files before rendering.",
    )
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help="Delete cached VTP files before rendering.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    library_path = LD_LIBRARY_PATH
    if not PVPYTHON.exists():
        raise SystemExit(f"PVPYTHON does not exist: {PVPYTHON}")
    if not PARAVIEW_SHEET_RENDER_HELPER.exists():
        raise SystemExit(f"ParaView sheet render helper does not exist: {PARAVIEW_SHEET_RENDER_HELPER}")

    TEMP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    VTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clean_cache:
        print(
            "Ignoring --clean-cache for Stage 1-owned VTP cache; "
            "rerun Stage 1 to regenerate sheet VTPs."
        )
    if args.rebuild_cache:
        print(
            "Ignoring --rebuild-cache for Stage 1-owned VTP cache; "
            "rerun Stage 1 to regenerate sheet VTPs."
        )

    cleanup_stale_temp_dirs(TEMP_DIRECTORY)
    require_free_space(TEMP_DIRECTORY, MIN_TEMP_FREE_GB, "Temporary filesystem")
    require_free_space(OUTPUT_DIRECTORY, MIN_OUTPUT_FREE_GB, "Output filesystem")

    rs_files = sorted(RS_DIRECTORY.glob("*.rs"))
    if not rs_files:
        raise SystemExit(f"No .rs files found in {RS_DIRECTORY}")

    pending_rs_files = [rs_path for rs_path in rs_files if should_render_timestep(rs_path)]
    skipped_count = len(rs_files) - len(pending_rs_files)
    if skipped_count:
        print(
            f"Skipping {skipped_count} time step(s) with existing sheet images "
            f"(SHEET_RENDERER_REPLACE_EXISTING_IMAGES={SHEET_RENDERER_REPLACE_EXISTING_IMAGES})"
        )

    if not pending_rs_files:
        print("All sheet images already exist; nothing to render.")
        return 0

    workers = min(max(1, int(SHEET_RENDERER_WORKERS)), len(pending_rs_files))
    bounds_workers = min(max(1, int(SHEET_RENDERER_WORKERS)), len(rs_files))
    print(f"Rendering {len(pending_rs_files)} of {len(rs_files)} time step(s) with {workers} worker process(es)")
    print(f"Temporary VTP directory: {TEMP_DIRECTORY}")
    print(f"Persistent VTP cache: {VTP_CACHE_DIR}")
    print(f"Temporary free space: {free_gb(TEMP_DIRECTORY):.1f} GiB")
    print(f"Output free space: {free_gb(OUTPUT_DIRECTORY):.1f} GiB")

    render_bounds = None
    render_rebuild_cache = False
    if SHEET_RENDERER_USE_GLOBAL_BOUNDS:
        render_bounds = collect_global_render_bounds(
            rs_files=rs_files,
            library_path=library_path,
            rebuild_cache=args.rebuild_cache,
            workers=bounds_workers,
        )
        render_rebuild_cache = False
        print(f"Global sheet image bounds: {render_bounds}")
        print(f"Sheet image size: {SHEET_RENDERER_IMAGE_SIZE[0]}x{SHEET_RENDERER_IMAGE_SIZE[1]} px")

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(render_one, rs_path, library_path, render_rebuild_cache, render_bounds): rs_path
            for rs_path in pending_rs_files
        }

        for future in as_completed(futures):
            rs_path = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                print(f"Failed to render {rs_path}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Export command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode)
