# Reeb Flow Visualizer Code Map

This document is a working map of the current codebase. It is meant to shorten future
re-entry time when changing the Sankey viewer or the preprocessing pipeline.

## What this repo does

The repo turns a sequence of time-varying Reeb-space snapshots into:

- `sheet_overlaps.json`: full overlap graph across timesteps
- `sankey.html`: legacy Plotly Sankey output
- `interactive_sankey_viewer/`: the current D3-based browser viewer

The key idea is:

- `rsi` files describe sheet metadata and regular-vertex membership
- `rs` files store the Reeb-space traversal/cache state
- `sheet_overlaps.json` is derived from the `rsi` data
- the viewer reads the overlap JSON and renders an interactive Sankey-like view
- `compareSheetShapes/` builds an additional score-based sheet-matching cache
- `match_summary_viewer/` renders a separate correspondence dashboard from that cache

## Directory layout

Important files:

- `common.py`
- `run_pipeline.py`
- `stage_01_run_fv99.py`
- `stage_02_build_sankey_data.py`
- `stage_03_compute_sheet_overlaps.py`
- `stage_04_plot_sankey.py`
- `interactive_sankey_viewer/stage_04_interactive_sankey_viewer.py`
- `match_summary_viewer/stage_05_match_summary_viewer.py`
- `SheetRenderer/render_rs_directory_orbital_colours.py`
- `SheetRenderer/render_rs_sheets.py`
- `compareSheetShapes/compare_sheet_shapes.py`
- `unified_sankey_viewer/viewer_common.py`

Generated outputs live under `BASE_DIR`:

- `downsampledGrids/`
- `reebSpaces/`
- `sheetInfo/`
- `sheetREndering/`
- `sankey/`
- `sankey/interactive_sankey_viewer/`
- `sankey/match_summary_viewer/`
- `sankey/index.html`
- `sankey/dashboard.css`
- `sankey/dashboard.js`
- `compareSheetShapesCache/`
- `interactive_sankey_viewer/viewer_common.js`
- `match_summary_viewer/viewer_common.js`

## Shared config: `common.py`

`common.py` centralizes paths and constants.

Important values:

- `BASE_DIR`
- `VTU_DIR`
- `RS_DIR`
- `RSI_DIR`
- `OUTPUT_DIR`
- `RSI_JSON_DIR`
- `VIEWER_DIR`
- `SHEET_IMAGE_DIR`
- `OVERLAP_FILE`
- `HTML_FILE`
- `FV99`
- `EPSILON`
- `RESERVE_CORES`
- `TOP_N_SHEETS`
- `SANKEY_TITLE`

Runtime library paths:

- `FV99_ROOT`
- `VTK_LIB_DIR`
- `TTK_BUILD_LIB_DIR`
- `TTK_INSTALL_LIB_DIR`

The Python stages import from `common.py`, so future path changes should usually happen there
first.

## Shared viewer runtime: `unified_sankey_viewer/viewer_common.py`

This module holds browser-runtime helpers that both generated viewers load:

- `shared_viewer_css()`: shared CSS for the top range bar, selected ranges, viewport window, and drag affordances
- `write_viewer_common_js(viewer_dir)`: writes `viewer_common.js` into the generated viewer directory

The shared JS currently provides:

- `bindCommittedNumberInput(input, commitFn)`: stops pointer/click propagation on number inputs and commits only on Enter or blur
- `renderRangeRows(holder, opts)`: draws the common range editor rows, including selection, commit-on-Enter/blur, and delete
- `recenterViewportFromBarIndex(targetTime, opts)`: shared viewport recentering logic for the black window drag/click behavior
- `renderRangeBar(svg, opts)`: draws the common top range bar, selected range blocks, drag preview, and viewport window

## Pipeline entry point: `run_pipeline.py`

`run_pipeline.py` runs the stages in order.

Current stage list:

1. `stage_02_build_sankey_data.build_rsi_json_stage`
2. `compareSheetShapes.compare_sheet_shapes.main` (stage 2b)
3. `stage_03_compute_sheet_overlaps.compute_sheet_overlaps_stage`
4. `unified_sankey_viewer.stage_07_unified_sankey_viewer.build_unified_sankey_viewer_stage`
5. `stage_04_plot_sankey.plot_sankey_stage`

