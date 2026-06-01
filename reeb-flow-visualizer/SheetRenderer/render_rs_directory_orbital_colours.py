#!/usr/bin/env python3
"""
Batch render Reeb-space images with a uniform sheet color.

Uses project paths from common.py, then run:

    python3 scripts/render_rs_directory_orbital_colours.py

For every .rs file in RS_DIRECTORY, this script looks for matching .rsi and
.vtu files by stem, reconstructs temporary drawable Reeb-space geometry through
the existing C++ executable, and writes only PNG files:

    OUTPUT_DIRECTORY/<rs-stem>/<rs-stem>.png
    OUTPUT_DIRECTORY/<rs-stem>/sheet_<sheet-id>.png

Cached VTP behavior:
  - default: reuse cached VTPs and build only missing ones
  - --rebuild-cache: force rebuilding all VTP cache entries
  - --clean-cache: clear VTP cache before rendering

When SHEET_RENDERER_USE_GLOBAL_BOUNDS is enabled in common.py, all output PNGs
use the same global 2D sheet-space frame and fixed canvas size.
"""

from __future__ import annotations

import argparse
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

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    FV99,
    RSI_DIR,
    RS_DIR,
    SHEET_IMAGE_DIR,
    SHEET_RENDERER_GLOBAL_PADDING,
    SHEET_RENDERER_IMAGE_SIZE,
    SHEET_RENDERER_TEMP_DIR,
    SHEET_RENDERER_UNIFORM_SHEET_COLOR,
    SHEET_RENDERER_USE_GLOBAL_BOUNDS,
    SHEET_RENDERER_WORKERS,
    TOP_N_SHEETS,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTK_LIB_DIR,
    VTU_DIR,
)
from render_rs_sheets import (  # noqa: E402
    export_sheet_vtp,
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
VTP_CACHE_DIR = TEMP_DIRECTORY / "vtp_cache"

DPI = 200
FIGURE_WIDTH = SHEET_RENDERER_IMAGE_SIZE[0] / DPI
FIGURE_HEIGHT = SHEET_RENDERER_IMAGE_SIZE[1] / DPI
UNIFORM_SHEET_COLOR = SHEET_RENDERER_UNIFORM_SHEET_COLOR
CONTEXT_CACHE_DIR = TEMP_DIRECTORY / "context_cache"


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


def make_render_axes():
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, FIGURE_HEIGHT), dpi=DPI)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.set_position([0, 0, 1, 1])
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    return fig, ax


def apply_render_bounds(
    ax,
    render_bounds: tuple[float, float, float, float] | None,
) -> None:
    if render_bounds is not None:
        xmin, ymin, xmax, ymax = render_bounds
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.margins(0)
    else:
        ax.autoscale_view()
        ax.margins(0.03)


