#!/usr/bin/env bash
# Submit Stage-1 fv99 artifact jobs for all requested datasets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBMIT_ONE="${SCRIPT_DIR}/submit_fv99_stage1_dataset.sh"

if [[ $# -gt 0 ]]; then
  DATASETS=("$@")
else
  DATASETS=(MVK_s1 MVK_s2 stilbene torus)
fi

for dataset in "${DATASETS[@]}"; do
  dataset_lower="$(printf "%s" "${dataset}" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "${MAX_PARALLEL:-}" ]]; then
    parallel="${MAX_PARALLEL}"
  elif [[ "${dataset_lower}" == *stilbene* ]]; then
    parallel=16
  else
    parallel=8
  fi
  "${SUBMIT_ONE}" "${dataset}" "${parallel}"
done
