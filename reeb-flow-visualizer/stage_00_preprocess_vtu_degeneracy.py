#!/usr/bin/env python3

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
import hashlib
import os
import time

import numpy as np
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

from common import (
    DEGENERACY_PREPROCESS_DELETE_UNFIXED,
    DEGENERACY_PREPROCESS_EPSILONS,
    DEGENERACY_PREPROCESS_LOG_FILE,
    DEGENERACY_PREPROCESS_ORIENTATION_TOLERANCE,
    DEGENERACY_PREPROCESS_RANDOM_SEED,
    FV99_FNAME,
    FV99_GNAME,
    RS_DIR,
    RSI_DIR,
    RSI_JSON_DIR,
    VTU_DIR,
)


@dataclass(frozen=True)
class DegenerateTriangle:
    cell_id: int
    point_ids: tuple[int, int, int]
    area2: float


def epsilon_label(value: float) -> str:
    return f"{value:.12g}"


def stable_seed(vtu_file: Path, epsilon: float) -> int | None:
    if DEGENERACY_PREPROCESS_RANDOM_SEED is None:
        return None
    payload = f"{DEGENERACY_PREPROCESS_RANDOM_SEED}:{vtu_file.name}:{epsilon_label(epsilon)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little", signed=False)


def read_vtu(vtu_file: Path) -> vtk.vtkUnstructuredGrid:
    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(vtu_file))
    reader.Update()
    if reader.GetErrorCode():
        raise RuntimeError(f"failed to read VTU file: {vtu_file}")
    grid = vtk.vtkUnstructuredGrid()
    grid.DeepCopy(reader.GetOutput())
    return grid


def point_array_values(grid: vtk.vtkUnstructuredGrid, name: str, vtu_file: Path) -> np.ndarray:
    array = grid.GetPointData().GetArray(name)
    if array is None:
        raise KeyError(f"point-data array {name!r} not found in {vtu_file}")
    if array.GetNumberOfComponents() != 1:
        raise ValueError(f"point-data array {name!r} in {vtu_file} is not scalar")
    return vtk_to_numpy(array).astype(np.float64, copy=True)


def iter_cell_triangles(grid: vtk.vtkUnstructuredGrid):
    for cell_id in range(grid.GetNumberOfCells()):
        point_ids = grid.GetCell(cell_id).GetPointIds()
        ids = [int(point_ids.GetId(i)) for i in range(point_ids.GetNumberOfIds())]
        if len(ids) < 3:
            continue
        for tri in combinations(ids, 3):
            yield cell_id, tri


def triangle_area2(f_values: np.ndarray, g_values: np.ndarray, point_ids: tuple[int, int, int]) -> float:
    a, b, c = point_ids
    return float(
        (f_values[b] - f_values[a]) * (g_values[c] - g_values[a])
        - (g_values[b] - g_values[a]) * (f_values[c] - f_values[a])
    )


def find_degenerate_triangles(
    grid: vtk.vtkUnstructuredGrid,
    f_values: np.ndarray,
    g_values: np.ndarray,
    max_examples: int = 5,
) -> tuple[int, list[DegenerateTriangle]]:
    tolerance = float(DEGENERACY_PREPROCESS_ORIENTATION_TOLERANCE)
    count = 0
    examples: list[DegenerateTriangle] = []
    for cell_id, tri in iter_cell_triangles(grid):
        area2 = triangle_area2(f_values, g_values, tri)
        if abs(area2) <= tolerance:
            count += 1
            if len(examples) < max_examples:
                examples.append(DegenerateTriangle(cell_id=cell_id, point_ids=tri, area2=area2))
    return count, examples


def replace_scalar_array(grid: vtk.vtkUnstructuredGrid, name: str, values: np.ndarray) -> None:
    point_data = grid.GetPointData()
    old_array = point_data.GetArray(name)
    if old_array is None:
        raise KeyError(f"point-data array {name!r} not found")

    new_array = numpy_to_vtk(values.astype(np.float64, copy=False), deep=True)
    new_array.SetName(name)

    point_data.RemoveArray(name)
    point_data.AddArray(new_array)


def write_vtu_atomic(grid: vtk.vtkUnstructuredGrid, vtu_file: Path) -> None:
    tmp_file = vtu_file.with_name(f".{vtu_file.name}.degeneracy_tmp")
    writer = vtk.vtkXMLUnstructuredGridWriter()
    writer.SetFileName(str(tmp_file))
    writer.SetInputData(grid)
    if writer.Write() != 1:
        tmp_file.unlink(missing_ok=True)
        raise RuntimeError(f"failed to write perturbed VTU file: {tmp_file}")
    os.replace(tmp_file, vtu_file)


def perturb_values(values: np.ndarray, epsilon: float, rng: np.random.Generator) -> np.ndarray:
    return values + rng.uniform(-epsilon, epsilon, size=values.shape)


