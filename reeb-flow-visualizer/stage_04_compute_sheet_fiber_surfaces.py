#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import vtk

from common import (
    FIBER_SURFACE_FAILED_LOG_FILE,
    FIBER_SURFACE_FIELD_F_ISOVALUE,
    FIBER_SURFACE_FIELD_G_ISOVALUE,
    FIBER_SURFACE_IMAGE_DIR,
    FIBER_SURFACE_LABELED_DIR,
    FIBER_SURFACE_MOLECULAR_STRUCTURE_DIR,
    FIBER_SURFACE_REBUILD,
    FIBER_SURFACE_RENDER_IMAGE_RESOLUTION,
    FIBER_SURFACE_RENDER_RETRIES,
    FIBER_SURFACE_RENDER_STATE_FILE,
    FIBER_SURFACE_RENDER_TIMEOUT_SECONDS,
    FIBER_SURFACE_TEMP_DIR,
    FIBER_SURFACE_TOP_N_SHEETS,
    FIBER_SURFACE_WORKERS,
    FV99_FNAME,
    FV99_GNAME,
    PVPYTHON,
    RSI_DIR,
    RS_DIR,
    VTU_DIR,
)
from stage_02_build_sankey_data import read_rsi

SCRIPT_DIR = Path(__file__).resolve().parent
PARAVIEW_RENDER_HELPER = SCRIPT_DIR / "render_fiber_surface_state.py"
SURFACE_ROLES = ("f_neg", "f_pos", "g_neg", "g_pos")


@dataclass(frozen=True)
class LabeledSurfaceOutput:
    field: str
    sign: str
    value: float
    path: Path

    @property
    def role(self) -> str:
        return f"{self.field}_{self.sign}"


@dataclass(frozen=True)
class ThresholdedSurface:
    filename: str
    path: Path
    field: str
    sign: str
    sheet_id: int
    cell_count: int

    @property
    def role(self) -> str:
        return f"{self.field}_{self.sign}"


@dataclass(frozen=True)
class RenderedSheetImage:
    filename: str
    sheet_id: int
    surfaces: list[ThresholdedSurface]


@dataclass(frozen=True)
class TimestepResult:
    vtu: Path
    status: str
    image_count: int = 0
    message: str = ""


def make_render_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


RENDER_ENV = make_render_environment()


