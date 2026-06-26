#!/usr/bin/env python3
"""
Export sheet-restricted orb00 fiber-surface VTP sequences.

Default use is the torus case discussed for roots 1917 and 156:

  python3 export_orb00_fiber_sequence.py \
    --base-dir /home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus \
    --orb00-value 0.8606019643343535

Outputs are named as ParaView-friendly sequences:

  sheet_1917_000.vtp, sheet_1917_001.vtp, ...
  sheet_156_000.vtp,  sheet_156_001.vtp,  ...

The default backend uses VTK contouring on the requested point-data scalar,
cuts the tracked sheet polygon in range space at that scalar value, and keeps
the corresponding interval on the contour. This avoids two issues seen for the
torus sequence: fv99 --fieldFValueFS can collapse the orb00 surface to a
boundary strip, while filtering by RSI source tetra vertices can create
scattered patches. The actual matched sheet id at each timestep is stored in
cell/field data and in manifest.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import vtk

vtk.vtkObject.GlobalWarningDisplayOff()
vtk.vtkLogger.SetStderrVerbosity(vtk.vtkLogger.VERBOSITY_ERROR)


DEFAULT_BASE_DIR = Path(
    "/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus"
)
DEFAULT_FV99 = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/build/fv99"
)
DEFAULT_FV99_ROOT = DEFAULT_FV99.parent.parent
DEFAULT_LIBRARY_DIRS = (
    DEFAULT_FV99_ROOT / "libraries/ttk/build/lib",
    DEFAULT_FV99_ROOT / "libraries/ttk/install/lib",
    DEFAULT_FV99_ROOT / "libraries/vtk/install/lib",
)


@dataclass(frozen=True)
class TrackEntry:
    root_sheet_id: int
    timestep_index: int
    stem: str
    sheet_id: int
    score: float | None


class BinaryReader:
    def __init__(self, file_obj):
        self.f = file_obj

    def read_exact(self, nbytes: int) -> bytes:
        data = self.f.read(nbytes)
        if len(data) != nbytes:
            raise EOFError(f"Unexpected end of RSI file. Needed {nbytes} bytes, got {len(data)}.")
        return data

    def size_t(self) -> int:
        return struct.unpack("<Q", self.read_exact(8))[0]

    def uint8(self) -> int:
        return struct.unpack("<B", self.read_exact(1))[0]

    def int(self) -> int:
        return struct.unpack("<i", self.read_exact(4))[0]

    def double(self) -> float:
        return struct.unpack("<d", self.read_exact(8))[0]


def value_text(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def value_token(value: float) -> str:
    text = value_text(abs(value)).replace(".", "p")
    return ("m" if float(value) < 0.0 else "p") + text


def load_tracking_data(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"tracking data not found: {path}")
    return json.loads(path.read_text())


def read_rsi_sheet_vertices(path: Path) -> dict[int, set[int]]:
    if not path.exists():
        raise FileNotFoundError(f"RSI file not found: {path}")
    with path.open("rb") as f:
        reader = BinaryReader(f)
        _is_vertex_singular = [reader.uint8() for _ in range(reader.size_t())]
        _sheet_area = {reader.int(): reader.double() for _ in range(reader.size_t())}
        sheet_vertices: dict[int, set[int]] = {}
        for _ in range(reader.size_t()):
            sheet_id = reader.int()
            sheet_vertices[sheet_id] = {
                reader.int()
                for _ in range(reader.size_t())
            }
    return sheet_vertices


def metric_score(match: dict, metric: str) -> float:
    metrics = match.get("metrics")
    if isinstance(metrics, dict) and metric in metrics:
        return float(metrics[metric])
    if metric in match:
        return float(match[metric])
    for fallback in ("combined", "shape_iou", "final_score"):
        if isinstance(metrics, dict) and fallback in metrics:
            return float(metrics[fallback])
        if fallback in match:
            return float(match[fallback])
    return 0.0


def timestep_map(tracking_data: dict) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for row in tracking_data.get("timesteps", []):
        mapping[int(row["timestep_index"])] = str(row["stem"])
    if not mapping:
        raise ValueError("tracking data has no timesteps")
    return mapping


def load_sheet_bboxes(path: Path) -> dict[int, tuple[float, float, float, float]]:
    if not path.exists():
        raise FileNotFoundError(f"sheet descriptor file not found: {path}")
    data = json.loads(path.read_text())
    bboxes: dict[int, tuple[float, float, float, float]] = {}
    for row in data.get("sheets", []):
        bbox = row.get("bbox")
        if bbox is None or len(bbox) != 4:
            continue
        f0, g0, f1, g1 = (float(value) for value in bbox)
        bboxes[int(row["sheet_id"])] = (
            min(f0, f1),
            min(g0, g1),
            max(f0, f1),
            max(g0, g1),
        )
    if not bboxes:
        raise ValueError(f"no sheet bboxes found in {path}")
    return bboxes


def bbox_scalar_and_clip_ranges(
    bbox: tuple[float, float, float, float],
    *,
    scalar_name: str,
    f_name: str,
    g_name: str,
) -> tuple[tuple[float, float], str, tuple[float, float]]:
    f_min, g_min, f_max, g_max = bbox
    if scalar_name == f_name:
        return (f_min, f_max), g_name, (g_min, g_max)
    if scalar_name == g_name:
        return (g_min, g_max), f_name, (f_min, f_max)
    raise ValueError(
        "range-bbox sheet filtering is defined only for the configured "
        f"range fields {f_name!r} and {g_name!r}; got {scalar_name!r}"
    )


def coordinate_axis_for_field(field_name: str, *, f_name: str, g_name: str) -> int:
    if field_name == f_name:
        return 0
    if field_name == g_name:
        return 1
    raise ValueError(
        f"field {field_name!r} is not one of the configured range fields "
        f"{f_name!r}, {g_name!r}"
    )


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


def pair_lookup(tracking_data: dict) -> dict[tuple[int, int], dict]:
    by_stride = tracking_data.get("shape_pairs_by_stride")
    if isinstance(by_stride, dict):
        pairs = by_stride.get("1", [])
    else:
        pairs = tracking_data.get("shape_pairs", [])
    lookup: dict[tuple[int, int], dict] = {}
    for pair in pairs:
        source_index = int(pair["source_timestep_index"])
        target_index = int(pair["target_timestep_index"])
        if target_index == source_index + 1:
            lookup[(source_index, target_index)] = pair
    return lookup


def follow_range_track(
    tracking_data: dict,
    *,
    root_sheet_id: int,
    start: int,
    end: int,
    metric: str,
) -> list[TrackEntry]:
    stems = timestep_map(tracking_data)
    pairs = pair_lookup(tracking_data)
    current_sheet_id = int(root_sheet_id)
    entries: list[TrackEntry] = []

    for timestep_index in range(int(start), int(end) + 1):
        if timestep_index not in stems:
            raise ValueError(f"missing timestep {timestep_index} in tracking data")
        entries.append(
            TrackEntry(
                root_sheet_id=int(root_sheet_id),
                timestep_index=timestep_index,
                stem=stems[timestep_index],
                sheet_id=current_sheet_id,
                score=None,
            )
        )
        if timestep_index == int(end):
            break

        pair = pairs.get((timestep_index, timestep_index + 1))
        if pair is None:
            raise ValueError(f"missing range pair {timestep_index}->{timestep_index + 1}")

        candidates = [
            match
            for match in pair.get("matches", [])
            if int(match.get("source_sheet_id", -1)) == current_sheet_id
        ]
        if not candidates:
            raise ValueError(
                f"no outgoing range match from sheet {current_sheet_id} "
                f"at timestep {timestep_index}"
            )

        best = max(candidates, key=lambda match: metric_score(match, metric))
        next_score = metric_score(best, metric)
        current_sheet_id = int(best["target_sheet_id"])
        entries[-1] = TrackEntry(
            root_sheet_id=int(root_sheet_id),
            timestep_index=timestep_index,
            stem=stems[timestep_index],
            sheet_id=entries[-1].sheet_id,
            score=next_score,
        )

    return entries


def make_fv99_environment(library_dirs: list[Path], omp_threads: int) -> dict[str, str]:
    env = os.environ.copy()
    paths = [str(path) for path in library_dirs if str(path)]
    if env.get("LD_LIBRARY_PATH"):
        paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(paths)
    env["OMP_NUM_THREADS"] = str(max(1, int(omp_threads)))
    return env


def run_fv99_orb00_surface(
    *,
    fv99: Path,
    vtu_file: Path,
    rs_file: Path,
    orb00_value: float,
    f_name: str,
    g_name: str,
    destination: Path,
    log_file: Path,
    temp_dir: Path,
    env: dict[str, str],
    rebuild: bool,
) -> Path:
    if destination.exists() and not rebuild:
        return destination

    if not fv99.exists():
        raise FileNotFoundError(f"fv99 binary not found: {fv99}")
    if not os.access(fv99, os.X_OK):
        raise PermissionError(f"fv99 binary is not executable: {fv99}")
    if not vtu_file.exists():
        raise FileNotFoundError(f"VTU file not found: {vtu_file}")
    if not rs_file.exists():
        raise FileNotFoundError(f"Reeb-space file not found: {rs_file}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(fv99),
        "-f",
        str(vtu_file),
        "-l",
        str(rs_file),
        "--fieldFValueFS",
        value_text(orb00_value),
        "--fName",
        f_name,
        "--gName",
        g_name,
        "--headless",
    ]

    with tempfile.TemporaryDirectory(
        prefix=f"{vtu_file.stem}_orb00_{value_token(orb00_value)}_",
        dir=temp_dir,
    ) as tmp_name:
        work_dir = Path(tmp_name)
        (work_dir / "output").mkdir(parents=True, exist_ok=True)
        with log_file.open("w") as log:
            result = subprocess.run(
                command,
                cwd=work_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )

        source = work_dir / "output" / "labeled.fs.f.vtp"
        if result.returncode != 0:
            raise RuntimeError(f"fv99 failed with return code {result.returncode}; log={log_file}")
        if not source.exists():
            raise RuntimeError(f"fv99 did not write output/labeled.fs.f.vtp; log={log_file}")

        destination.unlink(missing_ok=True)
        shutil.move(str(source), str(destination))

    return destination


def read_poly_data(path: Path) -> vtk.vtkPolyData:
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    output = reader.GetOutput()
    if output is None:
        raise ValueError(f"failed to read VTP: {path}")
    return output


def sheet_id_array_name(poly_data: vtk.vtkPolyData) -> str:
    cell_data = poly_data.GetCellData()
    for name in ("sheetId", "SheetId", "SheetID"):
        if cell_data.GetArray(name) is not None:
            return name
    names = [cell_data.GetArrayName(index) for index in range(cell_data.GetNumberOfArrays())]
    raise ValueError(f"VTP has no sheetId cell-data array; arrays={names}")


def range_vtp_sheet_intervals(
    range_poly_data: vtk.vtkPolyData,
    *,
    sheet_id: int,
    scalar_name: str,
    scalar_value: float,
    f_name: str,
    g_name: str,
) -> tuple[str, list[tuple[float, float]]]:
    scalar_axis = coordinate_axis_for_field(
        scalar_name,
        f_name=f_name,
        g_name=g_name,
    )
    clip_field_name = g_name if scalar_axis == 0 else f_name
    clip_axis = 1 if scalar_axis == 0 else 0

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
        return clip_field_name, []

    plane = vtk.vtkPlane()
    origin = [0.0, 0.0, 0.0]
    origin[scalar_axis] = float(scalar_value)
    normal = [0.0, 0.0, 0.0]
    normal[scalar_axis] = 1.0
    plane.SetOrigin(origin)
    plane.SetNormal(normal)

    cutter = vtk.vtkCutter()
    cutter.SetInputData(sheet_poly_data)
    cutter.SetCutFunction(plane)
    cutter.Update()
    cut_poly_data = cutter.GetOutput()

    intervals: list[tuple[float, float]] = []
    for cell_index in range(cut_poly_data.GetNumberOfCells()):
        cell = cut_poly_data.GetCell(cell_index)
        values = [
            float(cut_poly_data.GetPoint(cell.GetPointId(local_index))[clip_axis])
            for local_index in range(cell.GetNumberOfPoints())
        ]
        if values:
            intervals.append((min(values), max(values)))
    return clip_field_name, merge_intervals(intervals)


def add_constant_int_array(poly_data: vtk.vtkPolyData, name: str, value: int) -> None:
    cell_count = poly_data.GetNumberOfCells()
    arr = vtk.vtkIntArray()
    arr.SetName(name)
    arr.SetNumberOfTuples(cell_count)
    for index in range(cell_count):
        arr.SetValue(index, int(value))
    poly_data.GetCellData().AddArray(arr)

    field_arr = vtk.vtkIntArray()
    field_arr.SetName(name)
    field_arr.SetNumberOfTuples(1)
    field_arr.SetValue(0, int(value))
    poly_data.GetFieldData().AddArray(field_arr)


def add_constant_double_array(poly_data: vtk.vtkPolyData, name: str, value: float) -> None:
    cell_count = poly_data.GetNumberOfCells()
    arr = vtk.vtkDoubleArray()
    arr.SetName(name)
    arr.SetNumberOfTuples(cell_count)
    for index in range(cell_count):
        arr.SetValue(index, float(value))
    poly_data.GetCellData().AddArray(arr)

    field_arr = vtk.vtkDoubleArray()
    field_arr.SetName(name)
    field_arr.SetNumberOfTuples(1)
    field_arr.SetValue(0, float(value))
    poly_data.GetFieldData().AddArray(field_arr)


def write_poly_data(poly_data: vtk.vtkPolyData, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(destination))
    writer.SetInputData(poly_data)
    writer.SetDataModeToBinary()
    if writer.Write() != 1:
        raise RuntimeError(f"failed to write VTP: {destination}")
    return int(poly_data.GetNumberOfCells())


def threshold_fv99_labeled_sheet_surface(
    *,
    labeled_surface: Path,
    destination: Path,
    root_sheet_id: int,
    timestep_index: int,
    actual_sheet_id: int,
    orb00_value: float,
) -> int:
    poly_data = read_poly_data(labeled_surface)
    array_name = sheet_id_array_name(poly_data)

    threshold = vtk.vtkThreshold()
    threshold.SetInputData(poly_data)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        array_name,
    )
    threshold.SetLowerThreshold(float(actual_sheet_id) - 0.5)
    threshold.SetUpperThreshold(float(actual_sheet_id) + 0.5)
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())
    add_constant_int_array(output, "trackRootSheetId", int(root_sheet_id))
    add_constant_int_array(output, "sequenceTimestep", int(timestep_index))
    add_constant_int_array(output, "actualSheetId", int(actual_sheet_id))
    add_constant_double_array(output, "orb00Value", float(orb00_value))

    return write_poly_data(output, destination)


def contour_with_source_cells(
    vtu_file: Path,
    *,
    field_name: str,
    value: float,
) -> tuple[vtk.vtkUnstructuredGrid, vtk.vtkPolyData]:
    if not vtu_file.exists():
        raise FileNotFoundError(f"VTU file not found: {vtu_file}")

    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(vtu_file))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() <= 0:
        raise ValueError(f"failed to read nonempty VTU grid: {vtu_file}")
    if grid.GetPointData().GetArray(field_name) is None:
        names = [grid.GetPointData().GetArrayName(i) for i in range(grid.GetPointData().GetNumberOfArrays())]
        raise KeyError(f"point-data scalar {field_name!r} not found in {vtu_file}; arrays={names}")

    id_filter = vtk.vtkIdFilter()
    id_filter.SetInputData(grid)
    id_filter.SetCellIdsArrayName("sourceCellId")
    id_filter.CellIdsOn()
    id_filter.PointIdsOff()
    id_filter.Update()

    id_grid = vtk.vtkUnstructuredGrid()
    id_grid.DeepCopy(id_filter.GetOutput())
    id_grid.GetPointData().SetActiveScalars(field_name)

    contour = vtk.vtkContourFilter()
    contour.SetInputData(id_grid)
    contour.SetValue(0, float(value))
    contour.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(contour.GetOutput())
    if output.GetCellData().GetArray("sourceCellId") is None:
        raise ValueError("VTK contour did not preserve sourceCellId cell data")
    return id_grid, output


def threshold_vtk_contour_by_sheet_vertices(
    *,
    source_grid: vtk.vtkUnstructuredGrid,
    contour: vtk.vtkPolyData,
    sheet_vertices: set[int],
    destination: Path,
    root_sheet_id: int,
    timestep_index: int,
    actual_sheet_id: int,
    scalar_name: str,
    scalar_value: float,
    min_source_vertices: int,
) -> int:
    source_ids = contour.GetCellData().GetArray("sourceCellId")
    if source_ids is None:
        raise ValueError("contour has no sourceCellId array")

    hit_counts = vtk.vtkIntArray()
    hit_counts.SetName("sourceSheetVertexHitCount")
    hit_counts.SetNumberOfTuples(contour.GetNumberOfCells())

    for contour_cell_index in range(contour.GetNumberOfCells()):
        source_cell_id = int(source_ids.GetTuple1(contour_cell_index))
        source_cell = source_grid.GetCell(source_cell_id)
        hit_count = 0
        for point_index in range(source_cell.GetNumberOfPoints()):
            if int(source_cell.GetPointId(point_index)) in sheet_vertices:
                hit_count += 1
        hit_counts.SetValue(contour_cell_index, hit_count)

    annotated = vtk.vtkPolyData()
    annotated.DeepCopy(contour)
    annotated.GetCellData().AddArray(hit_counts)

    threshold = vtk.vtkThreshold()
    threshold.SetInputData(annotated)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        "sourceSheetVertexHitCount",
    )
    threshold.SetLowerThreshold(float(max(1, int(min_source_vertices))))
    threshold.SetUpperThreshold(4.0)
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())
    add_constant_int_array(output, "trackRootSheetId", int(root_sheet_id))
    add_constant_int_array(output, "sequenceTimestep", int(timestep_index))
    add_constant_int_array(output, "actualSheetId", int(actual_sheet_id))
    add_constant_double_array(output, f"{scalar_name}Value", float(scalar_value))
    return write_poly_data(output, destination)


def threshold_vtk_contour_by_range_intervals(
    *,
    contour: vtk.vtkPolyData,
    clip_field_name: str,
    intervals: list[tuple[float, float]],
    destination: Path,
    root_sheet_id: int,
    timestep_index: int,
    actual_sheet_id: int,
    scalar_name: str,
    scalar_value: float,
    range_padding: float,
) -> int:
    padding = max(0.0, float(range_padding))
    padded_intervals = [
        (float(start) - padding, float(end) + padding)
        for start, end in intervals
    ]

    if not padded_intervals:
        empty = vtk.vtkPolyData()
        add_constant_int_array(empty, "trackRootSheetId", int(root_sheet_id))
        add_constant_int_array(empty, "sequenceTimestep", int(timestep_index))
        add_constant_int_array(empty, "actualSheetId", int(actual_sheet_id))
        add_constant_double_array(empty, f"{scalar_name}Value", float(scalar_value))
        add_constant_int_array(empty, "rangeIntervalCount", 0)
        return write_poly_data(empty, destination)

    clip_values = contour.GetPointData().GetArray(clip_field_name)
    if clip_values is None:
        names = [
            contour.GetPointData().GetArrayName(index)
            for index in range(contour.GetPointData().GetNumberOfArrays())
        ]
        raise KeyError(
            f"contour has no point-data array {clip_field_name!r}; arrays={names}"
        )

    hit = vtk.vtkIntArray()
    hit.SetName("rangeIntervalHit")
    hit.SetNumberOfTuples(contour.GetNumberOfCells())
    centroid_values = vtk.vtkDoubleArray()
    centroid_values.SetName(f"{clip_field_name}Centroid")
    centroid_values.SetNumberOfTuples(contour.GetNumberOfCells())

    for cell_index in range(contour.GetNumberOfCells()):
        cell = contour.GetCell(cell_index)
        total = 0.0
        point_count = max(1, cell.GetNumberOfPoints())
        for local_index in range(cell.GetNumberOfPoints()):
            total += float(clip_values.GetTuple1(cell.GetPointId(local_index)))
        centroid = total / point_count
        centroid_values.SetValue(cell_index, centroid)
        in_interval = any(start <= centroid <= end for start, end in padded_intervals)
        hit.SetValue(cell_index, 1 if in_interval else 0)

    annotated = vtk.vtkPolyData()
    annotated.DeepCopy(contour)
    annotated.GetCellData().AddArray(hit)
    annotated.GetCellData().AddArray(centroid_values)

    threshold = vtk.vtkThreshold()
    threshold.SetInputData(annotated)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        "rangeIntervalHit",
    )
    threshold.SetLowerThreshold(1.0)
    threshold.SetUpperThreshold(1.0)
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())
    add_constant_int_array(output, "trackRootSheetId", int(root_sheet_id))
    add_constant_int_array(output, "sequenceTimestep", int(timestep_index))
    add_constant_int_array(output, "actualSheetId", int(actual_sheet_id))
    add_constant_double_array(output, f"{scalar_name}Value", float(scalar_value))
    add_constant_int_array(output, "rangeIntervalCount", len(intervals))
    return write_poly_data(output, destination)


def threshold_vtk_contour_by_range_bbox(
    *,
    contour: vtk.vtkPolyData,
    bbox: tuple[float, float, float, float],
    destination: Path,
    root_sheet_id: int,
    timestep_index: int,
    actual_sheet_id: int,
    scalar_name: str,
    scalar_value: float,
    f_name: str,
    g_name: str,
    range_padding: float,
) -> tuple[int, str, list[tuple[float, float]]]:
    scalar_range, clip_field_name, clip_range = bbox_scalar_and_clip_ranges(
        bbox,
        scalar_name=scalar_name,
        f_name=f_name,
        g_name=g_name,
    )
    padding = max(0.0, float(range_padding))
    intervals = (
        [clip_range]
        if scalar_range[0] - padding <= scalar_value <= scalar_range[1] + padding
        else []
    )
    cell_count = threshold_vtk_contour_by_range_intervals(
        contour=contour,
        clip_field_name=clip_field_name,
        intervals=intervals,
        destination=destination,
        root_sheet_id=root_sheet_id,
        timestep_index=timestep_index,
        actual_sheet_id=actual_sheet_id,
        scalar_name=scalar_name,
        scalar_value=scalar_value,
        range_padding=range_padding,
    )

    output = read_poly_data(destination)
    add_constant_double_array(output, "rangeBBoxFMin", float(bbox[0]))
    add_constant_double_array(output, "rangeBBoxGMin", float(bbox[1]))
    add_constant_double_array(output, "rangeBBoxFMax", float(bbox[2]))
    add_constant_double_array(output, "rangeBBoxGMax", float(bbox[3]))
    if intervals:
        add_constant_double_array(output, "rangeClipMin", float(intervals[0][0]))
        add_constant_double_array(output, "rangeClipMax", float(intervals[-1][1]))
    write_poly_data(output, destination)
    return cell_count, clip_field_name, intervals


def export_sequences(args: argparse.Namespace) -> dict:
    base_dir = args.base_dir.resolve()
    tracking_file = args.tracking_data or (base_dir / "sankey" / "tracking_data.json")
    vtu_dir = args.vtu_dir or (base_dir / "downsampledGrids")
    rs_dir = args.rs_dir or (base_dir / "reebSpaces")
    rsi_dir = args.rsi_dir or (base_dir / "sheetInfo")
    descriptor_dir = args.descriptor_dir or (
        base_dir / "compareSheetShapesCache" / "cache" / "timesteps"
    )
    range_vtp_dir = args.range_vtp_dir or (
        base_dir / "compareSheetShapesCache" / "cache" / "vtp"
    )

    scalar_name = str(args.field_name)
    scalar_value = float(args.orb00_value)
    out_dir = args.out_dir or (
        base_dir
        / "sheetFiberSurfaces"
        / "orb00_sequences"
        / f"{scalar_name}_{value_token(scalar_value)}_{args.backend}_{args.sheet_filter}"
    )
    out_dir = out_dir.resolve()
    labeled_dir = out_dir / "_labeled"
    log_dir = out_dir / "_logs"
    temp_dir = out_dir / "_tmp"

    tracking_data = load_tracking_data(tracking_file)
    tracks = [
        follow_range_track(
            tracking_data,
            root_sheet_id=int(root),
            start=int(args.start),
            end=int(args.end),
            metric=args.metric,
        )
        for root in args.roots
    ]

    env = make_fv99_environment(args.library_dir, args.omp_threads) if args.backend == "fv99" else {}
    labeled_cache: dict[str, Path] = {}
    contour_cache: dict[str, tuple[vtk.vtkUnstructuredGrid, vtk.vtkPolyData]] = {}
    rsi_cache: dict[str, dict[int, set[int]]] = {}
    bbox_cache: dict[str, dict[int, tuple[float, float, float, float]]] = {}
    range_interval_cache: dict[tuple[str, int], tuple[str, list[tuple[float, float]]]] = {}
    manifest_tracks = []

    if args.backend == "vtk" and args.sheet_filter == "range-vtp":
        needed_by_stem: dict[str, set[int]] = {}
        for track in tracks:
            for entry in track:
                needed_by_stem.setdefault(entry.stem, set()).add(int(entry.sheet_id))

        for stem, sheet_ids in sorted(needed_by_stem.items()):
            range_vtp_file = range_vtp_dir / f"{stem}.sheets.vtp"
            range_poly_data = read_poly_data(range_vtp_file)
            for sheet_id in sorted(sheet_ids):
                range_interval_cache[(stem, sheet_id)] = range_vtp_sheet_intervals(
                    range_poly_data,
                    sheet_id=sheet_id,
                    scalar_name=scalar_name,
                    scalar_value=scalar_value,
                    f_name=args.f_name,
                    g_name=args.g_name,
                )

    for track in tracks:
        manifest_entries = []
        for entry in track:
            vtu_file = vtu_dir / f"{entry.stem}.vtu"
            rs_file = rs_dir / f"{entry.stem}.rs"
            labeled_surface = labeled_dir / entry.stem / f"{scalar_name}_{value_token(scalar_value)}.vtp"

            destination = out_dir / f"sheet_{entry.root_sheet_id}_{entry.timestep_index:03d}.vtp"
            range_clip_field = None
            range_intervals = None
            if destination.exists() and not args.rebuild:
                cell_count = read_poly_data(destination).GetNumberOfCells()
            elif args.backend == "fv99":
                if entry.stem not in labeled_cache:
                    if scalar_name != args.f_name:
                        raise NotImplementedError(
                            "fv99 backend in this script supports only the F field "
                            f"({args.f_name}); use --backend vtk for {scalar_name}."
                        )
                    run_fv99_orb00_surface(
                        fv99=args.fv99,
                        vtu_file=vtu_file,
                        rs_file=rs_file,
                        orb00_value=scalar_value,
                        f_name=args.f_name,
                        g_name=args.g_name,
                        destination=labeled_surface,
                        log_file=log_dir / f"{entry.stem}.{scalar_name}_{value_token(scalar_value)}.fv99.log",
                        temp_dir=temp_dir,
                        env=env,
                        rebuild=bool(args.rebuild_labeled),
                    )
                    labeled_cache[entry.stem] = labeled_surface
                cell_count = threshold_fv99_labeled_sheet_surface(
                    labeled_surface=labeled_cache[entry.stem],
                    destination=destination,
                    root_sheet_id=entry.root_sheet_id,
                    timestep_index=entry.timestep_index,
                    actual_sheet_id=entry.sheet_id,
                    orb00_value=scalar_value,
                )
            else:
                if entry.stem not in contour_cache:
                    contour_cache[entry.stem] = contour_with_source_cells(
                        vtu_file,
                        field_name=scalar_name,
                        value=scalar_value,
                    )
                source_grid, contour = contour_cache[entry.stem]
                if args.sheet_filter == "source-vertices":
                    if entry.stem not in rsi_cache:
                        rsi_cache[entry.stem] = read_rsi_sheet_vertices(rsi_dir / f"{entry.stem}.rsi")
                    sheet_vertices = rsi_cache[entry.stem].get(int(entry.sheet_id), set())
                    if not sheet_vertices:
                        raise ValueError(f"sheet {entry.sheet_id} has no RSI regular vertices at {entry.stem}")
                    cell_count = threshold_vtk_contour_by_sheet_vertices(
                        source_grid=source_grid,
                        contour=contour,
                        sheet_vertices=sheet_vertices,
                        destination=destination,
                        root_sheet_id=entry.root_sheet_id,
                        timestep_index=entry.timestep_index,
                        actual_sheet_id=entry.sheet_id,
                        scalar_name=scalar_name,
                        scalar_value=scalar_value,
                        min_source_vertices=args.min_source_vertices,
                    )
                elif args.sheet_filter == "range-bbox":
                    if entry.stem not in bbox_cache:
                        bbox_cache[entry.stem] = load_sheet_bboxes(
                            descriptor_dir / f"{entry.stem}.json"
                        )
                    bbox = bbox_cache[entry.stem].get(int(entry.sheet_id))
                    if bbox is None:
                        raise ValueError(
                            f"sheet {entry.sheet_id} has no range bbox at {entry.stem}"
                        )
                    cell_count, range_clip_field, range_intervals = threshold_vtk_contour_by_range_bbox(
                        contour=contour,
                        bbox=bbox,
                        destination=destination,
                        root_sheet_id=entry.root_sheet_id,
                        timestep_index=entry.timestep_index,
                        actual_sheet_id=entry.sheet_id,
                        scalar_name=scalar_name,
                        scalar_value=scalar_value,
                        f_name=args.f_name,
                        g_name=args.g_name,
                        range_padding=args.range_padding,
                    )
                else:
                    cache_key = (entry.stem, int(entry.sheet_id))
                    range_clip_field, range_intervals = range_interval_cache.get(
                        cache_key,
                        (None, None),
                    )
                    if range_clip_field is None or range_intervals is None:
                        raise ValueError(
                            f"missing range-vtp intervals for sheet {entry.sheet_id} "
                            f"at {entry.stem}"
                        )
                    cell_count = threshold_vtk_contour_by_range_intervals(
                        contour=contour,
                        clip_field_name=range_clip_field,
                        intervals=range_intervals,
                        destination=destination,
                        root_sheet_id=entry.root_sheet_id,
                        timestep_index=entry.timestep_index,
                        actual_sheet_id=entry.sheet_id,
                        scalar_name=scalar_name,
                        scalar_value=scalar_value,
                        range_padding=args.range_padding,
                    )

            manifest_entry = {
                "filename": destination.name,
                "path": str(destination),
                "root_sheet_id": entry.root_sheet_id,
                "timestep_index": entry.timestep_index,
                "stem": entry.stem,
                "actual_sheet_id": entry.sheet_id,
                "outgoing_range_score": entry.score,
                "cell_count": cell_count,
            }
            if range_clip_field is not None and range_intervals is not None:
                manifest_entry["range_clip_field"] = range_clip_field
                manifest_entry["range_clip_intervals"] = [
                    [start, end]
                    for start, end in range_intervals
                ]
            manifest_entries.append(manifest_entry)
            print(
                f"root {entry.root_sheet_id}: t={entry.timestep_index:03d} "
                f"actual_sheet={entry.sheet_id} cells={cell_count} -> {destination.name}",
                flush=True,
            )

        manifest_tracks.append(
            {
                "root_sheet_id": int(track[0].root_sheet_id),
                "files": manifest_entries,
            }
        )

    manifest = {
        "base_dir": str(base_dir),
        "tracking_data": str(tracking_file),
        "backend": args.backend,
        "sheet_filter": args.sheet_filter,
        "scalar_name": scalar_name,
        "scalar_value": scalar_value,
        "orb00_value": scalar_value if scalar_name == "orb00" else None,
        "min_source_vertices": int(args.min_source_vertices),
        "range_padding": float(args.range_padding),
        "range_vtp_dir": str(range_vtp_dir),
        "f_name": args.f_name,
        "g_name": args.g_name,
        "metric": args.metric,
        "start": int(args.start),
        "end": int(args.end),
        "out_dir": str(out_dir),
        "tracks": manifest_tracks,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    if args.clean_tmp:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate ParaView-loadable sheet fiber-surface VTP sequences at a fixed orb00 value."
    )
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--orb00-value", type=float, required=True)
    parser.add_argument("--roots", type=int, nargs="+", default=[1917, 156])
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=19)
    parser.add_argument(
        "--metric",
        default="combined",
        help="Range metric used to follow the outgoing chain in tracking_data.json.",
    )
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--tracking-data", type=Path)
    parser.add_argument("--vtu-dir", type=Path)
    parser.add_argument("--rs-dir", type=Path)
    parser.add_argument("--rsi-dir", type=Path)
    parser.add_argument("--descriptor-dir", type=Path)
    parser.add_argument("--range-vtp-dir", type=Path)
    parser.add_argument(
        "--backend",
        choices=("vtk", "fv99"),
        default="vtk",
        help="vtk writes a true scalar contour; fv99 uses labeled.fs.f.vtp.",
    )
    parser.add_argument(
        "--sheet-filter",
        choices=("range-vtp", "range-bbox", "source-vertices"),
        default="range-vtp",
        help=(
            "For --backend vtk, range-vtp cuts the cached range-space sheet "
            "polygon at the scalar value; range-bbox clips by the sheet bbox; "
            "source-vertices uses the older RSI source-tetra vertex mask."
        ),
    )
    parser.add_argument(
        "--field-name",
        default="orb00",
        help="Point-data scalar to contour when using --backend vtk. Defaults to orb00.",
    )
    parser.add_argument(
        "--min-source-vertices",
        type=int,
        default=2,
        help="For --sheet-filter source-vertices, keep contour cells whose source tetra has at least this many vertices in the sheet.",
    )
    parser.add_argument(
        "--range-padding",
        type=float,
        default=1e-6,
        help="Padding applied to range-bbox scalar and clip intervals.",
    )
    parser.add_argument("--fv99", type=Path, default=DEFAULT_FV99)
    parser.add_argument("--f-name", default="orb00")
    parser.add_argument("--g-name", default="orb01")
    parser.add_argument("--omp-threads", type=int, default=1)
    parser.add_argument(
        "--library-dir",
        type=Path,
        action="append",
        default=list(DEFAULT_LIBRARY_DIRS),
        help="Library directory to prepend to LD_LIBRARY_PATH. May be repeated.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate thresholded sequence VTPs even if they already exist.",
    )
    parser.add_argument(
        "--rebuild-labeled",
        action="store_true",
        help="Regenerate cached labeled orb00 fiber surfaces before thresholding.",
    )
    parser.add_argument(
        "--clean-tmp",
        action="store_true",
        help="Remove temporary fv99 work directories after successful export.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = export_sequences(args)
    print(f"Wrote manifest: {Path(manifest['out_dir']) / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
