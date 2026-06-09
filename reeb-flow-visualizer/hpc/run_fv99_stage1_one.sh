#!/usr/bin/env bash
# Run the Stage-1 fv99 artifact generation for one VTU file.
# This script is intended to be launched as a Slurm array task by
# submit_fv99_stage1_dataset.sh.

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: run_fv99_stage1_one.sh DATASET_NAME [ARRAY_INDEX]

Environment variables:
  DATASETS_ROOT      Root containing dataset folders. Default:
                     /proj/reeb-space-storage/users/x_mohsh/datasets
  FV99               fv99 binary. Default: /home/x_mohsh/sat-hpc-3/build/fv99
  F_NAME             Range-space x field passed as --fName. Default: orb00
  G_NAME             Range-space y field passed as --gName. Default: orb01
  EPSILON            fv99 -e value. Default: 0.00000000
  REBUILD            1 to regenerate even when all artifacts exist. Default: 0
  RUN_FIBERS         1 to generate labeled fiber-surface VTPs. Default: 1
  PERTURB_ON_FAIL    1 to perturb once if primary .rs/.rsi/.vtp are missing. Default: 1
  PERTURB_SCRIPT     perturb.py path. Default: $FV99_ROOT/scripts/perturb.py
  PERTURB_EPSILON    perturb.py epsilon. Default: 0.00001
  REPLACE_ORIGINAL_ON_PERTURB
                     1 to replace input VTU by successful perturbed VTU. Default: 1
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

DATASET_NAME="$1"
ARRAY_INDEX="${2:-${SLURM_ARRAY_TASK_ID:-}}"
if [[ -z "${ARRAY_INDEX}" ]]; then
  echo "Missing ARRAY_INDEX argument and SLURM_ARRAY_TASK_ID is not set" >&2
  exit 2
fi
if ! [[ "${ARRAY_INDEX}" =~ ^[0-9]+$ ]]; then
  echo "ARRAY_INDEX must be a non-negative integer, got: ${ARRAY_INDEX}" >&2
  exit 2
fi

DATASETS_ROOT="${DATASETS_ROOT:-/proj/reeb-space-storage/users/x_mohsh/datasets}"
FV99="${FV99:-/home/x_mohsh/sat-hpc-3/build/fv99}"
F_NAME="${F_NAME:-orb00}"
G_NAME="${G_NAME:-orb01}"
EPSILON="${EPSILON:-0.00000000}"
REBUILD="${REBUILD:-0}"
RUN_FIBERS="${RUN_FIBERS:-1}"
PERTURB_ON_FAIL="${PERTURB_ON_FAIL:-1}"
PERTURB_EPSILON="${PERTURB_EPSILON:-0.00001}"
REPLACE_ORIGINAL_ON_PERTURB="${REPLACE_ORIGINAL_ON_PERTURB:-1}"
KEEP_FIBER_WORK="${KEEP_FIBER_WORK:-0}"
OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

DATASET_DIR="${DATASETS_ROOT}/${DATASET_NAME}"
VTU_DIR="${DATASET_DIR}/downsampledGrids"
LOG_DIR="${DATASET_DIR}/sankey"
VTU_MANIFEST="${VTU_MANIFEST:-${LOG_DIR}/hpc_vtu_manifest.txt}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/hpc_stage1_status.tsv}"

RS_DIR="${DATASET_DIR}/reebSpaces"
RSI_DIR="${DATASET_DIR}/sheetInfo"
SHEET_VTP_DIR="${DATASET_DIR}/compareSheetShapesCache/cache/vtp"
FIBER_LABELED_ROOT="${DATASET_DIR}/sheetFiberSurfaces/labeled"
FIBER_WORK_ROOT="${FIBER_WORK_ROOT:-${DATASET_DIR}/sheetFiberSurfaces/_tmp}"
PERTURBED_ROOT="${LOG_DIR}/fv99_perturbed_vtu"

