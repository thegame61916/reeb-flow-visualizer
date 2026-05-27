#!/usr/bin/env python3

import stage_01_run_fv99 as s1
import stage_02_build_sankey_data as s2
import stage_03_compute_sheet_overlaps as s3
import stage_04_compute_sheet_fiber_surfaces as s4

from compareSheetShapes.compare_sheet_shapes import (
    main as run_shape_matching_main,
)

from common import (
    RUN_STAGE_1_FV99,
    RUN_STAGE_2_RSI_JSON,
    RUN_STAGE_2B_SHAPE_MATCHING,
    RUN_STAGE_3_OVERLAPS,
    RUN_STAGE_4_SHEET_FIBER_SURFACES,
    RUN_UNIFIED_SANKEY_VIEWER,
    SHAPE_MATCHING_WORKERS,
)

from unified_sankey_viewer.stage_07_unified_sankey_viewer import (
    build_unified_sankey_viewer_stage,
)


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

    if RUN_STAGE_4_SHEET_FIBER_SURFACES:
        stages.append(
            (
                "Stage 4: compute sheet fiber surfaces",
                s4.compute_sheet_fiber_surfaces_stage,
            )
        )

    if RUN_UNIFIED_SANKEY_VIEWER:
        stages.append(
            (
                "Stage 5: build unified Sankey viewer",
                build_unified_sankey_viewer_stage,
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
