#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common import (
    FIBER_SURFACE_ADAPTIVE_ENABLED,
    FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE,
    FIBER_SURFACE_ADAPTIVE_FIELD,
    FIBER_SURFACE_ADAPTIVE_LABELED_DIR,
    FIBER_SURFACE_ADAPTIVE_TEMP_DIR,
    FIBER_SURFACE_ADAPTIVE_DEFAULT_POSITION,
    FIBER_SURFACE_ADAPTIVE_MAX_POSITION,
    FIBER_SURFACE_ADAPTIVE_MIN_POSITION,
    FIBER_SURFACE_ADAPTIVE_VALUE_PRECISION,
    FIBER_SURFACE_IMAGE_DIR,
    FIBER_SURFACE_MODE,
    FIBER_SURFACE_REBUILD,
    FIBER_SURFACE_RENDER_IMAGE_RESOLUTION,
    FIBER_SURFACE_RENDER_RETRIES,
    FIBER_SURFACE_RENDER_STATE_FILE,
    FIBER_SURFACE_RENDER_TIMEOUT_SECONDS,
    FIBER_SURFACE_TOP_N_SHEETS,
    FIBER_SURFACE_WORKERS,
    FV99,
    FV99_FNAME,
    FV99_GNAME,
    PVPYTHON,
    RSI_DIR,
    RS_DIR,
    VTU_DIR,
)
from compareSheetShapes.compare_sheet_shapes import MATCHES_FILE, load_timestep_cache
from stage_01_run_fv99 import FV99_ENV
from stage_04_compute_sheet_fiber_surfaces import (
    PARAVIEW_RENDER_HELPER,
    RENDER_ENV,
    ThresholdedSurface,
    TimestepResult,
    manifest_path,
    molecule_vtp_path,
    normalize_stem,
    render_molecule_vtp_path,
    threshold_sheet_surface,
    value_text,
    value_token,
    write_empty_molecule_vtp,
)

ADAPTIVE_RENDER_ROLE = "f_pos"


@dataclass(frozen=True)
class PositionCandidate:
    position: float
    weight: float
    source_stem: str
    target_stem: str
    source_sheet_id: int
    target_sheet_id: int
    direction: str
    score: float


@dataclass(frozen=True)
class SheetChoice:
    sheet_id: int
    position: float
    f_value: float
    candidate_count: int
    change_weight: float


@dataclass(frozen=True)
class RenderedAdaptiveImage:
    filename: str
    sheet_id: int
    choice: SheetChoice
    surface: ThresholdedSurface


def make_render_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


def signed_value_token(value: float) -> str:
    prefix = "m" if float(value) < 0.0 else "p"
    return prefix + value_token(value)


def rounded_value(value: float) -> float:
    return round(float(value), int(FIBER_SURFACE_ADAPTIVE_VALUE_PRECISION))


def clamp_position(value: float) -> float:
    return min(
        max(float(value), float(FIBER_SURFACE_ADAPTIVE_MIN_POSITION)),
        float(FIBER_SURFACE_ADAPTIVE_MAX_POSITION),
    )


def adaptive_labeled_surface_dir(vtu_file: Path) -> Path:
    return FIBER_SURFACE_ADAPTIVE_LABELED_DIR / vtu_file.stem


def adaptive_labeled_surface_path(vtu_file: Path, f_value: float) -> Path:
    return adaptive_labeled_surface_dir(vtu_file) / f"f_{signed_value_token(f_value)}.vtp"


def adaptive_labeled_manifest_path(vtu_file: Path) -> Path:
    return adaptive_labeled_surface_dir(vtu_file) / "adaptive_labeled_fiber_surfaces_manifest.json"