def cleanup_derived_files(stem: str) -> list[Path]:
    candidates = [
        RS_DIR / f"{stem}.rs",
        RSI_DIR / f"{stem}.rsi",
        RSI_JSON_DIR / f"{stem}.json",
    ]
    removed = []
    for path in candidates:
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def format_examples(examples: list[DegenerateTriangle]) -> str:
    if not examples:
        return "-"
    return ";".join(
        f"cell={example.cell_id},points={example.point_ids},area2={example.area2:.6g}"
        for example in examples
    )


def process_vtu_file(vtu_file: Path) -> tuple[str, str]:
    grid = read_vtu(vtu_file)
    f_values = point_array_values(grid, FV99_FNAME, vtu_file)
    g_values = point_array_values(grid, FV99_GNAME, vtu_file)
    initial_count, examples = find_degenerate_triangles(grid, f_values, g_values)

    if initial_count == 0:
        return "ok", (
            f"{vtu_file}\tstatus=ok\tdegenerate_triangles=0"
        )

    attempts = []
    for epsilon in DEGENERACY_PREPROCESS_EPSILONS:
        seed = stable_seed(vtu_file, float(epsilon))
        rng = np.random.default_rng(seed)
        perturbed_f = perturb_values(f_values, float(epsilon), rng)
        perturbed_g = perturb_values(g_values, float(epsilon), rng)
        remaining_count, remaining_examples = find_degenerate_triangles(grid, perturbed_f, perturbed_g)
        attempts.append(f"epsilon={epsilon_label(float(epsilon))},remaining={remaining_count}")

        if remaining_count == 0:
            out_grid = read_vtu(vtu_file)
            replace_scalar_array(out_grid, FV99_FNAME, perturbed_f)
            replace_scalar_array(out_grid, FV99_GNAME, perturbed_g)
            write_vtu_atomic(out_grid, vtu_file)
            removed = cleanup_derived_files(vtu_file.stem)
            return "perturbed", (
                f"{vtu_file}\tstatus=perturbed\tepsilon={epsilon_label(float(epsilon))}"
                f"\tinitial_degenerate_triangles={initial_count}"
                f"\tattempts={';'.join(attempts)}"
                f"\tseed={seed if seed is not None else '-'}"
                f"\tcleaned_outputs={','.join(str(path) for path in removed) if removed else '-'}"
                f"\texamples={format_examples(examples)}"
            )

    removed = cleanup_derived_files(vtu_file.stem)
    if DEGENERACY_PREPROCESS_DELETE_UNFIXED:
        vtu_file.unlink(missing_ok=True)
        status = "deleted"
    else:
        status = "unfixed"

    return status, (
        f"{vtu_file}\tstatus={status}\tinitial_degenerate_triangles={initial_count}"
        f"\tattempts={';'.join(attempts)}"
        f"\tcleaned_outputs={','.join(str(path) for path in removed) if removed else '-'}"
        f"\texamples={format_examples(examples)}"
    )


def preprocess_vtu_degeneracy_stage() -> None:
    if not VTU_DIR.exists():
        raise FileNotFoundError(f"VTU directory not found: {VTU_DIR}")

    DEGENERACY_PREPROCESS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    counts = {"ok": 0, "perturbed": 0, "deleted": 0, "unfixed": 0, "error": 0}
    lines = [
        f"# Degeneracy preprocessing log",
        f"# timestamp={time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# vtu_dir={VTU_DIR}",
        f"# fName={FV99_FNAME}",
        f"# gName={FV99_GNAME}",
        f"# epsilons={','.join(epsilon_label(float(value)) for value in DEGENERACY_PREPROCESS_EPSILONS)}",
        f"# orientation_tolerance={DEGENERACY_PREPROCESS_ORIENTATION_TOLERANCE}",
        f"# delete_unfixed={DEGENERACY_PREPROCESS_DELETE_UNFIXED}",
    ]

    print(f"Checking VTU degeneracy for {len(vtu_files)} files in {VTU_DIR}")
    errors: list[str] = []
    for index, vtu_file in enumerate(vtu_files, start=1):
        try:
            status, line = process_vtu_file(vtu_file)
        except Exception as exc:  # keep checking other files while logging the failure.
            status = "error"
            line = f"{vtu_file}\tstatus=error\terror={type(exc).__name__}: {exc}"
            errors.append(line)
        counts[status] = counts.get(status, 0) + 1
        lines.append(line)
        print(f"[{index}/{len(vtu_files)}] {status}: {vtu_file.name}", flush=True)

    lines.append(
        "# summary "
        + " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    DEGENERACY_PREPROCESS_LOG_FILE.write_text("\n".join(lines) + "\n")

    print(
        "Degeneracy preprocessing summary: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    print(f"Degeneracy log: {DEGENERACY_PREPROCESS_LOG_FILE}")

    if errors:
        raise RuntimeError(
            f"degeneracy preprocessing had {len(errors)} file error(s); see {DEGENERACY_PREPROCESS_LOG_FILE}"
        )


if __name__ == "__main__":
    preprocess_vtu_degeneracy_stage()