def save_render_figure(
    fig,
    output_path: Path,
    render_bounds: tuple[float, float, float, float] | None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if render_bounds is not None:
        fig.savefig(output_path, dpi=DPI)
    else:
        fig.savefig(output_path, bbox_inches="tight", pad_inches=0.03)


def add_polygon_collection(ax, polygons, colors) -> None:
    if not polygons:
        return
    collection = PolyCollection(polygons, facecolors=colors, edgecolors="none", antialiased=True)
    ax.add_collection(collection)


def render_colored(
    sheet_polygons,
    output_path: Path,
    colors_by_sheet: dict[int, tuple[float, float, float]],
    render_bounds: tuple[float, float, float, float] | None = None,
) -> None:
    fig, ax = make_render_axes()
    try:
        polygons = []
        colors = []

        for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
            rgb = colors_by_sheet.get(sheet_id, (0.85, 0.85, 0.85))
            polygons.append([sheet_polygons.points[i] for i in polygon])
            colors.append((rgb[0], rgb[1], rgb[2], 0.58))

        add_polygon_collection(ax, polygons, colors)
        apply_render_bounds(ax, render_bounds)
        save_render_figure(fig, output_path, render_bounds)
    finally:
        plt.close(fig)


def render_context(
    sheet_polygons,
    output_path: Path,
    render_bounds: tuple[float, float, float, float],
) -> None:
    fig, ax = make_render_axes()
    try:
        polygons = [
            [sheet_polygons.points[index] for index in polygon]
            for polygon in sheet_polygons.polygons
        ]
        colors = [(0.80, 0.80, 0.80, 0.08)] * len(polygons)
        add_polygon_collection(ax, polygons, colors)
        apply_render_bounds(ax, render_bounds)
        save_render_figure(fig, output_path, render_bounds)
    finally:
        plt.close(fig)


def collect_selected_sheet_polygons(sheet_polygons, sheet_ids: list[int]) -> dict[int, list[list[tuple[float, float]]]]:
    selected = set(sheet_ids)
    polygons_by_sheet: dict[int, list[list[tuple[float, float]]]] = {
        sheet_id: []
        for sheet_id in sheet_ids
    }
    for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
        if sheet_id not in selected:
            continue
        polygons_by_sheet[sheet_id].append([
            sheet_polygons.points[index]
            for index in polygon
        ])
    return polygons_by_sheet


def render_selected_sheet(
    sheet_polygons,
    output_path: Path,
    colors_by_sheet: dict[int, tuple[float, float, float]],
    selected_sheet: int,
    render_bounds: tuple[float, float, float, float] | None = None,
    context_image=None,
    selected_polygons: list[list[tuple[float, float]]] | None = None,
) -> None:
    fig, ax = make_render_axes()
    try:
        if context_image is not None and render_bounds is not None:
            xmin, ymin, xmax, ymax = render_bounds
            ax.imshow(
                context_image,
                extent=(xmin, xmax, ymin, ymax),
                origin="upper",
                interpolation="nearest",
            )

        polygons = []
        colors = []
        rgb = colors_by_sheet.get(selected_sheet, (0.85, 0.85, 0.85))
        if context_image is not None:
            polygons = list(selected_polygons or [])
            colors = [(rgb[0], rgb[1], rgb[2], 0.88)] * len(polygons)
        else:
            for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
                if sheet_id != selected_sheet:
                    polygons.append([sheet_polygons.points[index] for index in polygon])
                    colors.append((0.80, 0.80, 0.80, 0.08))
                    continue

                polygons.append([sheet_polygons.points[index] for index in polygon])
                colors.append((rgb[0], rgb[1], rgb[2], 0.88))

        add_polygon_collection(ax, polygons, colors)
        apply_render_bounds(ax, render_bounds)
        save_render_figure(fig, output_path, render_bounds)
    finally:
        plt.close(fig)


def uniform_sheet_colors(sheet_polygons) -> dict[int, tuple[float, float, float]]:
    return {
        sheet_id: UNIFORM_SHEET_COLOR
        for sheet_id in set(sheet_polygons.sheet_ids)
    }


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


def load_or_build_sheet_vtp(
    stem: str,
    vtu_path: Path,
    rs_path: Path,
    library_path: str,
    rebuild_cache: bool,
):
    cached_vtp = VTP_CACHE_DIR / f"{stem}.sheets.vtp"
    cache_status = "reused"

    if cached_vtp.exists() and not rebuild_cache:
        try:
            return read_sheet_vtp(cached_vtp), cache_status
        except Exception as exc:
            raise RuntimeError(
                f"Cached VTP is unreadable: {cached_vtp}. "
                f"Run with --rebuild-cache or --clean-cache. Error: {exc}"
            ) from exc

    cache_status = "rebuilt" if cached_vtp.exists() else "built"
    with tempfile.TemporaryDirectory(prefix=f"{TEMP_PREFIX}{stem}-", dir=TEMP_DIRECTORY) as tmp:
        tmp_vtp = Path(tmp) / f"{stem}.sheets.vtp"
        export_sheet_vtp(FV99, vtu_path, rs_path, tmp_vtp, library_path)
        cached_vtp.parent.mkdir(parents=True, exist_ok=True)
        tmp_vtp.replace(cached_vtp)

    return read_sheet_vtp(cached_vtp), cache_status


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
    colors_by_sheet = uniform_sheet_colors(sheet_polygons)

    render_colored(
        sheet_polygons,
        output_dir / f"{stem}.png",
        colors_by_sheet,
        render_bounds=render_bounds,
    )

    context_image = None
    selected_polygons_by_sheet: dict[int, list[list[tuple[float, float]]]] = {}
    if render_bounds is not None:
        context_path = CONTEXT_CACHE_DIR / f"{stem}.png"
        render_context(sheet_polygons, context_path, render_bounds)
        context_image = plt.imread(context_path)
        selected_polygons_by_sheet = collect_selected_sheet_polygons(sheet_polygons, sheets)

    for sheet_id in sheets:
        render_selected_sheet(
            sheet_polygons,
            output_dir / f"sheet_{sheet_id}.png",
            colors_by_sheet,
            selected_sheet=sheet_id,
            render_bounds=render_bounds,
            context_image=context_image,
            selected_polygons=selected_polygons_by_sheet.get(sheet_id),
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
    TEMP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    VTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clean_cache and VTP_CACHE_DIR.exists():
        shutil.rmtree(VTP_CACHE_DIR)
        VTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Cleared VTP cache: {VTP_CACHE_DIR}")

    cleanup_stale_temp_dirs(TEMP_DIRECTORY)
    require_free_space(TEMP_DIRECTORY, MIN_TEMP_FREE_GB, "Temporary filesystem")
    require_free_space(OUTPUT_DIRECTORY, MIN_OUTPUT_FREE_GB, "Output filesystem")

    rs_files = sorted(RS_DIRECTORY.glob("*.rs"))
    if not rs_files:
        raise SystemExit(f"No .rs files found in {RS_DIRECTORY}")

    workers = min(max(1, int(SHEET_RENDERER_WORKERS)), len(rs_files))
    print(f"Rendering {len(rs_files)} time step(s) with {workers} worker process(es)")
    print(f"Temporary VTP directory: {TEMP_DIRECTORY}")
    print(f"Persistent VTP cache: {VTP_CACHE_DIR}")
    print(f"Temporary free space: {free_gb(TEMP_DIRECTORY):.1f} GiB")
    print(f"Output free space: {free_gb(OUTPUT_DIRECTORY):.1f} GiB")

    render_bounds = None
    render_rebuild_cache = args.rebuild_cache
    if SHEET_RENDERER_USE_GLOBAL_BOUNDS:
        render_bounds = collect_global_render_bounds(
            rs_files=rs_files,
            library_path=library_path,
            rebuild_cache=args.rebuild_cache,
            workers=workers,
        )
        render_rebuild_cache = False
        print(f"Global sheet image bounds: {render_bounds}")
        print(f"Sheet image size: {SHEET_RENDERER_IMAGE_SIZE[0]}x{SHEET_RENDERER_IMAGE_SIZE[1]} px")

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(render_one, rs_path, library_path, render_rebuild_cache, render_bounds): rs_path
            for rs_path in rs_files
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
