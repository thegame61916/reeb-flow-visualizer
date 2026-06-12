# MVK Results Analysis

This note summarizes the current MVK results from:

- `/media/mohit/4TB_kingston_tufA2/hpc/datasets/MVK_s1`
- `/media/mohit/4TB_kingston_tufA2/hpc/datasets/MVK_s2`

It uses the current generated artifacts as the baseline. I also tested the
near-origin low-scalar exclusion in memory, without overwriting the baseline
outputs.

External references used for comparison:

- Main CSP/image-moment paper:
  `https://vgl.csa.iisc.ac.in/pdf/pub/Paper_Continuous_Scatterplot_and_Image_Moments_for_Time_Varying_Bivariate_Field_Analysis.pdf`
- Supplementary material:
  `https://vgl.csa.iisc.ac.in/pdf/pub/Supp_Material_Continuous_Scatterplot_and_Image_Moments_for_Time_Varying_Bivariate_Field_Analysis.pdf`

## Files Inspected

For both `MVK_s1` and `MVK_s2`, the analysis used:

- `sankey/unified_sankey_viewer/data.json`
- `sankey/tracking_data.json`
- `sankey/sheet_overlaps.json`
- `sankey/rsi_json/*.rsijson`
- `sankey/tracking_analysis/*.csv`
- `sankey/tracking_analysis/viewer_analysis.json`
- `compareSheetShapesCache/results/sheet_shape_summary.json`
- `compareSheetShapesCache/cache/matches/*.json`
- `downsampledGrids/*.vtu`
- `sheetInfo/*.rsi`

The viewer maps a timestep label to femtoseconds as:

```text
time_fs = step_label / 41.341374575751
```

Thus labels `1240-1280` correspond to approximately `30.00-30.96 fs`.

## Relationship To Previous MVK Findings

The previous continuous-scatterplot and image-moment study reports an important
MVK transition window near the S2 to S1 approach to the conical intersection,
with dense sampling around approximately `29-31 fs`. It also reports strong
changes in the oxygen track after about `26 fs`, and changes involving the vinyl
carbon atoms C3 and C4 near the transition.

Our strongest range-space Reeb-sheet event scores occur in the same temporal
neighborhood:

| Dataset | Top range interval | Time window | Event score | Mean best range score | Interpretation |
|---|---:|---:|---:|---:|---|
| MVK_s1 | `1180->1200` | `28.54->29.03 fs` | `36.66` | `0.467` | Start of strong range-sheet reconfiguration. |
| MVK_s1 | `1200->1220` | `29.03->29.51 fs` | `35.77` | `0.412` | Strongest continuation loss in MVK_s1. |
| MVK_s1 | `1276->1280` | `30.86->30.96 fs` | `31.70` | `0.515` | Late dense-window reconfiguration. |
| MVK_s2 | `1140->1160` | `27.58->28.06 fs` | `37.21` | `0.439` | Earliest strong range-sheet reconfiguration. |
| MVK_s2 | `1160->1180` | `28.06->28.54 fs` | `30.71` | `0.440` | Continuation of the same event window. |
| MVK_s2 | `1340->1360` | `32.41->32.90 fs` | `22.54` | `0.573` | Post-transition range reconfiguration. |

This is the clearest result: Reeb-space range-sheet tracking independently
marks the same broad transition interval reported by the CSP/image-moment
analysis. It does not yet identify C3, C4, or oxygen by itself, because the
current sheet tracking does not attach atom labels to sheets. The sheet and
fiber-surface image views should be used to inspect whether the selected sheets
are spatially close to the carbonyl oxygen and vinyl C3/C4 region.

## What The Current Method Adds

The CSP/image-moment paper gives timestep-level and atom-track summaries of the
continuous scatterplot. Our Reeb-space analysis adds feature-level continuity:
it tracks individual sheets and shows which prominent sheets persist, weaken,
split, merge, or switch correspondence.

The range metric gives several long-lived feature families:

