# Stilbene Results Analysis

This note summarizes the current stilbene results from:

- `/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/stilbene`

The analysis uses the regenerated tracking outputs in
`sankey/tracking_analysis/` and the current viewer data in
`sankey/unified_sankey_viewer/data.json`.

## Main Finding

After fixing the duplicate-timestep-index issue in the generated stilbene
viewer/tracking data, the previous `9495->9500` peak is no longer a valid top
event. The corrected range-space result is dominated by a robust late event
around `11915->11925`, which remains prominent under threshold and stride
sensitivity checks.

Unlike MVK, there is no external CSP/image-moment transition label in the
current notes, so the result should be framed as a Reeb-sheet tracking finding:
the method identifies specific intervals where prominent bivariate sheets lose
shape continuity.

## Range-Space Event Intervals

Event scores use the range/shape score with threshold `theta = 0.5`.

| Interval | Event score | Mean best score | Weak source/target | Split/merge | Observation |
|---:|---:|---:|---:|---:|---|
| `11915->11920` | `54.49` | `0.276` | `20/20` | `0/0` | Strongest range reconfiguration. |
| `11920->11925` | `53.44` | `0.328` | `20/20` | `0/0` | Continuation of late event. |
| `9380->9400` | `35.94` | `0.478` | `13/12` | `1/0` | Earlier secondary range event. |
| `9660->9680` | `28.99` | `0.550` | `9/9` | `2/2` | Secondary local reconfiguration. |
| `11910->11915` | `28.13` | `0.494` | `9/9` | `0/0` | Onset of late event. |
| `2420->2440` | `27.65` | `0.518` | `9/9` | `0/0` | Earlier secondary event. |

The corrected `11915->11920` event has all 20 top source and target sheets
below the `0.5` continuation threshold, with mean best shape score `0.276`.

## Long-Lived Reeb-Sheet Tracks

Despite the strong event intervals, the dataset also contains long-lived
feature families.

| Time labels | Sheet endpoints | Length | Rank range | Mean continuation |
|---:|---|---:|---:|---:|
| `2100->5340` | `86 -> 6270` | `163` | `1-10` | `0.860` |
| `9400->11255` | `38 -> 19239` | `162` | `1-8` | `0.844` |
| `5540->8700` | `19 -> 53844` | `159` | `1-14` | `0.916` |
| `10320->11915` | `19 -> 22` | `143` | `1-11` | `0.850` |
| `2600->5340` | `12536 -> 6270` | `138` | `1-10` | `0.866` |
| `7240->9660` | `1082 -> 172` | `134` | `2-17` | `0.834` |

These tracks are the most useful stilbene evidence after the event intervals:
they show that the approach can follow prominent bivariate features over long
parts of a larger molecular trajectory.

## Threshold Sensitivity

The late `11915->11920` event is stable across thresholds:

| Threshold | Top interval | Max event score | Max lifetime | Median lifetime |
|---:|---:|---:|---:|---:|
| `0.3` | `11915->11920` | `40.99` | `494` | `28` |
| `0.4` | `11915->11920` | `54.49` | `415` | `5` |
| `0.5` | `11915->11920` | `54.49` | `163` | `3` |
| `0.6` | `11915->11920` | `54.49` | `142` | `2` |
| `0.7` | `11915->11920` | `54.49` | `101` | `1` |

As expected, stricter thresholds shorten the median track length, but the top
event interval does not move.

## Sampling-Stride Check

The stride check confirms the late range event:

| Stride | Top range interval | Event score | Interpretation |
|---:|---:|---:|---|
| `1` | `11915->11920` | `54.49` | Strong late event. |
| `2` | `11910->11920` | `54.90` | Late event becomes dominant. |
| `3` | `11905->11920` | `54.87` | Late event remains dominant. |
| `4` | `11900->11920` | `54.87` | Late event remains dominant under coarser sampling. |

Conclusion: `11915->11925` is the robust stilbene range-space event after the
index correction.

## Domain-Space Events And Complementarity

Domain event ranking is useful for stilbene as a separate domain-space result.
It identifies intervals where the same domain vertices stop supporting the same
sheet behavior. These intervals are not expected to match the range-event
ranking.

Top domain events at threshold `theta = 0.5`:

| Interval | Domain event score | Mean best overlap | Weak source/target | Split/merge |
|---:|---:|---:|---:|---:|
| `8440->8460` | `58.44` | `0.078` | `20/20` | `0/0` |
| `8400->8420` | `58.29` | `0.086` | `20/20` | `0/0` |
| `6940->6960` | `58.25` | `0.088` | `20/20` | `0/0` |
| `8420->8440` | `58.24` | `0.088` | `20/20` | `0/0` |
| `6920->6940` | `58.23` | `0.089` | `20/20` | `0/0` |

These are good candidates for domain-view inspection, especially if we want to
show that domain support can reorganize at intervals different from the
range-shape event peaks.

Domain/range target disagreement is more useful after the index correction. The
top disagreement summaries now compare all 20 source sheets:

| Interval | Compared sources | Agreement fraction | Max disagreement score |
|---:|---:|---:|---:|
| `5260->5280` | `20` | `0.000` | `44.53` |
| `8940->8960` | `20` | `0.050` | `43.92` |
| `9060->9080` | `20` | `0.050` | `43.14` |

This can support targeted visual inspection. Domain event ranking remains the
main domain-space result, while disagreement can help choose concrete
source/target examples.

## Metric Usefulness

For stilbene, `combined` is identical to `shape_iou`.

| Candidate metric | Agreement with `combined` | Mean loss if used |
|---|---:|---:|
| `shape_iou` | `1.000` | `0.000` |
| `bbox_iou` | `0.882` | `0.032` |
| `centroid_similarity` | `0.849` | `0.043` |
| `area_ratio` | `0.568` | `0.232` |
| `overlap_max_percent` | `0.022` | `0.596` |

Recommendation: keep `shape_iou` as the primary metric. Keep `bbox_iou` and
`centroid_similarity` only as secondary diagnostics. `area_ratio` and
domain-overlap best-target agreement are not good substitutes for shape-based
tracking in this dataset.

## Suggested Figures

1. Stilbene event overview.
   - Show range/shape mode with `theta = 0.5`.
   - Include a panel around `9380-9680` and a panel around `11900-11935`.
   - Highlight `11915->11920`, `11920->11925`, `9380->9400`, and
     `9660->9680`.

2. Long-lived feature tracks.
   - Highlight tracks `86 -> 6270`, `38 -> 19239`, and `19 -> 53844`.
   - Show that persistent features coexist with sharp event intervals.

3. Sheet/fiber detail views.
   - Capture clicked-link details for `11915->11920` and `11920->11925`.
   - Use side-by-side sheet images and fiber-surface images to verify whether
     the metric event corresponds to a visually meaningful sheet change.

## Viewer Feature Recommendations From Stilbene

Most useful:

- Range event graph and ranked interval table.
- Domain event ranking.
- Domain/range disagreement examples.
- Long-lived track table.
- Threshold sensitivity.
- Stride selector, because it verifies that the late event persists under
  coarser sampling.
- Sheet/fiber image detail panels.

Less useful as paper evidence:

- Area-ratio metric as a main correspondence selector.