def value_text(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def value_token(value: float) -> str:
    text = value_text(abs(value))
    return text.replace(".", "p")


def normalize_stem(value: str) -> str:
    return Path(value).stem


def manifest_path(step_dir: Path) -> Path:
    return step_dir / "fiber_surface_images_manifest.json"


def expected_manifest_payload(vtu_file: Path) -> dict:
    return {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "molecule_vtp": str(molecule_vtp_path(vtu_file)),
        "rs": str(RS_DIR / f"{vtu_file.stem}.rs"),
        "rsi": str(RSI_DIR / f"{vtu_file.stem}.rsi"),
        "state_file": str(FIBER_SURFACE_RENDER_STATE_FILE),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "f_isovalue": float(FIBER_SURFACE_FIELD_F_ISOVALUE),
        "g_isovalue": float(FIBER_SURFACE_FIELD_G_ISOVALUE),
        "top_n_sheets": int(FIBER_SURFACE_TOP_N_SHEETS),
        "output_mode": "rendered_images_from_paraview_state",
        "surface_mode": "temporary_thresholded_from_stage1_labeled_fiber_surface",
        "threshold_cell_array": "sheetId",
        "image_resolution": list(FIBER_SURFACE_RENDER_IMAGE_RESOLUTION),
    }


def existing_outputs_complete(vtu_file: Path) -> bool:
    step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    path = manifest_path(step_dir)
    if not path.exists():
        return False

    try:
        manifest = json.loads(path.read_text())
    except Exception:
        return False

    expected = expected_manifest_payload(vtu_file)
    for key, value in expected.items():
        if manifest.get(key) != value:
            return False

    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        return False

    return all((step_dir / str(item.get("filename", ""))).exists() for item in images)


def check_inputs() -> None:
    if not PVPYTHON.exists():
        raise FileNotFoundError(f"pvpython binary not found: {PVPYTHON}")

    if not os.access(PVPYTHON, os.X_OK):
        raise PermissionError(f"pvpython binary is not executable: {PVPYTHON}")

    if not FIBER_SURFACE_RENDER_STATE_FILE.exists():
        raise FileNotFoundError(f"ParaView state file not found: {FIBER_SURFACE_RENDER_STATE_FILE}")

    if not PARAVIEW_RENDER_HELPER.exists():
        raise FileNotFoundError(f"ParaView render helper not found: {PARAVIEW_RENDER_HELPER}")

    if not VTU_DIR.exists():
        raise FileNotFoundError(f"VTU directory not found: {VTU_DIR}")

    if not RS_DIR.exists():
        raise FileNotFoundError(f"Reeb-space directory not found: {RS_DIR}")

    if not RSI_DIR.exists():
        raise FileNotFoundError(f"RSI directory not found: {RSI_DIR}")


def discover_timesteps(selected_stems: set[str] | None = None) -> list[Path]:
    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    if not selected_stems:
        return vtu_files

    return [
        vtu_file
        for vtu_file in vtu_files
        if vtu_file.stem in selected_stems
    ]


def molecule_vtp_path(vtu_file: Path) -> Path:
    return FIBER_SURFACE_MOLECULAR_STRUCTURE_DIR / f"{vtu_file.stem}.vtp"


def write_empty_molecule_vtp(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    poly_data = vtk.vtkPolyData()
    poly_data.SetPoints(vtk.vtkPoints())

    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(destination))
    writer.SetInputData(poly_data)
    writer.SetDataModeToBinary()
    if writer.Write() != 1:
        raise RuntimeError(f"failed to write empty molecular structure placeholder: {destination}")
    return destination


def render_molecule_vtp_path(vtu_file: Path, work_dir: Path) -> Path:
    molecule_file = molecule_vtp_path(vtu_file)
    if molecule_file.exists():
        return molecule_file

    return write_empty_molecule_vtp(
        work_dir / "molecularStructure" / f"{vtu_file.stem}.vtp"
    )


def read_top_sheet_ids(rsi_file: Path) -> list[int]:
    rsi_data = read_rsi(rsi_file)
    finite_areas = [
        (sheet_id, area)
        for sheet_id, area in rsi_data["sheet_area"].items()
        if math.isfinite(area)
    ]
    finite_areas.sort(key=lambda item: item[1], reverse=True)
    return [
        int(sheet_id)
        for sheet_id, _area in finite_areas[: int(FIBER_SURFACE_TOP_N_SHEETS)]
    ]


def cached_labeled_surface_path(vtu_file: Path, role: str) -> Path:
    return FIBER_SURFACE_LABELED_DIR / vtu_file.stem / f"{role}.vtp"


def cached_labeled_manifest_path(vtu_file: Path) -> Path:
    return FIBER_SURFACE_LABELED_DIR / vtu_file.stem / "labeled_fiber_surfaces_manifest.json"


def validate_cached_labeled_manifest(vtu_file: Path) -> str | None:
    path = cached_labeled_manifest_path(vtu_file)
    if not path.exists():
        return f"missing Stage 1 labeled fiber surface manifest: {path}"

    try:
        manifest = json.loads(path.read_text())
    except Exception as exc:
        return f"failed to read Stage 1 labeled fiber surface manifest {path}: {exc}"

    expected = {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "rs": str(RS_DIR / f"{vtu_file.stem}.rs"),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "f_isovalue": float(FIBER_SURFACE_FIELD_F_ISOVALUE),
        "g_isovalue": float(FIBER_SURFACE_FIELD_G_ISOVALUE),
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            return (
                f"Stage 1 labeled fiber surface manifest mismatch for {vtu_file.stem}: "
                f"{key}={manifest.get(key)!r}, expected {value!r}. Rerun Stage 1."
            )

    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, dict):
        return f"Stage 1 labeled fiber surface manifest has no surfaces map: {path}"

    for role in SURFACE_ROLES:
        expected_path = cached_labeled_surface_path(vtu_file, role)
        if surfaces.get(role) != str(expected_path):
            return (
                f"Stage 1 labeled fiber surface manifest path mismatch for {role}: "
                f"{surfaces.get(role)!r}, expected {str(expected_path)!r}. Rerun Stage 1."
            )

    return None


def cached_labeled_surfaces(vtu_file: Path) -> list[LabeledSurfaceOutput]:
    f_value = float(FIBER_SURFACE_FIELD_F_ISOVALUE)
    g_value = float(FIBER_SURFACE_FIELD_G_ISOVALUE)
    return [
        LabeledSurfaceOutput(
            field="f",
            sign="neg",
            value=-f_value,
            path=cached_labeled_surface_path(vtu_file, "f_neg"),
        ),
        LabeledSurfaceOutput(
            field="f",
            sign="pos",
            value=f_value,
            path=cached_labeled_surface_path(vtu_file, "f_pos"),
        ),
        LabeledSurfaceOutput(
            field="g",
            sign="neg",
            value=-g_value,
            path=cached_labeled_surface_path(vtu_file, "g_neg"),
        ),
        LabeledSurfaceOutput(
            field="g",
            sign="pos",
            value=g_value,
            path=cached_labeled_surface_path(vtu_file, "g_pos"),
        ),
    ]


def cell_data_array_name(poly_data: vtk.vtkPolyData) -> str:
    cell_data = poly_data.GetCellData()
    for name in ("sheetId", "SheetId", "SheetID"):
        if cell_data.GetArray(name) is not None:
            return name

    names = [
        cell_data.GetArrayName(index)
        for index in range(cell_data.GetNumberOfArrays())
    ]
    raise ValueError(f"fiber surface VTP has no sheetId cell-data array; arrays={names}")


def read_poly_data(path: Path) -> vtk.vtkPolyData:
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()

    poly_data = reader.GetOutput()
    if poly_data is None:
        raise ValueError(f"failed to read fiber surface VTP: {path}")
    return poly_data


def threshold_sheet_surface(
    source: Path,
    destination: Path,
    sheet_id: int,
) -> int:
    poly_data = read_poly_data(source)
    array_name = cell_data_array_name(poly_data)

    threshold = vtk.vtkThreshold()
    threshold.SetInputData(poly_data)
    threshold.SetInputArrayToProcess(
        0,
        0,
        0,
        vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS,
        array_name,
    )
    threshold.SetLowerThreshold(float(sheet_id) - 0.5)
    threshold.SetUpperThreshold(float(sheet_id) + 0.5)
    threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
    threshold.Update()

    geometry = vtk.vtkGeometryFilter()
    geometry.SetInputConnection(threshold.GetOutputPort())
    geometry.Update()

    output = vtk.vtkPolyData()
    output.DeepCopy(geometry.GetOutput())

    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(destination))
    writer.SetInputData(output)
    writer.SetDataModeToBinary()
    if writer.Write() != 1:
        raise RuntimeError(f"failed to write temporary thresholded fiber surface: {destination}")

    return int(output.GetNumberOfCells())


