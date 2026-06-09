#!/usr/bin/env bash
# Submit one Slurm array for one dataset.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: submit_fv99_stage1_dataset.sh DATASET_NAME [MAX_PARALLEL]

Examples:
  TIME_LIMIT=4:00:00 MEM=24G ./hpc/submit_fv99_stage1_dataset.sh stilbene 16
  TIME_LIMIT=2:00:00 MEM=20G ./hpc/submit_fv99_stage1_dataset.sh MVK_s1 8

Environment variables:
  DATASETS_ROOT       Default: /proj/reeb-space-storage/users/x_mohsh/datasets
  FV99                Default: /home/x_mohsh/sat-hpc-3/build/fv99
  TIME_LIMIT          Slurm wall time per VTU. Default: 4:00:00
  MEM                 Slurm memory per array task. Default: 24G
  CPUS_PER_TASK       Slurm CPUs per task. Default: 1
  MAX_PARALLEL        Array throttle if not passed as second argument. Default: 4
  ACCOUNT             Optional Slurm account
  PARTITION           Optional Slurm partition
  REBUILD             1 to regenerate complete artifacts. Default: 0
  RUN_FIBERS          1 to generate labeled fiber-surface VTPs. Default: 1
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

DATASET_NAME="$1"
MAX_PARALLEL="${2:-${MAX_PARALLEL:-4}}"
DATASETS_ROOT="${DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/datasets}"
FV99="${FV99:-/home/x_mohsh/sat-hpc-3/build/fv99}"
TIME_LIMIT="${TIME_LIMIT:-4:00:00}"
MEM="${MEM:-24G}"
CPUS_PER_TASK="${CPUS_PER_TASK:-1}"
REBUILD="${REBUILD:-0}"
RUN_FIBERS="${RUN_FIBERS:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ONE="${SCRIPT_DIR}/run_fv99_stage1_one.sh"
DATASET_DIR="${DATASETS_ROOT}/${DATASET_NAME}"
VTU_DIR="${DATASET_DIR}/downsampledGrids"
LOG_DIR="${DATASET_DIR}/sankey"
SLURM_LOG_DIR="${LOG_DIR}/slurm"
MANIFEST="${LOG_DIR}/hpc_vtu_manifest.txt"
STATUS_FILE="${LOG_DIR}/hpc_stage1_status.tsv"

if [[ ! -x "${RUN_ONE}" ]]; then
  echo "Worker script not executable: ${RUN_ONE}" >&2
  exit 2
fi
if [[ ! -d "${VTU_DIR}" ]]; then
  echo "VTU directory not found: ${VTU_DIR}" >&2
  exit 2
fi
if ! [[ "${MAX_PARALLEL}" =~ ^[0-9]+$ ]] || [[ "${MAX_PARALLEL}" -lt 1 ]]; then
  echo "MAX_PARALLEL must be a positive integer, got: ${MAX_PARALLEL}" >&2
  exit 2
fi

mkdir -p "${LOG_DIR}" "${SLURM_LOG_DIR}"
find "${VTU_DIR}" -maxdepth 1 -type f -name '*.vtu' | sort > "${MANIFEST}"
COUNT="$(wc -l < "${MANIFEST}" | tr -d '[:space:]')"
if [[ "${COUNT}" -eq 0 ]]; then
  echo "No .vtu files found in ${VTU_DIR}" >&2
  exit 1
fi

# Keep previous status as history, but make this run easy to identify.
printf "# submit_time=%s dataset=%s count=%s max_parallel=%s rebuild=%s run_fibers=%s\n" \
  "$(date -Is)" "${DATASET_NAME}" "${COUNT}" "${MAX_PARALLEL}" "${REBUILD}" "${RUN_FIBERS}" >> "${STATUS_FILE}"

SBATCH_ARGS=(
  --job-name "fv99_${DATASET_NAME}"
  --time "${TIME_LIMIT}"
  --nodes 1
  --ntasks 1
  --cpus-per-task "${CPUS_PER_TASK}"
  --mem "${MEM}"
  --array "0-$((COUNT - 1))%${MAX_PARALLEL}"
  --output "${SLURM_LOG_DIR}/%x_%A_%a.out"
  --error "${SLURM_LOG_DIR}/%x_%A_%a.err"
  --export "ALL,DATASETS_ROOT=${DATASETS_ROOT},FV99=${FV99},VTU_MANIFEST=${MANIFEST},STATUS_FILE=${STATUS_FILE},REBUILD=${REBUILD},RUN_FIBERS=${RUN_FIBERS}"
)

if [[ -n "${ACCOUNT:-}" ]]; then
  SBATCH_ARGS+=(--account "${ACCOUNT}")
fi
if [[ -n "${PARTITION:-}" ]]; then
  SBATCH_ARGS+=(--partition "${PARTITION}")
fi

sbatch "${SBATCH_ARGS[@]}" "${RUN_ONE}" "${DATASET_NAME}"

echo "Submitted ${COUNT} VTUs for ${DATASET_NAME} with max_parallel=${MAX_PARALLEL}"
echo "Manifest: ${MANIFEST}"
echo "Status:   ${STATUS_FILE}"
echo "Logs:     ${SLURM_LOG_DIR}"
