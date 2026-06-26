from pathlib import Path


# ================= USER SETTINGS =================

BASE_DIR = Path("/home/mohit/Desktop/postdoc/timeVaryingReebSpace/hpc/datasets/torus")

DATASET_CONFIGS = {
    "stilbene": {
        "state_file": "sampleFSImage_stilbene.pvsm",
        "f_isovalue": 0.05,
        "g_isovalue": 0.05,
        "fiber_surface_mode": "fixed",
    },
    "mvk": {
        "state_file": "sampleFSImage_MVK.pvsm",
        "f_isovalue": 0.07,
        "g_isovalue": 0.07,
        "fiber_surface_mode": "fixed",
    },
    "torus": {
        "state_file": "sampleFSImage_torus.pvsm",
        "f_isovalue": 0.0,
        "g_isovalue": -10.0,
        "fiber_surface_mode": "adaptive_f_range_change",
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
FIBER_SURFACE_MODE = config.get("fiber_surface_mode", "fixed")
FIBER_SURFACE_ADAPTIVE_ENABLED = FIBER_SURFACE_MODE == "adaptive_f_range_change"
FIBER_SURFACE_ADAPTIVE_FIELD = "f"
FIBER_SURFACE_ADAPTIVE_DEFAULT_POSITION = 0.5
FIBER_SURFACE_ADAPTIVE_MIN_POSITION = 0.05
FIBER_SURFACE_ADAPTIVE_MAX_POSITION = 0.95
FIBER_SURFACE_ADAPTIVE_VALUE_PRECISION = 6
    
# Range-field selection passed to fv99.
# Keep these close to BASE_DIR so dataset switches can update them together.
FV99_FNAME = "orb00"
FV99_GNAME = "orb01"

# Exclude regular domain vertices whose selected scalar pair lies close to
# the range-space origin. Sheets are kept even when all their vertices are
# filtered, so they behave like zero-vertex sheets downstream.
EXCLUDE_LOW_SCALAR_VALUES_NEAR_ORIGIN = False
LOW_SCALAR_ORIGIN_THRESHOLDS_BY_DATASET = {
    "stilbene": {"orb00": 0.011, "orb01": 0.011},
    "mvk": {"orb00": 0.003, "orb01": 0.003},
    "torus": {"orb00": 0.0, "orb01": 0.0},
}
LOW_SCALAR_ORIGIN_THRESHOLDS = LOW_SCALAR_ORIGIN_THRESHOLDS_BY_DATASET.get(dataset_key, {})

# Stage 1 one-shot fallback. If fv99 fails without producing both .rs and
# .rsi outputs, perturb the input VTU once with this script/epsilon and retry
# fv99 against the perturbed file.
FV99_PERTURB_SCRIPT = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/scripts/perturb.py"
)
FV99_PERTURB_EPSILON = "0.00001"

FV99 = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/build/fv99"
)

# Runtime fv99 perturbation stays zero. Stage 1's fallback uses perturb.py
# to create a perturbed VTU and then retries fv99 with this same epsilon.
EPSILON = "0.00000000"
RESERVE_CORES = 43
FV99_OMP_THREADS = 1
TOP_N_SHEETS = 20
VIEWER_DEFAULT_TOP_SHEETS = 10
# Precompute direct timestep-pair comparisons for strides 1..N.
# A value of 4 computes links/matches for t->t+1, t->t+2, t->t+3, and t->t+4.
SANKEY_TIMESTEP_STRIDE_MAX = 4
SHEET_RENDERER_WORKERS = 5
SHEET_RENDERER_REBUILD_CACHE = False
SHEET_RENDERER_CLEAN_CACHE = False
SHEET_RENDERER_REPLACE_EXISTING_IMAGES = False
SHEET_RENDERER_USE_GLOBAL_BOUNDS = True
SHEET_RENDERER_IMAGE_SIZE = (1600, 1600)
SHEET_RENDERER_GLOBAL_PADDING = 0.03
SHEET_RENDERER_RENDER_TIMEOUT_SECONDS = 300

