#!/usr/bin/env python3

import json
import math
import struct

from common import (
    EXCLUDE_LOW_SCALAR_VALUES_NEAR_ORIGIN,
    FV99_FNAME,
    FV99_GNAME,
    LOW_SCALAR_ORIGIN_FILTER_LOG_FILE,
    LOW_SCALAR_ORIGIN_THRESHOLDS,
    OUTPUT_DIR,
    RSI_DIR,
    RSI_JSON_DIR,
    RSI_JSON_WARNINGS_LOG_FILE,
    TOP_N_SHEETS,
    VTU_DIR,
)


class BinaryReader:
    def __init__(self, file_obj):
        self.f = file_obj

    def read_exact(self, nbytes):
        data = self.f.read(nbytes)
        if len(data) != nbytes:
            raise EOFError(
                f"Unexpected end of file. Needed {nbytes} bytes, got {len(data)}."
            )
        return data

    def size_t(self):
        return struct.unpack("<Q", self.read_exact(8))[0]

    def uint8(self):
        return struct.unpack("<B", self.read_exact(1))[0]

    def int(self):
        return struct.unpack("<i", self.read_exact(4))[0]

    def double(self):
        return struct.unpack("<d", self.read_exact(8))[0]


def read_rsi(rsi_file):
    with rsi_file.open("rb") as f:
        reader = BinaryReader(f)

        is_vertex_singular = [
            bool(reader.uint8())
            for _ in range(reader.size_t())
        ]

        sheet_area = {
            reader.int(): reader.double()
            for _ in range(reader.size_t())
        }

        sheet_regular_vertices = {}
        for _ in range(reader.size_t()):
            sheet_id = reader.int()
            sheet_regular_vertices[sheet_id] = [
                reader.int()
                for _ in range(reader.size_t())
            ]

        extra_bytes = len(f.read())

    return {
        "rsi_file": str(rsi_file),
        "is_vertex_singular": is_vertex_singular,
        "sheet_area": sheet_area,
        "sheet_regular_vertices": sheet_regular_vertices,
        "extra_bytes": extra_bytes,
    }


def json_safe_number(value):
    return value if math.isfinite(value) else None


def active_regular_vertex_count(is_vertex_singular, excluded_vertices):
    return sum(
        1
        for vertex_id, is_singular in enumerate(is_vertex_singular)
        if not is_singular and vertex_id not in excluded_vertices
    )


def get_top_sheets(rsi_data, excluded_vertices=None):
    excluded_vertices = excluded_vertices or set()
    sheet_area = rsi_data["sheet_area"]
    sheet_vertices = rsi_data["sheet_regular_vertices"]

    finite_areas = [
        (sheet_id, area)
        for sheet_id, area in sheet_area.items()
        if math.isfinite(area)
    ]

    finite_areas.sort(key=lambda item: item[1], reverse=True)

    top_sheets = []
    for rank, (sheet_id, area) in enumerate(finite_areas[:TOP_N_SHEETS], start=1):
        original_vertices = sheet_vertices.get(sheet_id, [])
        vertices = [
            vertex
            for vertex in original_vertices
            if vertex not in excluded_vertices
        ]

        top_sheets.append(
            {
                "sheet_id": sheet_id,
                "rank": rank,
                "area": json_safe_number(area),
                "num_vertices": len(vertices),
                "num_vertices_before_low_scalar_filter": len(original_vertices),
                "num_low_scalar_filtered_vertices": len(original_vertices) - len(vertices),
                "vertices": vertices,
            }
        )

    return top_sheets


def low_scalar_threshold(field_name):
    try:
        return abs(float(LOW_SCALAR_ORIGIN_THRESHOLDS.get(field_name, 0.0)))
    except Exception:
        return 0.0