Stage 1 (`fv99`) is commented out in the current pipeline runner.

## Stage 1: `stage_01_run_fv99.py`

Purpose:

- run `fv99` on each `.vtu`
- generate `.rs` and `.rsi`

Behavior:

- scans `VTU_DIR`
- writes `.rs` into `RS_DIR`
- writes `.rsi` into `RSI_DIR`
- uses a thread pool with `RESERVE_CORES` reserved
- sets up `LD_LIBRARY_PATH` from TTK/VTK install/build directories

Notes:

- this stage is currently not wired into the default pipeline runner
- it is still useful as a regeneration step if `.rs/.rsi` need to be rebuilt

## Stage 2: `stage_02_build_sankey_data.py`

Purpose:

- read each `.rsi`
- convert it into a small JSON summary
- keep the top `TOP_N_SHEETS` sheets by area

Key input:

- `RSI_DIR/*.rsi`

Key output:

- `RSI_JSON_DIR/*.rsijson`

Important data fields in each timestep summary:

- `is_vertex_singular`
- `sheet_area`
- `sheet_regular_vertices`
- `top_sheets`

`top_sheets` includes:

- `sheet_id`
- `rank`
- `area`
- `num_vertices`
- `vertices`

Important implementation details:

- areas are filtered for finiteness
- sheets are sorted descending by area
- the vertex list is preserved for overlap computation
- warnings are collected for:
  - extra bytes at EOF
  - NaN / inf areas
  - out-of-range vertex IDs

## Stage 3: `stage_03_compute_sheet_overlaps.py`

Purpose:

- combine the per-timestep `.rsijson` files into one global overlap graph

Key input:

- `RSI_JSON_DIR/*.rsijson`

Key output:

- `OVERLAP_FILE` (`sheet_overlaps.json`)

How links are computed:

- for each adjacent timestep pair
- compare every top sheet in timestep `t` with every top sheet in timestep `t+1`
- intersect their vertex sets
- `overlap_vertices = |source_vertices ∩ target_vertices|`

Per-link metadata includes:

- source/target node IDs
- timestep indices and labels
- sheet IDs and ranks
- source/target area
- source/target regular-vertex counts
- `overlap_vertices`
- source/target overlap percentages

Important invariant:

- links only exist between adjacent timesteps

## `compareSheetShapes/compare_sheet_shapes.py`

Purpose:

- compute sheet-shape correspondence scores without using PNG pixels
- cache per-timestep descriptors and adjacent-pair match results
- provide the data source for the match summary viewer

Key input caches:

- `compareSheetShapesCache/cache/timesteps/*.json`
- `compareSheetShapesCache/cache/timesteps/*.npz`
- `compareSheetShapesCache/results/sheet_shape_matches.json`
- `compareSheetShapesCache/results/sheet_shape_summary.json`

Per-sheet descriptors include:

- `sheet_id`
- `rank`
- `area`
- `num_vertices`
- `bbox`
- `centroid`
- `thumbnail` if a sheet image can be linked

Per-match scores include:

- `final_score`
- `shape_iou`
- `support_jaccard`
- `area_ratio`
- `bbox_iou`
- `centroid_similarity`

The new summary viewer reads these cached results directly.

## Stage 4b: `stage_04_plot_sankey.py`

Purpose:

- keep the legacy Plotly Sankey output around

Current behavior:

- filters top-ranked nodes
- applies a few overlap thresholds
- uses Plotly Sankey with fixed node positions
- colors nodes by area using a linear RGB interpolation

This file is mostly a fallback/reference now.

## Stage 5: `match_summary_viewer/stage_05_match_summary_viewer.py`

Purpose:

- build a separate score-based sheet correspondence dashboard
- consume the cached outputs from `compareSheetShapes/`
- keep the existing overlap Sankey untouched

Key inputs:

- `compareSheetShapesCache/cache/timesteps/*.json`
- `compareSheetShapesCache/results/sheet_shape_matches.json`
- `sheetREndering/` for hover thumbnails

Key output:

- `sankey/match_summary_viewer/`

Viewer behavior:

- nodes represent sheets
- node height is based on sheet area
- links represent similarity scores between adjacent timesteps
- link thickness is globally normalized by the selected score mode
- a top range bar is rendered above the panel stack
- timestep labels are rendered along the top of each score panel as `index. label` with a femtosecond sublabel
- the top bar shows the current visible viewport as a black window
- the black window is the drag handle for horizontal panning and recenters the view on click
- zoom and pan are shared across all score panels so the match views stay linked
- score panels can be added with `+`
- each panel can choose one score mode or the combined score
- timestep ranges and thresholding work like the overlap viewer, but only affect the summary dashboard
- threshold slider updates are coalesced onto animation frames and only toggle link visibility, so dragging stays smooth
- summary links use the same neutral overlap-style fill and hover palette as the domain-based local-scaling viewer, with no per-link color tinting
- range row textboxes ignore pointer clicks on the row itself; they commit only on Enter or when focus leaves the whole row
- the top range bar and range-row editing are drawn through shared helpers in `unified_sankey_viewer/viewer_common.py`
- the top range bar disables browser text selection while dragging so tick labels do not get highlighted
- summary range gaps are proportional to the number of hidden timesteps between selected ranges, with extra slack for ribbon width
- the summary viewer camera and top labels use the actual timestep-center x positions, so the black window and labels stay aligned across gaps
- the summary viewer camera fit now includes the proportional gap space, not just node bounds
- the summary viewer uses a gap scale factor so large hidden ranges are visibly separated after fit-to-view

This viewer is intentionally standalone and additive.

## Interactive viewer: `interactive_sankey_viewer/stage_04_interactive_sankey_viewer.py`

This is the main file to edit for viewer behavior.

It writes:

- `index.html`
- `viewer.js`
- `style.css`
- `data.json`

into `VIEWER_DIR`.

It also links/copies `sheet_images/` into the viewer directory so thumbnails work in the browser.

### Viewer file responsibilities

#### `write_data_json()`

Creates browser-ready JSON from `OVERLAP_FILE`.

It keeps all node/link metadata and additionally resolves image paths when possible.

#### `write_index_html()`

Writes the page shell:

- header
- controls
- minimap
- chart container
- details panel
- tooltip container

It loads D3 and D3 Sankey from CDN, then `viewer_common.js`, then `viewer.js`.

#### `write_style_css()`

Defines the static layout and visual style:

- three-column application layout
- control panel
- viewer area
- details panel
- tooltip
- node/link styles
- range bar styles

The shared range-bar / viewport styles live in `unified_sankey_viewer/viewer_common.py` and are appended to the generated CSS.

The viewer also loads `viewer_common.js`, which provides the shared top-bar renderer and committed-number-input helper.

#### `write_viewer_js()`

Emits the entire browser app logic.

## Current viewer data model

`data.json` contains:

- `timesteps`
- `nodes`
- `links`
- `viewer`

Node fields used by the viewer:

- `id`
- `timestep_index`
- `timestep_label`
- `sheet_id`
- `rank`
- `area`
- `num_vertices`
- `rsi_file`
- `rsijson_file`
- `thumbnail` / `image` if resolved
- `color` if resolved

Link fields used by the viewer:

- `source`
- `target`
- `overlap_vertices`
- `source_percent`
- `target_percent`
- `source_area`
- `target_area`
- `source_num_vertices`
- `target_num_vertices`

## Viewer interaction model

### Controls

Current controls include:

- minimum overlap percent slider
- percent mode selector:
  - max(source, target)
  - source_percent
  - target_percent
- hide isolated nodes checkbox
- timestep range editor
- add/delete range
- node ordering selector
- node size selector
- node scaling selector:
  - local scaling
  - global scaling
- zoom buttons
- center button

### Range selection

The viewer supports selected timestep ranges.

Important behavior:

- only nodes/links inside the selected ranges are shown
- multiple disjoint ranges are allowed
- the top range bar can be used interactively
- Delete/Backspace removes the selected range

### Pan/zoom

The viewer uses SVG transforms plus scroll/pan management.

Important behavior:

- zoom and pan should not rebuild the graph unless controls change
- camera updates are throttled with `requestAnimationFrame`
- the viewer tries to preserve focus on rerender

### Side details panel

Clicking a node shows:

- sheet metadata
- image thumbnail if available

Clicking a link shows:

- source/target sheet metadata
- source/target images if available

## Node ordering logic

The viewer currently supports:

