# Paper Figure Exports

Use the unified Sankey viewer interactively, click **Save figure preset**, then replay that preset with Playwright to create high-resolution PNGs.

Each dataset viewer build creates:

```text
<dataset>/sankey/paper_exports/presets
<dataset>/sankey/paper_exports/images
```

Browsers cannot silently save a downloaded preset into the dataset directory from a static page. After clicking **Save figure preset**, move the downloaded `*.figure_preset.json` into the dataset's `sankey/paper_exports/presets` folder. The exporter writes images to `sankey/paper_exports/images` by default when `--viewer` is provided.

Install Playwright once from this repository:

```bash
npm install --no-save playwright
```

Example:

```bash
node paper_exports/export_viewer_figure.mjs \
  --viewer /home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1/sankey/unified_sankey_viewer \
  --preset /home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/MVK_s1/sankey/paper_exports/presets/MVK_s1_example.figure_preset.json \
  --target active-canvas \
  --scale 3 \
  --width 2600 \
  --height 1500
```

Useful targets: `active-canvas`, `active-panel`, `details`, `analysis`, `viewer`, `main`, `full`.
