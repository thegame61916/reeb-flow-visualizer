# Tetralith fv99 Stage-1 Artifact Jobs

These scripts generate the fv99 artifacts produced by local Stage 1, but as
Slurm array jobs on Tetralith.

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

- datasets: `/proj/reeb-space-storage/users/x_mohsh/datasets`
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

# Use a different fv99 or dataset root.
FV99=/path/to/fv99 DATASETS_ROOT=/path/to/datasets hpc/submit_fv99_stage1_all.sh
```