def expected_manifest_payload(vtu_file: Path) -> dict:
    return {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "molecule_vtp": str(molecule_vtp_path(vtu_file)),
        "rs": str(RS_DIR / f"{vtu_file.stem}.rs"),
        "rsi": str(RSI_DIR / f"{vtu_file.stem}.rsi"),
        "state_file": str(FIBER_SURFACE_RENDER_STATE_FILE),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "top_n_sheets": int(FIBER_SURFACE_TOP_N_SHEETS),
        "output_mode": "rendered_images_from_paraview_state",
        "surface_mode": "adaptive_f_range_change",
        "adaptive_field": FIBER_SURFACE_ADAPTIVE_FIELD,
        "adaptive_default_position": float(FIBER_SURFACE_ADAPTIVE_DEFAULT_POSITION),
        "adaptive_min_position": float(FIBER_SURFACE_ADAPTIVE_MIN_POSITION),
        "adaptive_max_position": float(FIBER_SURFACE_ADAPTIVE_MAX_POSITION),
        "adaptive_value_precision": int(FIBER_SURFACE_ADAPTIVE_VALUE_PRECISION),
        "threshold_cell_array": "sheetId",
        "image_resolution": list(FIBER_SURFACE_RENDER_IMAGE_RESOLUTION),
    }


def existing_outputs_complete(vtu_file: Path) -> bool:
    step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    path = manifest_path(step_dir)
    if not path.exists():
        return False

    try:
        manifest = json.loads(path.read_text())
    except Exception:
        return False

    expected = expected_manifest_payload(vtu_file)
    for key, value in expected.items():
        if manifest.get(key) != value:
            return False

    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        return False

    return all((step_dir / str(item.get("filename", ""))).exists() for item in images)


def check_inputs() -> None:
    if not FIBER_SURFACE_ADAPTIVE_ENABLED:
        raise RuntimeError(
            "adaptive fiber surfaces are disabled for this dataset; "
            f"FIBER_SURFACE_MODE={FIBER_SURFACE_MODE!r}"
        )
    if FIBER_SURFACE_ADAPTIVE_FIELD != "f":
        raise NotImplementedError("adaptive fiber-surface stage currently supports only field 'f'")
    if not FV99.exists():
        raise FileNotFoundError(f"fv99 binary not found: {FV99}")
    if not os.access(FV99, os.X_OK):
        raise PermissionError(f"fv99 binary is not executable: {FV99}")
    if not PVPYTHON.exists():
        raise FileNotFoundError(f"pvpython binary not found: {PVPYTHON}")
    if not os.access(PVPYTHON, os.X_OK):
        raise PermissionError(f"pvpython binary is not executable: {PVPYTHON}")
    if not FIBER_SURFACE_RENDER_STATE_FILE.exists():
        raise FileNotFoundError(f"ParaView state file not found: {FIBER_SURFACE_RENDER_STATE_FILE}")
    if not PARAVIEW_RENDER_HELPER.exists():
        raise FileNotFoundError(f"ParaView render helper not found: {PARAVIEW_RENDER_HELPER}")
    if not MATCHES_FILE.exists():
        raise FileNotFoundError(f"shape matches missing: {MATCHES_FILE}; run Stage 3A first")
    if not VTU_DIR.exists():
        raise FileNotFoundError(f"VTU directory not found: {VTU_DIR}")


def discover_timesteps(selected_stems: set[str] | None = None) -> list[Path]:
    vtu_files = sorted(VTU_DIR.glob("*.vtu"))
    if not selected_stems:
        return vtu_files
    return [vtu_file for vtu_file in vtu_files if vtu_file.stem in selected_stems]


def load_stride_one_pairs() -> list[dict]:
    payload = json.loads(MATCHES_FILE.read_text())
    by_stride = payload.get("pairwise_matches_by_stride")
    if isinstance(by_stride, dict):
        pairs = by_stride.get("1", [])
    else:
        pairs = payload.get("pairwise_matches", [])
    if not isinstance(pairs, list):
        raise ValueError(f"shape matches file has invalid pair list: {MATCHES_FILE}")
    return pairs


def descriptors_by_sheet(descriptors) -> dict[int, object]:
    return {int(sheet.sheet_id): sheet for sheet in descriptors.sheets}


