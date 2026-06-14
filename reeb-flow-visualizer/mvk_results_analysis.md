# MVK Results Analysis

This note summarizes the regenerated MVK results from:

- `/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1`
- `/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s2`

The analysis uses the current `sankey/unified_sankey_viewer/data.json`,
`sankey/tracking_data.json`, `sankey/tracking_analysis/*.csv`, and
`sankey/tracking_analysis/viewer_analysis.json` artifacts.

Important implementation note: `stage_06_analyze_tracking_results.py` was
updated to look up sheets by `timestep_index` instead of assuming that
`timesteps[i]` has index `i`. This matters for non-uniformly ordered timestep
lists and keeps the exported diagnostics aligned with the browser viewer.

The MVK timestep labels convert to femtoseconds as:

```text
time_fs = step_label / 41.341374575751
```

Thus labels `1240-1280` correspond to approximately `30.00-30.96 fs`.

## Alignment Check After Rerun

The main MVK result is still aligned with the previous writeup: range-space
Reeb-sheet event scores recover the known MVK transition region near
`29-31 fs`. There is no new qualitative finding that changes the paper story.

There are two factual updates from the rerun:

- In `MVK_s1`, `1200->1220` is now the strongest range event, followed by
  `1180->1200`. The same transition window remains dominant.
- In `MVK_s2`, the post-transition interval is now `1340->1380`, not
  `1340->1360`, because the current artifacts do not contain
  `step_01360.rsijson`. Its event score is stronger than before.

## Range-Space Event Intervals

Event scores use the range/shape score with threshold `theta = 0.5`. In the
current configuration, `combined` is identical to `shape_iou`.

| Dataset | Interval | Time window | Event score | Mean best score | Interpretation |
|---|---:|---:|---:|---:|---|
| MVK_s1 | `1200->1220` | `29.03->29.51 fs` | `35.77` | `0.412` | Strongest MVK_s1 range continuation loss. |
| MVK_s1 | `1180->1200` | `28.54->29.03 fs` | `34.63` | `0.468` | Start of strong range-sheet reconfiguration. |
| MVK_s1 | `1276->1280` | `30.86->30.96 fs` | `31.70` | `0.515` | Late dense-window reconfiguration. |
| MVK_s2 | `1140->1160` | `27.58->28.06 fs` | `37.21` | `0.439` | Earliest strong MVK_s2 range reconfiguration. |
| MVK_s2 | `1340->1380` | `32.41->33.38 fs` | `33.14` | `0.443` | Post-transition range reconfiguration; spans missing `1360`. |
| MVK_s2 | `1160->1180` | `28.06->28.54 fs` | `30.71` | `0.440` | Continuation of the main event window. |

This supports the same application-level connection as before: Reeb-space sheet
tracking independently marks the broad transition window reported by the prior
continuous-scatterplot/image-moment MVK analysis. The current metrics do not
identify oxygen, C3, or C4 directly; that claim still needs sheet/fiber views
with atom-aware inspection.

## Long-Lived Reeb-Sheet Tracks

The range metric also gives persistent feature families.

| Dataset | Time span | Sheet endpoints | Rank range | Mean continuation |
|---|---:|---|---:|---:|
| MVK_s1 | `0.00->35.80 fs` | `0 -> 6341` | `1-1` | `0.978` |
| MVK_s1 | `0.00->35.80 fs` | `5251 -> 8` | `2-2` | `0.950` |
| MVK_s1 | `0.00->35.80 fs` | `4 -> 5` | `3-3` | `0.896` |
| MVK_s2 | `0.00->35.80 fs` | `24 -> 503` | `2-4` | `0.900` |
| MVK_s2 | `0.00->27.58 fs` | `22 -> 1892` | `1-1` | `0.948` |
| MVK_s2 | `13.06->35.80 fs` | `2 -> 0` | `1-4` | `0.899` |

These tracks are useful evidence that the method gives feature-level temporal
structure, not only independent timestep descriptors.

## Sampling-Stride Check

The same broad MVK transition is recovered under the precomputed timestep
strides from 1 to 4.

| Dataset | Stride | Top range interval | Event score | Interpretation |
|---|---:|---:|---:|---|
| MVK_s1 | 1 | `1200->1220` | `35.77` | Main peak at `29.03-29.51 fs`. |
| MVK_s1 | 2 | `1180->1220` | `47.66` | Coarser window covers the same peak. |
| MVK_s1 | 3 | `1160->1220` | `49.71` | Coarser window still centers on the transition. |
| MVK_s1 | 4 | `1140->1220` | `50.39` | Coarser window expands over the same event. |
| MVK_s2 | 1 | `1140->1160` | `37.21` | Main peak at `27.58-28.06 fs`. |
| MVK_s2 | 2 | `1140->1180` | `50.55` | Same early transition window. |
| MVK_s2 | 3 | `1120->1180` | `53.39` | Same early transition window. |
| MVK_s2 | 4 | `1100->1180` | `53.80` | Same early transition window. |

Conclusion: the MVK range-event result is robust to the available sampling
strides.

## Domain-Space Events And Complementarity

