#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

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
    RS_DIR,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTU_DIR,
    VTK_LIB_DIR,
)


@dataclass(frozen=True)
class SurfaceRecord:
    source: Path
    destination_name: str
    field: str
    sign: str
    sheet_id: str


@dataclass(frozen=True)
class TimestepResult:
    vtu: Path
    status: str
    moved_count: int = 0
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
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "f_isovalue": float(FIBER_SURFACE_FIELD_F_ISOVALUE),
        "g_isovalue": float(FIBER_SURFACE_FIELD_G_ISOVALUE),
        "top_n_sheets": int(FIBER_SURFACE_TOP_N_SHEETS),
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

    return all((step_dir / str(name)).exists() for name in outputs)


def check_inputs() -> None:
    if not FV99.exists():
        raise FileNotFoundError(f"fv99 binary not found: {FV99}")

    if not os.access(FV99, os.X_OK):
        raise PermissionError(f"fv99 binary is not executable: {FV99}")

    if not VTU_DIR.exists():
        raise FileNotFoundError(f"VTU directory not found: {VTU_DIR}")

    if not RS_DIR.exists():
        raise FileNotFoundError(f"Reeb-space directory not found: {RS_DIR}")


def discover_timesteps(selected_stems: set[str] | None = None) -> list[Path]:
    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    if not selected_stems:
        return vtu_files

    return [
        vtu_file
        for vtu_file in vtu_files
        if vtu_file.stem in selected_stems
    ]


def run_fv99_for_sign(
    vtu_file: Path,
    rs_file: Path,
    sign: str,
    f_value: float,
    g_value: float,
    work_dir: Path,
    log_file: Path,
) -> tuple[int, list[SurfaceRecord]]:
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
        "--sheetsToProcess",
        str(int(FIBER_SURFACE_TOP_N_SHEETS)),
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

    records: list[SurfaceRecord] = []
    tokens = {
        "f": value_token(f_value),
        "g": value_token(g_value),
    }

    for field in ("f", "g"):
        for source in sorted(output_dir.glob(f"fs.{field}.*.vtp")):
            parts = source.name.split(".")
            if len(parts) < 4:
                continue
            sheet_id = parts[2]
            records.append(
                SurfaceRecord(
                    source=source,
                    destination_name=f"sheet_{sheet_id}_{field}_{sign}_{tokens[field]}.vtp",
                    field=field,
                    sign=sign,
                    sheet_id=sheet_id,
                )
            )

    return result.returncode, records


def move_records(records: list[SurfaceRecord], step_dir: Path, rebuild: bool) -> list[str]:
    moved: list[str] = []
    step_dir.mkdir(parents=True, exist_ok=True)

    for record in records:
        destination = step_dir / record.destination_name
        if destination.exists():
            if not rebuild:
                moved.append(record.destination_name)
                continue
            destination.unlink()

        shutil.move(str(record.source), str(destination))
        moved.append(record.destination_name)

    return moved


def write_manifest(vtu_file: Path, output_names: list[str]) -> None:
    step_dir = FIBER_SURFACE_DIR / vtu_file.stem
    payload = expected_manifest_payload(vtu_file)
    payload["outputs"] = sorted(output_names)
    manifest_path(step_dir).write_text(json.dumps(payload, indent=2) + "\n")


def compute_timestep(vtu_file: Path, rebuild: bool) -> TimestepResult:
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    if not rs_file.exists():
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"missing rs file: {rs_file}",
        )

    if not rebuild and existing_outputs_complete(vtu_file):
        return TimestepResult(vtu=vtu_file, status="skipped_existing")

    step_dir = FIBER_SURFACE_DIR / vtu_file.stem
    log_dir = FIBER_SURFACE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    all_records: list[SurfaceRecord] = []

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
            returncode, records = run_fv99_for_sign(
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

            fields_with_output = {(record.field, record.sign) for record in records}
            expected_outputs = {("f", sign), ("g", sign)}
            if fields_with_output != expected_outputs:
                return TimestepResult(
                    vtu=vtu_file,
                    status="failed",
                    message=f"{sign} missing outputs log={log_file}",
                )

            all_records.extend(records)

        output_names = move_records(all_records, step_dir, rebuild)

    write_manifest(vtu_file, output_names)
    return TimestepResult(
        vtu=vtu_file,
        status="done",
        moved_count=len(output_names),
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

    failed_lines: list[str] = []

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [
            pool.submit(compute_timestep, vtu_file, effective_rebuild)
            for vtu_file in vtu_files
        ]

        for count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result.status == "done":
                status = f"done moved={result.moved_count}"
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