def mask_to_f_value(mask: np.ndarray, global_bounds: tuple[float, float, float, float], grid_size: int) -> tuple[float | None, float]:
    weights = mask.astype(bool).sum(axis=0).astype(float)
    total = float(weights.sum())
    if total <= 0.0:
        return None, 0.0

    x_indices = np.arange(grid_size, dtype=float) + 0.5
    weighted_index = float(np.dot(x_indices, weights) / total)
    xmin, _ymin, xmax, _ymax = global_bounds
    if xmax <= xmin:
        return None, total
    f_value = xmin + (weighted_index / float(grid_size)) * (xmax - xmin)
    return f_value, total


def relative_position(sheet, f_value: float | None) -> float | None:
    if f_value is None:
        return None
    xmin, _ymin, xmax, _ymax = sheet.bbox
    width = float(xmax) - float(xmin)
    if width <= 1e-12:
        return None
    return clamp_position((float(f_value) - float(xmin)) / width)


def candidate_from_change_mask(
    change_mask: np.ndarray,
    sheet,
    descriptors,
) -> tuple[float | None, float]:
    f_value, weight = mask_to_f_value(
        change_mask,
        tuple(float(v) for v in descriptors.global_bounds),
        int(descriptors.grid_size),
    )
    return relative_position(sheet, f_value), weight


def add_candidate(
    candidates: dict[tuple[str, int], list[PositionCandidate]],
    stem: str,
    sheet_id: int,
    position: float | None,
    weight: float,
    pair: dict,
    match: dict,
    direction: str,
) -> None:
    if position is None:
        return
    candidates.setdefault((stem, int(sheet_id)), []).append(
        PositionCandidate(
            position=clamp_position(position),
            weight=max(float(weight), 1.0),
            source_stem=str(pair.get("source_stem", "")),
            target_stem=str(pair.get("target_stem", "")),
            source_sheet_id=int(match.get("source_sheet_id")),
            target_sheet_id=int(match.get("target_sheet_id")),
            direction=direction,
            score=float(match.get("final_score", match.get("shape_iou", 0.0))),
        )
    )


def best_by_key(matches: list[dict], key: str) -> dict[int, dict]:
    best: dict[int, dict] = {}
    for match in matches:
        sheet_id = int(match[key])
        score = float(match.get("final_score", match.get("shape_iou", 0.0)))
        current = best.get(sheet_id)
        current_score = float(current.get("final_score", current.get("shape_iou", 0.0))) if current else -1.0
        if current is None or score > current_score:
            best[sheet_id] = match
    return best


def compute_position_candidates() -> dict[tuple[str, int], list[PositionCandidate]]:
    candidates: dict[tuple[str, int], list[PositionCandidate]] = {}
    cache: dict[str, tuple[object, dict[int, np.ndarray], dict[int, object]]] = {}

    def cached(stem: str):
        if stem not in cache:
            descriptors, masks = load_timestep_cache(stem)
            cache[stem] = (descriptors, masks, descriptors_by_sheet(descriptors))
        return cache[stem]

    for pair in load_stride_one_pairs():
        source_stem = str(pair.get("source_stem", ""))
        target_stem = str(pair.get("target_stem", ""))
        if not source_stem or not target_stem:
            continue
        matches = pair.get("matches", [])
        if not isinstance(matches, list) or not matches:
            continue

        source_desc, source_masks, source_sheets = cached(source_stem)
        target_desc, target_masks, target_sheets = cached(target_stem)

        for source_sheet_id, match in best_by_key(matches, "source_sheet_id").items():
            target_sheet_id = int(match.get("target_sheet_id"))
            source_sheet = source_sheets.get(source_sheet_id)
            if source_sheet is None or source_sheet_id not in source_masks or target_sheet_id not in target_masks:
                continue
            change = np.logical_and(source_masks[source_sheet_id].astype(bool), ~target_masks[target_sheet_id].astype(bool))
            if not change.any():
                change = np.logical_xor(source_masks[source_sheet_id].astype(bool), target_masks[target_sheet_id].astype(bool))
            position, weight = candidate_from_change_mask(change, source_sheet, source_desc)
            add_candidate(candidates, source_stem, source_sheet_id, position, weight, pair, match, "outgoing")

        for target_sheet_id, match in best_by_key(matches, "target_sheet_id").items():
            source_sheet_id = int(match.get("source_sheet_id"))
            target_sheet = target_sheets.get(target_sheet_id)
            if target_sheet is None or source_sheet_id not in source_masks or target_sheet_id not in target_masks:
                continue
            change = np.logical_and(target_masks[target_sheet_id].astype(bool), ~source_masks[source_sheet_id].astype(bool))
            if not change.any():
                change = np.logical_xor(source_masks[source_sheet_id].astype(bool), target_masks[target_sheet_id].astype(bool))
            position, weight = candidate_from_change_mask(change, target_sheet, target_desc)
            add_candidate(candidates, target_stem, target_sheet_id, position, weight, pair, match, "incoming")

    return candidates


