from pathlib import Path


# ================= USER SETTINGS =================

BASE_DIR = Path("/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/MVK_s1")

DATASET_CONFIGS = {
    "stilbene": {
        "state_file": "sampleFSImage_stilbene.pvsm",
        "f_isovalue": 0.05,
        "g_isovalue": 0.05,
    },
    "mvk": {
        "state_file": "sampleFSImage_MVK.pvsm",
        "f_isovalue": 0.07,
        "g_isovalue": 0.07,
    },
    "torus": {
        "state_file": "sampleFSImage_torus.pvsm",
        "f_isovalue": 0.0,
        "g_isovalue": -10.0,
    },
}

last_dir = BASE_DIR.name.lower()

if "stilbene" in last_dir:
    dataset_key = "stilbene"
elif "mvk" in last_dir:
    dataset_key = "mvk"
elif "torus" in last_dir:
    dataset_key = "torus"
else:
    raise ValueError(f"Unknown dataset type from BASE_DIR last directory: {BASE_DIR.name}")

config = DATASET_CONFIGS[dataset_key]

# Fiber-surface extraction for top Reeb sheets. For each timestep, the stage
# writes surfaces for +f, -f, +g, and -g at these absolute isovalues.
FIBER_SURFACE_FIELD_F_ISOVALUE = config["f_isovalue"]
FIBER_SURFACE_FIELD_G_ISOVALUE = config["g_isovalue"]
    
# Range-field selection passed to fv99.
# Keep these close to BASE_DIR so dataset switches can update them together.
FV99_FNAME = "orb00"
FV99_GNAME = "orb01"

FV99 = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/build/fv99"
)

EPSILON = "0.00000000"
RESERVE_CORES = 20
FV99_OMP_THREADS = 1
TOP_N_SHEETS = 20
VIEWER_DEFAULT_TOP_SHEETS = 10
SHEET_RENDERER_WORKERS = 4
SHEET_RENDERER_REBUILD_CACHE = False
SHEET_RENDERER_CLEAN_CACHE = False
SHEET_RENDERER_USE_GLOBAL_BOUNDS = True
SHEET_RENDERER_IMAGE_SIZE = (1600, 1600)
SHEET_RENDERER_GLOBAL_PADDING = 0.03

# Default weights for combined shape score.
# These are used by compareSheetShapes, overlap attachment metadata,
# and unified viewer defaults.
SHAPE_SCORE_DEFAULT_WEIGHTS = {
    "shape_iou": 0.6,
    "area_ratio": 1.9,
    "bbox_iou": 0.2,
    "centroid_similarity": 0.1,
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

# Analysis thresholds used by the tracking diagnostics stage. The preferred
# threshold is used for ranked interval JSON and plots; all thresholds are
# written to the event/lifetime CSVs for sensitivity checks.
TRACKING_ANALYSIS_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7)
TRACKING_ANALYSIS_PREFERRED_THRESHOLD = 0.5
TRACKING_ANALYSIS_TOP_INTERVALS = 12
TRACKING_ANALYSIS_TOP_FEATURES = 12
TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT = 0.5

# Pipeline stage flags
RUN_STAGE_1_FV99 = False
RUN_STAGE_2_RSI_JSON = False
RUN_STAGE_3A_SHAPE_MATCHING = False
RUN_STAGE_3B_OVERLAPS = False
RUN_STAGE_4A_SHEET_RENDERING = False
RUN_STAGE_4B_SHEET_FIBER_SURFACES = False
RUN_STAGE_5_UNIFIED_SANKEY_VIEWER = True
RUN_STAGE_6_TRACKING_ANALYSIS = True