def threshold_labeled_surfaces(
    labeled_surfaces: list[LabeledSurfaceOutput],
    top_sheet_ids: list[int],
    temp_step_dir: Path,
) -> dict[int, dict[str, ThresholdedSurface]]:
    temp_step_dir.mkdir(parents=True, exist_ok=True)
    by_sheet: dict[int, dict[str, ThresholdedSurface]] = {
        sheet_id: {}
        for sheet_id in top_sheet_ids
    }

    for labeled_surface in labeled_surfaces:
        if not labeled_surface.path.exists():
            raise FileNotFoundError(f"missing labeled fiber surface: {labeled_surface.path}")

        token = value_token(labeled_surface.value)
        for sheet_id in top_sheet_ids:
            filename = (
                f"sheet_{sheet_id}_{labeled_surface.field}_"
                f"{labeled_surface.sign}_{token}.vtp"
            )
            destination = temp_step_dir / filename
            cell_count = threshold_sheet_surface(
                labeled_surface.path,
                destination,
                sheet_id,
            )
            by_sheet[sheet_id][labeled_surface.role] = ThresholdedSurface(
                filename=filename,
                path=destination,
                field=labeled_surface.field,
                sign=labeled_surface.sign,
                sheet_id=sheet_id,
                cell_count=cell_count,
            )

    return by_sheet


