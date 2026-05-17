#!/usr/bin/env python3

import json
import math
from collections import defaultdict

from common import HTML_FILE, OVERLAP_FILE, SANKEY_TITLE


# ================= VISUAL SETTINGS =================

MAX_RANK_SHOWN = 12

MIN_OVERLAP_VERTICES = 10
MIN_SOURCE_PERCENT = 2.0
MIN_TARGET_PERCENT = 2.0

NODE_PAD = 35
NODE_THICKNESS = 10

BASE_WIDTH_PER_TIMESTEP = 320
MIN_WIDTH = 1800
MAX_WIDTH = 8000

MIN_HEIGHT = 1100
HEIGHT_PER_NODE = 55

LINK_OPACITY = 0.16

# ====================================================


def load_overlap_data():
    return json.loads(OVERLAP_FILE.read_text())


def area_color(area, min_area, max_area):
    if area is None or not math.isfinite(area):
        return "rgb(150,150,150)"

    if max_area <= min_area:
        ratio = 0.5
    else:
        ratio = (area - min_area) / (max_area - min_area)

    red = int(60 + 170 * ratio)
    green = int(125 - 45 * ratio)
    blue = int(210 - 150 * ratio)

    return f"rgb({red},{green},{blue})"


def node_label(node):
    return f"S{node['sheet_id']} | R{node['rank']}"


def should_keep_node(node):
    return node["rank"] <= MAX_RANK_SHOWN


def should_keep_link(link):
    return (
        link["overlap_vertices"] >= MIN_OVERLAP_VERTICES
        or link["source_percent"] >= MIN_SOURCE_PERCENT
        or link["target_percent"] >= MIN_TARGET_PERCENT
    )


def node_positions(nodes, num_timesteps):
    """
    Place nodes in columns by timestep and spread them vertically
    within each timestep to avoid overlap.
    """

    nodes_by_timestep = defaultdict(list)

    for node in nodes:
        nodes_by_timestep[node["timestep_index"]].append(node)

    for timestep_index in nodes_by_timestep:
        nodes_by_timestep[timestep_index].sort(key=lambda n: n["rank"])

    x_values = [0.0] * len(nodes)
    y_values = [0.0] * len(nodes)

    node_to_index = {
        node["id"]: index
        for index, node in enumerate(nodes)
    }

    for timestep_index, timestep_nodes in nodes_by_timestep.items():
        if num_timesteps <= 1:
            x = 0.5
        else:
            x = timestep_index / (num_timesteps - 1)

        n = len(timestep_nodes)

        for local_index, node in enumerate(timestep_nodes):
            global_index = node_to_index[node["id"]]

            if n == 1:
                y = 0.5
            else:
                y = 0.03 + local_index * (0.94 / (n - 1))

            x_values[global_index] = x
            y_values[global_index] = y

    return x_values, y_values


def compute_figure_width(num_timesteps):
    width = num_timesteps * BASE_WIDTH_PER_TIMESTEP
    return min(MAX_WIDTH, max(MIN_WIDTH, width))


def compute_figure_height(nodes):
    if not nodes:
        return MIN_HEIGHT

    timestep_counts = defaultdict(int)

    for node in nodes:
        timestep_counts[node["timestep_index"]] += 1

    max_nodes_in_column = max(timestep_counts.values())

    return max(MIN_HEIGHT, HEIGHT_PER_NODE * max_nodes_in_column)