def choice_for_sheet(stem: str, sheet, candidates: dict[tuple[str, int], list[PositionCandidate]]) -> SheetChoice:
    sheet_candidates = candidates.get((stem, int(sheet.sheet_id)), [])
    if sheet_candidates:
        total_weight = sum(candidate.weight for candidate in sheet_candidates)
        if total_weight <= 0.0:
            position = float(FIBER_SURFACE_ADAPTIVE_DEFAULT_POSITION)
        else:
            position = sum(candidate.position * candidate.weight for candidate in sheet_candidates) / total_weight
        change_weight = total_weight
    else:
        position = float(FIBER_SURFACE_ADAPTIVE_DEFAULT_POSITION)
        change_weight = 0.0

    position = clamp_position(position)
    xmin, _ymin, xmax, _ymax = sheet.bbox
    if float(xmax) > float(xmin):
        f_value = float(xmin) + position * (float(xmax) - float(xmin))
    else:
        f_value = float(sheet.centroid[0]) if sheet.centroid else 0.0

    return SheetChoice(
        sheet_id=int(sheet.sheet_id),
        position=position,
        f_value=rounded_value(f_value),
        candidate_count=len(sheet_candidates),
        change_weight=float(change_weight),
    )


def run_fv99_adaptive_f_surface(vtu_file: Path, rs_file: Path, f_value: float, destination: Path, log_file: Path) -> tuple[bool, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_ADAPTIVE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    command = [
        str(FV99),
        "-f", str(vtu_file),
        "-l", str(rs_file),
        "--fieldFValueFS", value_text(f_value),
        "--fName", FV99_FNAME,
        "--gName", FV99_GNAME,
        "--headless",
    ]

    with tempfile.TemporaryDirectory(
        prefix=f"{vtu_file.stem}_adaptive_f_{signed_value_token(f_value)}_",
        dir=FIBER_SURFACE_ADAPTIVE_TEMP_DIR,
    ) as tmp_name:
        work_dir = Path(tmp_name)
        (work_dir / "output").mkdir(parents=True, exist_ok=True)

        with log_file.open("w") as log:
            result = subprocess.run(
                command,
                cwd=work_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=FV99_ENV,
            )

        source = work_dir / "output" / "labeled.fs.f.vtp"
        if result.returncode != 0:
            return False, f"fv99 returncode={result.returncode}; log={log_file}"
        if not source.exists():
            return False, f"fv99 did not produce output/labeled.fs.f.vtp; log={log_file}"

        destination.unlink(missing_ok=True)
        shutil.move(str(source), str(destination))

    return True, ""


def ensure_adaptive_labeled_surfaces(vtu_file: Path, rs_file: Path, choices: list[SheetChoice], rebuild: bool, log_dir: Path) -> dict[float, Path]:
    values = sorted({choice.f_value for choice in choices})
    outputs: dict[float, Path] = {}
    manifest_runs = []

    for f_value in values:
        path = adaptive_labeled_surface_path(vtu_file, f_value)
        log_file = log_dir / f"{vtu_file.stem}.adaptive_f_{signed_value_token(f_value)}.fv99.log"
        if rebuild or not path.exists():
            ok, message = run_fv99_adaptive_f_surface(vtu_file, rs_file, f_value, path, log_file)
            if not ok:
                raise RuntimeError(message)
            status = "generated"
        else:
            status = "skipped_existing"

        outputs[f_value] = path
        manifest_runs.append(
            {
                "field": "f",
                "value": f_value,
                "path": str(path),
                "log": str(log_file),
                "status": status,
            }
        )

    payload = {
        "timestep": vtu_file.stem,
        "vtu": str(vtu_file),
        "rs": str(rs_file),
        "f_name": FV99_FNAME,
        "g_name": FV99_GNAME,
        "surface_mode": "adaptive_f_range_change",
        "value_precision": int(FIBER_SURFACE_ADAPTIVE_VALUE_PRECISION),
        "surfaces": manifest_runs,
    }
    adaptive_labeled_manifest_path(vtu_file).write_text(json.dumps(payload, indent=2) + "\n")
    return outputs


def threshold_adaptive_surfaces(
    choices: list[SheetChoice],
    labeled_by_value: dict[float, Path],
    temp_step_dir: Path,
) -> dict[int, ThresholdedSurface]:
    temp_step_dir.mkdir(parents=True, exist_ok=True)
    by_sheet: dict[int, ThresholdedSurface] = {}

    for choice in choices:
        source = labeled_by_value[choice.f_value]
        filename = f"sheet_{choice.sheet_id}_f_pos_{signed_value_token(choice.f_value)}.vtp"
        destination = temp_step_dir / filename
        cell_count = threshold_sheet_surface(source, destination, choice.sheet_id)
        by_sheet[choice.sheet_id] = ThresholdedSurface(
            filename=filename,
            path=destination,
            field="f",
            sign="pos",
            sheet_id=choice.sheet_id,
            cell_count=cell_count,
        )

    return by_sheet


def render_adaptive_sheet_images(
    vtu_file: Path,
    molecule_file: Path,
    choices_by_sheet: dict[int, SheetChoice],
    surfaces_by_sheet: dict[int, ThresholdedSurface],
    image_step_dir: Path,
    render_work_dir: Path,
    render_log_file: Path,
) -> list[RenderedAdaptiveImage]:
    image_step_dir.mkdir(parents=True, exist_ok=True)
    render_work_dir.mkdir(parents=True, exist_ok=True)

    images: list[RenderedAdaptiveImage] = []
    image_specs = []

    for sheet_id, choice in choices_by_sheet.items():
        surface = surfaces_by_sheet[sheet_id]
        filename = f"sheet_{sheet_id}.png"
        output = image_step_dir / filename
        images.append(
            RenderedAdaptiveImage(
                filename=filename,
                sheet_id=sheet_id,
                choice=choice,
                surface=surface,
            )
        )
        image_specs.append(
            {
                "sheet_id": sheet_id,
                "output": str(output),
                "fiber_surfaces": {
                    ADAPTIVE_RENDER_ROLE: str(surface.path),
                },
            }
        )

    for image in images:
        (image_step_dir / image.filename).unlink(missing_ok=True)

    empty_fiber_surface = write_empty_molecule_vtp(render_work_dir / "empty_fiber_surface.vtp")
    base_spec = {
        "state_file": str(FIBER_SURFACE_RENDER_STATE_FILE),
        "vtu": str(vtu_file),
        "molecule_vtp": str(molecule_file),
        "empty_fiber_surface": str(empty_fiber_surface),
        "image_resolution": list(FIBER_SURFACE_RENDER_IMAGE_RESOLUTION),
    }

    pending_specs = list(image_specs)
    max_attempts = max(1, 1 + int(FIBER_SURFACE_RENDER_RETRIES))
    last_returncode = 0
    last_log_file = render_log_file

    for attempt in range(1, max_attempts + 1):
        spec = dict(base_spec)
        spec["images"] = pending_specs

        spec_file = render_work_dir / f"render_spec_attempt_{attempt}.json"
        spec_file.write_text(json.dumps(spec, indent=2) + "\n")

        attempt_log_file = (
            render_log_file
            if attempt == 1
            else render_log_file.with_name(
                f"{render_log_file.stem}.attempt_{attempt}{render_log_file.suffix}"
            )
        )
        command = [
            str(PVPYTHON),
            "--force-offscreen-rendering",
            str(PARAVIEW_RENDER_HELPER),
            "--spec",
            str(spec_file),
        ]
        with attempt_log_file.open("w") as log:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=RENDER_ENV,
                timeout=int(FIBER_SURFACE_RENDER_TIMEOUT_SECONDS),
            )

        last_returncode = result.returncode
        last_log_file = attempt_log_file
        pending_specs = [spec for spec in image_specs if not Path(spec["output"]).exists()]

        if not pending_specs:
            if result.returncode != 0:
                with attempt_log_file.open("a") as log:
                    log.write(
                        "\nAccepted nonzero pvpython returncode "
                        f"{result.returncode} because all expected screenshots exist.\n"
                    )
            break

        with attempt_log_file.open("a") as log:
            log.write(
                "\nMissing screenshots after attempt "
                f"{attempt}/{max_attempts}: "
                f"{[Path(spec['output']).name for spec in pending_specs]}\n"
            )

    missing_outputs = [Path(spec["output"]).name for spec in pending_specs]
    if missing_outputs:
        raise RuntimeError(
            f"pvpython returncode={last_returncode}; did not write image(s) "
            f"{missing_outputs} after {max_attempts} attempt(s); log={last_log_file}"
        )

    return images