def render_sheet_images(
    vtu_file: Path,
    molecule_file: Path,
    surfaces_by_sheet: dict[int, dict[str, ThresholdedSurface]],
    image_step_dir: Path,
    render_work_dir: Path,
    render_log_file: Path,
) -> list[RenderedSheetImage]:
    image_step_dir.mkdir(parents=True, exist_ok=True)
    render_work_dir.mkdir(parents=True, exist_ok=True)

    images: list[RenderedSheetImage] = []
    image_specs = []

    for sheet_id, surfaces_by_role in surfaces_by_sheet.items():
        missing_roles = [role for role in SURFACE_ROLES if role not in surfaces_by_role]
        if missing_roles:
            raise RuntimeError(f"sheet {sheet_id} is missing surface role(s): {missing_roles}")

        filename = f"sheet_{sheet_id}.png"
        output = image_step_dir / filename
        surfaces = [surfaces_by_role[role] for role in SURFACE_ROLES]
        images.append(
            RenderedSheetImage(
                filename=filename,
                sheet_id=sheet_id,
                surfaces=surfaces,
            )
        )
        image_specs.append(
            {
                "sheet_id": sheet_id,
                "output": str(output),
                "fiber_surfaces": {
                    role: str(surfaces_by_role[role].path)
                    for role in SURFACE_ROLES
                },
            }
        )

    for image in images:
        (image_step_dir / image.filename).unlink(missing_ok=True)

    base_spec = {
        "state_file": str(FIBER_SURFACE_RENDER_STATE_FILE),
        "vtu": str(vtu_file),
        "molecule_vtp": str(molecule_file),
        "image_resolution": list(FIBER_SURFACE_RENDER_IMAGE_RESOLUTION),
    }

    pending_specs = list(image_specs)
    max_attempts = max(1, 1 + int(FIBER_SURFACE_RENDER_RETRIES))
    last_returncode = 0
    last_log_file = render_log_file

    for attempt in range(1, max_attempts + 1):
        spec = dict(base_spec)
        spec["images"] = pending_specs

        spec_file = render_work_dir / f"render_spec_attempt_{attempt}.json"
        spec_file.write_text(json.dumps(spec, indent=2) + "\n")

        attempt_log_file = (
            render_log_file
            if attempt == 1
            else render_log_file.with_name(
                f"{render_log_file.stem}.attempt_{attempt}{render_log_file.suffix}"
            )
        )
        command = [
            str(PVPYTHON),
            "--force-offscreen-rendering",
            str(PARAVIEW_RENDER_HELPER),
            "--spec",
            str(spec_file),
        ]
        with attempt_log_file.open("w") as log:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=RENDER_ENV,
                timeout=int(FIBER_SURFACE_RENDER_TIMEOUT_SECONDS),
            )

        last_returncode = result.returncode
        last_log_file = attempt_log_file
        pending_specs = [
            spec
            for spec in image_specs
            if not Path(spec["output"]).exists()
        ]

        if not pending_specs:
            if result.returncode != 0:
                with attempt_log_file.open("a") as log:
                    log.write(
                        "\nAccepted nonzero pvpython returncode "
                        f"{result.returncode} because all expected screenshots exist.\n"
                    )
            break

        with attempt_log_file.open("a") as log:
            log.write(
                "\nMissing screenshots after attempt "
                f"{attempt}/{max_attempts}: "
                f"{[Path(spec['output']).name for spec in pending_specs]}\n"
            )
    else:
        pass

    missing_outputs = [Path(spec["output"]).name for spec in pending_specs]
    if missing_outputs:
        raise RuntimeError(
            f"pvpython returncode={last_returncode}; did not write image(s) "
            f"{missing_outputs} after {max_attempts} attempt(s); log={last_log_file}"
        )

    return images


def write_manifest(
    vtu_file: Path,
    top_sheet_ids: list[int],
    images: list[RenderedSheetImage],
) -> None:
    step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    payload = expected_manifest_payload(vtu_file)
    payload["top_sheet_ids"] = top_sheet_ids
    payload["images"] = [
        {
            "filename": image.filename,
            "sheet_id": image.sheet_id,
            "surfaces": [
                {
                    "field": surface.field,
                    "sign": surface.sign,
                    "cell_count": surface.cell_count,
                    "temporary_filename": surface.filename,
                }
                for surface in image.surfaces
            ],
        }
        for image in images
    ]
    manifest_path(step_dir).write_text(json.dumps(payload, indent=2) + "\n")


