#!/usr/bin/env python3
"""
Generate synthetic time-varying torus datasets as VTU tetrahedral grids.

Each output timestep contains point-data arrays:
  - orb00: implicit torus function
  - orb01: height function, using the global z coordinate

The script creates:
  DATASET_ROOT/torusGrowing/downsampledGrids/step_XXXXX.vtu
  DATASET_ROOT/torusMoving/downsampledGrids/step_XXXXX.vtu
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


# ================= USER SETTINGS =================

DATASET_ROOT = Path("/media/mohit/8tbh/postdoc/timeVaryingReebFeatures")
GROWING_DATASET_NAME = "torusGrowing"
MOVING_DATASET_NAME = "torusMoving"

# Number of grid vertices along x, y, z. Tetrahedra are built from each cube.
GRID_RESOLUTION = (25, 25, 25)

NUM_TIMESTEPS = 25
STEP_START = 0
STEP_STRIDE = 20
STEP_DIGITS = 5

DOMAIN_BOUNDS = (
    (-2.0, 2.0),
    (-2.0, 2.0),
    (-2.0, 2.0),
)

# torusGrowing radii. Both radii increase smoothly over time.
GROWING_MAJOR_RADIUS_START = 0.45
GROWING_MAJOR_RADIUS_END = 0.95
GROWING_MINOR_RADIUS_START = 0.14
GROWING_MINOR_RADIUS_END = 0.32
GROWING_CENTER = (0.0, 0.0, 0.0)

# torusMoving radii and smooth center trajectory.
MOVING_MAJOR_RADIUS = 0.72
MOVING_MINOR_RADIUS = 0.22
MOVING_CENTER_AMPLITUDE = (0.55, 0.40, 0.22)

OVERWRITE_EXISTING = True

# ==================================================


Point = tuple[float, float, float]
ScalarFunction = Callable[[Point, float], tuple[float, float]]


@dataclass(frozen=True)
class GridTopology:
    points: list[Point]
    connectivity: list[int]
    offsets: list[int]
    cell_types: list[int]


TETRA_VTK_CELL_TYPE = 10


def smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def timestep_fraction(index: int, count: int) -> float:
    if count <= 1:
        return 0.0
    return index / float(count - 1)


def step_name(index: int) -> str:
    value = STEP_START + index * STEP_STRIDE
    return f"step_{value:0{STEP_DIGITS}d}.vtu"


def linspace(start: float, end: float, count: int) -> list[float]:
    if count < 2:
        raise ValueError("Each grid-resolution dimension must be at least 2")
    delta = (end - start) / float(count - 1)
    return [start + i * delta for i in range(count)]


def point_id(i: int, j: int, k: int, ny: int, nz: int) -> int:
    return (i * ny + j) * nz + k


def make_grid_topology() -> GridTopology:
    nx, ny, nz = GRID_RESOLUTION
    xs = linspace(*DOMAIN_BOUNDS[0], nx)
    ys = linspace(*DOMAIN_BOUNDS[1], ny)
    zs = linspace(*DOMAIN_BOUNDS[2], nz)

    points = [(x, y, z) for x in xs for y in ys for z in zs]

    # Six-tet decomposition around the local cube diagonal 0-7. This gives
    # matching face diagonals between neighboring grid cubes.
    local_tets = (
        (0, 1, 3, 7),
        (0, 3, 2, 7),
        (0, 2, 6, 7),
        (0, 6, 4, 7),
        (0, 4, 5, 7),
        (0, 5, 1, 7),
    )

    connectivity: list[int] = []
    offsets: list[int] = []
    cell_types: list[int] = []

    for i in range(nx - 1):
        for j in range(ny - 1):
            for k in range(nz - 1):
                cube = (
                    point_id(i, j, k, ny, nz),
                    point_id(i + 1, j, k, ny, nz),
                    point_id(i, j + 1, k, ny, nz),
                    point_id(i + 1, j + 1, k, ny, nz),
                    point_id(i, j, k + 1, ny, nz),
                    point_id(i + 1, j, k + 1, ny, nz),
                    point_id(i, j + 1, k + 1, ny, nz),
                    point_id(i + 1, j + 1, k + 1, ny, nz),
                )
                for tet in local_tets:
                    connectivity.extend(cube[idx] for idx in tet)
                    offsets.append(len(connectivity))
                    cell_types.append(TETRA_VTK_CELL_TYPE)

    return GridTopology(
        points=points,
        connectivity=connectivity,
        offsets=offsets,
        cell_types=cell_types,
    )


def torus_implicit(point: Point, center: Point, major_radius: float, minor_radius: float) -> float:
    x, y, z = point
    cx, cy, cz = center
    radial_distance = math.hypot(x - cx, y - cy)
    return (radial_distance - major_radius) ** 2 + (z - cz) ** 2 - minor_radius ** 2


def growing_fields(point: Point, time_fraction: float) -> tuple[float, float]:
    t = smoothstep(time_fraction)
    major_radius = lerp(GROWING_MAJOR_RADIUS_START, GROWING_MAJOR_RADIUS_END, t)
    minor_radius = lerp(GROWING_MINOR_RADIUS_START, GROWING_MINOR_RADIUS_END, t)
    return torus_implicit(point, GROWING_CENTER, major_radius, minor_radius), point[2]


def moving_center(time_fraction: float) -> Point:
    angle = 2.0 * math.pi * time_fraction
    ax, ay, az = MOVING_CENTER_AMPLITUDE
    return (
        ax * math.sin(angle),
        ay * math.sin(angle + math.pi / 3.0),
        az * math.sin(angle + math.pi / 5.0),
    )


def moving_fields(point: Point, time_fraction: float) -> tuple[float, float]:
    center = moving_center(time_fraction)
    return torus_implicit(point, center, MOVING_MAJOR_RADIUS, MOVING_MINOR_RADIUS), point[2]


def format_values(values: Iterable[object], items_per_line: int = 8) -> str:
    lines: list[str] = []
    current: list[str] = []

    for value in values:
        current.append(str(value))
        if len(current) >= items_per_line:
            lines.append(" ".join(current))
            current = []

    if current:
        lines.append(" ".join(current))

    return "\n".join(lines)


def format_float(value: float) -> str:
    return f"{value:.12g}"


def write_vtu(path: Path, topology: GridTopology, scalar_function: ScalarFunction, time_fraction: float) -> None:
    orb00: list[str] = []
    orb01: list[str] = []
    point_values: list[str] = []

    for point in topology.points:
        f_value, g_value = scalar_function(point, time_fraction)
        orb00.append(format_float(f_value))
        orb01.append(format_float(g_value))
        point_values.extend(format_float(coord) for coord in point)

    number_of_points = len(topology.points)
    number_of_cells = len(topology.cell_types)

    text = f"""<?xml version=\"1.0\"?>
