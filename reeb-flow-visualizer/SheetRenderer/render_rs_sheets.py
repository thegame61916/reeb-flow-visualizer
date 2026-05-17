#!/usr/bin/env python3
"""
Render Reeb-space images and per-sheet images.

The .rs file written by this project is a binary cache of ReebSpace2 traversal
state. It does not contain the range-space polygon coordinates needed for
rendering. This script therefore either:

  1. reads an optional existing .vtp generated with --outputSheetPolygons, or
  2. runs the project executable with -f <mesh> -l <rs> -o <temporary.vtp>
     --headless to get drawable geometry without keeping polygon files.
"""

from __future__ import annotations

import argparse
import colorsys
import os
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection


@dataclass(frozen=True)
class RsMetadata:
    component_count: int
    sheet_ids: tuple[int, ...]


@dataclass(frozen=True)
class SheetPolygons:
    points: list[tuple[float, float]]
    polygons: list[list[int]]
    sheet_ids: list[int]


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

    def bool(self) -> bool:
        return self._read("<?")

    def size(self) -> int:
        # The C++ writer uses size_t. The target platform for this project is
        # 64-bit Linux, so size_t is serialized as an unsigned 64-bit integer.
        return self._read("<Q")


def read_rs_metadata(path: Path) -> RsMetadata:
    """Read enough of this project's .rs binary format to report sheet IDs."""
    reader = BinaryReader(path)

    for _ in range(reader.size()):
        reader.int32()
        reader.bool()

    for _ in range(reader.size()):
        for _ in range(reader.size()):
            reader.int32()
            reader.bool()

    for _ in range(reader.size()):
        for _ in range(reader.size()):
            reader.int32()
            reader.bool()

    for _ in range(reader.size()):
        for _ in range(reader.size()):
            reader.int32()

    parents = [reader.int32() for _ in range(reader.size())]
    _ranks = [reader.int32() for _ in range(reader.size())]

    def read_fiber_graph() -> None:
        for _ in range(reader.size()):
            reader.int32()
            reader.int32()
        for _ in range(reader.size()):
            reader.int32()
            reader.int32()

    for _ in range(reader.size()):
        read_fiber_graph()
        read_fiber_graph()

    for _ in range(reader.size()):
        for _ in range(reader.size()):
            reader.int32()
            reader.int32()

    component_count = reader.int32()

    def find(i: int) -> int:
        seen: set[int] = set()
        while 0 <= i < len(parents) and parents[i] != i and i not in seen:
            seen.add(i)
            i = parents[i]
        return i

    sheet_ids = tuple(sorted({find(i) for i in range(len(parents))}))
    return RsMetadata(component_count=component_count, sheet_ids=sheet_ids)


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _strip_namespace(child.tag) == name:
            return child
    return None


def _find_data_array(parent: ET.Element, name: str | None = None) -> ET.Element | None:
    for child in parent.iter():
        if _strip_namespace(child.tag) != "DataArray":
            continue
        if name is None or child.attrib.get("Name", "").lower() == name.lower():
            return child
    return None


def _parse_numbers(data_array: ET.Element, cast=float) -> list:
    fmt = data_array.attrib.get("format", "ascii").lower()
    if fmt not in ("", "ascii"):
        raise ValueError(
            "Only ASCII VTP files are supported. Generate them with this "
            "project's --outputSheetPolygons path, which writes ASCII."
        )
    text = data_array.text or ""
    return [cast(item) for item in text.split()]