# Default weights for combined range score.
# These are used by compareSheetShapes, overlap attachment metadata,
# and unified viewer range-score defaults.
SHAPE_SCORE_DEFAULT_WEIGHTS = {
    "shape_iou": 1.0,
    "area_ratio": 0.0,
    "bbox_iou": 0.0,
    "centroid_similarity": 0.0,
}

# Same weights with stage_03 range-metric key prefix.
RANGE_SCORE_DEFAULT_WEIGHTS = {
    f"range_{metric}": weight
    for metric, weight in SHAPE_SCORE_DEFAULT_WEIGHTS.items()
}

# Viewer styling defaults used by analysis plots and support-filtered Sankey links.
ANALYSIS_PLOT_DEFAULT_COLOR = "#6b7280"
ANALYSIS_PLOT_SELECTED_COLOR = "#ef4444"
ANALYSIS_PLOT_SELECTED_STROKE_COLOR = "#991b1b"
ANALYSIS_PLOT_DEEMPHASIS_TRANSPARENCY = 0
UNSUPPORTED_LINK_DEFAULT_TRANSPARENCY = 100


# Analysis thresholds used by the tracking diagnostics stage. The preferred
# threshold is used for ranked interval JSON and plots; all thresholds are
# written to the event/lifetime CSVs for sensitivity checks.
TRACKING_ANALYSIS_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7)
TRACKING_ANALYSIS_PREFERRED_THRESHOLD = 0.5
TRACKING_ANALYSIS_TOP_INTERVALS = 12
TRACKING_ANALYSIS_TOP_FEATURES = 12
TRACKING_ANALYSIS_TOP_DISAGREEMENTS = 12
TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT = 1.0

# Event-score formula used by both precomputed tracking analysis and the
# browser-side runtime analysis. Each term reads one component from the
# interval summary and multiplies it by the configured weight.
TRACKING_ANALYSIS_EVENT_SCORE_TERMS = (
    {"component": "source_weak_count", "weight": 1.0},
    {"component": "target_weak_count", "weight": 1.0},
    {"component": "possible_splits", "weight": TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT},
    {"component": "possible_merges", "weight": TRACKING_ANALYSIS_SPLIT_MERGE_WEIGHT},
    {"component": "continuation_gap_source_count", "weight": 1.0},
)


def tracking_analysis_event_score_components(
    *,
    source_weak_count,
    target_weak_count,
    possible_splits,
    possible_merges,
    mean_best_combined,
    source_sheet_count,
):
    return {
        "source_weak_count": float(source_weak_count),
        "target_weak_count": float(target_weak_count),
        "possible_splits": float(possible_splits),
        "possible_merges": float(possible_merges),
        "continuation_gap_source_count": (1.0 - float(mean_best_combined)) * max(float(source_sheet_count), 1.0),
    }


def tracking_analysis_event_score(components):
    return sum(
        float(term["weight"]) * float(components.get(term["component"], 0.0))
        for term in TRACKING_ANALYSIS_EVENT_SCORE_TERMS
    )


def tracking_analysis_event_score_formula_text():
    labels = []
    for term in TRACKING_ANALYSIS_EVENT_SCORE_TERMS:
        weight = float(term["weight"])
        component = term["component"]
        if weight == 1.0:
            labels.append(component)
        else:
            labels.append(f"{weight:g}*{component}")
    return " + ".join(labels)

# Pipeline stage flags
RUN_STAGE_1_FV99 = False
RUN_STAGE_2_RSI_JSON = False
RUN_STAGE_3A_SHAPE_MATCHING = False
RUN_STAGE_3B_OVERLAPS = False
RUN_STAGE_4A_SHEET_RENDERING = False
RUN_STAGE_4B_SHEET_FIBER_SURFACES = False
RUN_STAGE_4C_ADAPTIVE_FIBER_SURFACES = FIBER_SURFACE_ADAPTIVE_ENABLED
RUN_STAGE_5A_BUILD_UNIFIED_SANKEY_DATA = True
RUN_STAGE_5B_TRACKING_ANALYSIS = True
RUN_STAGE_5C_UNIFIED_SANKEY_VIEWER = True

