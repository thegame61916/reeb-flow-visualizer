#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import subprocess

from common import (
    EPSILON,
    FV99_ATTEMPT_DETAILS_DIR,
    FV99,
    FV99_FAILED_LOG_FILE,
    FV99_OMP_THREADS,
    FV99_PARTIAL_LOG_FILE,
    FV99_PERTURBED_VTU_DIR,
    FV99_RETRY_EPSILONS,
    RESERVE_CORES,
    RSI_DIR,
    RS_DIR,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTU_DIR,
    VTK_LIB_DIR,
)


PERTURB_SCRIPT = Path(__file__).resolve().parent / "SheetRenderer" / "perturb.py"


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

    if not PERTURB_SCRIPT.exists():
        raise FileNotFoundError(f"Perturbation script not found: {PERTURB_SCRIPT}")


def epsilon_token(epsilon: str) -> str:
    return epsilon.replace("-", "m").replace(".", "p")


def fv99_log_path(vtu_file: Path, epsilon: str) -> Path:
    return FV99_FAILED_LOG_FILE.parent / f"{vtu_file.stem}.fv99.eps_{epsilon_token(epsilon)}.log"


def perturbed_vtu_path(vtu_file: Path, epsilon: str) -> Path:
    return FV99_PERTURBED_VTU_DIR / f"{vtu_file.stem}_eps_{epsilon_token(epsilon)}.vtu"


