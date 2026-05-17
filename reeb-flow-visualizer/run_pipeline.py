#!/usr/bin/env python3

import stage_01_run_fv99 as s1
import stage_02_build_sankey_data as s2
import stage_03_compute_sheet_overlaps as s3
import stage_04_plot_sankey as s4
from interactive_sankey_viewer.stage_04_interactive_sankey_viewer import (
    build_interactive_sankey_viewer_stage,
)


STAGES = [
    #("Stage 1: run fv99", s1.run_fv99_stage),
    ("Stage 2: build RSI JSON files", s2.build_rsi_json_stage),
    ("Stage 3: compute sheet overlaps", s3.compute_sheet_overlaps_stage),
    ("Stage 4: build interactive Sankey viewer", build_interactive_sankey_viewer_stage),
    ("Stage 4b: plot legacy Plotly Sankey HTML", s4.plot_sankey_stage),
]


def run_stage(title, func):
    print(f"\n{'=' * 80}")
    print(title)
    print(f"{'=' * 80}")

    func()


def main():
    for title, func in STAGES:
        run_stage(title, func)


if __name__ == "__main__":
    main()
