# Reeb-Space Tracking Analysis Notes

This file is a working memory for continuing the paper-oriented analysis of the
generated Reeb-space tracking results. It records what was inspected, how the
numbers in the summary were computed, what is only a heuristic, and what still
needs implementation before these results should be used in a paper.

## Scope Of The Inspection

Repository:

```text
/home/mohit/Desktop/postdoc/timeVaryingReebSpace/scripts/reeb-flow-visualizer
```

Generated datasets inspected:

```text
/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/MVK_s1
/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/MVK_s2
/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/stilbene
/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/torusGrowing
/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/torusMoving
```

Main result files inspected:

```text
sankey/unified_sankey_viewer/data.json
sankey/sheet_overlaps.json
compareSheetShapesCache/results/sheet_shape_matches.json
compareSheetShapesCache/results/sheet_shape_summary.json
compareSheetShapesCache/cache/global_bounds.json
sheetRendering/<step>/step_<id>.png
sheetFiberSurfaceImages/<step>/fiber_surface_images_manifest.json
```

Relevant code paths:

```text
stage_02_build_sankey_data.py
stage_03_compute_sheet_overlaps.py
compareSheetShapes/compare_sheet_shapes.py
unified_sankey_viewer/stage_07_unified_sankey_viewer.py
```

## Current Metrics

The current shape matching code computes these metrics for every pair of top
sheets in adjacent timesteps:

- `shape_iou`: raster-mask intersection-over-union of the two sheet polygons in
  the bivariate range plane.
- `support_jaccard`: Jaccard overlap of the regular domain vertices stored in
  the RSI file for the two sheets.
- `area_ratio`: `min(area_a, area_b) / max(area_a, area_b)`.
- `bbox_iou`: intersection-over-union of the range-space bounding boxes.
- `centroid_similarity`: `exp(-centroid_distance / global_range_diagonal)`.
- `combined`: weighted sum using `SHAPE_SCORE_DEFAULT_WEIGHTS` in `common.py`.

Current weights:

```python
{
    "shape_iou": 0.40,
    "support_jaccard": 0.30,
    "area_ratio": 0.15,
    "bbox_iou": 0.10,
    "centroid_similarity": 0.05,
}
```

Important caveat: saying `shape_iou` is good because it agrees with `combined`
is partially circular, since `shape_iou` has the largest weight in `combined`.
The stronger claim is more modest:

1. `shape_iou` is highly discriminative in the generated data.
2. It usually selects the same target as the current combined score.
3. It agrees with visual intuition better than vertex Jaccard for range-space
   sheet shape tracking.
4. It should still be validated by manual inspection and a sensitivity study.

## How Metric Usefulness Was Judged

The previous inspection used four practical diagnostics:

1. Distribution: does the metric have useful spread, or is it almost always
   close to zero or close to one?
2. Redundancy with current combined score: correlation with `combined`.
3. Best-target agreement: for each source sheet, choose the best target by one
   metric and compare that target to the best target chosen by `combined`.
4. Domain-vs-shape disagreement: compare best target by domain overlap
   (`overlap_max_percent`) to best target by shape `combined`.

Observed best-target agreement with `combined`:

| Dataset | `shape_iou` | `bbox_iou` | `support_jaccard` | `area_ratio` | `centroid_similarity` |
|---|---:|---:|---:|---:|---:|
| MVK_s1 | 95.6% | 94.2% | 50.0% | 65.9% | 90.9% |
| MVK_s2 | 93.7% | 91.4% | 48.6% | 58.1% | 88.0% |
| stilbene | 96.1% | 89.3% | 40.5% | 47.7% | 87.1% |
| torusGrowing | 85.4% | 44.2% | 70.2% | 43.1% | 42.7% |
| torusMoving | 37.7% | 29.2% | 46.5% | 89.2% | 31.2% |

Interpretation:

- For real datasets, `shape_iou` is the clearest single shape metric.
- `bbox_iou` is useful but less precise than `shape_iou`.
- `centroid_similarity` is often high for many candidates, so it is weak as a
  standalone matching metric.
- `support_jaccard` is often small. This does not make it useless; it means it
  measures a different question: whether the same domain vertices participate.
- Synthetic torus results need more care. Many tiny or zero-vertex sheets make
  event and metric statistics noisy.

## Domain-vs-Shape Disagreement

Best target by vertex/domain overlap and best target by shape matching agree
very little:

| Dataset | Agreement |
|---|---:|
| MVK_s1 | 9.7% |
| MVK_s2 | 9.6% |
| stilbene | 8.7% |
| torusGrowing | 16.0% |
| torusMoving | 1.8% |

This is not necessarily bad. It suggests the two metrics capture different
notions:

- Domain overlap: continuity of the spatial support in the input domain.
- Shape/range overlap: continuity of the sheet footprint in the bivariate range
  space.

For the paper, this disagreement can be used as evidence that Reeb-space sheet
tracking supports complementary views of feature evolution.

## Continuity Threshold

The continuity threshold used in the previous summary was not part of the
pipeline. It was an exploratory post-processing heuristic.

For adjacent timesteps `T_i` and `T_{i+1}`, let

```text
C(s, t) = combined shape score between source sheet s in T_i
          and target sheet t in T_{i+1}.
```

For each source sheet:

```text
best_out(s) = max_t C(s, t)
```

For each target sheet:

```text
best_in(t) = max_s C(s, t)
```

Using a threshold `theta = 0.5`:

