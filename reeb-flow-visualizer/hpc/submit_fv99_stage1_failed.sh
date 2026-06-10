#!/usr/bin/env bash
# Submit only the timesteps listed in a dataset rerun-failed stems file.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: submit_fv99_stage1_failed.sh DATASET_NAME [MAX_PARALLEL]

Reads, by default:
  $OUTPUT_DATASETS_ROOT/$DATASET_NAME/sankey/rerun_failed_stems.txt

The explicit rerun list bypasses hpc_completed_stems.txt by default, because
these are timesteps missing from the copied local artifact set.
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

DATASET_NAME="$1"
MAX_PARALLEL="${2:-${MAX_PARALLEL:-4}}"
OUTPUT_DATASETS_ROOT="${OUTPUT_DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/hpc_outputs/datasets}"
RERUN_STEMS_FILE="${RERUN_STEMS_FILE:-${OUTPUT_DATASETS_ROOT}/${DATASET_NAME}/sankey/rerun_failed_stems.txt}"
SKIP_COMPLETED_STEMS="${SKIP_COMPLETED_STEMS:-0}"

if [[ ! -f "${RERUN_STEMS_FILE}" ]]; then
  echo "Rerun stems file not found: ${RERUN_STEMS_FILE}" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RERUN_STEMS_FILE="${RERUN_STEMS_FILE}" \
SKIP_COMPLETED_STEMS="${SKIP_COMPLETED_STEMS}" \
"${SCRIPT_DIR}/submit_fv99_stage1_dataset.sh" "${DATASET_NAME}" "${MAX_PARALLEL}"
