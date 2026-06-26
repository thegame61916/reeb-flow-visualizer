#!/usr/bin/env python3
"""
Export four sheet-restricted orb00 isosurfaces from two timesteps.

This is the fixed-value, four-sheet counterpart of
export_orb00_fiber_sequence.py. It deliberately does not call fv99. It starts
with the same working VTK contour path as that script:

1. Contour orb00 directly in the VTU volume with VTK.
2. Cut each requested range-space sheet at the same orb00 value.
3. Split the physical contour into complete connected surfaces.
4. Assign the two surfaces one-to-one to the two sheets using RSI domain
   vertex support.

Default invocation:

  python3 export_orb00_four_sheet_isosurfaces.py

This exports sheets 1795 and 1796 at timestep 18 and sheets 1855 and 1856 at
timestep 19, all at orb00=25.
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import struct
from pathlib import Path

import vtk


DEFAULT_BASE_DIR = Path(
    "/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus"
)

vtk.vtkObject.GlobalWarningDisplayOff()
vtk.vtkLogger.SetStderrVerbosity(vtk.vtkLogger.VERBOSITY_ERROR)


def value_text(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def value_token(value: float) -> str:
    text = value_text(abs(value)).replace(".", "p")
    return ("m" if float(value) < 0 else "p") + text


def default_output_dir(
    base_dir: Path,
    orb00_value: float,
    timesteps: tuple[int, int],
) -> Path:
    first, second = timesteps
    return (
        base_dir
        / "sheetFiberSurfaces"
        / "orb00_four_sheet_isosurfaces_vtk_components"
        / f"orb00_{value_token(orb00_value)}_t{first:03d}_t{second:03d}"
    )


class BinaryReader:
    def __init__(self, file_obj):
        self.file_obj = file_obj

    def read_exact(self, byte_count: int) -> bytes:
        data = self.file_obj.read(byte_count)
        if len(data) != byte_count:
            raise EOFError(
                f"unexpected end of RSI file: needed {byte_count} bytes, got {len(data)}"
            )
        return data

    def size_t(self) -> int:
        return struct.unpack("<Q", self.read_exact(8))[0]

    def uint8(self) -> int:
        return struct.unpack("<B", self.read_exact(1))[0]

    def int(self) -> int:
        return struct.unpack("<i", self.read_exact(4))[0]

    def double(self) -> float:
        return struct.unpack("<d", self.read_exact(8))[0]


def read_rsi_sheet_vertices(path: Path) -> dict[int, set[int]]:
    if not path.exists():
        raise FileNotFoundError(f"RSI file not found: {path}")
    with path.open("rb") as file_obj:
        reader = BinaryReader(file_obj)
        for _ in range(reader.size_t()):
            reader.uint8()
        for _ in range(reader.size_t()):
            reader.int()
            reader.double()
        sheets: dict[int, set[int]] = {}
        for _ in range(reader.size_t()):
            sheet_id = reader.int()
            sheets[sheet_id] = {
                reader.int()
                for _ in range(reader.size_t())
            }
    return sheets


def read_poly_data(path: Path) -> vtk.vtkPolyData:
    if not path.exists():
        raise FileNotFoundError(f"VTP file not found: {path}")
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    output = reader.GetOutput()
    if output is None:
        raise ValueError(f"failed to read VTP: {path}")
    result = vtk.vtkPolyData()
    result.DeepCopy(output)
    return result


def sheet_id_array_name(poly_data: vtk.vtkPolyData) -> str:
    cell_data = poly_data.GetCellData()
    for name in ("sheetId", "SheetId", "SheetID"):
        if cell_data.GetArray(name) is not None:
            return name
    arrays = [
        cell_data.GetArrayName(index)
        for index in range(cell_data.GetNumberOfArrays())
    ]
    raise ValueError(f"range VTP has no sheet-id cell array; arrays={arrays}")


def merge_intervals(
    intervals: list[tuple[float, float]],
    *,
    epsilon: float = 1e-6,
) -> list[tuple[float, float]]:
    cleaned = [
        (min(float(start), float(end)), max(float(start), float(end)))
        for start, end in intervals
        if abs(float(end) - float(start)) > epsilon
    ]
    cleaned.sort()
    merged: list[tuple[float, float]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1] + epsilon:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def range_vtp_sheet_intervals(
    range_poly_data: vtk.vtkPolyData,
    *,
    sheet_id: int,
    orb00_value: float,
) -> list[tuple[float, float]]:
    threshold = vtk.vtkThreshold()
    threshold.SetInputData(range_poly_data)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        sheet_id_array_name(range_poly_data),
    )
    threshold.SetLowerThreshold(float(sheet_id) - 0.5)
    threshold.SetUpperThreshold(float(sheet_id) + 0.5)
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()
    sheet_poly_data = geometry.GetOutput()
    if sheet_poly_data.GetNumberOfCells() == 0:
        return []

    plane = vtk.vtkPlane()
    plane.SetOrigin(float(orb00_value), 0.0, 0.0)
    plane.SetNormal(1.0, 0.0, 0.0)

    cutter = vtk.vtkCutter()
    cutter.SetInputData(sheet_poly_data)
    cutter.SetCutFunction(plane)
    cutter.Update()
    cut_poly_data = cutter.GetOutput()

    intervals: list[tuple[float, float]] = []
    for cell_index in range(cut_poly_data.GetNumberOfCells()):
        cell = cut_poly_data.GetCell(cell_index)
        orb01_values = [
            float(cut_poly_data.GetPoint(cell.GetPointId(local_index))[1])
            for local_index in range(cell.GetNumberOfPoints())
        ]
        if orb01_values:
            intervals.append((min(orb01_values), max(orb01_values)))
    return merge_intervals(intervals)


def contour_orb00(
    vtu_file: Path,
    *,
    orb00_value: float,
) -> tuple[vtk.vtkUnstructuredGrid, vtk.vtkPolyData]:
    if not vtu_file.exists():
        raise FileNotFoundError(f"VTU file not found: {vtu_file}")

    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(vtu_file))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() <= 0:
        raise ValueError(f"failed to read nonempty VTU grid: {vtu_file}")
    if grid.GetPointData().GetArray("orb00") is None:
        arrays = [
            grid.GetPointData().GetArrayName(index)
            for index in range(grid.GetPointData().GetNumberOfArrays())
        ]
        raise KeyError(f"point-data scalar 'orb00' not found; arrays={arrays}")

    id_filter = vtk.vtkIdFilter()
    id_filter.SetInputData(grid)
    id_filter.SetCellIdsArrayName("sourceCellId")
    id_filter.CellIdsOn()
    id_filter.PointIdsOff()
    id_filter.Update()

    source_grid = vtk.vtkUnstructuredGrid()
    source_grid.DeepCopy(id_filter.GetOutput())
    source_grid.GetPointData().SetActiveScalars("orb00")

    contour = vtk.vtkContourFilter()
    contour.SetInputData(source_grid)
    contour.SetValue(0, float(orb00_value))
    contour.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(contour.GetOutput())
    if output.GetNumberOfCells() <= 0:
        raise ValueError(
            f"orb00={value_text(orb00_value)} produced an empty contour in {vtu_file}"
        )
    if output.GetPointData().GetArray("orb01") is None:
        arrays = [
            output.GetPointData().GetArrayName(index)
            for index in range(output.GetPointData().GetNumberOfArrays())
        ]
        raise KeyError(f"contour has no interpolated 'orb01' array; arrays={arrays}")
    if output.GetCellData().GetArray("sourceCellId") is None:
        raise ValueError("contour did not preserve sourceCellId")
    return source_grid, output


def add_int_metadata(poly_data: vtk.vtkPolyData, name: str, value: int) -> None:
    cells = vtk.vtkIntArray()
    cells.SetName(name)
    cells.SetNumberOfTuples(poly_data.GetNumberOfCells())
    cells.Fill(int(value))
    poly_data.GetCellData().AddArray(cells)

    field = vtk.vtkIntArray()
    field.SetName(name)
    field.InsertNextValue(int(value))
    poly_data.GetFieldData().AddArray(field)


def add_double_metadata(poly_data: vtk.vtkPolyData, name: str, value: float) -> None:
    cells = vtk.vtkDoubleArray()
    cells.SetName(name)
    cells.SetNumberOfTuples(poly_data.GetNumberOfCells())
    cells.Fill(float(value))
    poly_data.GetCellData().AddArray(cells)

    field = vtk.vtkDoubleArray()
    field.SetName(name)
    field.InsertNextValue(float(value))
    poly_data.GetFieldData().AddArray(field)


def write_poly_data(poly_data: vtk.vtkPolyData, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(destination))
    writer.SetInputData(poly_data)
    writer.SetDataModeToBinary()
    if writer.Write() != 1:
        raise RuntimeError(f"failed to write VTP: {destination}")


def connected_contour_regions(contour: vtk.vtkPolyData) -> vtk.vtkPolyData:
    connectivity = vtk.vtkConnectivityFilter()
    connectivity.SetInputData(contour)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.ColorRegionsOn()
    connectivity.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(connectivity.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())
    if output.GetCellData().GetArray("RegionId") is None:
        raise ValueError("connectivity filter did not produce RegionId")
    if output.GetCellData().GetArray("sourceCellId") is None:
        raise ValueError("connectivity filter did not preserve sourceCellId")
    return output


def region_sheet_support(
    *,
    source_grid: vtk.vtkUnstructuredGrid,
    connected_contour: vtk.vtkPolyData,
    sheet_vertices: dict[int, set[int]],
    sheet_ids: tuple[int, int],
) -> tuple[list[int], dict[tuple[int, int], int]]:
    region_ids_array = connected_contour.GetCellData().GetArray("RegionId")
    source_ids = connected_contour.GetCellData().GetArray("sourceCellId")
    region_ids = sorted(
        {
            int(region_ids_array.GetTuple1(cell_index))
            for cell_index in range(connected_contour.GetNumberOfCells())
        }
    )
    support = {
        (sheet_id, region_id): 0
        for sheet_id in sheet_ids
        for region_id in region_ids
    }

    for cell_index in range(connected_contour.GetNumberOfCells()):
        region_id = int(region_ids_array.GetTuple1(cell_index))
        source_cell = source_grid.GetCell(int(source_ids.GetTuple1(cell_index)))
        source_vertices = [
            int(source_cell.GetPointId(local_index))
            for local_index in range(source_cell.GetNumberOfPoints())
        ]
        for sheet_id in sheet_ids:
            vertices = sheet_vertices[sheet_id]
            support[(sheet_id, region_id)] += sum(
                vertex_id in vertices
                for vertex_id in source_vertices
            )
    return region_ids, support


def assign_regions_to_sheets(
    *,
    region_ids: list[int],
    support: dict[tuple[int, int], int],
    sheet_ids: tuple[int, int],
) -> dict[int, int]:
    if len(region_ids) < len(sheet_ids):
        raise ValueError(
            f"orb00 contour has {len(region_ids)} connected component(s), "
            f"but {len(sheet_ids)} sheets require distinct components"
        )

    best_assignment: dict[int, int] | None = None
    best_score = -1
    for selected_regions in itertools.permutations(region_ids, len(sheet_ids)):
        score = sum(
            support[(sheet_id, region_id)]
            for sheet_id, region_id in zip(sheet_ids, selected_regions)
        )
        if score > best_score:
            best_score = score
            best_assignment = dict(zip(sheet_ids, selected_regions))
    if best_assignment is None:
        raise ValueError("could not assign contour components to sheets")
    return best_assignment


def extract_contour_region(
    *,
    connected_contour: vtk.vtkPolyData,
    region_id: int,
    component_support: int,
    intervals: list[tuple[float, float]],
    sheet_id: int,
    timestep: int,
    orb00_value: float,
    destination: Path,
) -> dict:
    if not intervals:
        raise ValueError(
            f"sheet {sheet_id} does not intersect orb00={value_text(orb00_value)} "
            f"at timestep {timestep}"
        )

    threshold = vtk.vtkThreshold()
    threshold.SetInputData(connected_contour)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        "RegionId",
    )
    threshold.SetLowerThreshold(float(region_id))
    threshold.SetUpperThreshold(float(region_id))
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())
    if output.GetNumberOfCells() <= 0:
        raise ValueError(
            f"range restriction produced an empty surface for sheet {sheet_id} "
            f"at timestep {timestep}"
        )

    add_int_metadata(output, "requestedSheetId", sheet_id)
    add_int_metadata(output, "timestepIndex", timestep)
    add_int_metadata(output, "rangeIntervalCount", len(intervals))
    add_int_metadata(output, "assignedContourRegion", region_id)
    add_int_metadata(output, "componentSupport", component_support)
    add_double_metadata(output, "orb00Value", orb00_value)
    write_poly_data(output, destination)

    return {
        "timestep": timestep,
        "sheet_id": sheet_id,
        "orb00_value": orb00_value,
        "range_clip_field": "orb01",
        "range_clip_intervals": [[start, end] for start, end in intervals],
        "assigned_contour_region": region_id,
        "component_support": component_support,
        "path": str(destination),
        "points": int(output.GetNumberOfPoints()),
        "cells": int(output.GetNumberOfCells()),
        "bounds": [float(value) for value in output.GetBounds()],
    }


def export(args: argparse.Namespace) -> dict:
    base_dir = args.base_dir.expanduser().resolve()
    timesteps = (int(args.timesteps[0]), int(args.timesteps[1]))
    sheets_by_timestep = {
        timesteps[0]: tuple(int(value) for value in args.sheets_at_first),
        timesteps[1]: tuple(int(value) for value in args.sheets_at_second),
    }
    orb00_value = float(args.orb00_value)
    out_dir = (
        args.out_dir.expanduser().resolve()
        if args.out_dir
        else default_output_dir(base_dir, orb00_value, timesteps)
    )
    range_vtp_dir = (
        args.range_vtp_dir.expanduser().resolve()
        if args.range_vtp_dir
        else base_dir / "compareSheetShapesCache" / "cache" / "vtp"
    )
    rsi_dir = (
        args.rsi_dir.expanduser().resolve()
        if args.rsi_dir
        else base_dir / "sheetInfo"
    )

    if args.rebuild and out_dir.exists():
        shutil.rmtree(out_dir)

    outputs: list[dict] = []
    for timestep in timesteps:
        stem = f"{args.stem_prefix}{timestep}"
        source_grid, contour = contour_orb00(
            base_dir / args.vtu_subdir / f"{stem}.vtu",
            orb00_value=orb00_value,
        )
        connected_contour = connected_contour_regions(contour)
        range_poly_data = read_poly_data(range_vtp_dir / f"{stem}.sheets.vtp")
        sheet_vertices = read_rsi_sheet_vertices(rsi_dir / f"{stem}.rsi")
        sheet_ids = sheets_by_timestep[timestep]
        missing_sheet_ids = [
            sheet_id
            for sheet_id in sheet_ids
            if sheet_id not in sheet_vertices
        ]
        if missing_sheet_ids:
            raise ValueError(
                f"RSI file for {stem} has no vertices for sheets {missing_sheet_ids}"
            )
        region_ids, support = region_sheet_support(
            source_grid=source_grid,
            connected_contour=connected_contour,
            sheet_vertices=sheet_vertices,
            sheet_ids=sheet_ids,
        )
        region_assignment = assign_regions_to_sheets(
            region_ids=region_ids,
            support=support,
            sheet_ids=sheet_ids,
        )

        for sheet_id in sheet_ids:
            intervals = range_vtp_sheet_intervals(
                range_poly_data,
                sheet_id=sheet_id,
                orb00_value=orb00_value,
            )
            destination = out_dir / f"t{timestep:03d}_sheet_{sheet_id}.vtp"
            assigned_region = region_assignment[sheet_id]
            result = extract_contour_region(
                connected_contour=connected_contour,
                region_id=assigned_region,
                component_support=support[(sheet_id, assigned_region)],
                intervals=intervals,
                sheet_id=sheet_id,
                timestep=timestep,
                orb00_value=orb00_value,
                destination=destination,
            )
            outputs.append(result)
            print(
                f"t={timestep:03d} sheet={sheet_id}: "
                f"{result['points']} points, {result['cells']} cells, "
                f"component={assigned_region}, support={result['component_support']} "
                f"-> {destination}",
                flush=True,
            )

    manifest = {
        "base_dir": str(base_dir),
        "method": "vtk-contour-connected-components-with-rsi-one-to-one-assignment",
        "uses_fv99": False,
        "orb00_value": orb00_value,
        "timesteps": list(timesteps),
        "sheets_at_first": list(sheets_by_timestep[timesteps[0]]),
        "sheets_at_second": list(sheets_by_timestep[timesteps[1]]),
        "range_vtp_dir": str(range_vtp_dir),
        "rsi_dir": str(rsi_dir),
        "out_dir": str(out_dir),
        "outputs": outputs,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote manifest: {manifest_path}")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export four full sheet-restricted orb00 isosurfaces using VTK "
            "contour components and RSI one-to-one sheet assignment."
        )
    )
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--orb00-value", type=float, default=25.0)
    parser.add_argument("--timesteps", type=int, nargs=2, default=(18, 19))
    parser.add_argument("--sheets-at-first", type=int, nargs=2, default=(1795, 1796))
    parser.add_argument("--sheets-at-second", type=int, nargs=2, default=(1855, 1856))
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--stem-prefix", default="bi_torus_")
    parser.add_argument("--vtu-subdir", type=Path, default=Path("downsampledGrids"))
    parser.add_argument("--range-vtp-dir", type=Path)
    parser.add_argument("--rsi-dir", type=Path)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Remove and regenerate the selected output directory.",
    )
    return parser.parse_args()


def main() -> int:
    export(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
