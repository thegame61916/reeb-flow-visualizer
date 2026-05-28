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
    FIBER_SURFACE_DIR,
    FIBER_SURFACE_FAILED_LOG_FILE,
    FIBER_SURFACE_FIELD_F_ISOVALUE,
    FIBER_SURFACE_FIELD_G_ISOVALUE,
    FIBER_SURFACE_REBUILD,
    FIBER_SURFACE_TEMP_DIR,
    FIBER_SURFACE_TOP_N_SHEETS,
    FIBER_SURFACE_WORKERS,
    FV99,
    FV99_FNAME,
    FV99_GNAME,
    FV99_OMP_THREADS,
    RSI_DIR,
    RS_DIR,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTU_DIR,
    VTK_LIB_DIR,
)
from stage_02_build_sankey_data import read_rsi


@dataclass(frozen=True)
class LabeledSurfaceOutput:
    field: str
    sign: str
    value: float
    path: Path


@dataclass(frozen=True)
class ThresholdedSurface:
    filename: str
    field: str
    sign: str
    sheet_id: int
    cell_count: int


@dataclass(frozen=True)
class TimestepResult:
    vtu: Path
    status: str
    output_count: int = 0
    message: str = ""


def make_fv99_environment() -> dict[str, str]:
    env = os.environ.copy()
    library_paths = [
        str(TTK_BUILD_LIB_DIR),
        str(TTK_INSTALL_LIB_DIR),
        str(VTK_LIB_DIR),
    ]

    if env.get("LD_LIBRARY_PATH"):
        library_paths.append(env["LD_LIBRARY_PATH"])

    env["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)
    env["OMP_NUM_THREADS"] = str(FV99_OMP_THREADS)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


FV99_ENV = make_fv99_environment()


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
    return step_dir / "fiber_surfaces_manifest.json"


def expected_manifest_payload(vtu_file: Path) -> dict:
    return {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "rs": str(RS_DIR / f"{vtu_file.stem}.rs"),
        "rsi": str(RSI_DIR / f"{vtu_file.stem}.rsi"),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "f_isovalue": float(FIBER_SURFACE_FIELD_F_ISOVALUE),
        "g_isovalue": float(FIBER_SURFACE_FIELD_G_ISOVALUE),
        "top_n_sheets": int(FIBER_SURFACE_TOP_N_SHEETS),
        "surface_mode": "thresholded_from_full_labeled_fiber_surface",
        "threshold_cell_array": "sheetId",
    }


def existing_outputs_complete(vtu_file: Path) -> bool:
    step_dir = FIBER_SURFACE_DIR / vtu_file.stem
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

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return False

    return all((step_dir / str(item.get("filename", ""))).exists() for item in outputs)


def check_inputs() -> None:
    if not FV99.exists():
        raise FileNotFoundError(f"fv99 binary not found: {FV99}")

    if not os.access(FV99, os.X_OK):
        raise PermissionError(f"fv99 binary is not executable: {FV99}")

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


def run_fv99_for_sign(
    vtu_file: Path,
    rs_file: Path,
    sign: str,
    f_value: float,
    g_value: float,
    work_dir: Path,
    log_file: Path,
) -> tuple[int, list[LabeledSurfaceOutput]]:
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(FV99),
        "-f",
        str(vtu_file),
        "-l",
        str(rs_file),
        "--fieldFValueFS",
        value_text(f_value),
        "--fieldGValueFS",
        value_text(g_value),
        "--fName",
        FV99_FNAME,
        "--gName",
        FV99_GNAME,
        "--headless",
    ]

    with log_file.open("w") as log:
        result = subprocess.run(
            command,
            cwd=work_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=FV99_ENV,
        )

    outputs = [
        LabeledSurfaceOutput(
            field="f",
            sign=sign,
            value=f_value,
            path=output_dir / "labeled.fs.f.vtp",
        ),
        LabeledSurfaceOutput(
            field="g",
            sign=sign,
            value=g_value,
            path=output_dir / "labeled.fs.g.vtp",
        ),
    ]

    return result.returncode, outputs


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
        raise RuntimeError(f"failed to write thresholded fiber surface: {destination}")

    return int(output.GetNumberOfCells())


