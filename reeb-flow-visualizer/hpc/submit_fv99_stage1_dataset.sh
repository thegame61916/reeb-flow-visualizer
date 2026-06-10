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
  DATASETS_ROOT       Input datasets root. Default: /proj/reeb-space-storage/users/x_mohsh/datasets
  OUTPUT_DATASETS_ROOT
                      Artifact output root. Default: /media/mohit/4TB_kingston_tufA2/hpc/datasets
  FV99                Default: /home/x_mohsh/sat-hpc-3/build/fv99
  TIME_LIMIT          Slurm wall time per VTU. Default: 4:00:00
  MEM                 Slurm memory per array task. Default: 24G
  CPUS_PER_TASK       Slurm CPUs per task. Default: 1
  MAX_PARALLEL        Array throttle if not passed as second argument. Default: 4
  ACCOUNT             Optional Slurm account
  PARTITION           Optional Slurm partition
  REBUILD             1 to regenerate complete artifacts. Default: 0
  RUN_FIBERS          1 to generate labeled fiber-surface VTPs. Default: 1
  SKIP_COMPLETED_STEMS
                      1 to exclude stems in the completed-stems file from new arrays. Default: 1
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

DATASET_NAME="$1"
MAX_PARALLEL="${2:-${MAX_PARALLEL:-4}}"
DATASETS_ROOT="${DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/datasets}"
OUTPUT_DATASETS_ROOT="${OUTPUT_DATASETS_ROOT:-/media/mohit/4TB_kingston_tufA2/hpc/datasets}"
FV99="${FV99:-/home/x_mohsh/sat-hpc-3/build/fv99}"
TIME_LIMIT="${TIME_LIMIT:-4:00:00}"
MEM="${MEM:-24G}"
CPUS_PER_TASK="${CPUS_PER_TASK:-1}"
REBUILD="${REBUILD:-0}"
RUN_FIBERS="${RUN_FIBERS:-1}"
SKIP_COMPLETED_STEMS="${SKIP_COMPLETED_STEMS:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ONE="${SCRIPT_DIR}/run_fv99_stage1_one.sh"
INPUT_DATASET_DIR="${DATASETS_ROOT}/${DATASET_NAME}"
OUTPUT_DATASET_DIR="${OUTPUT_DATASETS_ROOT}/${DATASET_NAME}"
VTU_DIR="${INPUT_DATASET_DIR}/downsampledGrids"
LOG_DIR="${OUTPUT_DATASET_DIR}/sankey"
SLURM_LOG_DIR="${LOG_DIR}/slurm"
MANIFEST="${LOG_DIR}/hpc_vtu_manifest.txt"
STATUS_FILE="${LOG_DIR}/hpc_stage1_status.tsv"
COMPLETED_STEMS_FILE="${COMPLETED_STEMS_FILE:-${LOG_DIR}/hpc_completed_stems.txt}"

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
ALL_MANIFEST="${MANIFEST}.all"
find "${VTU_DIR}" -maxdepth 1 -type f -name '*.vtu' | sort > "${ALL_MANIFEST}"
if [[ "${SKIP_COMPLETED_STEMS}" == "1" && -f "${COMPLETED_STEMS_FILE}" ]]; then
  : > "${MANIFEST}"
  while IFS= read -r vtu_file; do
    stem="$(basename "${vtu_file}" .vtu)"
    key="${stem}"$'\t'"run_fibers=${RUN_FIBERS}"
    if grep -Fxq "${key}" "${COMPLETED_STEMS_FILE}" || grep -Fxq "${stem}" "${COMPLETED_STEMS_FILE}"; then
      continue
    fi
    printf "%s\n" "${vtu_file}" >> "${MANIFEST}"
  done < "${ALL_MANIFEST}"
else
  cp "${ALL_MANIFEST}" "${MANIFEST}"
fi
COUNT="$(wc -l < "${MANIFEST}" | tr -d '[:space:]')"
TOTAL_COUNT="$(wc -l < "${ALL_MANIFEST}" | tr -d '[:space:]')"
if [[ "${COUNT}" -eq 0 ]]; then
  if [[ "${TOTAL_COUNT}" -eq 0 ]]; then
    echo "No .vtu files found in ${VTU_DIR}" >&2
  else
    echo "No uncompleted .vtu files to submit for ${DATASET_NAME}."
    echo "Completed stems: ${COMPLETED_STEMS_FILE}"
  fi
  exit 0
fi

# Keep previous status as history, but make this run easy to identify.
printf "# submit_time=%s dataset=%s count=%s total_count=%s max_parallel=%s rebuild=%s run_fibers=%s skip_completed=%s input_root=%s output_root=%s\n" \
  "$(date -Is)" "${DATASET_NAME}" "${COUNT}" "${TOTAL_COUNT}" "${MAX_PARALLEL}" "${REBUILD}" "${RUN_FIBERS}" "${SKIP_COMPLETED_STEMS}" "${DATASETS_ROOT}" "${OUTPUT_DATASETS_ROOT}" >> "${STATUS_FILE}"

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
  --export "ALL,DATASETS_ROOT=${DATASETS_ROOT},OUTPUT_DATASETS_ROOT=${OUTPUT_DATASETS_ROOT},FV99=${FV99},VTU_MANIFEST=${MANIFEST},STATUS_FILE=${STATUS_FILE},COMPLETED_STEMS_FILE=${COMPLETED_STEMS_FILE},REBUILD=${REBUILD},RUN_FIBERS=${RUN_FIBERS},SKIP_COMPLETED_STEMS=${SKIP_COMPLETED_STEMS}"
)

if [[ -n "${ACCOUNT:-}" ]]; then
  SBATCH_ARGS+=(--account "${ACCOUNT}")
fi
if [[ -n "${PARTITION:-}" ]]; then
  SBATCH_ARGS+=(--partition "${PARTITION}")
fi

sbatch "${SBATCH_ARGS[@]}" "${RUN_ONE}" "${DATASET_NAME}"

echo "Submitted ${COUNT}/${TOTAL_COUNT} uncompleted VTUs for ${DATASET_NAME} with max_parallel=${MAX_PARALLEL}"
echo "Input:    ${VTU_DIR}"
echo "Output:   ${OUTPUT_DATASET_DIR}"
echo "Manifest: ${MANIFEST}"
echo "Status:   ${STATUS_FILE}"
echo "Completed:${COMPLETED_STEMS_FILE}"
echo "Logs:     ${SLURM_LOG_DIR}"
