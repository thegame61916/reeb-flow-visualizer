# Tetralith fv99 Stage-1 Artifact Jobs

These scripts generate the fv99 artifacts produced by local Stage 1, but as
Slurm array jobs on Tetralith. Input VTUs are read from `DATASETS_ROOT`; all
generated artifacts, logs, and resume checks are written under
`OUTPUT_DATASETS_ROOT`.

For each input `downsampledGrids/*.vtu`, the worker writes:

- `reebSpaces/<step>.rs`
- `sheetInfo/<step>.rsi`
- `compareSheetShapesCache/cache/vtp/<step>.sheets.vtp`
- `sheetFiberSurfaces/labeled/<step>/{f_pos,g_pos,f_neg,g_neg}.vtp`
- `sheetFiberSurfaces/labeled/<step>/labeled_fiber_surfaces_manifest.json`
- logs and a tab-separated status file under `sankey/`

The worker is resumable. With `REBUILD=0` it skips timesteps where all expected
artifacts already exist.

Default remote paths:

- input datasets: `/proj/reeb-space-storage/users/x_mohsh/datasets`
- output datasets: `/proj/reeb-space-storage/users/x_mohsh/hpc_outputs/datasets`
- fv99: `/home/x_mohsh/sat-hpc-3/build/fv99`

Submit all default datasets:

```bash
cd /proj/reeb-space-storage/users/x_mohsh/stage1_hpc_scripts
chmod +x hpc/*.sh
TIME_LIMIT=4:00:00 MEM=24G hpc/submit_fv99_stage1_all.sh
```

Submit one dataset:

```bash
TIME_LIMIT=4:00:00 MEM=24G hpc/submit_fv99_stage1_dataset.sh stilbene 16
```

Check progress/results:

```bash
squeue -u x_mohsh
hpc/check_fv99_stage1_artifacts.sh stilbene
```

Useful overrides:

```bash
# Regenerate existing outputs.
REBUILD=1 hpc/submit_fv99_stage1_dataset.sh MVK_s1 8

# Disable fiber-surface generation if you only want .rs/.rsi/sheet VTP.
RUN_FIBERS=0 hpc/submit_fv99_stage1_dataset.sh MVK_s1 8

# Use a different fv99, input root, or output root.
FV99=/path/to/fv99 \
DATASETS_ROOT=/path/to/input/datasets \
OUTPUT_DATASETS_ROOT=/path/to/output/datasets \
hpc/submit_fv99_stage1_all.sh
```

Important: `OUTPUT_DATASETS_ROOT` must be visible from the machine running the
Slurm jobs. The default path is on Tetralith project storage. After the jobs
finish, copy or pull those outputs to your local disk.


## Staging With Limited Tetralith Storage

Each successful timestep is recorded in:

```bash
$OUTPUT_DATASETS_ROOT/<dataset>/sankey/hpc_completed_stems.txt
```

This file is small and is used to avoid recomputing timesteps after bulky
artifacts have been copied away and removed from Tetralith. Preserve the
`hpc_completed_stems.txt` file if you clean output directories.

A safe cleanup after copying a dataset locally is to remove bulky artifact
folders but keep `sankey/hpc_completed_stems.txt`, for example:

```bash
DATASET=stilbene
OUT=/proj/reeb-space-storage/users/x_mohsh/hpc_outputs/datasets/$DATASET
rm -rf "$OUT/reebSpaces" "$OUT/sheetInfo" "$OUT/compareSheetShapesCache" "$OUT/sheetFiberSurfaces"
```

The next submission excludes stems listed in `hpc_completed_stems.txt` from the
new Slurm array. Set `SKIP_COMPLETED_STEMS=0` only if you intentionally want to
recompute copied/cleaned timesteps.