def write_manifest(vtu_file: Path, choices: list[SheetChoice], images: list[RenderedAdaptiveImage]) -> None:
    step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    payload = expected_manifest_payload(vtu_file)
    payload["top_sheet_ids"] = [choice.sheet_id for choice in choices]
    payload["images"] = [
        {
            "filename": image.filename,
            "sheet_id": image.sheet_id,
            "adaptive_position": image.choice.position,
            "adaptive_f_value": image.choice.f_value,
            "candidate_count": image.choice.candidate_count,
            "change_weight": image.choice.change_weight,
            "surfaces": [
                {
                    "field": image.surface.field,
                    "sign": image.surface.sign,
                    "role": ADAPTIVE_RENDER_ROLE,
                    "value": image.choice.f_value,
                    "cell_count": image.surface.cell_count,
                    "temporary_filename": image.surface.filename,
                }
            ],
        }
        for image in images
    ]
    manifest_path(step_dir).write_text(json.dumps(payload, indent=2) + "\n")


def compute_timestep(vtu_file: Path, candidates: dict[tuple[str, int], list[PositionCandidate]], rebuild: bool) -> TimestepResult:
    rs_file = RS_DIR / f"{vtu_file.stem}.rs"
    if not rs_file.exists():
        return TimestepResult(vtu=vtu_file, status="failed", message=f"missing rs file: {rs_file}")

    rsi_file = RSI_DIR / f"{vtu_file.stem}.rsi"
    if not rsi_file.exists():
        return TimestepResult(vtu=vtu_file, status="failed", message=f"missing rsi file: {rsi_file}")

    if not rebuild and existing_outputs_complete(vtu_file):
        return TimestepResult(vtu=vtu_file, status="skipped_existing")

    try:
        descriptors, _masks = load_timestep_cache(vtu_file.stem)
    except Exception as exc:
        return TimestepResult(
            vtu=vtu_file,
            status="failed",
            message=f"failed to read compare-sheet cache for {vtu_file.stem}: {exc}",
        )

    sheets = list(descriptors.sheets)[: int(FIBER_SURFACE_TOP_N_SHEETS)]
    if not sheets:
        return TimestepResult(vtu=vtu_file, status="failed", message="no cached top sheets")

    choices = [choice_for_sheet(vtu_file.stem, sheet, candidates) for sheet in sheets]
    choices_by_sheet = {choice.sheet_id: choice for choice in choices}

    image_step_dir = FIBER_SURFACE_IMAGE_DIR / vtu_file.stem
    log_dir = FIBER_SURFACE_IMAGE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_ADAPTIVE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{vtu_file.stem}_adaptive_", dir=FIBER_SURFACE_ADAPTIVE_TEMP_DIR) as tmp_name:
        tmp_dir = Path(tmp_name)
        try:
            labeled_by_value = ensure_adaptive_labeled_surfaces(
                vtu_file,
                rs_file,
                choices,
                rebuild,
                log_dir,
            )
            surfaces_by_sheet = threshold_adaptive_surfaces(
                choices,
                labeled_by_value,
                tmp_dir / "thresholded",
            )
            render_log_file = log_dir / f"{vtu_file.stem}.adaptive.render.log"
            render_work_dir = tmp_dir / "render"
            molecule_file = render_molecule_vtp_path(vtu_file, render_work_dir)
            images = render_adaptive_sheet_images(
                vtu_file,
                molecule_file,
                choices_by_sheet,
                surfaces_by_sheet,
                image_step_dir,
                render_work_dir,
                render_log_file,
            )
        except subprocess.TimeoutExpired as exc:
            return TimestepResult(vtu=vtu_file, status="failed", message=f"render timeout after {exc.timeout}s")
        except Exception as exc:
            return TimestepResult(vtu=vtu_file, status="failed", message=f"adaptive render failed: {exc}")

    write_manifest(vtu_file, choices, images)
    return TimestepResult(vtu=vtu_file, status="done", image_count=len(images))