- `best_out(s) < theta` was counted as a weak/no continuation for source sheet
  `s`.
- `best_in(t) < theta` was counted as a weak/no predecessor for target sheet
  `t`.
- A source with two or more targets above `theta` was counted as a possible
  split.
- A target with two or more sources above `theta` was counted as a possible
  merge.

The threshold `0.5` was chosen only as a mid-scale heuristic for a score in
`[0, 1]`. It is not yet calibrated. Before using it in the paper, implement a
threshold sensitivity analysis over values such as `0.3`, `0.4`, `0.5`, `0.6`,
and `0.7`.

## Candidate Intervals From Current Data

These are candidate intervals for closer inspection, not final paper claims.

### MVK_s1

Strongest event region:

```text
step_01180 -> step_01220
```

Most notable pair:

```text
step_01200 -> step_01220
```

For `1200 -> 1220`, mean best combined score was about `0.396`, and 18/20 top
sheets had best continuation below `0.5`.

Long-lived feature:

```text
rank-1 sheet persists across all 82 available timesteps
```

### MVK_s2

Strongest event region:

```text
step_01120 -> step_01180
```

Notable pairs:

```text
step_01140 -> step_01160
step_01160 -> step_01180
```

The dominant rank-1 feature appears to break/reconfigure around this interval.

### stilbene

Strongest event region:

```text
step_11910 -> step_11925
```

Most notable pairs:

```text
step_11915 -> step_11920
step_11920 -> step_11925
```

Both pairs had all 20 top sheets below the exploratory `0.5` continuity
threshold. This is a strong candidate for a paper figure, especially because the
previous CSP/moment paper reports an important late coupling window for
stilbene.

Other large top-area jumps:

```text
step_01220 -> step_01240
step_06360 -> step_06380
step_06440 -> step_06480
step_07660 -> step_07680
step_09580 -> step_09600
```

These need visual/fiber-surface inspection before interpretation.

### Synthetic Torus

Useful for validation, but not yet clean enough for a main result.

Torus growing:

- rank-1, rank-2, and rank-3 tracks persist across all 25 timesteps.
- many tiny/zero-vertex top sheets create noisy birth/death counts.

Torus moving:

- rank-1 track persists across all 25 timesteps.
- top area jumps at `40 -> 60`, `280 -> 300`, and `440 -> 460`, but this may be
  a synthetic construction artifact.

## Implemented Analysis Stage

The first reproducible analysis stage has now been implemented in:

```text
stage_06_analyze_tracking_results.py
```

It consumes existing `sankey/unified_sankey_viewer/data.json` files and does
not recompute Reeb spaces, sheet VTPs, sheet images, or fiber-surface images.

Default run for `common.BASE_DIR`:

```bash
python3 stage_06_analyze_tracking_results.py
```

Run for all known datasets:

```bash
python3 stage_06_analyze_tracking_results.py --all-known
```

Outputs are written to:

```text
<dataset>/sankey/tracking_analysis/
```

The stage writes:

```text
metric_summary.csv
best_target_agreement.csv
event_scores.csv
sheet_lifetimes.csv
interesting_intervals.json
analysis_metadata.json
plots/*.png
plots/*.pdf
```

`common.py` now contains analysis thresholds, preferred threshold, top interval
count, output directory, and the optional pipeline flag
`RUN_STAGE_6_TRACKING_ANALYSIS`. The pipeline hook runs this stage after the
unified Sankey viewer when that flag is enabled.

## Remaining Work After The Analysis Stage

The first analysis stage is implemented and writes the required CSV/JSON/plot
outputs. Before writing final paper results, the remaining work is to interpret
and validate those outputs rather than to invent the diagnostics manually again.

Concrete next steps:

1. Inspect `interesting_intervals.json` for each dataset and select a small
   number of intervals for paper figures.
2. Use `event_scores.csv` to check threshold sensitivity across 0.3, 0.4, 0.5,
   0.6, and 0.7. Important intervals should remain visible across nearby
   thresholds.
3. Use `sheet_lifetimes.csv` to identify persistent tracks and decide which
   tracks should be shown or summarized.
4. Use `best_target_agreement.csv` and `metric_summary.csv` to justify which
   metrics are primary and which are supplementary.
5. Manually inspect sheet and fiber-surface images linked from
   `interesting_intervals.json` before making chemical or structural claims.
6. Decide how to report zero-regular-vertex sheets, especially for the synthetic
   torus datasets.

Important: the implemented analysis stage still uses heuristic thresholds. The
threshold sensitivity results and visual inspection must be part of the paper
selection process.

## Paper Narrative Direction

The likely paper story is:

1. Existing CSP/moment work summarizes atom- or segment-level bivariate behavior
   through moment tracks.
2. Reeb-space sheets provide a complementary feature-level decomposition of the
   bivariate field.
3. Tracking sheets over time reveals persistence, reconfiguration, and
   birth/death-like behavior of bivariate features.
4. Domain overlap and range-shape similarity answer different questions and
   should be shown side by side.
5. Shape IoU is the most promising primary range-shape metric, while other
   shape descriptors are supporting diagnostics or supplementary material.

## Caution For Future Work

Do not present the exploratory threshold/event counts as final until:

- threshold sensitivity is implemented;
- candidate events are visually inspected in the viewer;
- sheet and fiber-surface images for the key intervals are checked;
- degenerate/zero-vertex top sheets are handled consistently in summaries;
- synthetic torus data is cleaned or clearly described as a validation toy case.