def sanitize_log_text(text: str) -> str:
    return " ".join(text.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


def attempts_detail_path(vtu_file: Path) -> Path:
    return FV99_ATTEMPT_DETAILS_DIR / f"{vtu_file.stem}.attempts.tsv"


def write_perturbed_vtu_snapshot(vtu_file: Path, epsilon: str):
    output_file = perturbed_vtu_path(vtu_file, epsilon)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["python3", str(PERTURB_SCRIPT), str(vtu_file), epsilon, str(output_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.returncode == 0 and output_file.exists():
        return output_file, None

    message = sanitize_log_text(result.stdout or "")
    error = f"perturb_returncode={result.returncode}"
    if message:
        error = f"{error}, perturb_output={message}"
    return None, error


def run_fv99_attempt(vtu_input_file: Path, rs_file: Path, rsi_file: Path, epsilon: str):
    rs_file.unlink(missing_ok=True)
    rsi_file.unlink(missing_ok=True)

    log_file = fv99_log_path(vtu_input_file, epsilon)

    command = [
        str(FV99),
        "-f", str(vtu_input_file),
        "-e", epsilon,
        "-s", str(rs_file),
        "-i", str(rsi_file),
    ]

    with log_file.open("w") as log:
        result = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=FV99_ENV,
        )

    has_outputs = rs_file.exists() and rsi_file.exists()
    success = result.returncode == 0 and has_outputs
    partial = result.returncode != 0 and has_outputs

    return {
        "epsilon": epsilon,
        "returncode": result.returncode,
        "has_outputs": has_outputs,
        "success": success,
        "partial": partial,
        "log_file": log_file,
        "perturbed_vtu": None,
        "perturb_snapshot_error": None,
    }


def run_fv99_with_perturbation_retries(vtu_file: Path):
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    rsi_file = RSI_DIR / f"{vtu_file.stem}.rsi"

    attempts = []

    initial_attempt = run_fv99_attempt(vtu_file, rs_file, rsi_file, EPSILON)
    attempts.append(initial_attempt)

    if initial_attempt["success"]:
        return {
            "vtu_file": vtu_file,
            "status": "done",
            "pass_epsilon": EPSILON,
            "pass_returncode": initial_attempt["returncode"],
            "attempts": attempts,
        }

    if initial_attempt["partial"]:
        return {
            "vtu_file": vtu_file,
            "status": "partial",
            "pass_epsilon": EPSILON,
            "pass_returncode": initial_attempt["returncode"],
            "attempts": attempts,
        }

    for retry_epsilon in FV99_RETRY_EPSILONS:
        retry_attempt = run_fv99_attempt(vtu_file, rs_file, rsi_file, retry_epsilon)
        attempts.append(retry_attempt)

        if retry_attempt["has_outputs"]:
            snapshot_path, snapshot_error = write_perturbed_vtu_snapshot(vtu_file, retry_epsilon)
            retry_attempt["perturbed_vtu"] = snapshot_path
            retry_attempt["perturb_snapshot_error"] = snapshot_error
            return {
                "vtu_file": vtu_file,
                "status": "recovered_with_perturbation",
                "pass_epsilon": retry_epsilon,
                "pass_returncode": retry_attempt["returncode"],
                "attempts": attempts,
            }

    return {
        "vtu_file": vtu_file,
        "status": "failed_after_perturbation",
        "pass_epsilon": None,
        "pass_returncode": None,
        "attempts": attempts,
    }


def get_pass_attempt(result):
    for attempt in result["attempts"]:
        if attempt["has_outputs"]:
            return attempt
    return None


def get_worked_perturbation_attempt(result):
    if result["status"] != "recovered_with_perturbation":
        return "-"

    for idx, attempt in enumerate(result["attempts"], start=1):
        if attempt["has_outputs"]:
            return idx - 1

    return "-"


def write_attempt_details(result):
    output_path = attempts_detail_path(result["vtu_file"])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["epsilon\treturncode\thas_outputs\tlog\tperturbed_vtu\tperturbed_vtu_error"]
    for attempt in result["attempts"]:
        lines.append(
            "\t".join(
                [
                    attempt["epsilon"],
                    str(attempt["returncode"]),
                    str(attempt["has_outputs"]),
                    str(attempt["log_file"]),
                    str(attempt["perturbed_vtu"] if attempt["perturbed_vtu"] else "-"),
                    str(attempt["perturb_snapshot_error"] if attempt["perturb_snapshot_error"] else "-"),
                ]
            )
        )

    output_path.write_text("\n".join(lines))
    return output_path


def format_failed_entry(result, details_path: Path):
    pass_attempt = get_pass_attempt(result)
    pass_log = pass_attempt["log_file"] if pass_attempt else result["attempts"][-1]["log_file"]
    worked_perturbation_attempt = get_worked_perturbation_attempt(result)

    pass_epsilon = result["pass_epsilon"] if result["pass_epsilon"] is not None else "-"
    pass_returncode = result["pass_returncode"] if result["pass_returncode"] is not None else "-"
    perturbed_vtu = pass_attempt["perturbed_vtu"] if pass_attempt and pass_attempt["perturbed_vtu"] else "-"

    return (
        f"{result['vtu_file']}\t"
        f"status={result['status']}\t"
        f"pass_epsilon={pass_epsilon}\t"
        f"pass_returncode={pass_returncode}\t"
        f"worked_perturbation_attempt={worked_perturbation_attempt}\t"
        f"attempt_count={len(result['attempts'])}\t"
        f"pass_log={pass_log}\t"
        f"perturbed_vtu={perturbed_vtu}\t"
        f"attempts_detail={details_path}"
    )


def run_fv99_stage():
    check_inputs()

    RS_DIR.mkdir(parents=True, exist_ok=True)
    RSI_DIR.mkdir(parents=True, exist_ok=True)
    FV99_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FV99_PERTURBED_VTU_DIR.mkdir(parents=True, exist_ok=True)
    FV99_ATTEMPT_DETAILS_DIR.mkdir(parents=True, exist_ok=True)

    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    num_workers = max(1, (os.cpu_count() or 1) - RESERVE_CORES)

    print(f"Running fv99 on {len(vtu_files)} files using {num_workers} parallel jobs")

    failed_entries = []
    partial_entries = []
    done_count = 0

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(run_fv99_with_perturbation_retries, vtu_file)
            for vtu_file in vtu_files
        ]

        for count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            status = result["status"]

            if status == "done":
                done_count += 1
                status_text = "done"
            elif status == "partial":
                status_text = f"partial returncode={result['pass_returncode']}"
            elif status == "recovered_with_perturbation":
                status_text = (
                    f"recovered epsilon={result['pass_epsilon']} "
                    f"returncode={result['pass_returncode']}"
                )
            else:
                status_text = "failed after perturbation retries"

            print(
                f"[{count}/{len(vtu_files)}] {status_text}: {result['vtu_file'].name}",
                flush=True,
            )

            if status != "done":
                details_path = write_attempt_details(result)
                entry = format_failed_entry(result, details_path)
                failed_entries.append(entry)

                if status == "partial" or (
                    status == "recovered_with_perturbation"
                    and result["pass_returncode"] != 0
                ):
                    partial_entries.append(entry)

    FV99_FAILED_LOG_FILE.write_text("\n".join(failed_entries))
    FV99_PARTIAL_LOG_FILE.write_text("\n".join(partial_entries))

    print(f"Done files: {done_count}")
    print(f"Logged files: {len(failed_entries)}")
    print(f"Partial files: {len(partial_entries)}")
    print(f"Failure log: {FV99_FAILED_LOG_FILE}")
    print(f"Partial log: {FV99_PARTIAL_LOG_FILE}")
    print(f"Perturbed VTU dir: {FV99_PERTURBED_VTU_DIR}")