# Backward-compatible aliases for older scripts/imports.
RUN_STAGE_2B_SHAPE_MATCHING = RUN_STAGE_3A_SHAPE_MATCHING
RUN_STAGE_3_OVERLAPS = RUN_STAGE_3B_OVERLAPS
RUN_STAGE_4_SHEET_RENDERING = RUN_STAGE_4A_SHEET_RENDERING
RUN_STAGE_4_SHEET_FIBER_SURFACES = RUN_STAGE_4B_SHEET_FIBER_SURFACES
RUN_STAGE_4_ADAPTIVE_FIBER_SURFACES = RUN_STAGE_4C_ADAPTIVE_FIBER_SURFACES
RUN_STAGE_5_UNIFIED_SANKEY_VIEWER = RUN_STAGE_5C_UNIFIED_SANKEY_VIEWER
RUN_STAGE_6_TRACKING_ANALYSIS = RUN_STAGE_5B_TRACKING_ANALYSIS
RUN_UNIFIED_SANKEY_VIEWER = RUN_STAGE_5C_UNIFIED_SANKEY_VIEWER

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
FV99_PERTURBED_VTU_DIR = OUTPUT_DIR / "fv99_perturbed_vtu"
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
FIBER_SURFACE_LABELED_DIR = FIBER_SURFACE_DIR / "labeled"
FIBER_SURFACE_ADAPTIVE_LABELED_DIR = FIBER_SURFACE_DIR / "adaptive_labeled"
FIBER_SURFACE_IMAGE_DIR = BASE_DIR / "sheetFiberSurfaceImages"
FIBER_SURFACE_TEMP_DIR = SHEET_RENDERER_TEMP_DIR / "fiber_surfaces"
FIBER_SURFACE_ADAPTIVE_TEMP_DIR = FIBER_SURFACE_TEMP_DIR / "adaptive"
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


# Output files
OVERLAP_FILE = OUTPUT_DIR / "sheet_overlaps.json"
SHEET_VTP_CACHE_DIR = BASE_DIR / "compareSheetShapesCache" / "cache" / "vtp"


# Log files
FV99_FAILED_LOG_FILE = OUTPUT_DIR / "fv99_failed_files.log"
FV99_PARTIAL_LOG_FILE = OUTPUT_DIR / "fv99_partial_files.log"
FV99_RECOVERED_LOG_FILE = OUTPUT_DIR / "fv99_recovered_files.log"
RSI_JSON_WARNINGS_LOG_FILE = OUTPUT_DIR / "rsi_json_warnings.log"
LOW_SCALAR_ORIGIN_FILTER_LOG_FILE = OUTPUT_DIR / "low_scalar_origin_filter.log"
OVERLAP_WARNINGS_LOG_FILE = OUTPUT_DIR / "sheet_overlap_warnings.log"
FIBER_SURFACE_FAILED_LOG_FILE = OUTPUT_DIR / "fiber_surface_failed_files.log"
FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE = OUTPUT_DIR / "adaptive_fiber_surface_failed_files.log"
SHAPE_MATCHING_SKIPPED_LOG_FILE = OUTPUT_DIR / "shape_matching_skipped_timesteps.log"


# Runtime library paths
FV99_ROOT = FV99.parent.parent

VTK_LIB_DIR = FV99_ROOT / "libraries/vtk/install/lib"
TTK_BUILD_LIB_DIR = FV99_ROOT / "libraries/ttk/build/lib"
TTK_INSTALL_LIB_DIR = FV99_ROOT / "libraries/ttk/install/lib"