def read_vtu_scalar_array(vtu_file, array_name):
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(vtu_file))
    reader.Update()
    if reader.GetErrorCode():
        raise RuntimeError(f"failed to read VTU file: {vtu_file}")
    array = reader.GetOutput().GetPointData().GetArray(array_name)
    if array is None:
        raise KeyError(f"point-data array {array_name!r} not found in {vtu_file}")
    if array.GetNumberOfComponents() != 1:
        raise ValueError(f"point-data array {array_name!r} in {vtu_file} is not scalar")
    return vtk_to_numpy(array)


def low_scalar_origin_filter_for_rsi(rsi_file, rsi_data):
    if not EXCLUDE_LOW_SCALAR_VALUES_NEAR_ORIGIN:
        return set(), {"enabled": False, "status": "disabled"}, []

    f_threshold = low_scalar_threshold(FV99_FNAME)
    g_threshold = low_scalar_threshold(FV99_GNAME)
    if f_threshold <= 0.0 and g_threshold <= 0.0:
        return set(), {
            "enabled": True,
            "status": "zero_threshold",
            "fields": [FV99_FNAME, FV99_GNAME],
            "thresholds": {FV99_FNAME: f_threshold, FV99_GNAME: g_threshold},
        }, []

    vtu_file = VTU_DIR / f"{rsi_file.stem}.vtu"
    if not vtu_file.exists():
        return set(), {
            "enabled": True,
            "status": "missing_vtu",
            "vtu_file": str(vtu_file),
            "fields": [FV99_FNAME, FV99_GNAME],
            "thresholds": {FV99_FNAME: f_threshold, FV99_GNAME: g_threshold},
        }, [f"missing VTU for low-scalar origin filter: {vtu_file}"]

    f_values = read_vtu_scalar_array(vtu_file, FV99_FNAME)
    g_values = read_vtu_scalar_array(vtu_file, FV99_GNAME)
    vertex_count = len(rsi_data["is_vertex_singular"])
    if len(f_values) != vertex_count or len(g_values) != vertex_count:
        return set(), {
            "enabled": True,
            "status": "vertex_count_mismatch",
            "vtu_file": str(vtu_file),
            "fields": [FV99_FNAME, FV99_GNAME],
            "thresholds": {FV99_FNAME: f_threshold, FV99_GNAME: g_threshold},
            "rsi_vertex_count": vertex_count,
            "f_value_count": len(f_values),
            "g_value_count": len(g_values),
        }, [
            f"low-scalar origin filter skipped for {rsi_file}: "
            f"VTU/RSI vertex count mismatch ({len(f_values)}, {len(g_values)} vs {vertex_count})"
        ]

    excluded = {
        vertex_id
        for vertex_id, (f_value, g_value) in enumerate(zip(f_values, g_values))
        if abs(float(f_value)) <= f_threshold and abs(float(g_value)) <= g_threshold
    }
    excluded_regular = sum(
        1
        for vertex_id in excluded
        if not rsi_data["is_vertex_singular"][vertex_id]
    )
    return excluded, {
        "enabled": True,
        "status": "applied",
        "vtu_file": str(vtu_file),
        "fields": [FV99_FNAME, FV99_GNAME],
        "thresholds": {FV99_FNAME: f_threshold, FV99_GNAME: g_threshold},
        "excluded_vertices": len(excluded),
        "excluded_regular_vertices": excluded_regular,
    }, []


def write_rsi_json(rsi_data, output_file, excluded_vertices=None, filter_info=None):
    excluded_vertices = excluded_vertices or set()
    filter_info = filter_info or {"enabled": False, "status": "disabled"}
    is_vertex_singular = rsi_data["is_vertex_singular"]
    original_regular_vertices = len(is_vertex_singular) - sum(is_vertex_singular)

    data = {
        "rsi_file": rsi_data["rsi_file"],
        "top_n_sheets": TOP_N_SHEETS,
        "num_vertices": len(is_vertex_singular),
        "num_singular_vertices": sum(is_vertex_singular),
        "num_regular_vertices": active_regular_vertex_count(is_vertex_singular, excluded_vertices),
        "num_regular_vertices_before_low_scalar_filter": original_regular_vertices,
        "low_scalar_origin_filter": filter_info,
        "top_sheets": get_top_sheets(rsi_data, excluded_vertices),
    }

    output_file.write_text(json.dumps(data, indent=2, allow_nan=False))