| Dataset | Track | Time span | Sheet path endpoints | Rank range | Mean continuation |
|---|---:|---:|---|---:|---:|
| MVK_s1 | Range track | `0.00->35.80 fs` | `sheet 0 -> sheet 6341` | `1-1` | `0.978` |
| MVK_s1 | Range track | `0.00->35.80 fs` | `sheet 5251 -> sheet 8` | `2-2` | `0.950` |
| MVK_s1 | Range track | `0.00->35.80 fs` | `sheet 4 -> sheet 5` | `3-3` | `0.896` |
| MVK_s2 | Range track | `0.00->35.80 fs` | `sheet 24 -> sheet 503` | `2-4` | `0.901` |
| MVK_s2 | Range track | `0.00->27.58 fs` | `sheet 22 -> sheet 1892` | `1-1` | `0.948` |
| MVK_s2 | Range track | `13.06->35.80 fs` | `sheet 2 -> sheet 0` | `1-4` | `0.900` |

These tracks are useful evidence that large Reeb-space sheets can be followed as
features, not only remeasured independently at each timestep. The event
diagnostics then identify where those feature correspondences become weak.

## Domain-Space Versus Range-Space Results

Range-space similarity answers: do two sheets occupy a similar region and shape
in the bivariate range? Domain-space overlap answers: do the same mesh vertices
continue to support corresponding sheets?

The two modes are complementary for MVK.

Range-space intervals concentrate around the known photochemical transition
window:

- MVK_s1: `28.54-31.45 fs`
- MVK_s2: `27.58-29.51 fs`, with another event at `32.41-32.90 fs`

Domain-space intervals instead emphasize early and mid-trajectory spatial
support churn:

| Dataset | Top domain interval | Time window | Domain event score | Mean best normalized overlap |
|---|---:|---:|---:|---:|
| MVK_s1 | `460->480` | `11.13->11.61 fs` | `49.34` | `0.108` |
| MVK_s1 | `960->980` | `23.22->23.71 fs` | `49.11` | `0.095` |
| MVK_s1 | `780->800` | `18.87->19.35 fs` | `49.08` | `0.096` |
| MVK_s2 | `320->340` | `7.74->8.22 fs` | `52.63` | `0.068` |
| MVK_s2 | `300->320` | `7.26->7.74 fs` | `52.60` | `0.070` |
| MVK_s2 | `260->280` | `6.29->6.77 fs` | `52.58` | `0.071` |

The domain intervals do not duplicate the range transition story. They show that
spatial support can reorganize substantially even when the strongest
range-space event is elsewhere. This is useful if framed as a different
question: range asks about feature geometry in the bivariate map, while domain
asks about which parts of the molecule/domain support those features.

## Domain-Change Diagnostic

The domain-change diagnostic uses source retention, target inheritance,
split/merge counts, and a churn term. It is more directly interpretable than raw
domain interval scores for MVK.

Top domain-change intervals:

| Dataset | Top interval | Time window | Domain-change score | Mean retention | Mean inheritance | Churn |
|---|---:|---:|---:|---:|---:|---:|
| MVK_s1 | `720->740` | `17.42->17.90 fs` | `40.96` | `0.149` | `0.156` | `0.848` |
| MVK_s1 | `560->580` | `13.55->14.03 fs` | `40.86` | `0.163` | `0.151` | `0.843` |
| MVK_s1 | `660->680` | `15.96->16.45 fs` | `40.84` | `0.156` | `0.160` | `0.842` |
| MVK_s2 | `320->340` | `7.74->8.22 fs` | `39.41` | `0.181` | `0.179` | `0.820` |
| MVK_s2 | `300->320` | `7.26->7.74 fs` | `39.40` | `0.177` | `0.183` | `0.820` |
| MVK_s2 | `280->300` | `6.77->7.26 fs` | `39.38` | `0.185` | `0.177` | `0.819` |

This diagnostic seems useful, but it should be presented as a domain-support
reorganization score, not as a replacement for range event scores.

