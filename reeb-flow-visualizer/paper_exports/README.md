# Paper Figure Exports

Use the unified Sankey viewer interactively, click **Save figure preset**, then replay that preset with Playwright to create high-resolution PNGs.

Install Playwright once from this repository:

```bash
npm install --no-save playwright
```

Example:

```bash
node paper_exports/export_viewer_figure.mjs \
  --viewer /home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1/sankey/unified_sankey_viewer \
  --preset ~/Downloads/MVK_s1_example.figure_preset.json \
  --out /media/mohit/4TB_kingston_tufA2/hpc/paper_figures \
  --target active-canvas \
  --scale 3 \
  --width 2600 \
  --height 1500
```

Useful targets: `active-canvas`, `active-panel`, `details`, `analysis`, `viewer`, `main`, `full`.
