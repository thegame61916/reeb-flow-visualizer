#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import subprocess
import sys

from common import (
    EPSILON,
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


def fv99_command(vtu_file: Path, rs_file: Path, rsi_file: Path):
    return [
        str(FV99),
        "-f", str(vtu_file),
        "-e", EPSILON,
        "-s", str(rs_file),
        "-i", str(rsi_file),
        "--fName", FV99_FNAME,
        "--gName", FV99_GNAME,
        "--headless",
    ]


def run_fv99_command(vtu_file: Path, rs_file: Path, rsi_file: Path, log_file: Path):
    rs_file.unlink(missing_ok=True)
    rsi_file.unlink(missing_ok=True)
    with log_file.open("w") as log:
        return subprocess.run(
            fv99_command(vtu_file, rs_file, rsi_file),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=FV99_ENV,
        )


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
    log_file = FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.fv99.log"

    result = run_fv99_command(vtu_file, rs_file, rsi_file, log_file)

    has_outputs = rs_file.exists() and rsi_file.exists()
    success = result.returncode == 0 and has_outputs
    partial = result.returncode != 0 and has_outputs
    details = {
        "log_file": log_file,
        "normal_returncode": result.returncode,
        "perturbed_vtu": None,
        "perturb_log": None,
        "perturb_returncode": None,
        "retry_log": None,
        "retry_returncode": None,
        "recovered_with_perturbation": False,
        "original_vtu_replaced": False,
        "replacement_vtu": None,
    }

    if success or partial:
        return vtu_file, success, partial, result.returncode, details

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

    retry_result = run_fv99_command(perturbed_vtu, rs_file, rsi_file, retry_log)
    retry_has_outputs = rs_file.exists() and rsi_file.exists()
    retry_success = retry_result.returncode == 0 and retry_has_outputs
    retry_partial = retry_result.returncode != 0 and retry_has_outputs
    details["retry_returncode"] = retry_result.returncode
    details["recovered_with_perturbation"] = retry_success

    if retry_success:
        os.replace(perturbed_vtu, vtu_file)
        details["original_vtu_replaced"] = True
        details["replacement_vtu"] = vtu_file
        return vtu_file, True, False, retry_result.returncode, details
    if retry_partial:
        return vtu_file, False, True, retry_result.returncode, details
    return vtu_file, False, False, retry_result.returncode, details


def run_fv99_stage():
    check_inputs()

    RS_DIR.mkdir(parents=True, exist_ok=True)
    RSI_DIR.mkdir(parents=True, exist_ok=True)
    FV99_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FV99_PERTURBED_VTU_DIR.mkdir(parents=True, exist_ok=True)

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
            elif partial:
                status = f"partial returncode={returncode}"
            else:
                status = f"failed returncode={returncode}"
            print(f"[{count}/{len(vtu_files)}] {status}: {vtu_file.name}", flush=True)

            detail_fields = [
                f"normal_returncode={details.get('normal_returncode')}",
                f"log={log_file}",
            ]
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