- decreasing area
- increasing rank
- crossing-minimized

Implementation notes:

- `orderNodes()` groups nodes by timestep
- local ordering is then used for vertical packing
- the crossing-minimized mode uses a barycentric-style iterative sweep

## Node sizing logic

This is one of the most important parts of the viewer.

### `sheet area`

Node height is based on `node.area`.

### `vertex count`

Node height is based on `node.num_vertices`.

### Local scaling

Current behavior:

- each timestep column is normalized separately
- values are comparable inside a timestep, not across timesteps

### Global scaling

The intended behavior is mode-dependent:

- for area mode, use global area-based styling while keeping the node geometry readable
- for vertex-count mode, node height and link thickness are globally comparable across timesteps

The code has evolved a lot here, so when editing this part, check the current branch in
`applyOrderedYPositions()`, `assignLinkOffsets()`, and the fill helper.

## Link geometry

The viewer renders links as filled ribbons, not stroked paths.

Important helpers:

- `assignLinkOffsets()`
- `sankeyRibbonPath(link)`

Link slots are stored explicitly on the link object:

- `_sourceY0`
- `_sourceY1`
- `_targetY0`
- `_targetY1`

This is important because the ribbon path needs both the source and target vertical boundaries.

## Color logic

### Area mode

The node fill has a separate “global area” color mode.

Current state:

- `sheet area + global scaling` uses an alternate fill path
- the fill is generated by `areaGlobalFill(node)`
- node fill uses inline style so CSS does not override it

### Thumbnail-derived color

When thumbnails exist, the viewer also tries to derive a color from the thumbnail file name.

This is mostly a fallback / metadata convenience.

## Important helper functions in `viewer.js`

The current browser app is organized around these functions:

- `getControls()`
- `normalizedRanges()`
- `filterData()`
- `buildDisplayLayout()`
- `orderNodes()`
- `computeBarycentricOrder()`
- `applyTemporalXPositions()`
- `applyOrderedYPositions()`
- `assignLinkOffsets()`
- `sankeyRibbonPath()`
- `areaGlobalFill()`
- `renderSankey()`
- `renderRangeRows()`
- `updateStats()`
- `showNodeDetails()`
- `showLinkDetails()`
- `renderMiniMap()`
- `applyViewportTransform()`

## Sheet images

The viewer can show sheet images if they are available under `SHEET_IMAGE_DIR`.

Behavior:

- images are linked into the viewer folder as `sheet_images/`
- node tooltips and details panels can show the thumbnail
- link details can show source and target images side-by-side

If images are missing, the viewer should still work with metadata only.

## What to watch when editing

### If modifying file paths

Update:

- `common.py`
- `write_data_json()`
- any image-linking logic in the viewer stage

### If modifying node or link sizing

Check:

- `applyOrderedYPositions()`
- `assignLinkOffsets()`
- any mode-specific branch for `area` vs `vertex count`

### If modifying range behavior

Check:

- `normalizedRanges()`
- `filterData()`
- `renderRangeRows()`
- mouse/keyboard handlers in `bindControls()`

### If modifying colors

Check:

- `areaGlobalFill()`
- node `style("fill", ...)`
- CSS rules that may override SVG attributes

### If modifying performance

Check:

- `renderSankey()`
- `scheduleViewportUpdate()`
- `requestAnimationFrame` use
- control event handlers that may trigger full rerenders

## Current practical defaults

The viewer currently starts with:

- the first 0..20 timestep range selected
- local scaling unless explicitly changed
- the interactive D3 viewer as the main output

## Files most likely to be edited next

- `interactive_sankey_viewer/stage_04_interactive_sankey_viewer.py`
- `match_summary_viewer/stage_05_match_summary_viewer.py`
- `common.py`
- `compareSheetShapes/compare_sheet_shapes.py`
- `stage_03_compute_sheet_overlaps.py`
- `stage_02_build_sankey_data.py`
- `stage_04_plot_sankey.py`

## Notes for future Codex sessions

Before changing the viewer, check the current generated output under:

- `OUTPUT_DIR/interactive_sankey_viewer/`

If behavior looks wrong, inspect both:

- the Python generator in `interactive_sankey_viewer/stage_04_interactive_sankey_viewer.py`
- the generated `viewer.js`

That file is the real runtime source for the browser.
