#!/usr/bin/env python3
"""Render Reeb-space sheet PNGs with ParaView surface actors.

This script is intended to be run with ParaView's pvpython, not system Python.
It receives a JSON spec produced by SheetRenderer/render_rs_directory_orbital_colours.py.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from pathlib import Path

import paraview.simple as pvs  # type: ignore

try:
    import vtk  # type: ignore
except Exception:  # pragma: no cover - pvpython usually provides one of these APIs.
    from vtkmodules.vtkCommonCore import vtkPoints, vtkUnsignedCharArray  # type: ignore
    from vtkmodules.vtkCommonDataModel import vtkPolyData  # type: ignore
    from vtkmodules.vtkCommonDataModel import vtkCellArray  # type: ignore
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader, vtkXMLPolyDataWriter  # type: ignore

    class _VtkModule:
        vtkPoints = vtkPoints
        vtkUnsignedCharArray = vtkUnsignedCharArray
        vtkPolyData = vtkPolyData
        vtkCellArray = vtkCellArray
        vtkXMLPolyDataReader = vtkXMLPolyDataReader
        vtkXMLPolyDataWriter = vtkXMLPolyDataWriter

    vtk = _VtkModule()


SHEET_ID_ARRAY_NAMES = ("SheetId", "SheetID", "sheet_id", "sheetId")


def rgb255(rgb: list[float] | tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(round(float(channel) * 255.0)))) for channel in rgb)  # type: ignore[return-value]


def read_polydata(path: Path):
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    output = reader.GetOutput()
    if output is None:
        raise RuntimeError(f"Could not read VTP {path}")
    return output


def sheet_id_array(polydata):
    cell_data = polydata.GetCellData()
    for name in SHEET_ID_ARRAY_NAMES:
        array = cell_data.GetArray(name)
        if array is not None:
            return array
    raise RuntimeError(
        "Sheet VTP does not contain a SheetId cell-data array; "
        f"looked for {', '.join(SHEET_ID_ARRAY_NAMES)}"
    )


def add_sheet_color_array(polydata, colors_by_sheet: dict[int, tuple[int, int, int]], default_rgb: tuple[int, int, int]) -> None:
    sheets = sheet_id_array(polydata)
    colors = vtk.vtkUnsignedCharArray()
    colors.SetName("SheetColor")
    colors.SetNumberOfComponents(3)
    colors.SetNumberOfTuples(polydata.GetNumberOfCells())

    for cell_id in range(polydata.GetNumberOfCells()):
        sheet_id = int(sheets.GetTuple1(cell_id))
        colors.SetTuple3(cell_id, *colors_by_sheet.get(sheet_id, default_rgb))

    polydata.GetCellData().AddArray(colors)
    polydata.GetCellData().SetScalars(colors)


def write_polydata(polydata, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(polydata)
    if writer.Write() != 1:
        raise RuntimeError(f"Failed to write temporary VTP {path}")
    return path


def styled_all_cells_vtp(input_vtp: Path, output_vtp: Path, colors_by_sheet: dict[int, tuple[int, int, int]], default_rgb: tuple[int, int, int]) -> Path:
    polydata = read_polydata(input_vtp)
    add_sheet_color_array(polydata, colors_by_sheet, default_rgb)
    return write_polydata(polydata, output_vtp)


def filtered_sheets_vtp(
    input_vtp: Path,
    output_vtp: Path,
    colors_by_sheet: dict[int, tuple[int, int, int]],
    default_rgb: tuple[int, int, int],
    include_sheet_ids: set[int] | None = None,
    exclude_sheet_ids: set[int] | None = None,
) -> Path:
    source = read_polydata(input_vtp)
    sheets = sheet_id_array(source)

    filtered = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    if source.GetPoints() is not None:
        points.DeepCopy(source.GetPoints())
    filtered.SetPoints(points)

    cells = vtk.vtkCellArray()
    colors = vtk.vtkUnsignedCharArray()
    colors.SetName("SheetColor")
    colors.SetNumberOfComponents(3)

    for cell_id in range(source.GetNumberOfCells()):
        sheet_id = int(sheets.GetTuple1(cell_id))
        if include_sheet_ids is not None and sheet_id not in include_sheet_ids:
            continue
        if exclude_sheet_ids is not None and sheet_id in exclude_sheet_ids:
            continue
        cells.InsertNextCell(source.GetCell(cell_id).GetPointIds())
        colors.InsertNextTuple3(*colors_by_sheet.get(sheet_id, default_rgb))

    filtered.SetPolys(cells)
    filtered.GetCellData().AddArray(colors)
    filtered.GetCellData().SetScalars(colors)
    return write_polydata(filtered, output_vtp)


def selected_sheet_vtp(input_vtp: Path, output_vtp: Path, selected_sheet: int, color_rgb: tuple[int, int, int]) -> Path:
    source = read_polydata(input_vtp)
    sheets = sheet_id_array(source)

    selected = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    if source.GetPoints() is not None:
        points.DeepCopy(source.GetPoints())
    selected.SetPoints(points)

    cells = vtk.vtkCellArray()
    colors = vtk.vtkUnsignedCharArray()
    colors.SetName("SheetColor")
    colors.SetNumberOfComponents(3)

    for cell_id in range(source.GetNumberOfCells()):
        if int(sheets.GetTuple1(cell_id)) != int(selected_sheet):
            continue
        cells.InsertNextCell(source.GetCell(cell_id).GetPointIds())
        colors.InsertNextTuple3(*color_rgb)

    selected.SetPolys(cells)
    selected.GetCellData().AddArray(colors)
    selected.GetCellData().SetScalars(colors)
    return write_polydata(selected, output_vtp)


def sheet_boundary_vtp(
    input_vtp: Path,
    output_vtp: Path,
    colors_by_sheet: dict[int, tuple[int, int, int]],
    default_rgb: tuple[int, int, int],
) -> Path:
    source = read_polydata(input_vtp)
    sheets = sheet_id_array(source)
    source_points = source.GetPoints()

    def point_key(point_id: int) -> tuple[float, float, float]:
        point = source_points.GetPoint(point_id)
        return (round(float(point[0]), 10), round(float(point[1]), 10), round(float(point[2]), 10))

    edge_counts: dict[tuple[int, tuple[float, float, float], tuple[float, float, float]], int] = {}
    edge_representatives: dict[tuple[int, tuple[float, float, float], tuple[float, float, float]], tuple[int, int]] = {}
    for cell_id in range(source.GetNumberOfCells()):
        sheet_id = int(sheets.GetTuple1(cell_id))
        point_ids = source.GetCell(cell_id).GetPointIds()
        count = point_ids.GetNumberOfIds()
        if count < 2:
            continue
        for local_id in range(count):
            a = int(point_ids.GetId(local_id))
            b = int(point_ids.GetId((local_id + 1) % count))
            if a == b:
                continue
            a_key = point_key(a)
            b_key = point_key(b)
            if a_key == b_key:
                continue
            edge = (sheet_id, a_key, b_key) if a_key < b_key else (sheet_id, b_key, a_key)
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
            edge_representatives.setdefault(edge, (a, b))

    boundary = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    if source_points is not None:
        points.DeepCopy(source_points)
    boundary.SetPoints(points)

    lines = vtk.vtkCellArray()
    colors = vtk.vtkUnsignedCharArray()
    colors.SetName("SheetColor")
    colors.SetNumberOfComponents(3)

    for (sheet_id, _a_key, _b_key), count in edge_counts.items():
        if count != 1:
            continue
        a, b = edge_representatives[(sheet_id, _a_key, _b_key)]
        lines.InsertNextCell(2)
        lines.InsertCellPoint(a)
        lines.InsertCellPoint(b)
        colors.InsertNextTuple3(*colors_by_sheet.get(sheet_id, default_rgb))

    boundary.SetLines(lines)
    boundary.GetCellData().AddArray(colors)
    boundary.GetCellData().SetScalars(colors)
    return write_polydata(boundary, output_vtp)


def data_bounds_from_vtp(vtp: Path) -> tuple[float, float, float, float]:
    polydata = read_polydata(vtp)
    bounds = polydata.GetBounds()
    if bounds is None:
        return (-0.5, -0.5, 0.5, 0.5)
    xmin, xmax, ymin, ymax = float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])
    if not all(math.isfinite(value) for value in (xmin, xmax, ymin, ymax)):
        return (-0.5, -0.5, 0.5, 0.5)
    return (xmin, ymin, xmax, ymax)


def set_camera(view, bounds: tuple[float, float, float, float], resolution: list[int]) -> None:
    xmin, ymin, xmax, ymax = bounds
    width = max(1e-12, xmax - xmin)
    height = max(1e-12, ymax - ymin)
    cx = (xmin + xmax) * 0.5
    cy = (ymin + ymax) * 0.5
    aspect = float(resolution[0]) / float(resolution[1]) if resolution[1] else 1.0

    view.CameraParallelProjection = 1
    view.CameraFocalPoint = [cx, cy, 0.0]
    view.CameraPosition = [cx, cy, 1.0]
    view.CameraViewUp = [0.0, 1.0, 0.0]
    view.CameraParallelScale = max(height * 0.5, width / (2.0 * aspect))
    view.CenterOfRotation = [cx, cy, 0.0]


def configure_view(view, spec: dict) -> None:
    resolution = [int(spec.get("image_resolution", [1600, 1600])[0]), int(spec.get("image_resolution", [1600, 1600])[1])]
    view.ViewSize = resolution
    view.OrientationAxesVisibility = 0
    background = spec.get("background", [1.0, 1.0, 1.0])
    try:
        view.UseColorPaletteForBackground = 0
    except Exception:
        pass
    view.Background = [float(background[0]), float(background[1]), float(background[2])]


def show_source(
    path: Path,
    view,
    opacity: float,
    color: list[float] | None = None,
    direct_rgb: bool = False,
    representation: str = "Surface",
    line_width: float | None = None,
):
    source = pvs.XMLPolyDataReader(FileName=[str(path)])
    display = pvs.Show(source, view)
    display.Representation = representation
    display.Opacity = float(opacity)
    if line_width is not None:
        try:
            display.LineWidth = float(line_width)
        except Exception:
            pass

    if direct_rgb:
        pvs.ColorBy(display, ("CELLS", "SheetColor"))
        try:
            display.MapScalars = 0
        except Exception:
            pass
        try:
            display.SetScalarBarVisibility(view, False)
        except Exception:
            pass
    else:
        try:
            pvs.ColorBy(display, None)
        except Exception:
            try:
                display.ColorArrayName = [None, ""]
            except Exception:
                pass
        if color is not None:
            display.DiffuseColor = [float(color[0]), float(color[1]), float(color[2])]

    for attr, value in (
        ("Lighting", 0),
        ("Ambient", 1.0),
        ("Diffuse", 0.0),
        ("Specular", 0.0),
    ):
        try:
            setattr(display, attr, value)
        except Exception:
            pass

    return source


def save_current_image(view, output: Path, resolution: list[int]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    pvs.Render(view)
    pvs.SaveScreenshot(str(output), view, ImageResolution=resolution)


def render_overview(view, input_vtp: Path, image: dict, tmp_dir: Path, resolution: list[int]) -> None:
    colors_by_sheet = {
        int(sheet_id): rgb255(rgb)
        for sheet_id, rgb in image.get("colors_by_sheet", {}).items()
    }
    default_rgb = rgb255(image.get("default_color", [0.85, 0.85, 0.85]))
    base_sheet_ids = {int(sheet_id) for sheet_id in image.get("base_sheet_ids", [])}
    sources = []

    if base_sheet_ids:
        base_vtp = filtered_sheets_vtp(
            input_vtp,
            tmp_dir / "overview_base.vtp",
            colors_by_sheet=colors_by_sheet,
            default_rgb=default_rgb,
            include_sheet_ids=base_sheet_ids,
        )
        sources.append(show_source(base_vtp, view, image.get("base_opacity", 0.22), direct_rgb=True))
        rest_vtp = filtered_sheets_vtp(
            input_vtp,
            tmp_dir / "overview_rest.vtp",
            colors_by_sheet=colors_by_sheet,
            default_rgb=default_rgb,
            exclude_sheet_ids=base_sheet_ids,
        )
        sources.append(show_source(rest_vtp, view, image.get("opacity", 0.78), direct_rgb=True))
    else:
        styled = styled_all_cells_vtp(
            input_vtp,
            tmp_dir / "overview.vtp",
            colors_by_sheet=colors_by_sheet,
            default_rgb=default_rgb,
        )
        sources.append(show_source(styled, view, image.get("opacity", 0.58), direct_rgb=True))

    save_current_image(view, Path(image["output"]), resolution)
    for source in reversed(sources):
        pvs.Delete(source)


def render_selected(view, input_vtp: Path, image: dict, tmp_dir: Path, resolution: list[int]) -> None:
    selected_sheet = int(image["selected_sheet"])
    context_colors = {
        int(sheet_id): rgb255(rgb)
        for sheet_id, rgb in image.get("context_colors_by_sheet", {}).items()
    }
    boundary_colors = {
        int(sheet_id): rgb255(rgb)
        for sheet_id, rgb in image.get("boundary_colors_by_sheet", {}).items()
    }

    context_vtp = tmp_dir / "context_colored.vtp"
    if not context_vtp.exists():
        styled_all_cells_vtp(
            input_vtp,
            context_vtp,
            colors_by_sheet=context_colors,
            default_rgb=rgb255(image.get("context_default_color", [0.88, 0.88, 0.86])),
        )
    context = show_source(
        context_vtp,
        view,
        image.get("context_opacity", 0.10),
        direct_rgb=True,
    )

    boundary_vtp = tmp_dir / "context_boundaries.vtp"
    if not boundary_vtp.exists():
        sheet_boundary_vtp(
            input_vtp,
            boundary_vtp,
            colors_by_sheet=boundary_colors,
            default_rgb=rgb255(image.get("boundary_default_color", [0.36, 0.36, 0.34])),
        )
    boundary = show_source(
        boundary_vtp,
        view,
        image.get("boundary_opacity", 0.42),
        direct_rgb=True,
        representation="Surface",
        line_width=float(image.get("boundary_width", 1.25)),
    )

    selected_vtp = selected_sheet_vtp(
        input_vtp,
        tmp_dir / f"sheet_{selected_sheet}.vtp",
        selected_sheet,
        rgb255(image.get("selected_color", [0.20, 0.60, 0.90])),
    )
    selected = show_source(
        selected_vtp,
        view,
        image.get("selected_opacity", 0.88),
        direct_rgb=True,
    )
    save_current_image(view, Path(image["output"]), resolution)
    pvs.Delete(selected)
    pvs.Delete(boundary)
    pvs.Delete(context)


def render_spec(spec: dict) -> None:
    input_vtp = Path(spec["vtp"])
    resolution = [int(spec.get("image_resolution", [1600, 1600])[0]), int(spec.get("image_resolution", [1600, 1600])[1])]
    bounds = spec.get("bounds")
    if bounds is None:
        bounds_tuple = data_bounds_from_vtp(input_vtp)
    else:
        bounds_tuple = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))

    disable_reset = getattr(pvs, "DisableFirstRenderCameraReset", None) or getattr(pvs, "_DisableFirstRenderCameraReset", None)
    if disable_reset is not None:
        disable_reset()

    view = pvs.CreateView("RenderView")
    pvs.SetActiveView(view)
    configure_view(view, spec)
    set_camera(view, bounds_tuple, resolution)

    with tempfile.TemporaryDirectory(prefix="sheet_surface_render_") as tmp_name:
        tmp_dir = Path(tmp_name)
        for image in spec.get("images", []):
            mode = image.get("mode")
            set_camera(view, bounds_tuple, resolution)
            if mode == "overview":
                render_overview(view, input_vtp, image, tmp_dir, resolution)
            elif mode == "selected":
                render_selected(view, input_vtp, image, tmp_dir, resolution)
            else:
                raise RuntimeError(f"Unsupported sheet render mode: {mode}")

    pvs.Delete(view)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    render_spec(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