def compute_adaptive_fiber_surfaces_stage(
    selected_stems: set[str] | None = None,
    workers: int | None = None,
    rebuild: bool | None = None,
) -> None:
    check_inputs()

    FIBER_SURFACE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_ADAPTIVE_LABELED_DIR.mkdir(parents=True, exist_ok=True)
    FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    effective_workers = max(1, int(workers if workers is not None else FIBER_SURFACE_WORKERS))
    effective_rebuild = FIBER_SURFACE_REBUILD if rebuild is None else rebuild
    vtu_files = discover_timesteps(selected_stems)
    candidates = compute_position_candidates()

    print(
        f"Rendering adaptive f fiber-surface images for {len(vtu_files)} timesteps "
        f"using {effective_workers} worker(s)"
    )
    print(f"Fiber mode: {FIBER_SURFACE_MODE}")
    print(f"Image directory: {FIBER_SURFACE_IMAGE_DIR}")
    print(f"Adaptive labeled directory: {FIBER_SURFACE_ADAPTIVE_LABELED_DIR}")
    print(f"ParaView state: {FIBER_SURFACE_RENDER_STATE_FILE}")
    print(f"Sheet filter: top {FIBER_SURFACE_TOP_N_SHEETS} sheets from compare-sheet cache")

    failed_lines: list[str] = []

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [pool.submit(compute_timestep, vtu_file, candidates, effective_rebuild) for vtu_file in vtu_files]

        for count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result.status == "done":
                status = f"done images={result.image_count}"
            elif result.status == "skipped_existing":
                status = "skipped existing"
            else:
                status = f"failed {result.message}"
                failed_lines.append(f"{result.vtu}\t{result.message}")

            print(f"[{count}/{len(vtu_files)}] {status}: {result.vtu.name}", flush=True)

    FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE.write_text("\n".join(failed_lines))
    print(f"Failed files: {len(failed_lines)}")
    print(f"Failure log: {FIBER_SURFACE_ADAPTIVE_FAILED_LOG_FILE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render range-adaptive f fiber-surface images for top Reeb sheets."
    )
    parser.add_argument(
        "--timesteps",
        nargs="*",
        help="Optional timestep stems or filenames, e.g. step_01268 step_01280.vtu.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help=f"Parallel fv99/render jobs. Defaults to common.py value {FIBER_SURFACE_WORKERS}.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Re-render even if a matching adaptive image manifest already exists.",
    )
    args = parser.parse_args(argv)

    selected_stems = None
    if args.timesteps:
        selected_stems = {normalize_stem(value) for value in args.timesteps}

    compute_adaptive_fiber_surfaces_stage(
        selected_stems=selected_stems,
        workers=args.workers,
        rebuild=True if args.rebuild else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
