#!/usr/bin/env python3

import json
import re

from common import (
    OUTPUT_DIR,
    OVERLAP_FILE,
    OVERLAP_WARNINGS_LOG_FILE,
    RSI_JSON_DIR,
)


def read_rsi_json(rsijson_file):
    data = json.loads(rsijson_file.read_text())
    data["rsijson_file"] = str(rsijson_file)
    return data


def timestep_number(path):
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else None


def timestep_label(path):
    number = timestep_number(path)
    return str(number) if number is not None else path.stem


def timestep_sort_key(path):
    number = timestep_number(path)
    return number if number is not None else path.stem


def make_node_id(timestep_index, sheet_id):
    return f"t{timestep_index}_sheet{sheet_id}"


def prepare_sheet(sheet):
    vertices = sheet.get("vertices", [])

    prepared = dict(sheet)
    prepared["_vertex_set"] = set(vertices)

    return prepared


def make_node(timestep_index, timestep_label, rsijson_file, rsi_file, sheet):
    return {
        "id": make_node_id(timestep_index, sheet["sheet_id"]),
        "timestep_index": timestep_index,
        "timestep_label": timestep_label,
        "rsijson_file": str(rsijson_file),
        "rsi_file": rsi_file,
        "sheet_id": sheet["sheet_id"],
        "rank": sheet["rank"],
        "area": sheet["area"],
        "num_vertices": sheet["num_vertices"],
    }


def make_timestep(timestep_index, rsijson_file, data):
    label = timestep_label(rsijson_file)

    sheets = [
        prepare_sheet(sheet)
        for sheet in data.get("top_sheets", [])
    ]

    nodes = [
        make_node(
            timestep_index,
            label,
            rsijson_file,
            data.get("rsi_file"),
            sheet,
        )
        for sheet in sheets
    ]

    return {
        "index": timestep_index,
        "label": label,
        "rsijson_file": str(rsijson_file),
        "rsi_file": data.get("rsi_file"),
        "num_vertices": data.get("num_vertices"),
        "num_singular_vertices": data.get("num_singular_vertices"),
        "num_regular_vertices": data.get("num_regular_vertices"),
        "top_n_sheets": data.get("top_n_sheets"),
        "nodes": nodes,
        "sheets": sheets,
    }


def compute_link(source_timestep, target_timestep, source_sheet, target_sheet):
    source_vertices = source_sheet["_vertex_set"]
    target_vertices = target_sheet["_vertex_set"]

    overlap_vertices = len(source_vertices & target_vertices)

    source_count = len(source_vertices)
    target_count = len(target_vertices)

    source_percent = (
        100.0 * overlap_vertices / source_count
        if source_count else 0.0
    )

    target_percent = (
        100.0 * overlap_vertices / target_count
        if target_count else 0.0
    )

    return {
        "source": make_node_id(source_timestep["index"], source_sheet["sheet_id"]),
        "target": make_node_id(target_timestep["index"], target_sheet["sheet_id"]),
        "source_timestep_index": source_timestep["index"],
        "target_timestep_index": target_timestep["index"],
        "source_timestep_label": source_timestep["label"],
        "target_timestep_label": target_timestep["label"],
        "source_rsijson_file": source_timestep["rsijson_file"],
        "target_rsijson_file": target_timestep["rsijson_file"],
        "source_rsi_file": source_timestep["rsi_file"],
        "target_rsi_file": target_timestep["rsi_file"],
        "source_sheet_id": source_sheet["sheet_id"],
        "target_sheet_id": target_sheet["sheet_id"],
        "source_rank": source_sheet["rank"],
        "target_rank": target_sheet["rank"],
        "source_area": source_sheet["area"],
        "target_area": target_sheet["area"],
        "source_num_vertices": source_count,
        "target_num_vertices": target_count,
        "overlap_vertices": overlap_vertices,
        "source_percent": source_percent,
        "target_percent": target_percent,
    }


def compute_links(timesteps):
    links = []

    for source_timestep, target_timestep in zip(timesteps, timesteps[1:]):
        for source_sheet in source_timestep["sheets"]:
            for target_sheet in target_timestep["sheets"]:
                link = compute_link(
                    source_timestep,
                    target_timestep,
                    source_sheet,
                    target_sheet,
                )

                if link["overlap_vertices"] > 0:
                    links.append(link)

    return links


def find_warnings(timesteps):
    warnings = []

    for timestep in timesteps:
        if not timestep["sheets"]:
            warnings.append(f"{timestep['rsijson_file']}: no top sheets found")

        for sheet in timestep["sheets"]:
            vertices = sheet.get("vertices", [])

            if sheet.get("num_vertices", 0) != len(vertices):
                warnings.append(
                    f"{timestep['rsijson_file']}: sheet {sheet.get('sheet_id')} "
                    "num_vertices does not match vertex list length"
                )

    return warnings


def public_timestep_info(timestep):
    return {
        "index": timestep["index"],
        "label": timestep["label"],
        "rsijson_file": timestep["rsijson_file"],
        "rsi_file": timestep["rsi_file"],
        "num_vertices": timestep["num_vertices"],
        "num_singular_vertices": timestep["num_singular_vertices"],
        "num_regular_vertices": timestep["num_regular_vertices"],
        "top_n_sheets": timestep["top_n_sheets"],
        "node_ids": [node["id"] for node in timestep["nodes"]],
    }


def compute_sheet_overlaps_stage():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rsijson_files = sorted(
        RSI_JSON_DIR.glob("*.rsijson"),
        key=timestep_sort_key,
    )

    print(f"Reading {len(rsijson_files)} RSI JSON files")

    timesteps = []
    warning_lines = []

    for index, rsijson_file in enumerate(rsijson_files):
        try:
            data = read_rsi_json(rsijson_file)
            timestep = make_timestep(index, rsijson_file, data)
            timesteps.append(timestep)

            print(
                f"[{index + 1}/{len(rsijson_files)}] done: {rsijson_file.name}",
                flush=True,
            )

        except Exception as exc:
            warning_lines.append(f"{rsijson_file}: failed to read: {exc}")

            print(
                f"[{index + 1}/{len(rsijson_files)}] failed: {rsijson_file.name}",
                flush=True,
            )

    links = compute_links(timesteps)
    warning_lines.extend(find_warnings(timesteps))

    nodes = [
        node
        for timestep in timesteps
        for node in timestep["nodes"]
    ]

    output = {
        "rsijson_directory": str(RSI_JSON_DIR),
        "num_timesteps": len(timesteps),
        "num_nodes": len(nodes),
        "num_links": len(links),
        "timesteps": [
            public_timestep_info(timestep)
            for timestep in timesteps
        ],
        "nodes": nodes,
        "links": links,
    }

    OVERLAP_FILE.write_text(json.dumps(output, indent=2, allow_nan=False))
    OVERLAP_WARNINGS_LOG_FILE.write_text("\n".join(warning_lines))

    print(f"Overlap data: {OVERLAP_FILE}")
    print(f"Warnings: {len(warning_lines)}")
    print(f"Warning log: {OVERLAP_WARNINGS_LOG_FILE}")
    print(f"Nodes: {len(nodes)}")
    print(f"Links: {len(links)}")
