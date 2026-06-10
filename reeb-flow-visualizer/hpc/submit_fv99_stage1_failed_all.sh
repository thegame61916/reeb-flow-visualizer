#!/usr/bin/env bash
# Submit failed-timestep rerun lists for all requested datasets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBMIT_FAILED="${SCRIPT_DIR}/submit_fv99_stage1_failed.sh"
OUTPUT_DATASETS_ROOT="${OUTPUT_DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/hpc_outputs/datasets}"

if [[ $# -gt 0 ]]; then
  DATASETS=("$@")
else
  DATASETS=(MVK_s1 MVK_s2 stilbene torus)
fi

for dataset in "${DATASETS[@]}"; do
  rerun_file="${OUTPUT_DATASETS_ROOT}/${dataset}/sankey/rerun_failed_stems.txt"
  if [[ ! -f "${rerun_file}" ]]; then
    echo "Skipping ${dataset}: no rerun file ${rerun_file}"
    continue
  fi
  runnable_count="$(grep -vE '^[[:space:]]*(#|$)' "${rerun_file}" | wc -l | tr -d '[:space:]')"
  if [[ "${runnable_count}" -eq 0 ]]; then
    echo "Skipping ${dataset}: rerun file is empty"
    continue
  fi

  dataset_lower="$(printf "%s" "${dataset}" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "${MAX_PARALLEL:-}" ]]; then
    parallel="${MAX_PARALLEL}"
  elif [[ "${dataset_lower}" == *stilbene* ]]; then
    parallel=64
  else
    parallel=16
  fi
  "${SUBMIT_FAILED}" "${dataset}" "${parallel}"
done
