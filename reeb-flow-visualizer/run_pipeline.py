#!/usr/bin/env python3

import stage_01_run_fv99 as s1
import stage_02_build_sankey_data as s2
import stage_03_compute_sheet_overlaps as s3
import stage_04_plot_sankey as s4

from compareSheetShapes.compare_sheet_shapes import (
    main as run_shape_matching_main,
)

from unified_sankey_viewer.stage_07_unified_sankey_viewer import (
    build_unified_sankey_viewer_stage,
)


# ================= USER SETTINGS =================

RUN_STAGE_1_FV99 = False
RUN_STAGE_2_RSI_JSON = True
RUN_STAGE_2B_SHAPE_MATCHING = True
RUN_STAGE_3_OVERLAPS = True

RUN_UNIFIED_SANKEY_VIEWER = True

RUN_LEGACY_PLOTLY_SANKEY = False

# Shape matching can be expensive.
# Use a small number for testing, or None to use the default from shape_matching.py.
SHAPE_MATCHING_WORKERS = None

# ==================================================


def enabled_stages():
    stages = []

    if RUN_STAGE_1_FV99:
        stages.append(
            (
                "Stage 1: run fv99",
                s1.run_fv99_stage,
            )
        )

    if RUN_STAGE_2_RSI_JSON:
        stages.append(
            (
                "Stage 2: build RSI JSON files",
                s2.build_rsi_json_stage,
            )
        )

    if RUN_STAGE_2B_SHAPE_MATCHING:
        stages.append(
            (
                "Stage 2b: compare sheet shapes",
                run_shape_matching_stage,
            )
        )

    if RUN_STAGE_3_OVERLAPS:
        stages.append(
            (
                "Stage 3: compute sheet overlaps",
                s3.compute_sheet_overlaps_stage,
            )
        )

    if RUN_UNIFIED_SANKEY_VIEWER:
        stages.append(
            (
                "Stage 4: build unified Sankey viewer",
                build_unified_sankey_viewer_stage,
            )
        )

    if RUN_LEGACY_PLOTLY_SANKEY:
        stages.append(
            (
                "Stage 4b: plot legacy Plotly Sankey HTML",
                s4.plot_sankey_stage,
            )
        )

    return stages


def run_shape_matching_stage():
    args = []
    if SHAPE_MATCHING_WORKERS is not None:
        args.extend(
            [
                "--workers",
                str(int(SHAPE_MATCHING_WORKERS)),
            ]
        )

    exit_code = run_shape_matching_main(args)
    if exit_code:
        raise RuntimeError(f"shape matching stage failed with exit code {exit_code}")


def run_stage(title, func):
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)

    func()


def main():
    stages = enabled_stages()

    if not stages:
        print("No stages enabled.")
        return

    for title, func in stages:
        run_stage(title, func)


if __name__ == "__main__":
    main()