## Domain/Range Complementarity

The current complementarity tab is useful as an exploration aid, but I would not
make it a central paper result yet.

There is a scoring issue in the current implementation: the range score is on
`0-1`, while the domain max-overlap value used in the complementarity score is a
percentage on `0-100`. This makes the current complementarity magnitude hard to
interpret. For this analysis, I also computed a normalized variant:

```text
range_best = best range-IoU target score in [0, 1]
domain_best = best domain max-percent target score / 100

range_loss = range_best - range_score_for_domain_chosen_target
domain_loss = domain_best - domain_score_for_range_chosen_target
confidence = min(range_best, domain_best)

normalized_complementarity = 0.5 * (range_loss + domain_loss) * confidence
```

This normalized diagnostic gives more interpretable examples:

| Dataset | Interval | Time window | Max normalized complementarity | Agreement fraction | Strongest source | Range target | Domain target |
|---|---:|---:|---:|---:|---:|---:|---:|
| MVK_s1 | `1240->1244` | `29.99->30.09 fs` | `0.737` | `0.050` | `978` | `1095` | `78` |
| MVK_s1 | `1220->1240` | `29.51->29.99 fs` | `0.732` | `0.105` | `133` | `3` | `53` |
| MVK_s2 | `1244->1248` | `30.09->30.19 fs` | `0.801` | `0.050` | `144` | `128` | `117` |
| MVK_s2 | `1220->1240` | `29.51->29.99 fs` | `0.798` | `0.100` | `96` | `99` | `299` |
| MVK_s2 | `1248->1252` | `30.19->30.28 fs` | `0.755` | `0.100` | `128` | `120` | `109` |

This is promising because both stages show low agreement and high normalized
complementarity near the known transition window. However, the plot is not as
clean as the range-event plot, and the Spearman correlation is not smooth:

| Dataset | Spearman mean | Min | Median | Max |
|---|---:|---:|---:|---:|
| MVK_s1 baseline | `-0.094` | `-0.900` | `-0.133` | `0.783` |
| MVK_s2 baseline | `-0.190` | `-0.850` | `-0.233` | `0.650` |

Recommendation: keep complementarity in the tool for exploration, but
de-emphasize it in the paper. If used, fix the score normalization first and
present only selected examples near the transition window.

## Low-Origin Scalar Exclusion Sensitivity

I tested low-origin scalar exclusion in memory using the existing RSI files and
VTU point arrays. The baseline output files were not modified.

Tested symmetric thresholds for `orb00` and `orb01`: `0.001`, `0.003`, `0.006`,
and `0.01`.

For the requested threshold `0.003`:

| Dataset | Mean excluded regular vertices | Max excluded regular vertices | Effect |
|---|---:|---:|---|
| MVK_s1 | `4.04%` | `7.74%` | No material improvement in event ranking. |
| MVK_s2 | `7.24%` | `12.24%` | No material improvement in event ranking. |

Across all tested thresholds, the range results are unchanged and the domain
event/complementarity rankings remain qualitatively similar. Larger thresholds
remove many vertices, for example `21.96%` mean exclusion in MVK_s1 and `27.78%`
in MVK_s2 at threshold `0.01`, but they do not make the domain plots cleaner.

Recommendation: do not use the low-origin filter as a main MVK result. It can be
mentioned as a sensitivity check: removing near-origin scalar pairs did not
change the main range-space conclusion.

## Other MVK Behavior To Look For

The prior CSP/image-moment study points to several chemically meaningful
behaviors:

- The transition near the conical-intersection window around `29-31 fs`.
- Strong oxygen behavior after approximately `26 fs`.
- Changes involving the vinyl carbons C3 and C4 around the transition.
- A possible difference between global scatterplot change and localized atomic
  contributors.

Our method can plausibly capture the first item now: the strongest range-sheet
event scores land in the same time window. It may also capture oxygen/C3/C4
behavior indirectly through selected sheet/fiber-surface images, but that claim
requires visual inspection of the molecular overlays. The current artifacts do
not attach atom IDs to sheets, so the paper should not claim atom-specific
identification from the metric tables alone.