def write_sankey_html(data):
    try:
        import plotly.graph_objects as go
    except ImportError as exc:
        raise RuntimeError(
            "Plotly is required. Install it with: pip install plotly"
        ) from exc

    original_nodes = data["nodes"]
    original_links = data["links"]

    if not original_nodes:
        raise ValueError("No nodes found in overlap data.")

    nodes = [
        node
        for node in original_nodes
        if should_keep_node(node)
    ]

    node_index = {
        node["id"]: index
        for index, node in enumerate(nodes)
    }

    valid_links = [
        link
        for link in original_links
        if link["source"] in node_index
        and link["target"] in node_index
        and link["overlap_vertices"] > 0
        and should_keep_link(link)
    ]

    areas = [
        node["area"]
        for node in nodes
        if node.get("area") is not None and math.isfinite(node["area"])
    ]

    min_area = min(areas) if areas else 0.0
    max_area = max(areas) if areas else 1.0

    num_timesteps = data.get("num_timesteps", 0)

    x_values, y_values = node_positions(nodes, num_timesteps)

    node_customdata = [
        [
            node["timestep_label"],
            node["sheet_id"],
            node["rank"],
            node["area"],
            node["num_vertices"],
            node["rsi_file"],
            node["rsijson_file"],
        ]
        for node in nodes
    ]

    link_customdata = [
        [
            link["overlap_vertices"],
            link["source_percent"],
            link["target_percent"],
            link["source_sheet_id"],
            link["target_sheet_id"],
            link["source_rank"],
            link["target_rank"],
            link["source_area"],
            link["target_area"],
            link["source_rsi_file"],
            link["target_rsi_file"],
        ]
        for link in valid_links
    ]

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="fixed",
                valueformat=",d",
                node=dict(
                    pad=NODE_PAD,
                    thickness=NODE_THICKNESS,
                    line=dict(
                        color="rgba(20,20,20,0.35)",
                        width=0.6,
                    ),
                    label=[
                        node_label(node)
                        for node in nodes
                    ],
                    color=[
                        area_color(node["area"], min_area, max_area)
                        for node in nodes
                    ],
                    x=x_values,
                    y=y_values,
                    customdata=node_customdata,
                    hovertemplate=(
                        "<b>Sheet %{customdata[1]}</b><br>"
                        "time: %{customdata[0]}<br>"
                        "rank by area: %{customdata[2]}<br>"
                        "area: %{customdata[3]:.6g}<br>"
                        "regular vertices: %{customdata[4]:,}<br>"
                        "rsi: %{customdata[5]}<br>"
                        "json: %{customdata[6]}"
                        "<extra></extra>"
                    ),
                ),
                link=dict(
                    source=[
                        node_index[link["source"]]
                        for link in valid_links
                    ],
                    target=[
                        node_index[link["target"]]
                        for link in valid_links
                    ],
                    value=[
                        link["overlap_vertices"]
                        for link in valid_links
                    ],
                    color=[
                        f"rgba(80,80,80,{LINK_OPACITY})"
                        for _ in valid_links
                    ],
                    customdata=link_customdata,
                    hovertemplate=(
                        "<b>Overlap: %{customdata[0]:,} vertices</b><br>"
                        "source share: %{customdata[1]:.2f}%<br>"
                        "target share: %{customdata[2]:.2f}%<br>"
                        "source sheet: %{customdata[3]} rank %{customdata[5]}<br>"
                        "target sheet: %{customdata[4]} rank %{customdata[6]}<br>"
                        "source area: %{customdata[7]:.6g}<br>"
                        "target area: %{customdata[8]:.6g}<br>"
                        "source rsi: %{customdata[9]}<br>"
                        "target rsi: %{customdata[10]}"
                        "<extra></extra>"
                    ),
                ),
            )
        ]
    )

    width = compute_figure_width(num_timesteps)
    height = compute_figure_height(nodes)

    fig.update_layout(
        title=dict(
            text=(
                f"{SANKEY_TITLE}<br>"
                f"<sup>"
                f"Showing ranks ≤ {MAX_RANK_SHOWN}; "
                f"links kept if overlap ≥ {MIN_OVERLAP_VERTICES} vertices "
                f"or source/target share ≥ {MIN_SOURCE_PERCENT:.1f}%"
                f"</sup>"
            ),
            x=0.02,
            xanchor="left",
        ),
        font=dict(size=11),
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=dict(l=30, r=30, t=85, b=30),
        width=width,
        height=height,
    )

    fig.update_traces(
        textfont=dict(size=10)
    )

    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(
        HTML_FILE,
        include_plotlyjs="cdn",
        full_html=True,
    )

    print(f"Original nodes: {len(original_nodes)}")
    print(f"Shown nodes:    {len(nodes)}")
    print(f"Original links: {len(original_links)}")
    print(f"Shown links:    {len(valid_links)}")
    print(f"Figure width:   {width}")
    print(f"Figure height:  {height}")


def plot_sankey_stage():
    data = load_overlap_data()
    write_sankey_html(data)

    print(f"Read overlap data: {OVERLAP_FILE}")
    print(f"Wrote Sankey HTML: {HTML_FILE}")
