#!/usr/bin/env python3
"""Render sheet fiber-surface images from a ParaView state file.

This script is intended to be run with ParaView's pvpython, not system Python.
It receives a JSON spec produced by stage_04_compute_sheet_fiber_surfaces.py.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import paraview.simple as pvs  # type: ignore


FIBER_ROLES = ("f_neg", "f_pos", "g_neg", "g_pos")


def classify_path(value: str | None) -> str | None:
    if not value:
        return None

    text = str(value)
    name = Path(text).name
    lower = name.lower()

    if lower.endswith(".vtu"):
        return "vtu"
    if "molecularstructure" in text.lower() or (lower.startswith("step_") and lower.endswith(".vtp")):
        return "molecule"
    for role in FIBER_ROLES:
        if f"_{role}_" in lower:
            return role
    return None


def replacement_for_role(role: str, spec: dict, image: dict) -> str:
    if role == "vtu":
        return spec["vtu"]
    if role == "molecule":
        return spec["molecule_vtp"]
    return image["fiber_surfaces"][role]


def patch_state_for_first_image(state_file: Path, spec: dict, patched_state: Path) -> None:
    first_image = spec["images"][0]
    tree = ET.parse(state_file)
    root = tree.getroot()

    for prop in root.findall('.//Proxy[@group="sources"]/Property'):
        if prop.attrib.get("name") not in {"FileName", "FileNameInfo"}:
            continue
        for element in prop.findall("Element"):
            role = classify_path(element.attrib.get("value"))
            if role is None:
                continue
            element.set("value", replacement_for_role(role, spec, first_image))

    tree.write(patched_state, encoding="utf-8", xml_declaration=False)


def get_proxy_filename(proxy) -> str | None:
    try:
        value = proxy.FileName
    except Exception:
        return None

    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value) if value else None


def set_proxy_filename(proxy, filename: str) -> None:
    try:
        proxy.FileName = filename
    except Exception:
        proxy.FileName = [filename]
    proxy.UpdatePipeline()


def source_role_from_name_or_file(name: str, proxy) -> str | None:
    role = classify_path(get_proxy_filename(proxy))
    if role is not None:
        return role
    return classify_path(name)


def find_reader_sources() -> dict[str, object]:
    sources = {}
    for key, proxy in pvs.GetSources().items():
        name = key[0] if isinstance(key, tuple) else str(key)
        role = source_role_from_name_or_file(name, proxy)
        if role is not None:
            sources[role] = proxy

    missing = [role for role in ("vtu", "molecule", *FIBER_ROLES) if role not in sources]
    if missing:
        available = sorted(
            f"{key}: {get_proxy_filename(proxy)}"
            for key, proxy in pvs.GetSources().items()
        )
        raise RuntimeError(f"state is missing reader role(s) {missing}; available={available}")

    return sources


def update_sources(sources: dict[str, object], spec: dict, image: dict) -> None:
    set_proxy_filename(sources["vtu"], spec["vtu"])
    set_proxy_filename(sources["molecule"], spec["molecule_vtp"])
    for role in FIBER_ROLES:
        set_proxy_filename(sources[role], image["fiber_surfaces"][role])


def save_image(view, output: Path, spec: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    resolution = spec.get("image_resolution")
    if resolution:
        kwargs["ImageResolution"] = [int(resolution[0]), int(resolution[1])]
    pvs.SaveScreenshot(str(output), view, **kwargs)


def render_spec(spec: dict) -> None:
    state_file = Path(spec["state_file"])
    if not spec.get("images"):
        return

    with tempfile.TemporaryDirectory(prefix="fiber_surface_state_") as tmp_name:
        patched_state = Path(tmp_name) / "state.pvsm"
        patch_state_for_first_image(state_file, spec, patched_state)

        disable_reset = getattr(pvs, "DisableFirstRenderCameraReset", None) or getattr(pvs, "_DisableFirstRenderCameraReset", None)
        if disable_reset is not None:
            disable_reset()
        pvs.LoadState(str(patched_state))

        view = pvs.GetActiveViewOrCreate("RenderView")
        pvs.SetActiveView(view)
        sources = find_reader_sources()

        for image in spec["images"]:
            update_sources(sources, spec, image)
            pvs.Render(view)
            save_image(view, Path(image["output"]), spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    render_spec(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
