# Reeb Flow Visualizer Code Map

This repository builds a self-contained unified Sankey viewer for tracking
time-varying Reeb-space sheets. The generated viewer lives entirely in:

`BASE_DIR/sankey/unified_sankey_viewer/`

Serve that directory directly:

```bash
cd /media/mohit/8tbh/postdoc/timeVaryingReebFeatures/stilbene/sankey/unified_sankey_viewer
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

## Current Pipeline

`run_pipeline.py` controls the active stages:

1. `stage_01_run_fv99.py` optionally generates `.rs` and `.rsi` from `.vtu`.
2. `stage_02_build_sankey_data.py` converts `.rsi` files into per-timestep
   `.rsijson` files containing top sheets and their domain vertices.
3A. `compareSheetShapes/compare_sheet_shapes.py` computes cached sheet-shape
    descriptors and adjacent-timestep shape-match scores.
3B. `stage_03_compute_sheet_overlaps.py` computes adjacent-timestep domain
    vertex overlaps and attaches available shape/range metrics to overlap links.
4A. `SheetRenderer/render_rs_directory_orbital_colours.py` renders sheet PNGs.
4B. `stage_04_compute_sheet_fiber_surfaces.py` renders top-sheet fiber-surface
    images.
5. `unified_sankey_viewer/stage_07_unified_sankey_viewer.py` writes the
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
- `SHEET_IMAGE_DIR`
- `OVERLAP_FILE`
- `FV99`
- `EPSILON`
- `FV99_OMP_THREADS`
- `RESERVE_CORES`
- `TOP_N_SHEETS`
- `VIEWER_DEFAULT_TOP_SHEETS`
- `SHAPE_SCORE_DEFAULT_WEIGHTS`
- `RANGE_SCORE_DEFAULT_WEIGHTS`

Runtime library paths are also derived here for VTK/TTK/FV99.

## Stage Details

### Stage 1: FV99

`stage_01_run_fv99.py` scans `VTU_DIR`, runs `fv99`, and writes:

- `.rs` files to `RS_DIR`
- `.rsi` files to `RSI_DIR`

`fv99` is run with `OMP_NUM_THREADS` set from `FV99_OMP_THREADS` in
`common.py`. The default is `1` to avoid OpenMP races in the arrangement code.

On `main`, stage 1 does not retry failed files with perturbation. If `fv99`
returns a non-zero code and does not produce both `.rs` and `.rsi`, that
timestep is logged and downstream stages will not include it because they only
discover timesteps with matching `.rs`/`.rsi` inputs. The perturbation-retry
workflow is kept on the `perturbation-degenerate-cases` branch.

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
- `vertices`

`TOP_N_SHEETS` controls how many sheets are preprocessed per timestep.

### Stage 3A: Shape Matching

`compareSheetShapes/compare_sheet_shapes.py` computes sheet correspondence
scores without using PNGs.

It builds reusable caches under:

`BASE_DIR/compareSheetShapesCache/`

Important outputs:

- `results/sheet_shape_matches.json`
- `results/sheet_shape_summary.json`
- `cache/timesteps/*.json`
- `cache/timesteps/*.npz`
- `cache/matches/*.json`
- `cache/vtp/*.sheets.vtp`

Range matching exports sheet geometry by running `fv99 --headless` through
`SheetRenderer/render_rs_sheets.py`. That export also sets `OMP_NUM_THREADS`
from `FV99_OMP_THREADS`, matching stage 1. If a timestep cannot export a VTP,
the cache build fails for that timestep; `main` does not synthesize fallback
range metrics from `.rsi` alone.

Per-link range metrics include:

- `final_score`
- `shape_iou`
- `support_jaccard`
- `area_ratio`
- `bbox_iou`
- `centroid_similarity`

### Stage 3B: Domain Overlaps

`stage_03_compute_sheet_overlaps.py` reads all `.rsijson` files and computes
adjacent-timestep sheet overlaps.

For every source sheet at timestep `t` and target sheet at `t + 1`, it computes:

`overlap_vertices = |source_vertices intersection target_vertices|`

The output is:

`BASE_DIR/sankey/sheet_overlaps.json`

This file also stores range metrics from the range-match cache when a
matching sheet pair exists.

### Stage 4A: Sheet Rendering

`SheetRenderer/render_rs_directory_orbital_colours.py` renders the full sheet
view and top-sheet PNGs into `SHEET_IMAGE_DIR`.

The stage reuses cached VTP geometry by default. `SHEET_RENDERER_REBUILD_CACHE`
and `SHEET_RENDERER_CLEAN_CACHE` in `common.py` control explicit cache rebuilds
or cache cleanup.

With `SHEET_RENDERER_USE_GLOBAL_BOUNDS = True`, the stage first computes one
global 2D sheet-space extent across all timesteps, expands it to the configured
`SHEET_RENDERER_IMAGE_SIZE` aspect ratio, and renders every PNG with that same
coordinate frame. This keeps image dimensions and visual scale fixed across
timesteps.

### Stage 4B: Sheet Fiber Surfaces

`stage_04_compute_sheet_fiber_surfaces.py` renders top-sheet fiber-surface
images into `FIBER_SURFACE_IMAGE_DIR`.

The stage uses the fiber-surface isovalues, ParaView state file, and render
retry settings from `common.py`.

### Stage 5: Unified Viewer

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
- mouse pan and wheel zoom
- panel resizing
- threshold controls
- top sheet count control
- node coloring by solid color, sheet area, or vertex count
- hide nodes with no visible links
- strongest outgoing link per node
- link darkness control
- sheet image hover/click details

## Generated Parent Directory

`BASE_DIR/sankey/` still contains pipeline data products such as:

- `sheet_overlaps.json`
- `rsi_json/`
- warning logs

The browser entry point is the generated `unified_sankey_viewer/` directory
itself, not the parent directory.