def threshold_labeled_surfaces(
    labeled_surfaces: list[LabeledSurfaceOutput],
    top_sheet_ids: list[int],
    step_dir: Path,
) -> list[ThresholdedSurface]:
    step_dir.mkdir(parents=True, exist_ok=True)
    records: list[ThresholdedSurface] = []

    for labeled_surface in labeled_surfaces:
        if not labeled_surface.path.exists():
            raise FileNotFoundError(f"missing labeled fiber surface: {labeled_surface.path}")

        token = value_token(labeled_surface.value)
        for sheet_id in top_sheet_ids:
            filename = (
                f"sheet_{sheet_id}_{labeled_surface.field}_"
                f"{labeled_surface.sign}_{token}.vtp"
            )
            destination = step_dir / filename
            cell_count = threshold_sheet_surface(
                labeled_surface.path,
                destination,
                sheet_id,
            )
            records.append(
                ThresholdedSurface(
                    filename=filename,
                    field=labeled_surface.field,
                    sign=labeled_surface.sign,
                    sheet_id=sheet_id,
                    cell_count=cell_count,
                )
            )

    return records


def write_manifest(
    vtu_file: Path,
    top_sheet_ids: list[int],
    outputs: list[ThresholdedSurface],
) -> None:
    step_dir = FIBER_SURFACE_DIR / vtu_file.stem
    payload = expected_manifest_payload(vtu_file)
    payload["top_sheet_ids"] = top_sheet_ids
    payload["outputs"] = [
        {
            "filename": output.filename,
            "field": output.field,
            "sign": output.sign,
            "sheet_id": output.sheet_id,
            "cell_count": output.cell_count,
        }
        for output in outputs
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

    step_dir = FIBER_SURFACE_DIR / vtu_file.stem
    log_dir = FIBER_SURFACE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    all_labeled_surfaces: list[LabeledSurfaceOutput] = []

    with tempfile.TemporaryDirectory(
        prefix=f"{vtu_file.stem}_",
        dir=FIBER_SURFACE_TEMP_DIR,
    ) as tmp_name:
        tmp_dir = Path(tmp_name)
        runs = [
            (
                "pos",
                float(FIBER_SURFACE_FIELD_F_ISOVALUE),
                float(FIBER_SURFACE_FIELD_G_ISOVALUE),
            ),
            (
                "neg",
                -float(FIBER_SURFACE_FIELD_F_ISOVALUE),
                -float(FIBER_SURFACE_FIELD_G_ISOVALUE),
            ),
        ]

        for sign, f_value, g_value in runs:
            run_dir = tmp_dir / sign
            run_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{vtu_file.stem}_{sign}.log"
            returncode, labeled_surfaces = run_fv99_for_sign(
                vtu_file,
                rs_file,
                sign,
                f_value,
                g_value,
                run_dir,
                log_file,
            )

            if returncode != 0:
                return TimestepResult(
                    vtu=vtu_file,
                    status="failed",
                    message=f"{sign} returncode={returncode} log={log_file}",
                )

            missing_outputs = [
                str(surface.path)
                for surface in labeled_surfaces
                if not surface.path.exists()
            ]
            if missing_outputs:
                return TimestepResult(
                    vtu=vtu_file,
                    status="failed",
                    message=f"{sign} missing labeled outputs: {', '.join(missing_outputs)} log={log_file}",
                )

            all_labeled_surfaces.extend(labeled_surfaces)

        try:
            outputs = threshold_labeled_surfaces(
                all_labeled_surfaces,
                top_sheet_ids,
                step_dir,
            )
        except Exception as exc:
            return TimestepResult(
                vtu=vtu_file,
                status="failed",
                message=f"threshold failed: {exc}",
            )

    write_manifest(vtu_file, top_sheet_ids, outputs)
    return TimestepResult(
        vtu=vtu_file,
        status="done",
        output_count=len(outputs),
    )


def compute_sheet_fiber_surfaces_stage(
    selected_stems: set[str] | None = None,
    workers: int | None = None,
    rebuild: bool | None = None,
) -> None:
    check_inputs()

    FIBER_SURFACE_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    effective_workers = max(1, int(workers if workers is not None else FIBER_SURFACE_WORKERS))
    effective_rebuild = FIBER_SURFACE_REBUILD if rebuild is None else rebuild
    vtu_files = discover_timesteps(selected_stems)

    print(
        f"Computing fiber surfaces for {len(vtu_files)} timesteps "
        f"using {effective_workers} worker(s)"
    )
    print(f"Output directory: {FIBER_SURFACE_DIR}")
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
                status = f"done outputs={result.output_count}"
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
            "Compute top-sheet fiber-surface VTPs for +/-f and +/-g isovalues."
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
        help=f"Parallel fv99 jobs. Defaults to common.py value {FIBER_SURFACE_WORKERS}.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Recompute even if a matching fiber-surface manifest already exists.",
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