def read_sheet_vtp(path: Path) -> SheetPolygons:
    tree = ET.parse(path)
    root = tree.getroot()

    piece = None
    for element in root.iter():
        if _strip_namespace(element.tag) == "Piece":
            piece = element
            break
    if piece is None:
        raise ValueError(f"No VTK Piece found in {path}")

    points_node = _find_child(piece, "Points")
    polys_node = _find_child(piece, "Polys")
    cell_data_node = _find_child(piece, "CellData")
    if points_node is None or polys_node is None or cell_data_node is None:
        raise ValueError(f"{path} is missing Points, Polys, or CellData")

    point_array = _find_data_array(points_node)
    connectivity_array = _find_data_array(polys_node, "connectivity")
    offsets_array = _find_data_array(polys_node, "offsets")
    sheet_array = _find_data_array(cell_data_node, "SheetId")
    if sheet_array is None:
        sheet_array = _find_data_array(cell_data_node, "SheetID")
    if point_array is None or connectivity_array is None or offsets_array is None or sheet_array is None:
        raise ValueError(f"{path} does not look like a sheet polygon VTP with SheetId cell data")

    coords = _parse_numbers(point_array, float)
    if len(coords) % 3 != 0:
        raise ValueError(f"Point coordinate array in {path} is not a multiple of 3")
    points = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 3)]

    connectivity = _parse_numbers(connectivity_array, int)
    offsets = _parse_numbers(offsets_array, int)
    sheet_ids = _parse_numbers(sheet_array, int)

    polygons: list[list[int]] = []
    start = 0
    for end in offsets:
        polygons.append(connectivity[start:end])
        start = end

    if len(polygons) != len(sheet_ids):
        raise ValueError(
            f"Cell count mismatch in {path}: {len(polygons)} polygons but {len(sheet_ids)} sheet IDs"
        )

    return SheetPolygons(points=points, polygons=polygons, sheet_ids=sheet_ids)


def polygon_area(poly: Iterable[tuple[float, float]]) -> float:
    pts = list(poly)
    if len(pts) < 3:
        return 0.0
    area = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def sheet_areas(data: SheetPolygons) -> dict[int, float]:
    areas: dict[int, float] = {}
    for polygon, sheet_id in zip(data.polygons, data.sheet_ids, strict=True):
        pts = [data.points[i] for i in polygon]
        areas[sheet_id] = areas.get(sheet_id, 0.0) + polygon_area(pts)
    return areas


def color_for_rank(rank: int) -> tuple[float, float, float]:
    # Golden-ratio hue stepping gives stable, well-separated colors.
    hue = (0.618033988749895 * rank) % 1.0
    return colorsys.hsv_to_rgb(hue, 0.62, 0.86)


def render(
    data: SheetPolygons,
    path: Path,
    selected_sheet: int | None,
    rank_by_sheet: dict[int, int],
    title: str | None,
    width: float,
    height: float,
    dpi: int,
    context: bool,
) -> None:
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    collections: list[list[tuple[float, float]]] = []
    colors: list[tuple[float, float, float, float]] = []

    for polygon, sheet_id in zip(data.polygons, data.sheet_ids, strict=True):
        if selected_sheet is not None and sheet_id != selected_sheet and not context:
            continue

        points = [data.points[i] for i in polygon]
        collections.append(points)

        if selected_sheet is None:
            rgb = color_for_rank(rank_by_sheet[sheet_id])
            colors.append((rgb[0], rgb[1], rgb[2], 0.55))
        elif sheet_id == selected_sheet:
            rgb = color_for_rank(rank_by_sheet[sheet_id])
            colors.append((rgb[0], rgb[1], rgb[2], 0.88))
        else:
            colors.append((0.80, 0.80, 0.80, 0.08))

    collection = PolyCollection(collections, facecolors=colors, edgecolors="none", antialiased=True)
    ax.add_collection(collection)
    ax.autoscale_view()
    ax.margins(0.03)

    if title:
        ax.set_title(title, fontsize=10)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def default_library_path(repo_root: Path) -> str:
    candidates = [
        repo_root / "libraries" / "ttk" / "install" / "lib",
        repo_root / "libraries" / "vtk" / "install" / "lib",
        repo_root / "libraries" / "cgal" / "install" / "lib",
    ]
    return os.pathsep.join(str(path.resolve()) for path in candidates if path.exists())


def export_sheet_vtp(exe: Path, mesh: Path, rs: Path, out_vtp: Path, library_path: str) -> None:
    out_vtp.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(exe),
        "-f",
        str(mesh),
        "-l",
        str(rs),
        "-o",
        str(out_vtp),
        "--headless",
    ]
    env = os.environ.copy()
    if library_path:
        old_library_path = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = (
            library_path if not old_library_path else library_path + os.pathsep + old_library_path
        )
    subprocess.run(command, check=True, env=env)