More general MVK photochemistry context suggests that excited-state
reorganization of the enone system, carbonyl involvement, and torsion around the
vinyl group are plausible behaviors to inspect. These are compatible with the
range-space reconfiguration we see near `28-31 fs`, but we should phrase this as
consistency rather than proof unless we add atom-aware annotations and cite the
specific chemistry sources in the final paper.

## Useful And Less Useful Tool Features

Most useful for MVK:

- Range-mode event intervals.
- Range-mode continuing features.
- Side-by-side sheet images and fiber-surface images for selected links.
- Domain-change intervals, if framed as spatial-support reorganization.
- The ability to highlight selected interval/feature points in the Sankey view.

Useful but should be de-emphasized:

- Domain/range complementarity, after score normalization.
- Spearman domain/range correlation, mainly as an exploratory plot.

Less useful as paper evidence right now:

- Low-origin scalar exclusion for MVK.
- Raw domain interval ranking alone, because it highlights valid but chemically
  harder-to-explain spatial-support churn.

## Suggested Figures

1. Range event overview around transition.
   - Dataset: `MVK_s1` and `MVK_s2`.
   - Viewer mode: range metrics, metric `range IoU`.
   - Time range: `1100-1300` for both, with dense window visible.
   - Highlight intervals:
     - MVK_s1: `1180->1200`, `1200->1220`, `1276->1280`.
     - MVK_s2: `1140->1160`, `1160->1180`, `1200->1220`.
   - Purpose: show that range-sheet event scores peak near the prior
     transition window.

2. Long-lived range features.
   - MVK_s1: highlight tracks beginning at sheets `0`, `5251`, and `4`.
   - MVK_s2: highlight tracks beginning at sheets `24`, `22`, and `2`.
   - Purpose: show that Reeb-space sheets provide trackable feature families,
     not just timestep-wise descriptors.

3. Domain-support reorganization.
   - MVK_s1: show `720->740` and/or `560->580`.
   - MVK_s2: show `320->340` and/or `300->320`.
   - Viewer mode: domain overlap / domain-change tab.
   - Purpose: demonstrate that the domain mode answers a different question:
     spatial-support churn.

4. Complementarity examples near transition.
   - MVK_s1: `1240->1244`, source sheet `978`, range target `1095`, domain
     target `78`.
   - MVK_s1: `1220->1240`, source sheet `133`, range target `3`, domain target
     `53`.
   - MVK_s2: `1244->1248`, source sheet `144`, range target `128`, domain
     target `117`.
   - MVK_s2: `1220->1240`, source sheet `96`, range target `99`, domain target
     `299`.
   - Purpose: show that range and domain can propose different correspondences
     during the transition window.

5. Sheet and fiber-surface detail panels.
   - Use the clicked link/node detail view for the examples above.
   - Capture side-by-side sheet images and fiber-surface images.
   - Purpose: connect metric events to actual sheet geometry and molecular
     context.

Additional data needed for stronger atom-specific claims:

- Atom labels or an atom-to-region annotation in the rendered molecule/fiber
  views.
- A manual or computed mapping from selected sheet/fiber images to oxygen, C3,
  and C4 neighborhoods.

## Main Paper Narrative

The MVK result should focus on three claims:

1. Range-space Reeb-sheet tracking recovers the known transition window near
   `29-31 fs`, independently of the prior CSP/image-moment descriptors.
2. Reeb-space sheets provide feature-level temporal tracks, including multiple
   long-lived range features across the full MVK trajectory.
3. Domain-space overlap provides a complementary view of spatial support: it
   exposes early support churn and local disagreement with range-space matching,
   but it should not be interpreted as the same signal as range geometry.

The low-origin filter and raw Spearman/complementarity plots should not be the
central evidence. They can appear in discussion or as exploratory diagnostics.
