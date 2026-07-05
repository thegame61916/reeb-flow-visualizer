# Reeb Flow Visualizer Code Map

This repository builds a self-contained unified Sankey viewer for tracking
time-varying Reeb-space sheets. The generated viewer lives entirely in:

`BASE_DIR/sankey/unified_sankey_viewer/`

Serve that directory directly:

```bash
cd /home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/stilbene/sankey/unified_sankey_viewer
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

## Current Pipeline

`run_pipeline.py` controls the active stages:

1. `stage_01_run_fv99.py` optionally generates `.rs`, `.rsi`, sheet VTP, and
   fixed labeled fiber-surface artifacts from `.vtu`.
2. `stage_02_build_sankey_data.py` converts `.rsi` files into per-timestep
   `.rsijson` files containing top sheets and their domain vertices.
3A. `compareSheetShapes/compare_sheet_shapes.py` computes cached sheet-shape
    descriptors and stride-aware shape-match scores.
3B. `stage_03_compute_sheet_overlaps.py` computes stride-aware domain vertex
    overlaps and attaches available shape/range metrics to overlap links.
4A. `SheetRenderer/render_rs_directory_orbital_colours.py` renders sheet PNGs.
4B. `stage_04_compute_sheet_fiber_surfaces.py` renders top-sheet fiber-surface
    images from fixed isovalues.
4C. `stage_04c_compute_adaptive_fiber_surfaces.py` renders adaptive `f`
    fiber-surface images for datasets configured with adaptive fiber mode.
5A. `unified_sankey_viewer/stage_07_unified_sankey_viewer.py` builds
    `sankey/tracking_data.json`.
5B. `stage_06_analyze_tracking_results.py` builds CSV/plot diagnostics and
    `sankey/tracking_analysis/viewer_analysis.json`.
5C. `unified_sankey_viewer/stage_07_unified_sankey_viewer.py` writes the
    self-contained browser viewer.

The old Plotly Sankey output and old dashboard shell have been removed from the
active code path.

## Shared Configuration

`common.py` centralizes paths and defaults:

- `BASE_DIR`
- `VTU_DIR`
- `RS_DIR`
- `RSI_DIR`
- `OUTPUT_DIR`
- `RSI_JSON_DIR`
- `UNIFIED_VIEWER_DIR`
- `TRACKING_DATA_FILE`
- `TRACKING_ANALYSIS_DIR`
- `SHEET_IMAGE_DIR`
- `FIBER_SURFACE_IMAGE_DIR`
- `OVERLAP_FILE`
- `FV99`
- `EPSILON`
- `FV99_OMP_THREADS`
- `RESERVE_CORES`
- `TOP_N_SHEETS`
- `VIEWER_DEFAULT_TOP_SHEETS`
- `SANKEY_TIMESTEP_STRIDE_MAX`
- `SHAPE_SCORE_DEFAULT_WEIGHTS`
- `RANGE_SCORE_DEFAULT_WEIGHTS`

Runtime library paths are also derived here for VTK/TTK/FV99.

## Stage Details

### Stage 1: FV99

`stage_01_run_fv99.py` scans `VTU_DIR`, runs `fv99`, and writes:

- `.rs` files to `RS_DIR`
- `.rsi` files to `RSI_DIR`
- sheet geometry VTP files to `SHEET_VTP_CACHE_DIR`
- fixed labeled fiber-surface VTP files to `FIBER_SURFACE_LABELED_DIR`, unless
  the dataset uses adaptive fiber mode

`fv99` is run with `OMP_NUM_THREADS` set from `FV99_OMP_THREADS` in
`common.py`. The default is `1` to avoid OpenMP races in the arrangement code.

If `fv99` returns a non-zero code and does not produce the primary `.rs`,
`.rsi`, and sheet VTP outputs, Stage 1 perturbs the input VTU once with
`FV99_PERTURB_SCRIPT` and retries. A successful local retry replaces the input
VTU with the perturbed copy and logs the recovery. Failed, partial, and
recovered timesteps are written to the Stage 1 log files under `OUTPUT_DIR`.

This stage is disabled by default because the Reeb-space outputs are usually
already computed.

### Stage 2: RSI JSON

`stage_02_build_sankey_data.py` reads binary `.rsi` files and writes compact
per-timestep `.rsijson` summaries under `RSI_JSON_DIR`.

Each top sheet stores:

- `sheet_id`
- `rank`
- `area`
- `num_vertices`
- `num_vertices_before_low_scalar_filter`
- `num_low_scalar_filtered_vertices`
- `vertices`

`TOP_N_SHEETS` controls how many sheets are preprocessed per timestep.
When `EXCLUDE_LOW_SCALAR_VALUES_NEAR_ORIGIN` is enabled, Stage 2 filters
regular vertices whose configured `(f, g)` scalar pair lies close to the range
origin and records the filter metadata in each `.rsijson`.

### Stage 3A: Shape Matching

`compareSheetShapes/compare_sheet_shapes.py` computes sheet correspondence
scores without using PNGs.

It builds reusable caches under:

`BASE_DIR/compareSheetShapesCache/`

Important outputs:

- `results/sheet_shape_matches.json`
- `results/sheet_shape_summary.json`
- `cache/global_bounds.json`
- `cache/manifest.json`
- `cache/timesteps/*.json`
- `cache/timesteps/*.npz`
- `cache/matches/*.json`
- `cache/vtp/*.sheets.vtp`

Range matching reads cached sheet geometry from `SHEET_VTP_CACHE_DIR`. If a VTP
is missing, it can export one by running `fv99 --headless` through
`SheetRenderer/render_rs_sheets.py`; that export also sets `OMP_NUM_THREADS`
from `FV99_OMP_THREADS`, matching stage 1. If a timestep cannot export a VTP,
the timestep is skipped for shape matching; `main` does not synthesize fallback
range metrics from `.rsi` alone.

Per-link range metrics include:

- `final_score`
- `shape_iou`
- `geometry_iou`
- `area_ratio`
- `bbox_iou`
- `centroid_similarity`

`SANKEY_TIMESTEP_STRIDE_MAX` controls which direct timestep strides are
precomputed. The match file contains stride-one compatibility data in
`pairwise_matches` and all configured strides in `pairwise_matches_by_stride`.

### Stage 3B: Domain Overlaps

`stage_03_compute_sheet_overlaps.py` reads all `.rsijson` files and computes
sheet overlaps for strides `1..SANKEY_TIMESTEP_STRIDE_MAX`.

For every source sheet at timestep `t` and target sheet at `t + stride`, it
computes:

`overlap_vertices = |source_vertices intersection target_vertices|`

The output is:

`BASE_DIR/sankey/sheet_overlaps.json`

This file also stores range metrics from the range-match cache when a
matching sheet pair exists.

### Stage 4A: Sheet Rendering

`SheetRenderer/render_rs_directory_orbital_colours.py` renders the full sheet
view and top-sheet PNGs into `SHEET_IMAGE_DIR`.

The stage reads the Stage 1-owned sheet VTP cache from `SHEET_VTP_CACHE_DIR`.
The `--rebuild-cache` and `--clean-cache` CLI flags are retained for
compatibility but are intentionally ignored; rerun Stage 1 to regenerate sheet
VTP geometry.

With `SHEET_RENDERER_USE_GLOBAL_BOUNDS = True`, the stage first computes one
global 2D sheet-space extent across all timesteps, expands it to the configured
`SHEET_RENDERER_IMAGE_SIZE` aspect ratio, and renders every PNG with that same
coordinate frame. This keeps image dimensions and visual scale fixed across
timesteps.

### Stage 4B: Sheet Fiber Surfaces

`stage_04_compute_sheet_fiber_surfaces.py` renders top-sheet fiber-surface
images into `FIBER_SURFACE_IMAGE_DIR`.

The stage uses the fixed fiber-surface isovalues, ParaView state file, render
retry settings, and Stage 1 labeled fiber-surface VTP artifacts from
`common.py`.

### Stage 4C: Adaptive Fiber Surfaces

`stage_04c_compute_adaptive_fiber_surfaces.py` is enabled when
`FIBER_SURFACE_MODE == "adaptive_f_range_change"`.

It uses stride-one shape matches and cached sheet descriptors to choose one
adaptive `f` value per top sheet, generates labeled fiber surfaces for those
values, thresholds them by sheet id, and renders the result into
`FIBER_SURFACE_IMAGE_DIR`.

### Stage 5A: Unified Tracking Data

`build_unified_sankey_data_stage()` prepares the full tracking payload and
writes:

- `BASE_DIR/sankey/tracking_data.json`

This file keeps rich per-match metadata for analysis and paper exports.

### Stage 5B: Tracking Analysis

`stage_06_analyze_tracking_results.py` reads tracking/viewer data and writes:

- `metric_summary.csv`
- `best_target_agreement.csv`
- `event_scores.csv`
- `sheet_lifetimes.csv`
- `interesting_intervals.json`
- `viewer_analysis.json`
- plot PNG/PDF files

The viewer embeds `viewer_analysis.json` when it exists.

### Stage 5C: Unified Viewer

`unified_sankey_viewer/stage_07_unified_sankey_viewer.py` writes:

- `index.html`
- `style.css`
- `viewer.js`
- `viewer_common.js`
- `data.json`
- `sheet_images` link/copy
- `fiber_surface_images` link/copy

All of these are generated inside:

`BASE_DIR/sankey/unified_sankey_viewer/`

No parent redirect or wrapper dashboard is generated.

## Viewer Capabilities

The unified viewer supports:

- domain-overlap mode
- range-metric mode
- multiple Sankey panels
- range selection and range deletion
- synchronized top range bar
- timestep stride selection
- mouse pan and wheel zoom
- panel resizing
- threshold controls
- support filters
- top sheet count control
- node coloring by solid color, sheet area, vertex count, or sheet centroid
- hide nodes with no visible links
- strongest outgoing link per node
- hide sheet labels
- link darkness control
- sheet and fiber-surface image hover/click details
- image zoom overlays
- tracking-analysis interval, track, and sensitivity views
- figure preset export for paper screenshots

The backend still computes and stores some diagnostics that are intentionally
not exposed in the current viewer UI: Sheet geometry IoU, best-supported
range/domain intervals, domain-stability summaries, and domain/range
complementarity. The JS runtime keeps the corresponding data paths so these can
be re-enabled without rerunning earlier pipeline stages.

## Generated Parent Directory

`BASE_DIR/sankey/` still contains pipeline data products such as:

- `sheet_overlaps.json`
- `rsi_json/`
- `tracking_data.json`
- `tracking_analysis/`
- `paper_exports/`
- warning logs

The browser entry point is the generated `unified_sankey_viewer/` directory
itself, not the parent directory.
