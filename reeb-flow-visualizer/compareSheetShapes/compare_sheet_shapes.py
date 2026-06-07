#!/usr/bin/env python3
"""
Compare sheet shapes across timesteps without using PNGs.

This is a standalone tool. It does not modify the existing pipeline.

What it does:
  1. discovers matching .rs / .rsi / .vtu files by timestep stem
  2. exports sheet geometry with fv99 into a local cache when needed
  3. preprocesses every sheet into reusable descriptors
  4. rasterizes sheet polygons into masks on a shared global grid
  5. computes pairwise similarity scores for adjacent timesteps
  6. writes cached descriptors and match results to disk

The goal is to support a future correspondence Sankey that uses shape
matching rather than raw vertex overlap alone.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
from matplotlib.path import Path as MplPath

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SHEET_RENDERER_DIR = REPO_ROOT / "SheetRenderer"

if str(SHEET_RENDERER_DIR) not in sys.path:
    sys.path.insert(0, str(SHEET_RENDERER_DIR))

from render_rs_sheets import (  # noqa: E402
    export_sheet_vtp,
    polygon_area,
    read_sheet_vtp,
    sheet_areas,
)


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

from common import (
    BASE_DIR,
    FV99,
    RESERVE_CORES,
    RSI_DIR,
    RS_DIR,
    SHAPE_SCORE_DEFAULT_WEIGHTS,
    SHAPE_MATCHING_SKIPPED_LOG_FILE,
    TOP_N_SHEETS,
    TTK_BUILD_LIB_DIR,
    TTK_INSTALL_LIB_DIR,
    VTU_DIR,
    VTK_LIB_DIR,
)

DEFAULT_LIBRARY_PATH = os.pathsep.join(
    str(path)
    for path in (VTK_LIB_DIR, TTK_BUILD_LIB_DIR, TTK_INSTALL_LIB_DIR)
    if path.exists()
)

DEFAULT_WORKERS = max(1, (os.cpu_count() or 1) - RESERVE_CORES)

GRID_SIZE = 256
STORAGE_ROOT = BASE_DIR / "compareSheetShapesCache"
CACHE_DIR = STORAGE_ROOT / "cache"
RESULTS_DIR = STORAGE_ROOT / "results"

RSI_JSON_CACHE_DIR = CACHE_DIR / "rsi_json"
VTP_CACHE_DIR = CACHE_DIR / "vtp"
VTP_EXPORT_LOG_DIR = CACHE_DIR / "vtp_export_logs"
TIMESTEP_CACHE_DIR = CACHE_DIR / "timesteps"
MATCH_CACHE_DIR = CACHE_DIR / "matches"

GLOBAL_BOUNDS_FILE = CACHE_DIR / "global_bounds.json"
MANIFEST_FILE = CACHE_DIR / "manifest.json"
TIMESTEP_INDEX_FILE = CACHE_DIR / "timestep_index.json"

MATCHES_FILE = RESULTS_DIR / "sheet_shape_matches.json"
SUMMARY_FILE = RESULTS_DIR / "sheet_shape_summary.json"


@dataclass(frozen=True)
class RSIData:
    is_vertex_singular: list[bool]
    sheet_area: dict[int, float]
    sheet_regular_vertices: dict[int, list[int]]


@dataclass(frozen=True)
class TimestepInput:
    index: int
    label: str
    stem: str
    rs: Path
    rsi: Path
    vtu: Path


@dataclass(frozen=True)
class SheetDescriptor:
    sheet_id: int
    rank: int
    area: float
    num_vertices: int
    vertices: tuple[int, ...]
    bbox: tuple[float, float, float, float]
    centroid: tuple[float, float]


@dataclass(frozen=True)
class TimestepDescriptors:
    timestep_index: int
    label: str
    stem: str
    global_bounds: tuple[float, float, float, float]
    grid_size: int
    sheets: tuple[SheetDescriptor, ...]


def ensure_dirs() -> None:
    for directory in (
        CACHE_DIR,
        RESULTS_DIR,
        RSI_JSON_CACHE_DIR,
        VTP_CACHE_DIR,
        VTP_EXPORT_LOG_DIR,
        TIMESTEP_CACHE_DIR,
        MATCH_CACHE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text)
    tmp_path.replace(path)


def save_npz_atomic(path: Path, **arrays) -> None:
    tmp_path = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(tmp_path, **arrays)
    tmp_path.replace(path)


class BinaryReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        self.offset = 0

    def _read(self, fmt: str):
        import struct

        size = struct.calcsize(fmt)
        if self.offset + size > len(self.data):
            raise ValueError(f"Unexpected end of file while reading {self.path}")
        value = struct.unpack_from(fmt, self.data, self.offset)
        self.offset += size
        return value[0] if len(value) == 1 else value

    def int32(self) -> int:
        return self._read("<i")

    def uint8(self) -> int:
        return self._read("<B")

    def float64(self) -> float:
        return self._read("<d")

    def size(self) -> int:
        return self._read("<Q")


def read_rsi(path: Path) -> RSIData:
    reader = BinaryReader(path)

    is_vertex_singular = [bool(reader.uint8()) for _ in range(reader.size())]

    sheet_area: dict[int, float] = {}
    for _ in range(reader.size()):
        sheet_id = reader.int32()
        area = reader.float64()
        sheet_area[sheet_id] = area

    sheet_regular_vertices: dict[int, list[int]] = {}
    for _ in range(reader.size()):
        sheet_id = reader.int32()
        sheet_regular_vertices[sheet_id] = [reader.int32() for _ in range(reader.size())]

    return RSIData(
        is_vertex_singular=is_vertex_singular,
        sheet_area=sheet_area,
        sheet_regular_vertices=sheet_regular_vertices,
    )


def timestep_number(path: Path) -> int | None:
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else None


def timestep_label(path: Path) -> str:
    number = timestep_number(path)
    return str(number) if number is not None else path.stem


def timestep_sort_key(path: Path):
    number = timestep_number(path)
    return (0, number) if number is not None else (1, path.stem)


def discover_timesteps() -> list[TimestepInput]:
    rs_files = {path.stem: path for path in RS_DIR.glob("*.rs")}
    rsi_files = {path.stem: path for path in RSI_DIR.glob("*.rsi")}
    vtu_files = {path.stem: path for path in VTU_DIR.glob("*.vtu")}

    common = sorted(
        rs_files.keys() & rsi_files.keys() & vtu_files.keys(),
        key=lambda stem: timestep_sort_key(rs_files[stem]),
    )

    timesteps: list[TimestepInput] = []
    for index, stem in enumerate(common):
        timesteps.append(
            TimestepInput(
                index=index,
                label=timestep_label(rs_files[stem]),
                stem=stem,
                rs=rs_files[stem],
                rsi=rsi_files[stem],
                vtu=vtu_files[stem],
            )
        )
    return timesteps


def make_library_path(extra: str | None = None) -> str:
    parts = [p for p in [DEFAULT_LIBRARY_PATH, extra] if p]
    return os.pathsep.join(parts)


def vtp_export_log_path(timestep: TimestepInput) -> Path:
    return VTP_EXPORT_LOG_DIR / f"{timestep.stem}.export.log"


def cached_vtp_path(timestep: TimestepInput) -> Path:
    return VTP_CACHE_DIR / f"{timestep.stem}.sheets.vtp"


def export_geometry_if_needed(
    timestep: TimestepInput,
    library_path: str,
    log_path: Path | None = None,
) -> Path:
    out_vtp = cached_vtp_path(timestep)
    if out_vtp.exists():
        return out_vtp

    tmp_vtp = out_vtp.with_name(f"{out_vtp.stem}.tmp{out_vtp.suffix}")
    if tmp_vtp.exists():
        tmp_vtp.unlink()
    try:
        export_sheet_vtp(FV99, timestep.vtu, timestep.rs, tmp_vtp, library_path, log_path=log_path)
        tmp_vtp.replace(out_vtp)
    except Exception:
        if tmp_vtp.exists():
            tmp_vtp.unlink()
        raise
    return out_vtp


def export_failure_details(exc: Exception) -> tuple[str, str]:
    returncode = getattr(exc, "returncode", None)
    if isinstance(returncode, int):
        if returncode < 0:
            status = f"returncode={returncode} signal={-returncode}"
        else:
            status = f"returncode={returncode}"
    else:
        status = f"error_type={type(exc).__name__}"
    command = getattr(exc, "cmd", None)
    command_text = " ".join(str(part) for part in command) if command else "-"
    return status, command_text


def exportable_timestep_worker(timestep: TimestepInput, library_path: str) -> dict:
    log_path = vtp_export_log_path(timestep)
    try:
        vtp_path = export_geometry_if_needed(timestep, library_path, log_path=log_path)
        sheet_polygons = read_sheet_vtp(vtp_path)
        return {
            "ok": True,
            "stem": timestep.stem,
            "point_count": len(sheet_polygons.points),
            "polygon_count": len(sheet_polygons.polygons),
            "vtp": str(vtp_path),
        }
    except Exception as exc:
        bad_vtp = cached_vtp_path(timestep)
        if bad_vtp.exists():
            try:
                bad_vtp.unlink()
            except OSError:
                pass
        status, command_text = export_failure_details(exc)
        return {
            "ok": False,
            "stem": timestep.stem,
            "status": status,
            "error": str(exc),
            "command": command_text,
            "log": str(log_path),
        }


def write_skipped_timesteps_log(skipped: list[dict]) -> None:
    SHAPE_MATCHING_SKIPPED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not skipped:
        write_text_atomic(SHAPE_MATCHING_SKIPPED_LOG_FILE, "# No timesteps skipped by compare-sheets VTP export.\n")
        return

    lines = [
        "# Timesteps skipped because fv99 could not export sheet geometry for compare-sheets.\n",
        "# Skipped timesteps are removed before adjacent range-shape matches are built.\n",
    ]
    for item in skipped:
        lines.append(
            "\t".join(
                [
                    str(item["vtu"]),
                    "status=skipped_vtp_export_failed",
                    str(item.get("failure_status", "-")),
                    f"rs={item['rs']}",
                    f"log={item.get('log', '-')}",
                    f"error={item.get('error', '-')}",
                ]
            )
            + "\n"
        )
    write_text_atomic(SHAPE_MATCHING_SKIPPED_LOG_FILE, "".join(lines))


def filter_exportable_timesteps(
    timesteps: list[TimestepInput],
    library_path: str,
    workers: int,
) -> list[TimestepInput]:
    if not timesteps:
        write_skipped_timesteps_log([])
        return []

    by_stem = {timestep.stem: timestep for timestep in timesteps}
    valid: list[TimestepInput] = []
    skipped: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(exportable_timestep_worker, timestep, library_path): timestep
            for timestep in timesteps
        }
        for i, future in enumerate(as_completed(futures), start=1):
            timestep = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                status, command_text = export_failure_details(exc)
                result = {
                    "ok": False,
                    "stem": timestep.stem,
                    "status": status,
                    "error": str(exc),
                    "command": command_text,
                    "log": str(vtp_export_log_path(timestep)),
                }

            if result.get("ok"):
                valid.append(by_stem[str(result["stem"])])
                print(
                    f"[shape export {i}/{len(futures)}] ok polygons={result['polygon_count']}: {result['stem']}",
                    flush=True,
                )
            else:
                skipped.append(
                    {
                        "stem": timestep.stem,
                        "vtu": timestep.vtu,
                        "rs": timestep.rs,
                        "failure_status": result.get("status", "-"),
                        "error": result.get("error", "-"),
                        "command": result.get("command", "-"),
                        "log": result.get("log", str(vtp_export_log_path(timestep))),
                    }
                )
                print(
                    f"[shape export {i}/{len(futures)}] skipped {result.get('status', '-')}: {timestep.stem}",
                    flush=True,
                )

    valid.sort(key=lambda timestep: timestep.index)
    skipped.sort(key=lambda item: timestep_sort_key(Path(str(item["vtu"]))))
    write_skipped_timesteps_log(skipped)
    if skipped:
        print(f"Skipped {len(skipped)} timestep(s); see {SHAPE_MATCHING_SKIPPED_LOG_FILE}", flush=True)
    return valid


def centroid_and_bbox(points: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], tuple[float, float, float, float]]:
    pts = list(points)
    if not pts:
        return (0.0, 0.0), (0.0, 0.0, 0.0, 0.0)

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return ((sum(xs) / len(xs), sum(ys) / len(ys)), (min(xs), min(ys), max(xs), max(ys)))


def sheet_centroid(sheet_points: list[list[tuple[float, float]]]) -> tuple[float, float]:
    weighted_x = 0.0
    weighted_y = 0.0
    total_area = 0.0
    for polygon in sheet_points:
        area = polygon_area(polygon)
        if area <= 0.0:
            continue
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)
        weighted_x += cx * area
        weighted_y += cy * area
        total_area += area
    if total_area <= 0.0:
        flat = [point for polygon in sheet_points for point in polygon]
        return centroid_and_bbox(flat)[0]
    return weighted_x / total_area, weighted_y / total_area


def sheet_bbox(sheet_points: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    flat = [point for polygon in sheet_points for point in polygon]
    return centroid_and_bbox(flat)[1]


def build_sheet_descriptors(
    timestep: TimestepInput,
    global_bounds: tuple[float, float, float, float],
    grid_size: int,
    library_path: str,
) -> TimestepDescriptors:
    vtp_path = export_geometry_if_needed(timestep, library_path)
    sheet_polygons = read_sheet_vtp(vtp_path)
    areas = sheet_areas(sheet_polygons)
    rsi = read_rsi(timestep.rsi)

    points_by_sheet: dict[int, list[list[tuple[float, float]]]] = {}
    for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
        points = [sheet_polygons.points[i] for i in polygon]
        points_by_sheet.setdefault(sheet_id, []).append(points)

    finite_areas = [
        (sheet_id, area)
        for sheet_id, area in areas.items()
        if math.isfinite(area)
    ]
    finite_areas.sort(key=lambda item: item[1], reverse=True)
    top_sheet_ids = [sheet_id for sheet_id, _ in finite_areas[:TOP_N_SHEETS]]

    descriptors: list[SheetDescriptor] = []
    for rank, sheet_id in enumerate(top_sheet_ids, start=1):
        polygons = points_by_sheet.get(sheet_id, [])
        verts = tuple(rsi.sheet_regular_vertices.get(sheet_id, ()))
        area = float(areas.get(sheet_id, 0.0))
        centroid = sheet_centroid(polygons)
        bbox = sheet_bbox(polygons)
        descriptors.append(
            SheetDescriptor(
                sheet_id=sheet_id,
                rank=rank,
                area=area,
                num_vertices=len(verts),
                vertices=verts,
                bbox=bbox,
                centroid=centroid,
            )
        )

    return TimestepDescriptors(
        timestep_index=timestep.index,
        label=timestep.label,
        stem=timestep.stem,
        global_bounds=global_bounds,
        grid_size=grid_size,
        sheets=tuple(descriptors),
    )


def points_to_mask(
    polygons: list[list[tuple[float, float]]],
    global_bounds: tuple[float, float, float, float],
    grid_size: int,
) -> np.ndarray:
    xmin, ymin, xmax, ymax = global_bounds
    if xmax <= xmin or ymax <= ymin:
        return np.zeros((grid_size, grid_size), dtype=np.uint8)

    xs = np.linspace(xmin, xmax, grid_size, endpoint=False) + (xmax - xmin) / grid_size / 2.0
    ys = np.linspace(ymin, ymax, grid_size, endpoint=False) + (ymax - ymin) / grid_size / 2.0
    xx, yy = np.meshgrid(xs, ys)
    points = np.column_stack([xx.ravel(), yy.ravel()])

    mask = np.zeros(points.shape[0], dtype=bool)
    for polygon in polygons:
        if len(polygon) < 3:
            continue
        path = MplPath(polygon)
        mask |= path.contains_points(points)
    return mask.reshape((grid_size, grid_size)).astype(np.uint8)


def collect_global_bounds(
    timesteps: list[TimestepInput],
    library_path: str,
    workers: int,
) -> tuple[float, float, float, float]:
    bounds: list[float] | None = None
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(collect_bounds_worker, timestep, library_path): timestep
            for timestep in timesteps
        }
        for i, future in enumerate(as_completed(futures), start=1):
            local = future.result()
            if local is None:
                print(f"[bounds {i}/{len(futures)}] empty: {futures[future].stem}", flush=True)
                continue
            if bounds is None:
                bounds = list(local)
            else:
                bounds[0] = min(bounds[0], local[0])
                bounds[1] = min(bounds[1], local[1])
                bounds[2] = max(bounds[2], local[2])
                bounds[3] = max(bounds[3], local[3])
            print(f"[bounds {i}/{len(futures)}] {futures[future].stem}", flush=True)
    if bounds is None:
        return (0.0, 0.0, 1.0, 1.0)
    return tuple(bounds)


def save_global_bounds(bounds: tuple[float, float, float, float]) -> None:
    write_text_atomic(GLOBAL_BOUNDS_FILE, json.dumps({"global_bounds": bounds}, indent=2))


def load_global_bounds() -> tuple[float, float, float, float] | None:
    if not GLOBAL_BOUNDS_FILE.exists():
        return None
    data = json.loads(GLOBAL_BOUNDS_FILE.read_text())
    bounds = data.get("global_bounds")
    if not bounds:
        return None
    return tuple(bounds)  # type: ignore[return-value]


def save_timestep_cache(
    timestep: TimestepInput,
    descriptors: TimestepDescriptors,
    masks: dict[int, np.ndarray],
) -> Path:
    cache_json = TIMESTEP_CACHE_DIR / f"{timestep.stem}.json"
    cache_npz = TIMESTEP_CACHE_DIR / f"{timestep.stem}.npz"

    payload = {
        "timestep_index": descriptors.timestep_index,
        "label": descriptors.label,
        "stem": descriptors.stem,
        "global_bounds": list(descriptors.global_bounds),
        "grid_size": descriptors.grid_size,
        "top_n_sheets": TOP_N_SHEETS,
        "sheets": [
            {
                "sheet_id": sheet.sheet_id,
                "rank": sheet.rank,
                "area": sheet.area,
                "num_vertices": sheet.num_vertices,
                "vertices": list(sheet.vertices),
                "bbox": list(sheet.bbox),
                "centroid": list(sheet.centroid),
            }
            for sheet in descriptors.sheets
        ],
    }
    write_text_atomic(cache_json, json.dumps(payload, indent=2))

    save_npz_atomic(
        cache_npz,
        sheet_ids=np.array([sheet.sheet_id for sheet in descriptors.sheets], dtype=np.int32),
        areas=np.array([sheet.area for sheet in descriptors.sheets], dtype=np.float64),
        num_vertices=np.array([sheet.num_vertices for sheet in descriptors.sheets], dtype=np.int32),
        vertices=np.array([np.array(sheet.vertices, dtype=np.int32) for sheet in descriptors.sheets], dtype=object),
        masks=np.array([masks[sheet.sheet_id] for sheet in descriptors.sheets], dtype=np.uint8),
    )
    return cache_json


def load_timestep_cache(stem: str) -> tuple[TimestepDescriptors, dict[int, np.ndarray]]:
    cache_json = TIMESTEP_CACHE_DIR / f"{stem}.json"
    cache_npz = TIMESTEP_CACHE_DIR / f"{stem}.npz"
    data = json.loads(cache_json.read_text())
    npz = np.load(cache_npz, allow_pickle=True)

    sheets = []
    sheet_ids = [int(x) for x in npz["sheet_ids"].tolist()]
    masks = npz["masks"]

    for idx, sheet_json in enumerate(data["sheets"]):
        sheets.append(
            SheetDescriptor(
                sheet_id=int(sheet_json["sheet_id"]),
                rank=int(sheet_json["rank"]),
                area=float(sheet_json["area"]),
                num_vertices=int(sheet_json["num_vertices"]),
                vertices=tuple(int(v) for v in sheet_json["vertices"]),
                bbox=tuple(float(v) for v in sheet_json["bbox"]),
                centroid=tuple(float(v) for v in sheet_json["centroid"]),
            )
        )

    descriptors = TimestepDescriptors(
        timestep_index=int(data["timestep_index"]),
        label=str(data["label"]),
        stem=str(data["stem"]),
        global_bounds=tuple(float(v) for v in data["global_bounds"]),
        grid_size=int(data["grid_size"]),
        sheets=tuple(sheets),
    )

    mask_map = {sheet_ids[i]: masks[i] for i in range(len(sheet_ids))}
    return descriptors, mask_map


def bounds_are_close(a: Iterable[float], b: Iterable[float]) -> bool:
    a_values = list(a)
    b_values = list(b)
    if len(a_values) != len(b_values):
        return False
    return all(
        math.isclose(float(x), float(y), rel_tol=0.0, abs_tol=1e-12)
        for x, y in zip(a_values, b_values)
    )


def timestep_cache_is_valid(stem: str, global_bounds: tuple[float, float, float, float] | None = None) -> bool:
    cache_json = TIMESTEP_CACHE_DIR / f"{stem}.json"
    cache_npz = TIMESTEP_CACHE_DIR / f"{stem}.npz"
    if not cache_json.exists() or not cache_npz.exists():
        return False
    try:
        data = json.loads(cache_json.read_text())
    except Exception:
        return False
    if not (
        data.get("grid_size") == GRID_SIZE
        and data.get("top_n_sheets") == TOP_N_SHEETS
        and data.get("stem") == stem
    ):
        return False
    if global_bounds is not None and not bounds_are_close(data.get("global_bounds", []), global_bounds):
        return False
    return True


def pair_cache_path(source_stem: str, target_stem: str) -> Path:
    return MATCH_CACHE_DIR / f"{source_stem}__{target_stem}.json"


def save_pair_cache(result: dict) -> None:
    source_stem = result["source_stem"]
    target_stem = result["target_stem"]
    path = pair_cache_path(source_stem, target_stem)
    payload = dict(result)
    payload["grid_size"] = GRID_SIZE
    payload["top_n_sheets"] = TOP_N_SHEETS
    payload["global_bounds"] = list(payload.get("global_bounds", []))
    write_text_atomic(path, json.dumps(payload, indent=2))


def load_pair_cache(source_stem: str, target_stem: str) -> dict | None:
    path = pair_cache_path(source_stem, target_stem)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if data.get("grid_size") != GRID_SIZE or data.get("top_n_sheets") != TOP_N_SHEETS:
        return None
    if data.get("source_stem") != source_stem or data.get("target_stem") != target_stem:
        return None
    if "global_bounds" not in data:
        return None
    return data


def cache_timestep_worker(
    timestep: TimestepInput,
    global_bounds: tuple[float, float, float, float],
    library_path: str,
) -> TimestepDescriptors:
    timestep, descriptors, masks = build_masks_for_timestep(timestep, global_bounds, library_path)
    save_timestep_cache(timestep, descriptors, masks)
    return descriptors


def collect_bounds_worker(
    timestep: TimestepInput,
    library_path: str,
) -> tuple[float, float, float, float] | None:
    vtp_path = export_geometry_if_needed(timestep, library_path)
    sheet_polygons = read_sheet_vtp(vtp_path)
    pts = sheet_polygons.points
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def build_masks_for_timestep(
    timestep: TimestepInput,
    global_bounds: tuple[float, float, float, float],
    library_path: str,
) -> tuple[TimestepInput, TimestepDescriptors, dict[int, np.ndarray]]:
    vtp_path = export_geometry_if_needed(timestep, library_path)
    sheet_polygons = read_sheet_vtp(vtp_path)
    areas = sheet_areas(sheet_polygons)
    rsi = read_rsi(timestep.rsi)

    points_by_sheet: dict[int, list[list[tuple[float, float]]]] = {}
    for polygon, sheet_id in zip(sheet_polygons.polygons, sheet_polygons.sheet_ids, strict=True):
        points = [sheet_polygons.points[i] for i in polygon]
        points_by_sheet.setdefault(sheet_id, []).append(points)

    finite_areas = [
        (sheet_id, area)
        for sheet_id, area in areas.items()
        if math.isfinite(area)
    ]
    finite_areas.sort(key=lambda item: item[1], reverse=True)
    top_sheet_ids = [sheet_id for sheet_id, _ in finite_areas[:TOP_N_SHEETS]]

    descriptors: list[SheetDescriptor] = []
    masks: dict[int, np.ndarray] = {}
    for rank, sheet_id in enumerate(top_sheet_ids, start=1):
        polygons = points_by_sheet.get(sheet_id, [])
        verts = tuple(rsi.sheet_regular_vertices.get(sheet_id, ()))
        area = float(areas.get(sheet_id, 0.0))
        centroid = sheet_centroid(polygons)
        bbox = sheet_bbox(polygons)
        descriptors.append(
            SheetDescriptor(
                sheet_id=sheet_id,
                rank=rank,
                area=area,
                num_vertices=len(verts),
                vertices=verts,
                bbox=bbox,
                centroid=centroid,
            )
        )
        masks[sheet_id] = points_to_mask(polygons, global_bounds, GRID_SIZE)

    timestep_desc = TimestepDescriptors(
        timestep_index=timestep.index,
        label=timestep.label,
        stem=timestep.stem,
        global_bounds=global_bounds,
        grid_size=GRID_SIZE,
        sheets=tuple(descriptors),
    )
    return timestep, timestep_desc, masks


def build_cache(timesteps: list[TimestepInput], workers: int, library_path: str, recompute_bounds: bool = False) -> tuple[tuple[float, float, float, float], list[TimestepDescriptors]]:
    cached_bounds = None if recompute_bounds else load_global_bounds()
    if cached_bounds is None:
        bounds = collect_global_bounds(timesteps, library_path, workers)
        save_global_bounds(bounds)
    else:
        bounds = cached_bounds

    results: list[TimestepDescriptors] = []
    missing_timesteps: list[TimestepInput] = []

    for timestep in timesteps:
        if timestep_cache_is_valid(timestep.stem, bounds):
            descriptors, _masks = load_timestep_cache(timestep.stem)
            results.append(descriptors)
            print(f"[cache existing] {descriptors.stem}", flush=True)
        else:
            missing_timesteps.append(timestep)

    if missing_timesteps:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(cache_timestep_worker, timestep, bounds, library_path): timestep
                for timestep in missing_timesteps
            }
            for i, future in enumerate(as_completed(futures), start=1):
                descriptors = future.result()
                results.append(descriptors)
                print(f"[cache {i}/{len(futures)}] {descriptors.stem}", flush=True)

    results.sort(key=lambda item: item.timestep_index)
    manifest = {
        "num_timesteps": len(results),
        "grid_size": GRID_SIZE,
        "top_n_sheets": TOP_N_SHEETS,
        "global_bounds": list(bounds),
        "timesteps": [item.stem for item in results],
    }
    write_text_atomic(MANIFEST_FILE, json.dumps(manifest, indent=2))
    write_text_atomic(TIMESTEP_INDEX_FILE, json.dumps({item.stem: item.timestep_index for item in results}, indent=2))
    return bounds, results



def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def centroid_similarity(
    a: tuple[float, float],
    b: tuple[float, float],
    global_bounds: tuple[float, float, float, float],
) -> float:
    xmin, ymin, xmax, ymax = global_bounds
    diag = math.hypot(xmax - xmin, ymax - ymin)
    if diag <= 0.0:
        return 1.0
    dist = math.hypot(a[0] - b[0], a[1] - b[1])
    return math.exp(-dist / diag)


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    a_bool = a.astype(bool)
    b_bool = b.astype(bool)
    inter = np.logical_and(a_bool, b_bool).sum()
    union = np.logical_or(a_bool, b_bool).sum()
    return float(inter / union) if union else 0.0


def shape_score(source: SheetDescriptor, target: SheetDescriptor, source_mask: np.ndarray, target_mask: np.ndarray, global_bounds: tuple[float, float, float, float]) -> dict[str, float]:
    geom = mask_iou(source_mask, target_mask)
    area_ratio = min(source.area, target.area) / max(source.area, target.area) if max(source.area, target.area) > 0 else 0.0
    bb_iou = bbox_iou(source.bbox, target.bbox)
    centroid = centroid_similarity(source.centroid, target.centroid, global_bounds)

    final = (
        SHAPE_SCORE_DEFAULT_WEIGHTS["shape_iou"] * geom
        + SHAPE_SCORE_DEFAULT_WEIGHTS["area_ratio"] * area_ratio
        + SHAPE_SCORE_DEFAULT_WEIGHTS["bbox_iou"] * bb_iou
        + SHAPE_SCORE_DEFAULT_WEIGHTS["centroid_similarity"] * centroid
    )

    return {
        "final_score": final,
        "shape_iou": geom,
        "area_ratio": area_ratio,
        "bbox_iou": bb_iou,
        "centroid_similarity": centroid,
    }


def load_cached_timestep(stem: str) -> tuple[TimestepDescriptors, dict[int, np.ndarray]]:
    return load_timestep_cache(stem)


def compare_pair(
    source_stem: str,
    target_stem: str,
    global_bounds: tuple[float, float, float, float],
) -> dict:
    source_desc, source_masks = load_cached_timestep(source_stem)
    target_desc, target_masks = load_cached_timestep(target_stem)

    pair_scores = []
    for source_sheet in source_desc.sheets:
        source_mask = source_masks[source_sheet.sheet_id]
        for target_sheet in target_desc.sheets:
            target_mask = target_masks[target_sheet.sheet_id]
            metrics = shape_score(source_sheet, target_sheet, source_mask, target_mask, global_bounds)
            if metrics["final_score"] <= 0.0:
                continue
            pair_scores.append(
                {
                    "source_sheet_id": source_sheet.sheet_id,
                    "target_sheet_id": target_sheet.sheet_id,
                    "source_rank": source_sheet.rank,
                    "target_rank": target_sheet.rank,
                    "source_area": source_sheet.area,
                    "target_area": target_sheet.area,
                    "source_num_vertices": source_sheet.num_vertices,
                    "target_num_vertices": target_sheet.num_vertices,
                    **metrics,
                }
            )

    pair_scores.sort(key=lambda item: item["final_score"], reverse=True)

    return {
        "source_timestep_index": source_desc.timestep_index,
        "source_label": source_desc.label,
        "source_stem": source_desc.stem,
        "target_timestep_index": target_desc.timestep_index,
        "target_label": target_desc.label,
        "target_stem": target_desc.stem,
        "global_bounds": list(global_bounds),
        "pair_count": len(pair_scores),
        "matches": pair_scores,
    }


def manifest_matches_timesteps(timesteps: list[TimestepInput]) -> bool:
    if not MANIFEST_FILE.exists():
        return False
    try:
        manifest = json.loads(MANIFEST_FILE.read_text())
    except Exception:
        return False
    return (
        manifest.get("grid_size") == GRID_SIZE
        and manifest.get("top_n_sheets") == TOP_N_SHEETS
        and manifest.get("timesteps") == [timestep.stem for timestep in timesteps]
    )


def compare_all_pairs(
    timesteps: list[TimestepInput],
    workers: int,
    global_bounds: tuple[float, float, float, float],
    library_path: str,
    rerun_cache: bool = False,
) -> dict:
    if rerun_cache:
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        ensure_dirs()

    ensure_dirs()

    stems = [t.stem for t in timesteps]
    manifest_ok = manifest_matches_timesteps(timesteps)

    if (
        not manifest_ok
        or not TIMESTEP_CACHE_DIR.exists()
        or not list(TIMESTEP_CACHE_DIR.glob("*.json"))
        or not list(TIMESTEP_CACHE_DIR.glob("*.npz"))
    ):
        print("Building cache...", flush=True)
        global_bounds, _descriptors = build_cache(
            timesteps,
            workers,
            library_path,
            recompute_bounds=not manifest_ok,
        )

    adjacent_pairs = list(zip(stems, stems[1:]))

    results = []
    missing_pairs: list[tuple[str, str]] = []

    for source_stem, target_stem in adjacent_pairs:
        cached = load_pair_cache(source_stem, target_stem)
        if cached is not None and cached.get("global_bounds") == list(global_bounds):
            results.append(cached)
            print(f"[match existing] {cached['source_label']} -> {cached['target_label']}", flush=True)
        else:
            missing_pairs.append((source_stem, target_stem))

    if missing_pairs:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(compare_pair, source_stem, target_stem, global_bounds): (source_stem, target_stem)
                for source_stem, target_stem in missing_pairs
            }
            for i, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                save_pair_cache(result)
                results.append(result)
                print(
                    f"[match {i}/{len(futures)}] {result['source_label']} -> {result['target_label']}",
                    flush=True,
                )

    results.sort(key=lambda item: item["source_timestep_index"])

    payload = {
        "num_timesteps": len(timesteps),
        "grid_size": GRID_SIZE,
        "top_n_sheets": TOP_N_SHEETS,
        "combined_score_weights": SHAPE_SCORE_DEFAULT_WEIGHTS,
        "global_bounds": list(global_bounds),
        "pairwise_matches": results,
    }
    write_text_atomic(MATCHES_FILE, json.dumps(payload, indent=2))

    summary = {
        "num_pairs": len(results),
        "max_pair_count": max((item["pair_count"] for item in results), default=0),
        "avg_pair_count": float(np.mean([item["pair_count"] for item in results])) if results else 0.0,
        "combined_score_weights": SHAPE_SCORE_DEFAULT_WEIGHTS,
        "top_pairs_by_match_count": sorted(
            (
                {
                    "source_label": item["source_label"],
                    "target_label": item["target_label"],
                    "pair_count": item["pair_count"],
                    "max_score": item["matches"][0]["final_score"] if item["matches"] else 0.0,
                    "top_matches": item["matches"][:10],
                }
                for item in results
            ),
            key=lambda x: (x["pair_count"], x["max_score"]),
            reverse=True,
        )[:20],
    }
    write_text_atomic(SUMMARY_FILE, json.dumps(summary, indent=2))
    return payload


def clear_cache() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    if RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)


def main(argv: list[str] | None = None) -> int:
    global TOP_N_SHEETS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel workers to use.")
    parser.add_argument("--rebuild-cache", action="store_true", help="Rebuild all caches from scratch.")
    parser.add_argument("--clear-cache", action="store_true", help="Delete cache and results before running.")
    parser.add_argument("--library-path", help="Extra LD_LIBRARY_PATH entries for fv99.")
    parser.add_argument("--top", type=int, default=TOP_N_SHEETS, help="Top N sheets per timestep.")
    args = parser.parse_args(argv)

    TOP_N_SHEETS = args.top

    if args.clear_cache:
        clear_cache()

    ensure_dirs()
    timesteps = discover_timesteps()
    if not timesteps:
        raise SystemExit("No matching rs/rsi/vtu timestep triplets were found.")

    library_path = make_library_path(args.library_path)
    if args.rebuild_cache:
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        ensure_dirs()

    timesteps = filter_exportable_timesteps(timesteps, library_path, args.workers)
    if len(timesteps) < 2:
        raise SystemExit("Fewer than two timesteps can export sheet geometry for compare-sheets.")

    manifest_ok = manifest_matches_timesteps(timesteps)
    if args.rebuild_cache:
        bounds, _descriptors = build_cache(timesteps, args.workers, library_path, recompute_bounds=True)
    elif not manifest_ok:
        bounds, _descriptors = build_cache(timesteps, args.workers, library_path, recompute_bounds=True)
    else:
        cached = load_global_bounds()
        if cached is None:
            bounds, _descriptors = build_cache(timesteps, args.workers, library_path, recompute_bounds=True)
        else:
            bounds = cached

    compare_all_pairs(timesteps, args.workers, bounds, library_path, rerun_cache=False)

    print(f"Wrote matches: {MATCHES_FILE}")
    print(f"Wrote summary: {SUMMARY_FILE}")
    print(f"Cache directory: {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
