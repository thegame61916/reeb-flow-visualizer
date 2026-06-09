#!/usr/bin/env bash
# Print a compact artifact/status summary for one or more datasets.

set -euo pipefail

DATASETS_ROOT="${DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/datasets}"
if [[ $# -gt 0 ]]; then
  DATASETS=("$@")
else
  DATASETS=(MVK_s1 MVK_s2 stilbene torus)
fi

count_files() {
  local dir="$1"
  local pattern="$2"
  if [[ -d "${dir}" ]]; then
    find "${dir}" -type f -name "${pattern}" | wc -l | tr -d '[:space:]'
  else
    printf "0"
  fi
}

for dataset in "${DATASETS[@]}"; do
  dataset_dir="${DATASETS_ROOT}/${dataset}"
  vtu_dir="${dataset_dir}/downsampledGrids"
  rs_dir="${dataset_dir}/reebSpaces"
  rsi_dir="${dataset_dir}/sheetInfo"
  sheet_vtp_dir="${dataset_dir}/compareSheetShapesCache/cache/vtp"
  fiber_dir="${dataset_dir}/sheetFiberSurfaces/labeled"
  status_file="${dataset_dir}/sankey/hpc_stage1_status.tsv"

  vtu_count="$(count_files "${vtu_dir}" '*.vtu')"
  rs_count="$(count_files "${rs_dir}" '*.rs')"
  rsi_count="$(count_files "${rsi_dir}" '*.rsi')"
  sheet_vtp_count="$(count_files "${sheet_vtp_dir}" '*.sheets.vtp')"
  fiber_vtp_count="$(count_files "${fiber_dir}" '*.vtp')"
  fiber_manifest_count="$(count_files "${fiber_dir}" 'labeled_fiber_surfaces_manifest.json')"
  expected_fiber_vtp=$((vtu_count * 4))

  echo "== ${dataset} =="
  echo "VTU: ${vtu_count}"
  echo "RS: ${rs_count}  RSI: ${rsi_count}  sheet VTP: ${sheet_vtp_count}"
  echo "fiber VTP: ${fiber_vtp_count}/${expected_fiber_vtp}  fiber manifests: ${fiber_manifest_count}/${vtu_count}"

  if [[ -f "${status_file}" ]]; then
    echo "status counts:"
    awk -F '\t' 'NF >= 4 && $1 !~ /^#/ { counts[$4]++ } END { for (status in counts) printf "  %s %d\n", status, counts[status] }' "${status_file}" | sort
    echo "last status lines:"
    tail -n 5 "${status_file}"
  else
    echo "no status file: ${status_file}"
  fi
  echo
done
