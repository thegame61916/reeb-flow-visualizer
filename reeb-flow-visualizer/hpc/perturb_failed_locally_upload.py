#!/usr/bin/env python3
"""Perturb failed local VTUs and upload them to Tetralith input downsampledGrids.

Default workflow is for stilbene:

1. Read failed stems from
   /media/mohit/4TB_kingston_tufA2/hpc/datasets/stilbene/sankey/rerun_failed_stems.txt
2. Perturb local input VTUs from
   /media/mohit/4TB_kingston_tufA2/hpc/datasets/stilbene/downsampledGrids
3. Upload the perturbed VTUs to
   x_mohsh@tetralith.nsc.liu.se:/proj/reeb-space-storage/users/x_mohsh/datasets/stilbene/downsampledGrids

Remote originals are backed up by rsync before replacement unless --no-backup is used.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_LOCAL_ROOT = Path("/media/mohit/4TB_kingston_tufA2/hpc/datasets")
DEFAULT_REMOTE_HOST = "x_mohsh@tetralith.nsc.liu.se"
DEFAULT_REMOTE_DATASETS_ROOT = "/proj/reeb-space-storage/users/x_mohsh/datasets"
DEFAULT_PERTURB_SCRIPT = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/scripts/perturb.py"
)


def read_failed_stems(path: Path) -> list[str]:
    stems: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        stem = Path(token).stem
        if stem and stem not in seen:
            stems.append(stem)
            seen.add(stem)
    return stems


def run_checked(command: list[str], *, dry_run: bool) -> None:
    print("+ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


def perturb_one(
    *,
    python_exe: str,
    perturb_script: Path,
    input_vtu: Path,
    output_vtu: Path,
    epsilon: str,
    dry_run: bool,
) -> bool:
    output_vtu.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_vtu.with_name(f".{output_vtu.name}.tmp.{os.getpid()}")
    tmp_output.unlink(missing_ok=True)

    command = [
        python_exe,
        str(perturb_script),
        str(input_vtu),
        epsilon,
        str(tmp_output),
    ]
    run_checked(command, dry_run=dry_run)
    if dry_run:
        return True

    if not tmp_output.is_file() or tmp_output.stat().st_size == 0:
        tmp_output.unlink(missing_ok=True)
        return False

    os.replace(tmp_output, output_vtu)
    return output_vtu.is_file() and output_vtu.stat().st_size > 0


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["stem", "status", "input_vtu", "perturbed_vtu", "epsilon", "message"]
    lines = ["\t".join(columns) + "\n"]
    for row in rows:
        lines.append("\t".join(row.get(column, "") for column in columns) + "\n")
    path.write_text("".join(lines))


def upload_outputs(
    *,
    outputs: list[Path],
    remote_host: str,
    remote_dir: str,
    backup: bool,
    dry_run: bool,
) -> None:
    if not outputs:
        print("No perturbed VTUs to upload.")
        return

    remote_dir = remote_dir.rstrip("/")
    backup_dir = f"{remote_dir}/_original_before_local_perturb"
    mkdir_command = ["ssh", remote_host, "mkdir", "-p", remote_dir]
    if backup:
        mkdir_command.append(backup_dir)
    run_checked(mkdir_command, dry_run=dry_run)

    command = ["rsync", "-av", "--partial", "--info=progress2"]
    if backup:
        command.extend(["--backup", f"--backup-dir={backup_dir}"])
    command.extend(str(path) for path in outputs)
    command.append(f"{remote_host}:{remote_dir}/")
    run_checked(command, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="stilbene")
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT)
    parser.add_argument("--failed-list", type=Path, help="Defaults to <local-root>/<dataset>/sankey/rerun_failed_stems.txt")
    parser.add_argument("--epsilon", default="0.00001")
    parser.add_argument("--python", dest="python_exe", default=sys.executable)
    parser.add_argument("--perturb-script", type=Path, default=DEFAULT_PERTURB_SCRIPT)
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-datasets-root", default=DEFAULT_REMOTE_DATASETS_ROOT)
    parser.add_argument("--remote-dir", help="Defaults to <remote-datasets-root>/<dataset>/downsampledGrids")
    parser.add_argument("--output-dir", type=Path, help="Defaults to <local-root>/<dataset>/sankey/local_perturbed_vtu")
    parser.add_argument("--manifest", type=Path, help="Defaults to <local-root>/<dataset>/sankey/local_perturbed_upload_manifest.tsv")
    parser.add_argument("--limit", type=int, help="Only process the first N failed stems.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-upload", action="store_true", help="Perturb locally but do not upload to Tetralith.")
    parser.add_argument("--no-backup", action="store_true", help="Do not back up overwritten remote originals.")
    args = parser.parse_args()

    dataset_dir = args.local_root / args.dataset
    input_dir = dataset_dir / "downsampledGrids"
    failed_list = args.failed_list or dataset_dir / "sankey" / "rerun_failed_stems.txt"
    output_dir = args.output_dir or dataset_dir / "sankey" / "local_perturbed_vtu"
    manifest = args.manifest or dataset_dir / "sankey" / "local_perturbed_upload_manifest.tsv"
    remote_dir = args.remote_dir or f"{args.remote_datasets_root.rstrip('/')}/{args.dataset}/downsampledGrids"

    if not failed_list.is_file():
        print(f"failed list not found: {failed_list}", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print(f"local input VTU directory not found: {input_dir}", file=sys.stderr)
        return 2
    if not args.perturb_script.is_file():
        print(f"perturb.py not found: {args.perturb_script}", file=sys.stderr)
        return 2

    stems = read_failed_stems(failed_list)
    if args.limit is not None:
        stems = stems[: max(0, args.limit)]

    print(f"Dataset: {args.dataset}")
    print(f"Failed list: {failed_list}")
    print(f"Failed stems: {len(stems)}")
    print(f"Local input VTUs: {input_dir}")
    print(f"Local perturbed output: {output_dir}")
    print(f"Remote target: {args.remote_host}:{remote_dir}")
    print(f"Python: {args.python_exe}")
    print(f"Perturb script: {args.perturb_script}")
    print(f"Epsilon: {args.epsilon}")

    rows: list[dict[str, str]] = []
    outputs: list[Path] = []

    for index, stem in enumerate(stems, start=1):
        input_vtu = input_dir / f"{stem}.vtu"
        output_vtu = output_dir / f"{stem}.vtu"
        print(f"[{index}/{len(stems)}] {stem}")

        if not input_vtu.is_file():
            message = f"missing local input VTU: {input_vtu}"
            print(f"  {message}")
            rows.append({
                "stem": stem,
                "status": "missing_input",
                "input_vtu": str(input_vtu),
                "perturbed_vtu": str(output_vtu),
                "epsilon": args.epsilon,
                "message": message,
            })
            continue

        try:
            ok = perturb_one(
                python_exe=args.python_exe,
                perturb_script=args.perturb_script,
                input_vtu=input_vtu,
                output_vtu=output_vtu,
                epsilon=args.epsilon,
                dry_run=args.dry_run,
            )
        except subprocess.CalledProcessError as exc:
            ok = False
            message = f"perturb.py returncode={exc.returncode}"
        else:
            message = "dry_run" if args.dry_run else "ok"

        rows.append({
            "stem": stem,
            "status": "perturbed" if ok else "failed",
            "input_vtu": str(input_vtu),
            "perturbed_vtu": str(output_vtu),
            "epsilon": args.epsilon,
            "message": message,
        })
        if ok and not args.dry_run:
            outputs.append(output_vtu)

    write_manifest(manifest, rows)
    print(f"Wrote manifest: {manifest}")

    if args.no_upload:
        print("Upload disabled by --no-upload.")
        return 0

    upload_outputs(
        outputs=outputs,
        remote_host=args.remote_host,
        remote_dir=remote_dir,
        backup=not args.no_backup,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
