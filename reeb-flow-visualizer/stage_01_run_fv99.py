#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import subprocess

from common import (
    EPSILON,
    FV99,
    FV99_FAILED_LOG_FILE,
    RESERVE_CORES,
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
    return env


FV99_ENV = make_fv99_environment()


def run_fv99(vtu_file: Path):
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    rsi_file = RSI_DIR / f"{vtu_file.stem}.rsi"

    command = [
        str(FV99),
        "-f", str(vtu_file),
        "-e", EPSILON,
        "-s", str(rs_file),
        "-i", str(rsi_file),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=FV99_ENV,
    )

    success = (
        result.returncode == 0
        and rs_file.exists()
        and rsi_file.exists()
    )

    return vtu_file, success


def run_fv99_stage():
    RS_DIR.mkdir(parents=True, exist_ok=True)
    RSI_DIR.mkdir(parents=True, exist_ok=True)
    FV99_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    num_workers = max(1, (os.cpu_count() or 1) - RESERVE_CORES)

    print(f"Running fv99 on {len(vtu_files)} files using {num_workers} parallel jobs")

    failed_files = []

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(run_fv99, vtu_file)
            for vtu_file in vtu_files
        ]

        for count, future in enumerate(as_completed(futures), start=1):
            vtu_file, success = future.result()

            status = "done" if success else "failed"
            print(f"[{count}/{len(vtu_files)}] {status}: {vtu_file.name}", flush=True)

            if not success:
                failed_files.append(str(vtu_file))

    FV99_FAILED_LOG_FILE.write_text("\n".join(failed_files))

    print(f"Failed files: {len(failed_files)}")
    print(f"Failure log: {FV99_FAILED_LOG_FILE}")