Domain event ranking is useful and should stay in the results. It answers a
different question from range-space ranking: when do the same mesh vertices stop
supporting the same sheet behavior? It is not expected to reproduce the
CSP/image-moment timing, because that prior work analyzes range-space
descriptors rather than persistent vertex support.

Current top domain-overlap events at threshold `theta = 0.5`:

| Dataset | Top domain interval | Domain event score | Mean best overlap | Weak source/target | Interpretation |
|---|---:|---:|---:|---:|---|
| MVK_s1 | `740->760` | `57.07` | `0.147` | `20/20` | Strong spatial-support behavior change. |
| MVK_s1 | `760->780` | `56.98` | `0.151` | `20/20` | Continuation of the same support-change band. |
| MVK_s1 | `820->840` | `56.96` | `0.152` | `20/20` | Later support-change band. |
| MVK_s2 | `560->580` | `56.70` | `0.165` | `20/20` | Strong spatial-support behavior change. |
| MVK_s2 | `960->980` | `56.57` | `0.171` | `20/20` | Later support-change band. |
| MVK_s2 | `520->540` | `56.48` | `0.176` | `20/20` | Early/mid support-change band. |

These intervals are a separate result direction: they identify when the domain
support of prominent sheets changes, even when the range-space event score is
not at its maximum.

Domain/range target disagreement then explains why the two rankings differ. It
asks whether the target chosen by range shape and the target chosen by domain
overlap are the same for a given source sheet. The strongest disagreement
summaries include transition-window examples:

| Dataset | Interval | Agreement fraction | Strongest disagreement score |
|---|---:|---:|---:|
| MVK_s1 | `1240->1244` | `0.050` | `40.15` |
| MVK_s1 | `1220->1240` | `0.105` | `35.91` |
| MVK_s1 | `1200->1220` | `0.105` | `22.73` |
| MVK_s2 | `1244->1248` | `0.050` | `44.34` |
| MVK_s2 | `1220->1240` | `0.100` | `43.85` |
| MVK_s2 | `1248->1252` | `0.100` | `42.72` |

Insight from disagreement: in the transition window, range and domain often
prefer different targets for the same source sheet. This suggests that a sheet
can keep a similar range-space footprint while its supporting vertices move, or
that the same vertices can start supporting a different range-space feature.

Recommendation: keep the target-disagreement table/detail view because it gives
actionable source/target examples for figures. De-emphasize only the aggregate
Spearman correlation and unnormalized complementarity magnitude, because those
summaries are harder to interpret than the selected source/target examples.

## Metric Usefulness

Current metric agreement against `combined`:

| Dataset | Candidate metric | Agreement | Mean loss if used |
|---|---|---:|---:|
| MVK_s1 | `shape_iou` | `1.000` | `0.000` |
| MVK_s1 | `bbox_iou` | `0.951` | `0.016` |
| MVK_s1 | `centroid_similarity` | `0.921` | `0.028` |
| MVK_s1 | `area_ratio` | `0.815` | `0.098` |
| MVK_s1 | `overlap_max_percent` | `0.112` | `0.630` |
| MVK_s2 | `shape_iou` | `1.000` | `0.000` |
| MVK_s2 | `bbox_iou` | `0.931` | `0.014` |
| MVK_s2 | `centroid_similarity` | `0.897` | `0.029` |
| MVK_s2 | `area_ratio` | `0.733` | `0.134` |
| MVK_s2 | `overlap_max_percent` | `0.077` | `0.627` |

This confirms that `shape_iou` should be the primary range metric. `bbox_iou`
and `centroid_similarity` are mostly redundant sanity checks; `area_ratio` is
less reliable as a correspondence metric; domain overlap is intentionally
different and should not be treated as a substitute for range matching.

## Suggested Figures

1. Range event overview around the transition.
   - MVK_s1: highlight `1200->1220`, `1180->1200`, `1276->1280`.
   - MVK_s2: highlight `1140->1160`, `1160->1180`, and `1340->1380`.
   - Use range/shape mode, metric `shape_iou`, threshold `0.5`.

2. Long-lived feature tracks.
   - MVK_s1: tracks starting at sheets `0`, `5251`, and `4`.
   - MVK_s2: tracks starting at sheets `24`, `22`, and `2`.

3. Domain/range disagreement detail views near the dense transition window.
   - MVK_s1: `1240->1244`, strongest source sheet `978`.
   - MVK_s2: `1244->1248`, strongest source sheet `144`.
   - Capture side-by-side sheet images and fiber-surface images.

## Viewer Feature Recommendations From MVK

Most useful:

- Range/shape event intervals.
- Domain event ranking.
- Continuing-feature tracks.
- Stride selector and threshold sensitivity.
- Sankey highlighting linked to selected event/track rows.
- Sheet and fiber-surface detail images.

Useful but secondary:

- Domain/range target disagreement examples.

De-emphasize or remove from the main analysis flow:

- Domain/range Spearman correlation.
- Unnormalized complementarity magnitude.
- Low-origin scalar filtering for MVK; it did not change the range conclusion.