def compute_timestep(vtu_file: Path, rebuild: bool) -> TimestepResult:
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    if not rs_file.exists():
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"missing rs file: {rs_file}",
        )

    rsi_file = RSI_DIR / f"{vtu_file.stem}.rsi"
    if not rsi_file.exists():
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"missing rsi file: {rsi_file}",
        )

    if not rebuild and existing_outputs_complete(vtu_file):
        return TimestepResult(vtu=vtu_file, status="skipped_existing")

    try:
        top_sheet_ids = read_top_sheet_ids(rsi_file)
    except Exception as exc:
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"failed to read top sheets from {rsi_file}: {exc}",
        )

    if not top_sheet_ids:
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"no finite top sheets in {rsi_file}",
        )

    image_step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    log_dir = FIBER_SURFACE_IMAGE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    all_labeled_surfaces = cached_labeled_surfaces(vtu_file)
    missing_outputs = [
        str(surface.path)
        for surface in all_labeled_surfaces
        if not surface.path.exists()
    ]
    if missing_outputs:
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=(
                "missing Stage 1 labeled fiber surface(s): "
                + ", ".join(missing_outputs)
            ),
        )

    manifest_error = validate_cached_labeled_manifest(vtu_file)
    if manifest_error:
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=manifest_error,
        )

    with tempfile.TemporaryDirectory(
        prefix=f"{vtu_file.stem}_",
        dir=FIBER_SURFACE_TEMP_DIR,
    ) as tmp_name:
        tmp_dir = Path(tmp_name)

        try:
            temp_surfaces_dir = tmp_dir / "thresholded"
            surfaces_by_sheet = threshold_labeled_surfaces(
                all_labeled_surfaces,
                top_sheet_ids,
                temp_surfaces_dir,
            )
            render_log_file = log_dir / f"{vtu_file.stem}.render.log"
            render_work_dir = tmp_dir / "render"
            molecule_file = render_molecule_vtp_path(vtu_file, render_work_dir)
            images = render_sheet_images(
                vtu_file,
                molecule_file,
                surfaces_by_sheet,
                image_step_dir,
                render_work_dir,
                render_log_file,
            )
        except subprocess.TimeoutExpired as exc:
            return TimestepResult(
                vtu=vtu_file,
                status="failed",
                message=f"render timeout after {exc.timeout}s",
            )
        except Exception as exc:
            return TimestepResult(
                vtu=vtu_file,
                status="failed",
                message=f"render failed: {exc}",
            )

    write_manifest(vtu_file, top_sheet_ids, images)
    return TimestepResult(
        vtu=vtu_file,
        status="done",
        image_count=len(images),
    )


def compute_sheet_fiber_surfaces_stage(
    selected_stems: set[str] | None = None,
    workers: int | None = None,
    rebuild: bool | None = None,
) -> None:
    check_inputs()

    FIBER_SURFACE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    effective_workers = max(1, int(workers if workers is not None else FIBER_SURFACE_WORKERS))
    effective_rebuild = FIBER_SURFACE_REBUILD if rebuild is None else rebuild
    vtu_files = discover_timesteps(selected_stems)

    print(
        f"Rendering fiber-surface images for {len(vtu_files)} timesteps "
        f"using {effective_workers} worker(s)"
    )
    print(f"Image directory: {FIBER_SURFACE_IMAGE_DIR}")
    print(f"ParaView state: {FIBER_SURFACE_RENDER_STATE_FILE}")
    print(
        "Isovalues: "
        f"{FV99_FNAME}=+/-{value_text(FIBER_SURFACE_FIELD_F_ISOVALUE)}, "
        f"{FV99_GNAME}=+/-{value_text(FIBER_SURFACE_FIELD_G_ISOVALUE)}"
    )
    print(f"Sheet filter: top {FIBER_SURFACE_TOP_N_SHEETS} sheets by RSI area")

    failed_lines: list[str] = []

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [
            pool.submit(compute_timestep, vtu_file, effective_rebuild)
            for vtu_file in vtu_files
        ]

        for count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result.status == "done":
                status = f"done images={result.image_count}"
            elif result.status == "skipped_existing":
                status = "skipped existing"
            else:
                status = f"failed {result.message}"
                failed_lines.append(f"{result.vtu}\t{result.message}")

            print(f"[{count}/{len(vtu_files)}] {status}: {result.vtu.name}", flush=True)

    FIBER_SURFACE_FAILED_LOG_FILE.write_text("\n".join(failed_lines))
    print(f"Failed files: {len(failed_lines)}")
    print(f"Failure log: {FIBER_SURFACE_FAILED_LOG_FILE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render top-sheet fiber-surface images for +/-f and +/-g isovalues."
        )
    )
    parser.add_argument(
        "--timesteps",
        nargs="*",
        help="Optional timestep stems or filenames, e.g. step_01268 step_01280.vtu.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help=f"Parallel fv99/render jobs. Defaults to common.py value {FIBER_SURFACE_WORKERS}.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Re-render even if a matching image manifest already exists.",
    )
    args = parser.parse_args(argv)

    selected_stems = None
    if args.timesteps:
        selected_stems = {normalize_stem(value) for value in args.timesteps}

    compute_sheet_fiber_surfaces_stage(
        selected_stems=selected_stems,
        workers=args.workers,
        rebuild=True if args.rebuild else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
