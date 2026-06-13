#!/usr/bin/env python3

import stage_01_run_fv99 as s1
import stage_02_build_sankey_data as s2
import stage_03_compute_sheet_overlaps as s3
import stage_04_compute_sheet_fiber_surfaces as s4
import stage_04c_compute_adaptive_fiber_surfaces as s4c
import stage_06_analyze_tracking_results as s6

from compareSheetShapes.compare_sheet_shapes import (
    main as run_shape_matching_main,
)

from common import (
    RUN_STAGE_1_FV99,
    RUN_STAGE_2_RSI_JSON,
    RUN_STAGE_3A_SHAPE_MATCHING,
    RUN_STAGE_3B_OVERLAPS,
    RUN_STAGE_4A_SHEET_RENDERING,
    RUN_STAGE_4B_SHEET_FIBER_SURFACES,
    RUN_STAGE_4C_ADAPTIVE_FIBER_SURFACES,
    RUN_STAGE_5A_BUILD_UNIFIED_SANKEY_DATA,
    RUN_STAGE_5B_TRACKING_ANALYSIS,
    RUN_STAGE_5C_UNIFIED_SANKEY_VIEWER,
    SHAPE_MATCHING_WORKERS,
    SHEET_RENDERER_CLEAN_CACHE,
    SHEET_RENDERER_REBUILD_CACHE,
)

from unified_sankey_viewer.stage_07_unified_sankey_viewer import (
    build_unified_sankey_data_stage,
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

    if RUN_STAGE_3A_SHAPE_MATCHING:
        stages.append(
            (
                "Stage 3A: compare sheet shapes",
                run_shape_matching_stage,
            )
        )

    if RUN_STAGE_3B_OVERLAPS:
        stages.append(
            (
                "Stage 3B: compute sheet overlaps",
                s3.compute_sheet_overlaps_stage,
            )
        )

    if RUN_STAGE_4A_SHEET_RENDERING:
        stages.append(
            (
                "Stage 4A: render sheet images",
                run_sheet_rendering_stage,
            )
        )

    if RUN_STAGE_4B_SHEET_FIBER_SURFACES:
        stages.append(
            (
                "Stage 4B: compute sheet fiber surfaces",
                s4.compute_sheet_fiber_surfaces_stage,
            )
        )

    if RUN_STAGE_4C_ADAPTIVE_FIBER_SURFACES:
        stages.append(
            (
                "Stage 4C: compute adaptive f fiber surfaces",
                s4c.compute_adaptive_fiber_surfaces_stage,
            )
        )

    if RUN_STAGE_5A_BUILD_UNIFIED_SANKEY_DATA:
        stages.append(
            (
                "Stage 5A: build unified Sankey data",
                build_unified_sankey_data_stage,
            )
        )

    if RUN_STAGE_5B_TRACKING_ANALYSIS:
        stages.append(
            (
                "Stage 5B: analyze tracking results",
                run_tracking_analysis_stage,
            )
        )

    if RUN_STAGE_5C_UNIFIED_SANKEY_VIEWER:
        stages.append(
            (
                "Stage 5C: build unified Sankey viewer",
                build_unified_sankey_viewer_stage,
            )
        )

    return stages


def run_sheet_rendering_stage():
    from SheetRenderer.render_rs_directory_orbital_colours import main as run_sheet_renderer_main

    args = []
    if SHEET_RENDERER_REBUILD_CACHE:
        args.append("--rebuild-cache")
    if SHEET_RENDERER_CLEAN_CACHE:
        args.append("--clean-cache")

    exit_code = run_sheet_renderer_main(args)
    if exit_code:
        raise RuntimeError(f"sheet rendering stage failed with exit code {exit_code}")


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


def run_tracking_analysis_stage():
    exit_code = s6.analyze_tracking_results_stage([])
    if exit_code:
        raise RuntimeError(f"tracking analysis stage failed with exit code {exit_code}")


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