if [[ ! -x "${FV99}" ]]; then
  echo "fv99 binary not found or not executable: ${FV99}" >&2
  exit 127
fi
if [[ ! -d "${VTU_DIR}" ]]; then
  echo "VTU directory not found: ${VTU_DIR}" >&2
  exit 2
fi
if [[ ! -f "${VTU_MANIFEST}" ]]; then
  echo "VTU manifest not found: ${VTU_MANIFEST}" >&2
  exit 2
fi

line_no=$((ARRAY_INDEX + 1))
VTU="$(sed -n "${line_no}p" "${VTU_MANIFEST}")"
if [[ -z "${VTU}" ]]; then
  echo "No VTU entry at index ${ARRAY_INDEX} in ${VTU_MANIFEST}" >&2
  exit 2
fi
if [[ ! -f "${VTU}" ]]; then
  echo "VTU file not found: ${VTU}" >&2
  exit 2
fi

base_name="$(basename "${VTU}")"
STEM="${base_name%.vtu}"
RS="${RS_DIR}/${STEM}.rs"
RSI="${RSI_DIR}/${STEM}.rsi"
SHEET_VTP="${SHEET_VTP_DIR}/${STEM}.sheets.vtp"
SHEET_FEATURES_VTP="${SHEET_VTP}.features.vtp"
SHEET_GRAPH_DOT="${SHEET_VTP}.graph.dot"
FIBER_DIR="${FIBER_LABELED_ROOT}/${STEM}"
FIBER_MANIFEST="${FIBER_DIR}/labeled_fiber_surfaces_manifest.json"
F_POS="${FIBER_DIR}/f_pos.vtp"
G_POS="${FIBER_DIR}/g_pos.vtp"
F_NEG="${FIBER_DIR}/f_neg.vtp"
G_NEG="${FIBER_DIR}/g_neg.vtp"
MAIN_LOG="${LOG_DIR}/${STEM}.fv99.log"
RETRY_LOG="${LOG_DIR}/${STEM}.fv99.perturbed.log"
PERTURB_LOG="${LOG_DIR}/${STEM}.perturb_vtu.log"
POS_FIBER_LOG="${LOG_DIR}/${STEM}.pos.fiber.fv99.log"
NEG_FIBER_LOG="${LOG_DIR}/${STEM}.neg.fiber.fv99.log"

mkdir -p "${LOG_DIR}" "${RS_DIR}" "${RSI_DIR}" "${SHEET_VTP_DIR}" \
  "${FIBER_DIR}" "${FIBER_WORK_ROOT}" "${PERTURBED_ROOT}"

# Infer the repository/source root from the binary path, unless provided.
FV99_DIR="$(cd "$(dirname "${FV99}")" && pwd)"
FV99_ROOT="${FV99_ROOT:-$(cd "${FV99_DIR}/.." && pwd)}"
PERTURB_SCRIPT="${PERTURB_SCRIPT:-${FV99_ROOT}/scripts/perturb.py}"

library_paths=()
for candidate in \
  "${FV99_ROOT}/libraries/ttk/build/lib" \
  "${FV99_ROOT}/libraries/ttk/install/lib" \
  "${FV99_ROOT}/libraries/vtk/install/lib" \
  "${FV99_ROOT}/libraries/vtk/install/lib64" \
  "${FV99_ROOT}/libraries/cgal/install/lib" \
  "${FV99_ROOT}/libraries/cgal/install/lib64" \
  "${FV99_ROOT}/build/lib" \
  "${FV99_DIR}"; do
  if [[ -d "${candidate}" ]]; then
    library_paths+=("${candidate}")
  fi
done
if [[ -n "${EXTRA_LD_LIBRARY_PATH:-}" ]]; then
  library_paths+=("${EXTRA_LD_LIBRARY_PATH}")
fi
if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
  library_paths+=("${LD_LIBRARY_PATH}")