def find_warnings(rsi_data):
    warnings = []

    if rsi_data["extra_bytes"]:
        warnings.append(f"extra bytes at end of file: {rsi_data['extra_bytes']}")

    for sheet_id, area in rsi_data["sheet_area"].items():
        if math.isnan(area):
            warnings.append(f"NaN area for sheet {sheet_id}")
        elif math.isinf(area):
            warnings.append(f"infinite area for sheet {sheet_id}")

    vertex_count = len(rsi_data["is_vertex_singular"])

    for sheet_id, vertices in rsi_data["sheet_regular_vertices"].items():
        bad_vertices = [
            vertex for vertex in vertices
            if vertex < 0 or vertex >= vertex_count
        ]

        if bad_vertices:
            sample = ", ".join(map(str, bad_vertices[:10]))
            warnings.append(
                f"sheet {sheet_id} has {len(bad_vertices)} "
                f"out-of-range vertex id(s): {sample}"
            )

    return warnings


def build_rsi_json_stage():
    if not RSI_DIR.exists():
        raise FileNotFoundError(f"RSI directory not found: {RSI_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RSI_JSON_DIR.mkdir(parents=True, exist_ok=True)

    rsi_files = sorted(RSI_DIR.glob("*.rsi"))
    print(f"Reading {len(rsi_files)} RSI files")

    warning_lines = []
    filter_log_lines = []
    converted_count = 0

    for count, rsi_file in enumerate(rsi_files, start=1):
        try:
            rsi_data = read_rsi(rsi_file)
            excluded_vertices, filter_info, filter_warnings = low_scalar_origin_filter_for_rsi(rsi_file, rsi_data)
            output_file = RSI_JSON_DIR / f"{rsi_file.stem}.rsijson"
            write_rsi_json(rsi_data, output_file, excluded_vertices, filter_info)

        except Exception as exc:
            warning_lines.append(f"{rsi_file}: failed to process: {exc}")
            print(f"[{count}/{len(rsi_files)}] failed: {rsi_file.name}", flush=True)
            continue

        converted_count += 1

        warnings = find_warnings(rsi_data) + filter_warnings
        warning_lines.extend(f"{rsi_file}: {warning}" for warning in warnings)
        filter_log_lines.append(
            "\t".join(
                [
                    str(rsi_file),
                    f"status={filter_info.get('status', '-')}",
                    f"enabled={filter_info.get('enabled', False)}",
                    f"excluded_vertices={filter_info.get('excluded_vertices', 0)}",
                    f"excluded_regular_vertices={filter_info.get('excluded_regular_vertices', 0)}",
                    f"thresholds={filter_info.get('thresholds', {})}",
                    f"vtu={filter_info.get('vtu_file', '-')}",
                ]
            )
        )

        status = "warning" if warnings else "done"
        print(f"[{count}/{len(rsi_files)}] {status}: {rsi_file.name}", flush=True)

    RSI_JSON_WARNINGS_LOG_FILE.write_text("\n".join(warning_lines))
    LOW_SCALAR_ORIGIN_FILTER_LOG_FILE.write_text("\n".join(filter_log_lines))

    print(f"RSI JSON directory: {RSI_JSON_DIR}")
    print(f"Converted files: {converted_count}")
    print(f"Warnings: {len(warning_lines)}")
    print(f"Warning log: {RSI_JSON_WARNINGS_LOG_FILE}")
    print(f"Low-scalar origin filter log: {LOW_SCALAR_ORIGIN_FILTER_LOG_FILE}")
