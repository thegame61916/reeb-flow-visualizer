#!/usr/bin/env python3
"""
Batch render Reeb-space images with sheet colors from VTU orbital scalars.

Edit the global variables below, then run:

    python3 scripts/render_rs_directory_orbital_colours.py

For every .rs file in RS_DIRECTORY, this script looks for matching .rsi and
.vtu files by stem, reconstructs temporary drawable Reeb-space geometry through
the existing C++ executable, and writes only PNG files:

    OUTPUT_DIRECTORY/<rs-stem>/<rs-stem>.png
    OUTPUT_DIRECTORY/<rs-stem>/<sheet-id>_<hex-color>.png

Sheet color logic:

    score(sheet) = sum over vertices in rsi sheet vertex list of
                   orb00(vertex)^2 - orb01(vertex)^2

Positive scores are green, negative scores are red. Saturation is normalized by
the largest absolute score among the rendered sheets for that .rs file.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

try:
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
except ImportError as exc:
    raise SystemExit("This script requires VTK Python bindings: import vtk failed.") from exc

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from render_rs_sheets import (  # noqa: E402
    default_library_path,
    export_sheet_vtp,
    read_sheet_vtp,
    sheet_areas,
)


# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

RS_DIRECTORY = Path("data/stilbene/allTS/reeb")
RSI_DIRECTORY = Path("data/stilbene/allTS/rsi")
VTU_DIRECTORY = Path("data/stilbene/allTS")
OUTPUT_DIRECTORY = Path("output/orbital_rs_renderings")

EXECUTABLE = Path("build/fv99")
LD_LIBRARY_PATH = None  # None means use local libraries/*/install/lib if present.

TOP_N_SHEETS = 10

ORB00_ARRAY_NAME = "orb00"
ORB01_ARRAY_NAME = "orb01"

FIGURE_WIDTH = 8.0
FIGURE_HEIGHT = 8.0
DPI = 200


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


def read_vtu_point_arrays(path: Path, array_0: str, array_1: str):
    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    mesh = reader.GetOutput()
    point_data = mesh.GetPointData()

    arr0 = point_data.GetArray(array_0)
    arr1 = point_data.GetArray(array_1)
    if arr0 is None or arr1 is None:
        names = [point_data.GetArrayName(i) for i in range(point_data.GetNumberOfArrays())]
        raise ValueError(
            f"{path} is missing {array_0!r} or {array_1!r}. Available point arrays: {names}"
        )

    return vtk_to_numpy(arr0), vtk_to_numpy(arr1)


def compute_sheet_scores(rsi: RsiData, orb00, orb01) -> dict[int, float]:
    scores: dict[int, float] = {}
    n = min(len(orb00), len(orb01))

    def scalar_value(array, index: int) -> float:
        value = array[index]
        if hasattr(value, "__len__"):
            return float(value[0])
        return float(value)

    for sheet_id, vertices in rsi.sheet_vertices.items():
        score = 0.0
        for vertex_id in vertices:
            if 0 <= vertex_id < n:
                a = scalar_value(orb00, vertex_id)
                b = scalar_value(orb01, vertex_id)
                score += a * a - b * b
        scores[sheet_id] = score

    return scores


def color_from_score(score: float, max_abs_score: float) -> tuple[float, float, float]:
    if max_abs_score <= 0.0:
        saturation = 0.0
    else:
        saturation = min(1.0, abs(score) / max_abs_score)

    # Keep near-zero sheets visible instead of pure white.
    saturation = 0.15 + 0.85 * saturation if saturation > 0.0 else 0.0

    if score >= 0.0:
        # Green, with saturation mixed against white.
        return (1.0 - saturation, 1.0, 1.0 - saturation)

    # Red, with saturation mixed against white.
    return (1.0, 1.0 - saturation, 1.0 - saturation)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(c * 255))) for c in rgb)
    return f"{r:02x}{g:02x}{b:02x}"


def render_colored(
    sheet_polygons,
    output_path: Path,
    colors_by_sheet: dict[int, tuple[float, float, float]],
    selected_sheet: int | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, FIGURE_HEIGHT), dpi=DPI)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    polygons = []
    colors = []

    for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
        if selected_sheet is not None and sheet_id != selected_sheet:
            polygons.append([sheet_polygons.points[i] for i in polygon])
            colors.append((0.80, 0.80, 0.80, 0.08))
            continue

        rgb = colors_by_sheet.get(sheet_id, (0.85, 0.85, 0.85))
        alpha = 0.88 if selected_sheet is not None else 0.58
        polygons.append([sheet_polygons.points[i] for i in polygon])
        colors.append((rgb[0], rgb[1], rgb[2], alpha))

    collection = PolyCollection(polygons, facecolors=colors, edgecolors="none", antialiased=True)
    ax.add_collection(collection)
    ax.autoscale_view()
    ax.margins(0.03)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


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
    available = set(drawable_areas) & set(rsi.sheet_vertices)

    def area(sheet_id: int) -> float:
        return rsi.sheet_area.get(sheet_id, drawable_areas.get(sheet_id, 0.0))

    sheets = sorted(available, key=area, reverse=True)
    if TOP_N_SHEETS is not None and TOP_N_SHEETS > 0:
        sheets = sheets[:TOP_N_SHEETS]
    return sheets


def render_one(rs_path: Path, library_path: str) -> None:
    stem = rs_path.stem
    rsi_path = matching_file(RSI_DIRECTORY, stem, ".rsi")
    vtu_path = matching_file(VTU_DIRECTORY, stem, ".vtu")

    output_dir = OUTPUT_DIRECTORY / stem
    output_dir.mkdir(parents=True, exist_ok=True)

    rsi = read_rsi(rsi_path)
    orb00, orb01 = read_vtu_point_arrays(vtu_path, ORB00_ARRAY_NAME, ORB01_ARRAY_NAME)
    scores = compute_sheet_scores(rsi, orb00, orb01)

    with tempfile.TemporaryDirectory(prefix=f"rs-render-{stem}-") as tmp:
        sheet_vtp = Path(tmp) / f"{stem}.sheets.vtp"
        export_sheet_vtp(EXECUTABLE, vtu_path, rs_path, sheet_vtp, library_path)
        sheet_polygons = read_sheet_vtp(sheet_vtp)

    sheets = sheets_to_render(rsi, sheet_polygons)
    max_abs_score = max((abs(scores.get(sheet_id, 0.0)) for sheet_id in sheets), default=0.0)
    colors_by_sheet = {
        sheet_id: color_from_score(scores.get(sheet_id, 0.0), max_abs_score)
        for sheet_id in set(sheet_polygons.sheet_ids)
    }

    render_colored(sheet_polygons, output_dir / f"{stem}.png", colors_by_sheet)

    for sheet_id in sheets:
        color_hex = rgb_to_hex(colors_by_sheet[sheet_id])
        render_colored(
            sheet_polygons,
            output_dir / f"{sheet_id}_{color_hex}.png",
            colors_by_sheet,
            selected_sheet=sheet_id,
        )

    print(f"{stem}: wrote {1 + len(sheets)} image(s) to {output_dir}")


def main() -> int:
    repo_root = SCRIPT_DIR.parents[0]
    library_path = LD_LIBRARY_PATH
    if library_path is None:
        library_path = default_library_path(repo_root)

    rs_files = sorted(RS_DIRECTORY.glob("*.rs"))
    if not rs_files:
        raise SystemExit(f"No .rs files found in {RS_DIRECTORY}")

    for rs_path in rs_files:
        try:
            render_one(rs_path, library_path)
        except Exception as exc:
            print(f"Failed to render {rs_path}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Export command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode)
