from pathlib import Path


# ================= USER SETTINGS =================

BASE_DIR = Path("/media/mohit/8tbh/postdoc/timeVaryingReebFeatures/stilbene")

FV99 = Path(
    "/home/mohit/Desktop/postdoc/petars_fiber_flexing/"
    "petarsCode/arrange-and-traverse-algorithm/build/fv99"
)

EPSILON = "0.00000000"
RESERVE_CORES = 20
TOP_N_SHEETS = 20

SANKEY_TITLE = "Time-Varying Reeb Sheet Overlap"

# ==================================================


# Input/output directories
VTU_DIR = BASE_DIR / "downsampledGrids"
RS_DIR = BASE_DIR / "reebSpaces"
RSI_DIR = BASE_DIR / "sheetInfo"

OUTPUT_DIR = BASE_DIR / "sankey"
RSI_JSON_DIR = OUTPUT_DIR / "rsi_json"
VIEWER_DIR = OUTPUT_DIR / "interactive_sankey_viewer"
SHEET_IMAGE_DIR = BASE_DIR / "sheetRendering"


# Output files
OVERLAP_FILE = OUTPUT_DIR / "sheet_overlaps.json"
HTML_FILE = OUTPUT_DIR / "sankey.html"


# Log files
FV99_FAILED_LOG_FILE = OUTPUT_DIR / "fv99_failed_files.log"
RSI_JSON_WARNINGS_LOG_FILE = OUTPUT_DIR / "rsi_json_warnings.log"
OVERLAP_WARNINGS_LOG_FILE = OUTPUT_DIR / "sheet_overlap_warnings.log"


# Runtime library paths
FV99_ROOT = FV99.parent.parent

VTK_LIB_DIR = FV99_ROOT / "libraries/vtk/install/lib"
TTK_BUILD_LIB_DIR = FV99_ROOT / "libraries/ttk/build/lib"
TTK_INSTALL_LIB_DIR = FV99_ROOT / "libraries/ttk/install/lib"
