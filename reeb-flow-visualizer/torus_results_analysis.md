# Torus Results Analysis

This note summarizes the current torus results from:

- `/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus`

The torus dataset is best used as a synthetic validation case. It has a very
stable range-space event centered at timestep `50`, plus long tracks that show
the tracker can follow coherent sheet families across the full sequence.

## Main Finding

The strongest range-space event is centered at the synthetic transition:

| Interval | Event score | Mean best score | Weak source/target | Split/merge |
|---:|---:|---:|---:|---:|
| `50->51` | `49.08` | `0.246` | `15/15` | `4/4` |
| `49->50` | `49.05` | `0.248` | `15/15` | `4/4` |
| `51->52` | `48.09` | `0.296` | `15/15` | `4/4` |
| `48->49` | `48.09` | `0.296` | `15/15` | `4/4` |
| `47->48` | `45.57` | `0.322` | `13/13` | `6/6` |
| `52->53` | `45.57` | `0.322` | `13/13` | `6/6` |

The symmetry around `49->52` is exactly the kind of behavior we want from a
synthetic sanity check.

## Long-Lived Tracks

The threshold-`0.5` track table contains multiple full-length or near-full-length
tracks:

| Time labels | Sheet endpoints | Length | Rank range | Mean continuation |
|---:|---|---:|---:|---:|
| `0->99` | `379 -> 43` | `100` | `2-10` | `0.926` |
| `0->99` | `383 -> 43` | `100` | `2-9` | `0.926` |
| `0->99` | `1183 -> 43` | `100` | `2-11` | `0.926` |
| `0->99` | `13903 -> 43` | `100` | `2-12` | `0.926` |
| `1->99` | `385 -> 43` | `99` | `2-10` | `0.928` |

These tracks validate continuity behavior, but they should be interpreted with
care: several tracks converge to the same final sheet. That is consistent with
the current non-one-to-one matching model, but it means the torus track table
should be used as validation evidence rather than as a claim of unique feature
identity.

## Threshold And Sampling-Stride Checks

The torus event is highly stable across thresholds:

| Threshold | Top interval | Max event score | Max lifetime | Median lifetime |
|---:|---:|---:|---:|---:|
| `0.3` | `50->51` | `49.08` | `100` | `1` |
| `0.4` | `50->51` | `49.08` | `100` | `1` |
| `0.5` | `50->51` | `49.08` | `100` | `1` |
| `0.6` | `50->51` | `49.08` | `100` | `1` |
| `0.7` | `50->51` | `49.08` | `100` | `1` |

The stride check also preserves the transition center:

| Stride | Top range interval | Event score |
|---:|---:|---:|
| `1` | `50->51` | `49.08` |
| `2` | `50->52` | `49.15` |
| `3` | `50->53` | `49.23` |
| `4` | `50->54` | `49.32` |

Conclusion: torus is the cleanest validation that the event diagnostic can
localize a controlled transition and remain stable under coarser sampling.

## Domain-Space Events And Complementarity

Domain event ranking is useful as a separate synthetic check. It identifies
where domain support changes, not where range shape changes. The top domain
events are:

| Interval | Domain event score | Mean best overlap | Weak source/target |
|---:|---:|---:|---:|
| `7->8` | `57.30` | `0.135` | `20/20` |
| `92->93` | `57.27` | `0.137` | `20/20` |
| `93->94` | `57.24` | `0.138` | `20/20` |
| `6->7` | `57.23` | `0.139` | `20/20` |

These do not duplicate the range event centered at timestep `50`. Instead, they
show separate domain-support behavior in the synthetic sequence.

Domain/range disagreement also has useful validation value because it peaks near
the synthetic range transition:

| Interval | Compared sources | Agreement fraction | Max disagreement score |
|---:|---:|---:|---:|
| `48->49` | `7` | `0.143` | `42.94` |
| `49->50` | `7` | `0.143` | `42.70` |
| `41->42` | `11` | `0.091` | `42.32` |

This gives a concrete insight: the synthetic transition is not only a high
range-event interval; it is also a place where range and domain matching select
different targets for the same source sheets. The range-event curve remains the
cleaner validation signal, while disagreement helps explain how domain and
range correspondences diverge near the transition.

## Metric Usefulness

For torus, `combined` is identical to `shape_iou`.

| Candidate metric | Agreement with `combined` | Mean loss if used |
|---|---:|---:|
| `shape_iou` | `1.000` | `0.000` |
| `bbox_iou` | `0.598` | `0.000` |
| `centroid_similarity` | `0.562` | `0.016` |
| `area_ratio` | `0.535` | `0.013` |
| `overlap_max_percent` | `0.175` | `0.265` |

The near-zero loss for `bbox_iou` is dataset-specific and appears to reflect
near-equivalent top choices in this synthetic geometry. It should not replace
`shape_iou` as the main metric.

## Suggested Figures

1. Torus event validation.
   - Show event score over timesteps with the peak centered on `50->51`.
   - Include stride panels or annotations for strides `1-4`.

2. Torus Sankey transition detail.
   - Show a window around `45-55`.
   - Highlight intervals `49->50`, `50->51`, and `51->52`.

3. Track continuity example.
   - Highlight one full-length track, such as `379 -> 43`.
   - Note in the caption that multiple tracks can converge under the current
     non-one-to-one tracking model.

## Viewer Feature Recommendations From Torus

Most useful:

- Range event graph.
- Domain event ranking.
- Stride selector.
- Threshold sensitivity.
- Sankey highlight around the transition.

Useful but secondary:

- Long-lived track table, with a caveat about non-one-to-one convergence.
- Domain/range disagreement for debugging synthetic behavior.

Less useful:

- Extra scalar shape metrics as top-level choices; they do not improve the
  synthetic validation story.
