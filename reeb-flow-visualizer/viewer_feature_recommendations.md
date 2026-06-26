# Viewer Feature Recommendations

These recommendations come from the regenerated MVK, stilbene, and torus
analyses.

## Keep As Primary

- Range/shape event intervals using `shape_iou`.
- Domain event ranking. This answers a different question: when the same
  domain vertices change which Reeb-space sheet behavior they support. It is
  not expected to match range-space event timing.
- Continuing-feature track table and Sankey highlighting.
- Threshold control for event and track sensitivity.
- Timestep stride selector. It is useful: MVK and torus remain stable across
  strides, and the corrected stilbene data also preserves its late event across
  strides.
- Linked sheet-image and fiber-surface detail panels.

## Keep, But Reframe

- Domain-overlap mode should remain available as a spatial-support inspection
  mode. It should not be presented as a replacement for range-space tracking.

## Hide Or Remove From The Main Flow

- Best supported range/domain intervals are computed by the runtime/backend
  but are hidden from the current viewer UI. The main event rankings remain the
  primary interval diagnostics.
- Domain/range target disagreement/complementarity is still available in the
  runtime/backend and generated analysis data, but the tab is hidden from the
  current viewer UI because it is not part of the primary paper workflow.
- `geometry_iou` is still computed and stored with range matches, but the
  Sheet geometry IoU metric option is hidden from the current viewer UI.
- Domain/range Spearman correlation was removed from the viewer because it did
  not provide a clean paper result in MVK, stilbene, or torus.
- `area_ratio` as a main correspondence metric. It has weak agreement with the
  shape-based target, especially for stilbene.
- `bbox_iou` and `centroid_similarity` as primary metric choices. They are
  useful sanity checks but mostly redundant for the current paper narrative.

## Naming And Defaults

- Default metric should be `shape_iou`.
- If `combined` remains identical to `shape_iou`, either remove the separate
  `combined` label from the UI or show it as `shape_iou (current combined)`.
- The default analysis panel should show range events first, then tracks, then
  optional domain-support inspection.

## Implementation Note

- Unnormalized complementarity magnitude has been fixed by normalizing the
  domain percentage to `[0, 1]` before computing domain/range disagreement.

`stage_06_analyze_tracking_results.py` now looks up sheets by `timestep_index`
instead of list position. Keep this behavior aligned with the browser runtime,
which also indexes timesteps by their explicit `timestep_index` field.