# Backward-compatible aliases for older scripts/imports.
RUN_STAGE_2B_SHAPE_MATCHING = RUN_STAGE_3A_SHAPE_MATCHING
RUN_STAGE_3_OVERLAPS = RUN_STAGE_3B_OVERLAPS
RUN_STAGE_4_SHEET_RENDERING = RUN_STAGE_4A_SHEET_RENDERING
RUN_STAGE_4_SHEET_FIBER_SURFACES = RUN_STAGE_4B_SHEET_FIBER_SURFACES
RUN_UNIFIED_SANKEY_VIEWER = RUN_STAGE_5_UNIFIED_SANKEY_VIEWER

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
TRACKING_DATA_FILE = OUTPUT_DIR / "tracking_data.json"
TRACKING_ANALYSIS_DIR = OUTPUT_DIR / "tracking_analysis"
TRACKING_ANALYSIS_VIEWER_FILE = TRACKING_ANALYSIS_DIR / "viewer_analysis.json"
SHEET_IMAGE_DIR = BASE_DIR / "sheetRendering"
SHEET_RENDERER_TEMP_DIR = (
    Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/sheet_renderer_tmp")
    / BASE_DIR.name
)
SHEET_RENDERER_UNIFORM_SHEET_COLOR = (0.20, 0.60, 0.90)
FIBER_SURFACE_TOP_N_SHEETS = TOP_N_SHEETS
FIBER_SURFACE_WORKERS = 20
FIBER_SURFACE_REBUILD = False
FIBER_SURFACE_DIR = BASE_DIR / "sheetFiberSurfaces"
FIBER_SURFACE_IMAGE_DIR = BASE_DIR / "sheetFiberSurfaceImages"
FIBER_SURFACE_TEMP_DIR = SHEET_RENDERER_TEMP_DIR / "fiber_surfaces"
FIBER_SURFACE_MOLECULAR_STRUCTURE_DIR = VTU_DIR / "molecularStructure"
FIBER_SURFACE_RENDER_STATE_FILE = Path(__file__).resolve().parent / config["state_file"]
FIBER_SURFACE_RENDER_IMAGE_RESOLUTION = (1600, 1200)
FIBER_SURFACE_RENDER_TIMEOUT_SECONDS = 300
FIBER_SURFACE_RENDER_RETRIES = 2
PVPYTHON = Path("/home/mohit/Desktop/ParaView-6.0.0-MPI-Linux-Python3.12-x86_64/bin/pvpython")

# Corner colors for centroid-position node coloring in the unified viewer.
# Coordinates use the global 2D sheet-space extent: x=min/max is left/right,
# y=min/max is bottom/top.
CENTROID_COLOR_CORNERS = {
    "bottom_left": "#2563eb",
    "bottom_right": "#dc2626",
    "top_left": "#16a34a",
    "top_right": "#f59e0b",
}

# Axis centroid node coloring in the unified viewer. The origin color is a
# neutral low-saturation color at (0, 0). Distance from the origin controls
# saturation, and angle controls the red-to-blue interpolation.
CENTROID_AXIS_DIAGONAL_COLORS = {
    "origin": "#808080",
    "x_axis": "#0000ff",
    "y_axis": "#ff0000",
}


# Output files
OVERLAP_FILE = OUTPUT_DIR / "sheet_overlaps.json"


# Log files
FV99_FAILED_LOG_FILE = OUTPUT_DIR / "fv99_failed_files.log"
FV99_PARTIAL_LOG_FILE = OUTPUT_DIR / "fv99_partial_files.log"
RSI_JSON_WARNINGS_LOG_FILE = OUTPUT_DIR / "rsi_json_warnings.log"
OVERLAP_WARNINGS_LOG_FILE = OUTPUT_DIR / "sheet_overlap_warnings.log"
FIBER_SURFACE_FAILED_LOG_FILE = OUTPUT_DIR / "fiber_surface_failed_files.log"


# Runtime library paths
FV99_ROOT = FV99.parent.parent

VTK_LIB_DIR = FV99_ROOT / "libraries/vtk/install/lib"
TTK_BUILD_LIB_DIR = FV99_ROOT / "libraries/ttk/build/lib"
TTK_INSTALL_LIB_DIR = FV99_ROOT / "libraries/ttk/install/lib"