def parse_sheet_list(value: str | None) -> set[int] | None:
    if not value:
        return None
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        result.add(int(part))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rs", required=True, type=Path, help="Input .rs file.")
    parser.add_argument("--mesh", type=Path, help="Original .vtu/.txt dataset. Required if --sheets-vtp is omitted.")
    parser.add_argument("--sheets-vtp", type=Path, help="Optional existing sheet .vtp from --outputSheetPolygons.")
    parser.add_argument("--exe", type=Path, default=Path("build/fv99"), help="Project executable used to export VTP.")
    parser.add_argument(
        "--ld-library-path",
        help="Library path prepended when running --exe. Defaults to local libraries/*/install/lib if present.",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("output/rs_renderings"), help="Output image directory.")
    parser.add_argument("--prefix", help="Output filename prefix. Defaults to the .rs stem.")
    parser.add_argument("--sheets", help="Comma-separated sheet IDs to render. Defaults to every sheet in the VTP.")
    parser.add_argument("--top", type=int, help="Render only the top N sheets by polygon area.")
    parser.add_argument("--no-individual", action="store_true", help="Only render the full Reeb space image.")
    parser.add_argument("--no-context", action="store_true", help="Do not draw gray context in individual sheet images.")
    parser.add_argument("--width", type=float, default=8.0, help="Figure width in inches.")
    parser.add_argument("--height", type=float, default=8.0, help="Figure height in inches.")
    parser.add_argument("--dpi", type=int, default=200, help="Output DPI.")
    args = parser.parse_args(argv)

    rs_path = args.rs.resolve()
    if not rs_path.exists():
        raise FileNotFoundError(rs_path)

    prefix = args.prefix or rs_path.stem
    out_dir = args.out_dir.resolve()

    metadata = read_rs_metadata(rs_path)
    print(f"Read {rs_path}")
    print(f"  component count: {metadata.component_count}")
    print(f"  sheet count from .rs: {len(metadata.sheet_ids)}")

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    sheets_vtp = args.sheets_vtp
    if sheets_vtp is None:
        if args.mesh is None:
            raise SystemExit("Provide --mesh so the Reeb-space geometry can be reconstructed for rendering.")
        temporary_directory = tempfile.TemporaryDirectory(prefix="rs-render-")
        sheets_vtp = Path(temporary_directory.name) / f"{prefix}.sheets.vtp"
        repo_root = Path(__file__).resolve().parents[1]
        library_path = args.ld_library_path
        if library_path is None:
            library_path = default_library_path(repo_root)
        export_sheet_vtp(args.exe, args.mesh, rs_path, sheets_vtp, library_path)

    sheet_polygons = read_sheet_vtp(sheets_vtp.resolve())
    areas = sheet_areas(sheet_polygons)
    sheets_by_area = [sheet for sheet, _area in sorted(areas.items(), key=lambda item: item[1], reverse=True)]
    rank_by_sheet = {sheet: rank for rank, sheet in enumerate(sheets_by_area)}

    if args.sheets_vtp is None:
        print("Reconstructed drawable Reeb-space geometry in a temporary file")
    else:
        print(f"Read {sheets_vtp}")
    print(f"  drawable sheets: {len(sheets_by_area)}")
    print(f"  polygons: {len(sheet_polygons.polygons)}")

    render(
        sheet_polygons,
        out_dir / f"{prefix}.full.png",
        selected_sheet=None,
        rank_by_sheet=rank_by_sheet,
        title=f"{prefix}: all sheets",
        width=args.width,
        height=args.height,
        dpi=args.dpi,
        context=True,
    )

    requested_sheets = parse_sheet_list(args.sheets)
    sheets_to_render = sheets_by_area
    if requested_sheets is not None:
        sheets_to_render = [sheet for sheet in sheets_by_area if sheet in requested_sheets]
    if args.top is not None:
        sheets_to_render = sheets_to_render[: args.top]

    if not args.no_individual:
        sheet_dir = out_dir / f"{prefix}_sheets"
        for sheet_id in sheets_to_render:
            render(
                sheet_polygons,
                sheet_dir / f"sheet_{sheet_id}.png",
                selected_sheet=sheet_id,
                rank_by_sheet=rank_by_sheet,
                title=f"{prefix}: sheet {sheet_id}",
                width=args.width,
                height=args.height,
                dpi=args.dpi,
                context=not args.no_context,
            )
        print(f"Wrote {len(sheets_to_render)} individual sheet image(s) to {sheet_dir}")

    print(f"Wrote full rendering to {out_dir / f'{prefix}.full.png'}")
    if temporary_directory is not None:
        temporary_directory.cleanup()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Export command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode)
