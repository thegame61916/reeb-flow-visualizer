#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

from common import (
    EPSILON,
    FIBER_SURFACE_ADAPTIVE_ENABLED,
    FIBER_SURFACE_FIELD_F_ISOVALUE,
    FIBER_SURFACE_FIELD_G_ISOVALUE,
    FIBER_SURFACE_MODE,
    FIBER_SURFACE_LABELED_DIR,
    FIBER_SURFACE_TEMP_DIR,
    FV99,
    FV99_FAILED_LOG_FILE,
    FV99_OMP_THREADS,
    FV99_PARTIAL_LOG_FILE,
    FV99_PERTURB_EPSILON,
    FV99_PERTURB_SCRIPT,
    FV99_PERTURBED_VTU_DIR,
    FV99_RECOVERED_LOG_FILE,
    RESERVE_CORES,
    FV99_FNAME,
    FV99_GNAME,
    RSI_DIR,
    RS_DIR,
    SHEET_VTP_CACHE_DIR,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTU_DIR,
    VTK_LIB_DIR,
)


def make_fv99_environment():
    env = os.environ.copy()

    library_paths = [
        str(TTK_BUILD_LIB_DIR),
        str(TTK_INSTALL_LIB_DIR),
        str(VTK_LIB_DIR),
    ]

    if env.get("LD_LIBRARY_PATH"):
        library_paths.append(env["LD_LIBRARY_PATH"])

    env["LD_LIBRARY_PATH"] = ":".join(library_paths)
    env["OMP_NUM_THREADS"] = str(FV99_OMP_THREADS)
    return env


FV99_ENV = make_fv99_environment()


def check_inputs():
    if not FV99.exists():
        raise FileNotFoundError(f"fv99 binary not found: {FV99}")

    if not os.access(FV99, os.X_OK):
        raise PermissionError(f"fv99 binary is not executable: {FV99}")

    if not VTU_DIR.exists():
        raise FileNotFoundError(f"VTU directory not found: {VTU_DIR}")


def epsilon_label(value) -> str:
    text = str(value)
    try:
        number = float(text)
    except Exception:
        return text
    if "e" in text.lower():
        return f"{number:.12f}".rstrip("0").rstrip(".")
    return text


def epsilon_slug(value) -> str:
    return epsilon_label(value).replace(".", "p").replace("-", "m")


