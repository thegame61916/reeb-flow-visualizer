#!/usr/bin/env python3

import json
import math
import struct

from common import (
    OUTPUT_DIR,
    RSI_DIR,
    RSI_JSON_DIR,
    RSI_JSON_WARNINGS_LOG_FILE,
    TOP_N_SHEETS,
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


def get_top_sheets(rsi_data):
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
        vertices = sheet_vertices.get(sheet_id, [])

        top_sheets.append(
            {
                "sheet_id": sheet_id,
                "rank": rank,
                "area": json_safe_number(area),
                "num_vertices": len(vertices),
                "vertices": vertices,
            }
        )

    return top_sheets


def write_rsi_json(rsi_data, output_file):
    is_vertex_singular = rsi_data["is_vertex_singular"]

    data = {
        "rsi_file": rsi_data["rsi_file"],
        "top_n_sheets": TOP_N_SHEETS,
        "num_vertices": len(is_vertex_singular),
        "num_singular_vertices": sum(is_vertex_singular),
        "num_regular_vertices": len(is_vertex_singular) - sum(is_vertex_singular),
        "top_sheets": get_top_sheets(rsi_data),
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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RSI_JSON_DIR.mkdir(parents=True, exist_ok=True)

    rsi_files = sorted(RSI_DIR.glob("*.rsi"))
    print(f"Reading {len(rsi_files)} RSI files")

    warning_lines = []
    converted_count = 0

    for count, rsi_file in enumerate(rsi_files, start=1):
        try:
            rsi_data = read_rsi(rsi_file)
            output_file = RSI_JSON_DIR / f"{rsi_file.stem}.rsijson"
            write_rsi_json(rsi_data, output_file)

        except Exception as exc:
            warning_lines.append(f"{rsi_file}: failed to process: {exc}")
            print(f"[{count}/{len(rsi_files)}] failed: {rsi_file.name}", flush=True)
            continue

        converted_count += 1

        warnings = find_warnings(rsi_data)
        warning_lines.extend(f"{rsi_file}: {warning}" for warning in warnings)

        status = "warning" if warnings else "done"
        print(f"[{count}/{len(rsi_files)}] {status}: {rsi_file.name}", flush=True)

    RSI_JSON_WARNINGS_LOG_FILE.write_text("\n".join(warning_lines))

    print(f"RSI JSON directory: {RSI_JSON_DIR}")
    print(f"Converted files: {converted_count}")
    print(f"Warnings: {len(warning_lines)}")
    print(f"Warning log: {RSI_JSON_WARNINGS_LOG_FILE}")
