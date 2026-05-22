from pathlib import Path


# ================= USER SETTINGS =================

BASE_DIR = Path("/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/stilbene")

FV99 = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/build/fv99"
)

EPSILON = "0.00000000"
RESERVE_CORES = 20
FV99_OMP_THREADS = 1
TOP_N_SHEETS = 20
VIEWER_DEFAULT_TOP_SHEETS = 10
SHEET_RENDERER_WORKERS = 10

# Default weights for combined shape score.
# These are used by compareSheetShapes, overlap attachment metadata,
# and unified viewer defaults.
SHAPE_SCORE_DEFAULT_WEIGHTS = {
    "shape_iou": 0.40,
    "support_jaccard": 0.30,
    "area_ratio": 0.15,
    "bbox_iou": 0.10,
    "centroid_similarity": 0.05,
}

# Same weights with stage_03 range-metric key prefix.
RANGE_SCORE_DEFAULT_WEIGHTS = {
    f"range_{metric}": weight
    for metric, weight in SHAPE_SCORE_DEFAULT_WEIGHTS.items()
}

# Default weights for hybrid score in unified viewer.
HYBRID_SCORE_DEFAULT_WEIGHTS = {
    "vertex_overlap": 0.50,
    "shape_combined": 0.50,
}

# Default overlap metric used as vertex component in hybrid mode.
HYBRID_VERTEX_METRIC_DEFAULT = "overlap_max_percent"

# Pipeline stage flags
RUN_STAGE_1_FV99 = True
RUN_STAGE_2_RSI_JSON = True
RUN_STAGE_2B_SHAPE_MATCHING = True
RUN_STAGE_3_OVERLAPS = True
RUN_UNIFIED_SANKEY_VIEWER = True

# Shape matching can be expensive.
# Use a small number for testing, or None to use the default from
# compareSheetShapes/compare_sheet_shapes.py.
SHAPE_MATCHING_WORKERS = None

# ==================================================


# Input/output directories
VTU_DIR = BASE_DIR / "downsampledGrids"
RS_DIR = BASE_DIR / "reebSpaces"
RSI_DIR = BASE_DIR / "sheetInfo"

OUTPUT_DIR = BASE_DIR / "sankey"
RSI_JSON_DIR = OUTPUT_DIR / "rsi_json"
UNIFIED_VIEWER_DIR = OUTPUT_DIR / "unified_sankey_viewer"
VIEWER_DIR = UNIFIED_VIEWER_DIR
SHEET_IMAGE_DIR = BASE_DIR / "sheetRendering"
SHEET_RENDERER_TEMP_DIR = (
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/sheet_renderer_tmp")
    / BASE_DIR.name
)
SHEET_RENDERER_UNIFORM_SHEET_COLOR = (0.20, 0.60, 0.90)


# Output files
OVERLAP_FILE = OUTPUT_DIR / "sheet_overlaps.json"


# Log files
FV99_FAILED_LOG_FILE = OUTPUT_DIR / "fv99_failed_files.log"
FV99_PARTIAL_LOG_FILE = OUTPUT_DIR / "fv99_partial_files.log"
RSI_JSON_WARNINGS_LOG_FILE = OUTPUT_DIR / "rsi_json_warnings.log"
OVERLAP_WARNINGS_LOG_FILE = OUTPUT_DIR / "sheet_overlap_warnings.log"


# Runtime library paths
FV99_ROOT = FV99.parent.parent

VTK_LIB_DIR = FV99_ROOT / "libraries/vtk/install/lib"
TTK_BUILD_LIB_DIR = FV99_ROOT / "libraries/ttk/build/lib"
TTK_INSTALL_LIB_DIR = FV99_ROOT / "libraries/ttk/install/lib"
