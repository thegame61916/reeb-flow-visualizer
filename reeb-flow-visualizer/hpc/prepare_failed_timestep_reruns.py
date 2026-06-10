#!/usr/bin/env python3
"""Prepare Tetralith rerun lists from locally copied HPC Stage-1 artifacts."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REQUIRED_FIBER_FILES = (
    "f_pos.vtp",
    "g_pos.vtp",
    "f_neg.vtp",
    "g_neg.vtp",
    "labeled_fiber_surfaces_manifest.json",
)


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def dataset_names(root: Path, requested: list[str]) -> list[str]:
    if requested:
        return requested
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def manifest_path(dataset_dir: Path) -> Path | None:
    candidates = (
        dataset_dir / "sankey" / "hpc_vtu_manifest.txt.all",
        dataset_dir / "sankey" / "hpc_vtu_manifest.txt",
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def read_expected_stems(dataset_dir: Path) -> tuple[list[str], Path | None]:
    # Prefer copied input VTUs when present. The HPC manifest can be overwritten
    # by a later subset/rerun submission, while downsampledGrids represents the
    # full local dataset the user wants to verify.
    vtu_stems = sorted({path.stem for path in (dataset_dir / "downsampledGrids").glob("*.vtu")})
    if vtu_stems:
        return vtu_stems, dataset_dir / "downsampledGrids"

    manifest = manifest_path(dataset_dir)
    if manifest is not None:
        stems = []
        for raw_line in manifest.read_text().splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            stems.append(Path(line).stem)
        return sorted(set(stems)), manifest

    # Last fallback: infer from copied artifacts. This cannot detect timesteps
    # with no copied artifact at all, so local VTUs or a manifest are preferred.
    stems: set[str] = set()
    for pattern in (
        "reebSpaces/*.rs",
        "sheetInfo/*.rsi",
        "compareSheetShapesCache/cache/vtp/*.sheets.vtp",
        "sheetFiberSurfaces/labeled/*",
    ):
        for path in dataset_dir.glob(pattern):
            name = path.name
            if name.endswith(".sheets.vtp"):
                stems.add(name[: -len(".sheets.vtp")])
            else:
                stems.add(path.stem if path.is_file() else path.name)
    return sorted(stems), None


def missing_outputs(dataset_dir: Path, stem: str, require_fibers: bool) -> list[str]:
    checks = [
        (dataset_dir / "reebSpaces" / f"{stem}.rs", "rs"),
        (dataset_dir / "sheetInfo" / f"{stem}.rsi", "rsi"),
        (dataset_dir / "compareSheetShapesCache" / "cache" / "vtp" / f"{stem}.sheets.vtp", "sheet_vtp"),
    ]
    if require_fibers:
        fiber_dir = dataset_dir / "sheetFiberSurfaces" / "labeled" / stem
        checks.extend((fiber_dir / filename, filename) for filename in REQUIRED_FIBER_FILES)
    return [label for path, label in checks if not nonempty(path)]


def write_outputs(dataset_dir: Path, failed: list[tuple[str, list[str]]], expected_count: int) -> tuple[Path, Path]:
    sankey_dir = dataset_dir / "sankey"
    sankey_dir.mkdir(parents=True, exist_ok=True)
    stems_file = sankey_dir / "rerun_failed_stems.txt"
    report_file = sankey_dir / "rerun_failed_report.tsv"
    stems_file.write_text("".join(f"{stem}\n" for stem, _ in failed))
    report_lines = ["stem\tstatus\tmissing\n"]
    for stem, missing in failed:
        report_lines.append(f"{stem}\tfailed\t{','.join(missing)}\n")
    report_lines.append(f"# expected={expected_count}\tfailed={len(failed)}\n")
    report_file.write_text("".join(report_lines))
    return stems_file, report_file


def upload_files(remote_host: str, remote_output_root: str, dataset: str, files: list[Path]) -> None:
    remote_dir = f"{remote_output_root.rstrip('/')}/{dataset}/sankey"
    subprocess.run(["ssh", remote_host, "mkdir", "-p", remote_dir], check=True)
    target = f"{remote_host}:{remote_dir}/"
    subprocess.run(["rsync", "-av", *(str(path) for path in files), target], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="*", help="Dataset names. Defaults to every directory under --local-root.")
    parser.add_argument("--local-root", default="/media/mohit/4TB_kingston_tufA2/hpc/datasets")
    parser.add_argument("--remote-host", default="x_mohsh@tetralith.nsc.liu.se")
    parser.add_argument("--remote-output-root", default="/proj/reeb-space-storage/users/x_mohsh/hpc_outputs/datasets")
    parser.add_argument("--no-upload", action="store_true", help="Only write local rerun lists; do not rsync to Tetralith.")
    parser.add_argument("--no-fibers", action="store_true", help="Do not require fiber-surface VTP artifacts.")
    args = parser.parse_args()

    local_root = Path(args.local_root).expanduser()
    if not local_root.is_dir():
        print(f"local root not found: {local_root}", file=sys.stderr)
        return 2

    for dataset in dataset_names(local_root, args.datasets):
        dataset_dir = local_root / dataset
        if not dataset_dir.is_dir():
            print(f"[{dataset}] missing local dataset directory: {dataset_dir}", file=sys.stderr)
            continue

        stems, manifest = read_expected_stems(dataset_dir)
        if not stems:
            print(f"[{dataset}] no expected timesteps found; copy sankey/hpc_vtu_manifest.txt first", file=sys.stderr)
            continue

        failed = []
        for stem in stems:
            missing = missing_outputs(dataset_dir, stem, require_fibers=not args.no_fibers)
            if missing:
                failed.append((stem, missing))

        stems_file, report_file = write_outputs(dataset_dir, failed, len(stems))
        manifest_text = str(manifest) if manifest is not None else "inferred-from-local-artifacts"
        print(f"[{dataset}] expected={len(stems)} failed={len(failed)} manifest={manifest_text}")
        print(f"[{dataset}] wrote {stems_file}")
        print(f"[{dataset}] wrote {report_file}")

        if not args.no_upload:
            upload_files(args.remote_host, args.remote_output_root, dataset, [stems_file, report_file])
            print(f"[{dataset}] uploaded rerun list to {args.remote_host}:{args.remote_output_root}/{dataset}/sankey/")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