fi
if [[ ${#library_paths[@]} -gt 0 ]]; then
  LD_LIBRARY_PATH="$(IFS=:; echo "${library_paths[*]}")"
  export LD_LIBRARY_PATH
fi
export OMP_NUM_THREADS QT_QPA_PLATFORM

append_status() {
  local status="$1"
  local details="${2:-}"
  printf "%s\t%s\t%s\t%s\t%s\n" "$(date -Is)" "${DATASET_NAME}" "${STEM}" "${status}" "${details}" >> "${STATUS_FILE}"
}

has_primary_outputs() {
  [[ -s "${RS}" && -s "${RSI}" && -s "${SHEET_VTP}" ]]
}

have_fiber_outputs() {
  [[ -s "${F_POS}" && -s "${G_POS}" && -s "${F_NEG}" && -s "${G_NEG}" && -s "${FIBER_MANIFEST}" ]]
}

is_complete() {
  if ! has_primary_outputs; then
    return 1
  fi
  if [[ "${RUN_FIBERS}" == "1" ]] && ! have_fiber_outputs; then
    return 1
  fi
  return 0
}

clear_outputs() {
  rm -f "${RS}" "${RSI}" "${SHEET_VTP}" "${SHEET_FEATURES_VTP}" "${SHEET_GRAPH_DOT}" \
    "${F_POS}" "${G_POS}" "${F_NEG}" "${G_NEG}" "${FIBER_MANIFEST}"
}

value_for_dataset() {
  local field="$1"
  local name_lower
  name_lower="$(printf "%s" "${DATASET_NAME}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${field}" == "f" && -n "${F_ISO:-}" ]]; then
    printf "%s\n" "${F_ISO}"
  elif [[ "${field}" == "g" && -n "${G_ISO:-}" ]]; then
    printf "%s\n" "${G_ISO}"
  elif [[ "${name_lower}" == *stilbene* ]]; then
    printf "0.05\n"
  elif [[ "${name_lower}" == *mvk* ]]; then
    printf "0.07\n"
  elif [[ "${name_lower}" == *torus* ]]; then
    if [[ "${field}" == "f" ]]; then
      printf "0.0\n"
    else
      printf -- "-10.0\n"
    fi
  else
    printf "0.05\n"
  fi
}

negate_number() {
  awk -v x="$1" 'BEGIN { y = -x; if (y == 0) y = 0; printf "%.12g\n", y }'
}

run_main_fv99() {
  local input_vtu="$1"
  local log_file="$2"
  "${FV99}" \
    -f "${input_vtu}" \
    -e "${EPSILON}" \
    -s "${RS}" \
    -i "${RSI}" \
    -o "${SHEET_VTP}" \
    --headless \
    --fName "${F_NAME}" \
    --gName "${G_NAME}" \
    > "${log_file}" 2>&1
}

perturb_vtu_once() {
  local source_vtu="$1"
  local output_vtu="$2"
  local log_file="$3"
  local tmp_vtu="${output_vtu}.tmp.$$"

  if [[ ! -f "${PERTURB_SCRIPT}" ]]; then
    echo "perturb.py not found: ${PERTURB_SCRIPT}" > "${log_file}"
    return 127
  fi

  rm -f "${tmp_vtu}" "${output_vtu}"
  python3 "${PERTURB_SCRIPT}" "${source_vtu}" "${PERTURB_EPSILON}" "${tmp_vtu}" > "${log_file}" 2>&1
  local rc=$?
  if [[ ${rc} -eq 0 && -s "${tmp_vtu}" ]]; then
    mv "${tmp_vtu}" "${output_vtu}"
  else
    rm -f "${tmp_vtu}"
  fi
  return ${rc}
}

run_fiber_sign() {
  local sign="$1"
  local input_vtu="$2"
  local f_value="$3"
  local g_value="$4"
  local log_file="$5"
  local work_dir
  work_dir="$(mktemp -d "${FIBER_WORK_ROOT}/${STEM}_${sign}.XXXXXX")"
  mkdir -p "${work_dir}/output"

  rm -f "${FIBER_DIR}/f_${sign}.vtp" "${FIBER_DIR}/g_${sign}.vtp"

  (
    cd "${work_dir}" && \
    "${FV99}" \
      -f "${input_vtu}" \
      -l "${RS}" \
      --fieldFValueFS "${f_value}" \
      --fieldGValueFS "${g_value}" \
      --headless \
      --fName "${F_NAME}" \
      --gName "${G_NAME}"
  ) > "${log_file}" 2>&1
  local rc=$?

  local f_source="${work_dir}/output/labeled.fs.f.vtp"
  local g_source="${work_dir}/output/labeled.fs.g.vtp"
  if [[ ${rc} -eq 0 && -s "${f_source}" && -s "${g_source}" ]]; then
    mv "${f_source}" "${FIBER_DIR}/f_${sign}.vtp"
    mv "${g_source}" "${FIBER_DIR}/g_${sign}.vtp"
  fi

  if [[ "${KEEP_FIBER_WORK}" != "1" ]]; then
    rm -rf "${work_dir}"
  fi

  if [[ ${rc} -ne 0 ]]; then
    return ${rc}
  fi
  [[ -s "${FIBER_DIR}/f_${sign}.vtp" && -s "${FIBER_DIR}/g_${sign}.vtp" ]]
}

write_fiber_manifest() {
  cat > "${FIBER_MANIFEST}" <<EOF
{
  "timestep": "${STEM}",
  "vtu": "${ARTIFACT_VTU}",
  "rs": "${RS}",
  "f_name": "${F_NAME}",
  "g_name": "${G_NAME}",
  "f_isovalue": ${F_ISOVALUE},
  "g_isovalue": ${G_ISOVALUE},
  "surfaces": {
    "f_pos": "${F_POS}",
    "g_pos": "${G_POS}",
    "f_neg": "${F_NEG}",
    "g_neg": "${G_NEG}"
  }
}
EOF
}

generate_fibers() {
  if [[ "${RUN_FIBERS}" != "1" ]]; then
    return 0
  fi

  rm -f "${F_POS}" "${G_POS}" "${F_NEG}" "${G_NEG}" "${FIBER_MANIFEST}"
  F_ISOVALUE="$(value_for_dataset f)"
  G_ISOVALUE="$(value_for_dataset g)"
  local neg_f neg_g
  neg_f="$(negate_number "${F_ISOVALUE}")"
  neg_g="$(negate_number "${G_ISOVALUE}")"

  run_fiber_sign "pos" "${ARTIFACT_VTU}" "${F_ISOVALUE}" "${G_ISOVALUE}" "${POS_FIBER_LOG}"
  local pos_rc=$?
  if [[ ${pos_rc} -ne 0 ]]; then
    append_status "partial_missing_fibers" "main_returncode=${MAIN_RC}; fiber_pos_returncode=${pos_rc}; fiber_pos_log=${POS_FIBER_LOG}; vtu=${ARTIFACT_VTU}"
    return 1
  fi

  run_fiber_sign "neg" "${ARTIFACT_VTU}" "${neg_f}" "${neg_g}" "${NEG_FIBER_LOG}"
  local neg_rc=$?
  if [[ ${neg_rc} -ne 0 ]]; then
    append_status "partial_missing_fibers" "main_returncode=${MAIN_RC}; fiber_neg_returncode=${neg_rc}; fiber_neg_log=${NEG_FIBER_LOG}; vtu=${ARTIFACT_VTU}"
    return 1
  fi

  write_fiber_manifest
  if ! have_fiber_outputs; then
    append_status "partial_missing_fibers" "main_returncode=${MAIN_RC}; fibers_missing_after_manifest=1; vtu=${ARTIFACT_VTU}"
    return 1
  fi
  return 0
}

if [[ "${REBUILD}" != "1" ]] && is_complete; then
  append_status "skipped_existing" "rs=${RS}; rsi=${RSI}; sheet_vtp=${SHEET_VTP}; fibers=${FIBER_DIR}"
  echo "skipped existing artifacts: ${base_name}"
  exit 0
fi

clear_outputs
ARTIFACT_VTU="${VTU}"
run_main_fv99 "${VTU}" "${MAIN_LOG}"
MAIN_RC=$?

if has_primary_outputs; then
  if [[ ${MAIN_RC} -eq 0 ]]; then
    primary_status="done"
  else
    primary_status="partial_primary_nonzero"
  fi
  if generate_fibers; then
    append_status "${primary_status}" "returncode=${MAIN_RC}; vtu=${ARTIFACT_VTU}; rs=${RS}; rsi=${RSI}; sheet_vtp=${SHEET_VTP}; fibers=${FIBER_DIR}"
    echo "${primary_status}: ${base_name}"
    exit 0
  fi
  echo "partial missing fibers: ${base_name}"
  exit 1
fi

if [[ "${PERTURB_ON_FAIL}" != "1" ]]; then
  append_status "failed" "returncode=${MAIN_RC}; log=${MAIN_LOG}; primary_outputs_exist=0"
  echo "failed returncode=${MAIN_RC}: ${base_name}"
  exit 1
fi

perturb_slug="$(printf "%s" "${PERTURB_EPSILON}" | sed 's/-/m/g; s/\./p/g')"
PERTURBED_VTU="${PERTURBED_ROOT}/${STEM}_eps_${perturb_slug}.vtu"
perturb_vtu_once "${VTU}" "${PERTURBED_VTU}" "${PERTURB_LOG}"
PERTURB_RC=$?
if [[ ${PERTURB_RC} -ne 0 || ! -s "${PERTURBED_VTU}" ]]; then
  append_status "failed" "returncode=${MAIN_RC}; log=${MAIN_LOG}; perturb_returncode=${PERTURB_RC}; perturb_log=${PERTURB_LOG}; primary_outputs_exist=0"
  echo "failed perturb returncode=${PERTURB_RC}: ${base_name}"
  exit 1
fi

clear_outputs
run_main_fv99 "${PERTURBED_VTU}" "${RETRY_LOG}"
MAIN_RC=$?
if ! has_primary_outputs; then
  append_status "failed" "returncode=${MAIN_RC}; normal_log=${MAIN_LOG}; perturb_returncode=${PERTURB_RC}; perturb_log=${PERTURB_LOG}; retry_log=${RETRY_LOG}; primary_outputs_exist=0; perturbed_vtu=${PERTURBED_VTU}"
  echo "failed after perturb returncode=${MAIN_RC}: ${base_name}"
  exit 1
fi

if [[ "${REPLACE_ORIGINAL_ON_PERTURB}" == "1" ]]; then
  mv "${PERTURBED_VTU}" "${VTU}"
  ARTIFACT_VTU="${VTU}"
  replaced="1"
else
  ARTIFACT_VTU="${PERTURBED_VTU}"
  replaced="0"
fi

if generate_fibers; then
  if [[ ${MAIN_RC} -eq 0 ]]; then
    status="recovered_with_perturbation"
  else
    status="partial_perturbed_primary_nonzero"
  fi
  append_status "${status}" "returncode=${MAIN_RC}; normal_log=${MAIN_LOG}; perturb_returncode=${PERTURB_RC}; perturb_log=${PERTURB_LOG}; retry_log=${RETRY_LOG}; perturb_epsilon=${PERTURB_EPSILON}; perturbed_source=${PERTURBED_VTU}; replaced_original=${replaced}; replacement_vtu=${ARTIFACT_VTU}; rs=${RS}; rsi=${RSI}; sheet_vtp=${SHEET_VTP}; fibers=${FIBER_DIR}"
  echo "${status}: ${base_name}"
  exit 0
fi

echo "partial missing fibers after perturb: ${base_name}"
exit 1
