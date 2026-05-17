I have a Python pipeline that produces `sheet_overlaps.json` for time-varying Reeb sheet overlaps in /media/mohit/8tbh/postdoc/timeVaryingReebFeatures/stilbene/sankey. 
Here is the pipeline code: /home/mohit/Desktop/postdoc/timeVaryingReebSpace/scripts/reeb-flow-visualizer
I want to replace/improve the current static Plotly Sankey stage with an interactive web-based Sankey viewer suitable for large data: up to ~700 timesteps and ~30 nodes per timestep.

Create aseparate directory in /home/mohit/Desktop/postdoc/timeVaryingReebSpace/scripts/reeb-flow-visualizer to write this code. Use Python for preprocessing and generate a browser viewer using D3.js / d3-sankey. Plotly is not flexible enough for this scale/custom interaction. D3 Sankey supports computing node/link positions and updating layouts, which is what we need.

Current data format:
- `sheet_overlaps.json` contains:
  - `timesteps`
  - `nodes`
  - `links`
- each node has:
  - `id`
  - `timestep_index`
  - `timestep_label`
  - `sheet_id`
  - `rank`
  - `area`
  - `num_vertices`
  - `rsi_file`
  - `rsijson_file`
- each link has:
  - `source`
  - `target`
  - `source_timestep_index`
  - `target_timestep_index`
  - `source_sheet_id`
  - `target_sheet_id`
  - `source_rank`
  - `target_rank`
  - `source_area`
  - `target_area`
  - `source_num_vertices`
  - `target_num_vertices`
  - `overlap_vertices`
  - `source_percent`
  - `target_percent`

Implement a new stage, preferably named:

`stage_04_interactive_sankey_viewer.py`

It should generate a viewer folder, for example:

`sankey_viewer/`
  `index.html`
  `viewer.js`
  `style.css`
  `data.json`

The viewer should support:

1. Overlap threshold filtering
   - Add slider for minimum overlap percentage.
   - Allow filtering by:
     - source_percent
     - target_percent
     - or max(source_percent, target_percent)
   - Default: max percent.
   - Only links passing threshold should be shown.
   - Nodes with no visible links should optionally be hidden using a checkbox.
   

3. Timestep range selection
   - Add text boxes:
     - start timestep
     - end timestep
   - Also add a range slider if simple.
   - Add a +sign to add more range intervals.
   - Only show nodes/links inside selected timestep ranges.
   - Also the range should be interactively selectible. I should be able to draw a rectangle and that range should show up. If I want to select more ranges then that should be allowed too. The rest of the sankey diagram should in background zoomed out to give a context about where different ranges are.

4. Node vertical ordering
   - Add dropdown:
     - decreasing area
     - increasing rank
     - crossing-minimized / barycentric heuristic
   - For decreasing area: sort nodes in each timestep by area descending.
   - For rank: sort by rank ascending.
   - For crossing-minimized: implement a simple iterative barycentric ordering:
       - initialize by rank
       - sweep left-to-right and right-to-left a few iterations
       - order nodes in each timestep by average position of connected neighbors
       - fall back to rank/area if no neighbors.

5. Sheet image support
   - Design the code so that each node can optionally have `image` or `thumbnail` path.
   - If image exists:
     - show thumbnail in a right-side info panel when a node is clicked.
   - For a link click:
     - show source sheet image and target sheet image side-by-side.
   - Do not try to embed images directly inside every node initially, because that will be too heavy.
   - If no image path exists, show metadata only.
   - The images will come from the folder that we have just generated, use the sheet id and timesteps to locate. Use the hexcode in sheet images to color the sankey diagram nodes.

6. Viewer usability
   - Use SVG for Sankey.
   - Add pan/zoom.
   - Add horizontal scrolling or a wide SVG.
   - Show hover tooltip for nodes and links.
   - Add side panel with detailed selected node/link metadata.
   - Add “Reset view” button.
   - Add visible counters:
     - visible timesteps
     - visible nodes
     - visible links

7. Performance requirements
   - Do not attempt to render all 700 timesteps by default.
   - Render only current filtered/ranged/downsampled subset.
   - Keep the original full data in JS memory, but rebuild filtered graph on control changes.
   - Debounce slider/textbox updates if necessary.
   - Avoid expensive recomputation unless controls change.

8. Python preprocessing
   - Read `OVERLAP_FILE` from `common.py`.
   - Write viewer files to a configurable directory, e.g. `VIEWER_DIR = OUTPUT_DIR / "interactive_sankey_viewer"`.
   - Convert node/link references into a D3-friendly JSON format.
   - Preserve all original metadata needed for tooltips and side panel.
   - Optionally add `thumbnail` fields later, but structure should support them.

9. Integration
   - Update the main pipeline so Stage 4 calls this interactive viewer stage instead of or in addition to the old Plotly stage.
   - Keep the old Plotly stage untouched unless necessary.
   - Add clear terminal output:
     - where viewer was written
     - how to open it
     - suggest running:
       `cd <viewer_dir>`
       `python3 -m http.server 8000`
       then open `http://localhost:8000`

10. Keep code simple, modular and readable like the rest of the stage pipeline.
    - Separate functions for:
      - loading data
      - writing data.json
      - writing index.html
      - writing viewer.js
      - writing style.css
    - In JS, separate functions for:
      - getControls()
      - filterData()
      - orderNodes()
      - computeBarycentricOrder()
      - renderSankey()
      - updateStats()
      - showNodeDetails()
      - showLinkDetails()

Important:
- Do not break existing `sheet_overlaps.json` format.
- Do not remove existing metadata.
- Prioritize a usable interactive viewer over a publication-quality static figure.
- Write complete working code, not pseudocode.