<VTKFile type=\"UnstructuredGrid\" version=\"0.1\" byte_order=\"LittleEndian\">
  <UnstructuredGrid>
    <Piece NumberOfPoints=\"{number_of_points}\" NumberOfCells=\"{number_of_cells}\">
      <PointData Scalars=\"orb00\">
        <DataArray type=\"Float64\" Name=\"orb00\" NumberOfComponents=\"1\" format=\"ascii\">
{format_values(orb00, 6)}
        </DataArray>
        <DataArray type=\"Float64\" Name=\"orb01\" NumberOfComponents=\"1\" format=\"ascii\">
{format_values(orb01, 6)}
        </DataArray>
      </PointData>
      <CellData>
      </CellData>
      <Points>
        <DataArray type=\"Float64\" NumberOfComponents=\"3\" format=\"ascii\">
{format_values(point_values, 9)}
        </DataArray>
      </Points>
      <Cells>
        <DataArray type=\"Int64\" Name=\"connectivity\" format=\"ascii\">
{format_values(topology.connectivity, 12)}
        </DataArray>
        <DataArray type=\"Int64\" Name=\"offsets\" format=\"ascii\">
{format_values(topology.offsets, 12)}
        </DataArray>
        <DataArray type=\"UInt8\" Name=\"types\" format=\"ascii\">
{format_values(topology.cell_types, 24)}
        </DataArray>
      </Cells>
    </Piece>
  </UnstructuredGrid>
</VTKFile>
"""

    path.write_text(text)


def generate_dataset(dataset_name: str, scalar_function: ScalarFunction, topology: GridTopology, overwrite: bool) -> None:
    out_dir = DATASET_ROOT / dataset_name / "downsampledGrids"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Writing {dataset_name} to {out_dir}")
    for index in range(NUM_TIMESTEPS):
        path = out_dir / step_name(index)
        if path.exists() and not overwrite:
            print(f"[{index + 1}/{NUM_TIMESTEPS}] skipped existing {path.name}")
            continue

        fraction = timestep_fraction(index, NUM_TIMESTEPS)
        write_vtu(path, topology, scalar_function, fraction)
        print(f"[{index + 1}/{NUM_TIMESTEPS}] wrote {path.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        choices=("both", "growing", "moving"),
        default="both",
        help="Which synthetic dataset to generate.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Skip existing timestep files instead of overwriting them.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overwrite = (OVERWRITE_EXISTING or args.overwrite) and not args.no_overwrite

    print(f"Dataset root: {DATASET_ROOT}")
    print(f"Grid resolution: {GRID_RESOLUTION}")
    print(f"Timesteps: {NUM_TIMESTEPS}")

    topology = make_grid_topology()
    print(f"Grid points: {len(topology.points)}")
    print(f"Tetrahedra: {len(topology.cell_types)}")

    if args.dataset in ("both", "growing"):
        generate_dataset(GROWING_DATASET_NAME, growing_fields, topology, overwrite)

    if args.dataset in ("both", "moving"):
        generate_dataset(MOVING_DATASET_NAME, moving_fields, topology, overwrite)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