def value_text(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def sheet_vtp_path(vtu_file: Path) -> Path:
    return SHEET_VTP_CACHE_DIR / f"{vtu_file.stem}.sheets.vtp"


def sheet_vtp_sidecar_paths(vtp_file: Path) -> list[Path]:
    return [
        Path(str(vtp_file) + ".features.vtp"),
        Path(str(vtp_file) + ".graph.dot"),
    ]


def labeled_surface_dir(vtu_file: Path) -> Path:
    return FIBER_SURFACE_LABELED_DIR / vtu_file.stem


def labeled_surface_path(vtu_file: Path, role: str) -> Path:
    return labeled_surface_dir(vtu_file) / f"{role}.vtp"


def labeled_surface_manifest_path(vtu_file: Path) -> Path:
    return labeled_surface_dir(vtu_file) / "labeled_fiber_surfaces_manifest.json"


def expected_labeled_surface_paths(vtu_file: Path) -> list[Path]:
    return [
        labeled_surface_path(vtu_file, "f_pos"),
        labeled_surface_path(vtu_file, "g_pos"),
        labeled_surface_path(vtu_file, "f_neg"),
        labeled_surface_path(vtu_file, "g_neg"),
    ]


def fv99_command(vtu_file: Path, rs_file: Path, rsi_file: Path, sheets_vtp_file: Path):
    return [
        str(FV99),
        "-f", str(vtu_file),
        "-e", EPSILON,
        "-s", str(rs_file),
        "-i", str(rsi_file),
        "-o", str(sheets_vtp_file),
        "--fName", FV99_FNAME,
        "--gName", FV99_GNAME,
        "--headless",
    ]


def run_fv99_command(vtu_file: Path, rs_file: Path, rsi_file: Path, sheets_vtp_file: Path, log_file: Path):
    rs_file.parent.mkdir(parents=True, exist_ok=True)
    rsi_file.parent.mkdir(parents=True, exist_ok=True)
    sheets_vtp_file.parent.mkdir(parents=True, exist_ok=True)

    rs_file.unlink(missing_ok=True)
    rsi_file.unlink(missing_ok=True)
    sheets_vtp_file.unlink(missing_ok=True)
    for sidecar in sheet_vtp_sidecar_paths(sheets_vtp_file):
        sidecar.unlink(missing_ok=True)

    with log_file.open("w") as log:
        return subprocess.run(
            fv99_command(vtu_file, rs_file, rsi_file, sheets_vtp_file),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=FV99_ENV,
        )


def run_fv99_fiber_sign(
    vtu_file: Path,
    rs_file: Path,
    sign: str,
    f_value: float,
    g_value: float,
    log_file: Path,
) -> dict:
    destination_dir = labeled_surface_dir(vtu_file)
    destination_dir.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    f_destination = labeled_surface_path(vtu_file, f"f_{sign}")
    g_destination = labeled_surface_path(vtu_file, f"g_{sign}")
    f_destination.unlink(missing_ok=True)
    g_destination.unlink(missing_ok=True)

    command = [
        str(FV99),
        "-f", str(vtu_file),
        "-l", str(rs_file),
        "--fieldFValueFS", value_text(f_value),
        "--fieldGValueFS", value_text(g_value),
        "--fName", FV99_FNAME,
        "--gName", FV99_GNAME,
        "--headless",
    ]

    with tempfile.TemporaryDirectory(
        prefix=f"{vtu_file.stem}_{sign}_",
        dir=FIBER_SURFACE_TEMP_DIR,
    ) as tmp_name:
        work_dir = Path(tmp_name)
        (work_dir / "output").mkdir(parents=True, exist_ok=True)

        with log_file.open("w") as log:
            result = subprocess.run(
                command,
                cwd=work_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=FV99_ENV,
            )

        f_source = work_dir / "output" / "labeled.fs.f.vtp"
        g_source = work_dir / "output" / "labeled.fs.g.vtp"
        ok = result.returncode == 0 and f_source.exists() and g_source.exists()

        move_error = None
        if ok:
            try:
                shutil.move(str(f_source), str(f_destination))
                shutil.move(str(g_source), str(g_destination))
            except OSError as exc:
                move_error = f"{type(exc).__name__}: {exc}"
                f_destination.unlink(missing_ok=True)
                g_destination.unlink(missing_ok=True)
                ok = False

    return {
        "sign": sign,
        "returncode": result.returncode,
        "log": log_file,
        "f_output": f_destination,
        "g_output": g_destination,
        "ok": ok,
        "move_error": move_error,
    }


def write_labeled_surface_manifest(vtu_file: Path, rs_file: Path, fiber_details: list[dict]) -> None:
    surfaces = {}
    for detail in fiber_details:
        sign = detail["sign"]
        surfaces[f"f_{sign}"] = str(detail["f_output"])
        surfaces[f"g_{sign}"] = str(detail["g_output"])

    payload = {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "rs": str(rs_file),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "f_isovalue": float(FIBER_SURFACE_FIELD_F_ISOVALUE),
        "g_isovalue": float(FIBER_SURFACE_FIELD_G_ISOVALUE),
        "surfaces": surfaces,
    }
    labeled_surface_manifest_path(vtu_file).write_text(json.dumps(payload, indent=2) + "\n")


def generate_fiber_surfaces(vtu_file: Path, rs_file: Path) -> tuple[bool, list[dict]]:
    labeled_surface_manifest_path(vtu_file).unlink(missing_ok=True)
    for path in expected_labeled_surface_paths(vtu_file):
        path.unlink(missing_ok=True)

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

    details: list[dict] = []
    for sign, f_value, g_value in runs:
        log_file = FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.{sign}.fiber.fv99.log"
        try:
            detail = run_fv99_fiber_sign(vtu_file, rs_file, sign, f_value, g_value, log_file)
        except Exception as exc:
            detail = {
                "sign": sign,
                "returncode": "error",
                "log": log_file,
                "f_output": labeled_surface_path(vtu_file, f"f_{sign}"),
                "g_output": labeled_surface_path(vtu_file, f"g_{sign}"),
                "ok": False,
                "move_error": f"{type(exc).__name__}: {exc}",
            }
        details.append(detail)
        if not detail["ok"]:
            return False, details

    write_labeled_surface_manifest(vtu_file, rs_file, details)
    return True, details


def finalize_with_artifacts(vtu_file: Path, success: bool, partial: bool, returncode: int, details: dict):
    if FIBER_SURFACE_ADAPTIVE_ENABLED:
        details["fiber_surfaces_ok"] = True
        details["fiber_surface_runs"] = []
        details["fiber_surface_mode"] = FIBER_SURFACE_MODE
        details["fiber_surface_note"] = "fixed fiber artifacts skipped; Stage 4C renders adaptive fiber images"
        return vtu_file, success, partial, returncode, details

    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    fiber_ok, fiber_details = generate_fiber_surfaces(vtu_file, rs_file)
    details["fiber_surfaces_ok"] = fiber_ok
    details["fiber_surface_runs"] = fiber_details

    if not fiber_ok:
        return vtu_file, False, True, returncode, details

    return vtu_file, success, partial, returncode, details


def perturb_output_path(vtu_file: Path) -> Path:
    return FV99_PERTURBED_VTU_DIR / f"{vtu_file.stem}_eps_{epsilon_slug(FV99_PERTURB_EPSILON)}.vtu"


def perturb_vtu_once(vtu_file: Path, output_file: Path, log_file: Path):
    script = Path(FV99_PERTURB_SCRIPT).expanduser()
    if not script.exists():
        raise FileNotFoundError(f"perturbation script not found: {script}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = output_file.with_name(f".{output_file.name}.tmp")
    tmp_file.unlink(missing_ok=True)
    output_file.unlink(missing_ok=True)

    command = [
        sys.executable,
        str(script),
        str(vtu_file),
        epsilon_label(FV99_PERTURB_EPSILON),
        str(tmp_file),
    ]
    with log_file.open("w") as log:
        result = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    if result.returncode == 0 and tmp_file.exists():
        os.replace(tmp_file, output_file)
    else:
        tmp_file.unlink(missing_ok=True)

    return result


def run_fv99(vtu_file: Path):
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    rsi_file = RSI_DIR / f"{vtu_file.stem}.rsi"
    sheets_vtp_file = sheet_vtp_path(vtu_file)
    log_file = FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.fv99.log"

    result = run_fv99_command(vtu_file, rs_file, rsi_file, sheets_vtp_file, log_file)

    has_outputs = rs_file.exists() and rsi_file.exists() and sheets_vtp_file.exists()
    success = result.returncode == 0 and has_outputs
    partial = result.returncode != 0 and has_outputs
    details = {
        "log_file": log_file,
        "normal_returncode": result.returncode,
        "sheet_vtp": sheets_vtp_file,
        "primary_outputs_exist": has_outputs,
        "perturbed_vtu": None,
        "perturb_log": None,
        "perturb_returncode": None,
        "retry_log": None,
        "retry_returncode": None,
        "recovered_with_perturbation": False,
        "original_vtu_replaced": False,
        "replacement_vtu": None,
        "fiber_surfaces_ok": False,
        "fiber_surface_runs": [],
        "fiber_surface_mode": FIBER_SURFACE_MODE,
        "fiber_surface_note": None,
    }

    if success or partial:
        return finalize_with_artifacts(vtu_file, success, partial, result.returncode, details)

    perturbed_vtu = perturb_output_path(vtu_file)
    perturb_log = FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.perturb_vtu.log"
    retry_log = FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.fv99.perturbed.log"
    details.update({
        "perturbed_vtu": perturbed_vtu,
        "perturb_log": perturb_log,
        "retry_log": retry_log,
    })

    try:
        perturb_result = perturb_vtu_once(vtu_file, perturbed_vtu, perturb_log)
        details["perturb_returncode"] = perturb_result.returncode
    except Exception as exc:
        perturb_log.write_text(f"perturbation setup failed: {type(exc).__name__}: {exc}\n")
        details["perturb_returncode"] = "error"
        return vtu_file, False, False, result.returncode, details

    if details["perturb_returncode"] != 0 or not perturbed_vtu.exists():
        return vtu_file, False, False, result.returncode, details

    retry_result = run_fv99_command(perturbed_vtu, rs_file, rsi_file, sheets_vtp_file, retry_log)
    retry_has_outputs = rs_file.exists() and rsi_file.exists() and sheets_vtp_file.exists()
    retry_success = retry_result.returncode == 0 and retry_has_outputs
    retry_partial = retry_result.returncode != 0 and retry_has_outputs
    details["retry_returncode"] = retry_result.returncode
    details["recovered_with_perturbation"] = retry_success

    if retry_success:
        os.replace(perturbed_vtu, vtu_file)
        details["original_vtu_replaced"] = True
        details["replacement_vtu"] = vtu_file
        return finalize_with_artifacts(vtu_file, True, False, retry_result.returncode, details)
    if retry_partial:
        os.replace(perturbed_vtu, vtu_file)
        details["original_vtu_replaced"] = True
        details["replacement_vtu"] = vtu_file
        return finalize_with_artifacts(vtu_file, False, True, retry_result.returncode, details)
    return vtu_file, False, False, retry_result.returncode, details


def run_fv99_stage():
    check_inputs()

    RS_DIR.mkdir(parents=True, exist_ok=True)
    RSI_DIR.mkdir(parents=True, exist_ok=True)
    SHEET_VTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FV99_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FV99_PERTURBED_VTU_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_LABELED_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    num_workers = max(1, (os.cpu_count() or 1) - RESERVE_CORES)

    print(f"Running fv99 on {len(vtu_files)} files using {num_workers} parallel jobs")

    failed_files = []
    partial_files = []
    recovered_files = []

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(run_fv99, vtu_file)
            for vtu_file in vtu_files
        ]

        for count, future in enumerate(as_completed(futures), start=1):
            vtu_file, success, partial, returncode, details = future.result()
            log_file = details["log_file"]

            if success and details.get("recovered_with_perturbation"):
                status = (
                    f"recovered perturb_epsilon={epsilon_label(FV99_PERTURB_EPSILON)} "
                    f"returncode={returncode}"
                )
            elif success:
                status = "done"
            elif partial and not details.get("fiber_surfaces_ok") and details.get("primary_outputs_exist"):
                status = f"partial missing_fiber_artifacts returncode={returncode}"
            elif partial:
                status = f"partial returncode={returncode}"
            else:
                status = f"failed returncode={returncode}"
            print(f"[{count}/{len(vtu_files)}] {status}: {vtu_file.name}", flush=True)

            detail_fields = [
                f"normal_returncode={details.get('normal_returncode')}",
                f"log={log_file}",
                f"sheet_vtp={details.get('sheet_vtp')}",
                f"primary_outputs_exist={details.get('primary_outputs_exist')}",
                f"fiber_surfaces_ok={details.get('fiber_surfaces_ok')}",
                f"fiber_surface_mode={details.get('fiber_surface_mode')}",
                f"fiber_surface_note={details.get('fiber_surface_note')}",
            ]
            for fiber_run in details.get("fiber_surface_runs", []):
                sign = fiber_run.get("sign")
                detail_fields.extend([
                    f"fiber_{sign}_returncode={fiber_run.get('returncode')}",
                    f"fiber_{sign}_log={fiber_run.get('log')}",
                    f"fiber_{sign}_f_output={fiber_run.get('f_output')}",
                    f"fiber_{sign}_g_output={fiber_run.get('g_output')}",
                    f"fiber_{sign}_move_error={fiber_run.get('move_error')}",
                ])
            if details.get("perturbed_vtu") is not None:
                detail_fields.extend([
                    f"perturb_epsilon={epsilon_label(FV99_PERTURB_EPSILON)}",
                    f"perturb_returncode={details.get('perturb_returncode')}",
                    f"perturbed_vtu={details.get('perturbed_vtu')}",
                    f"perturb_log={details.get('perturb_log')}",
                    f"retry_returncode={details.get('retry_returncode')}",
                    f"retry_log={details.get('retry_log')}",
                    f"original_vtu_replaced={details.get('original_vtu_replaced')}",
                    f"replacement_vtu={details.get('replacement_vtu')}",
                ])

            if success and details.get("recovered_with_perturbation"):
                recovered_files.append(f"{vtu_file}\treturncode={returncode}\t" + "\t".join(detail_fields))
            elif partial:
                partial_files.append(f"{vtu_file}\treturncode={returncode}\t" + "\t".join(detail_fields))
            elif not success:
                failed_files.append(f"{vtu_file}\treturncode={returncode}\t" + "\t".join(detail_fields))

    FV99_FAILED_LOG_FILE.write_text("\n".join(failed_files))
    FV99_PARTIAL_LOG_FILE.write_text("\n".join(partial_files))
    FV99_RECOVERED_LOG_FILE.write_text("\n".join(recovered_files))

    print(f"Failed files: {len(failed_files)}")
    print(f"Partial files: {len(partial_files)}")
    print(f"Recovered files: {len(recovered_files)}")
    print(f"Failure log: {FV99_FAILED_LOG_FILE}")
    print(f"Partial log: {FV99_PARTIAL_LOG_FILE}")
    print(f"Recovered log: {FV99_RECOVERED_LOG_FILE}")
