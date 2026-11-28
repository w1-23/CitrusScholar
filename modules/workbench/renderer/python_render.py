"""Single-job Workbench adapter for the pinned Tavotto Matplotlib engine.

The caller must isolate the process. Loading a source runs arbitrary Python, and
capture of a Matplotlib save call is not a security boundary.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
import math
import random
import runpy
from contextlib import chdir, nullcontext
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import matplotlib
from matplotlib import pyplot as plt
from matplotlib.figure import Figure
import numpy as np
from tavotto.engine import bridgeboot

# Adapter compatibility is tied to TAVOTTO_UPSTREAM.lock.json.
RENDERER = {"id": "tavotto-matplotlib", "version": "0.21.3"}
STEM = "figure"
TEXT_ROLES = {"title", "axis_label", "text", "legend_text"}
SERIES_ROLES = {"line", "scatter", "bar_series"}
LIMITATIONS = (
    "The convenience objects list exposes text edits. Other properties use Tavotto's native manifest and native patches.",
    "The source script is replayed on each render. Overrides do not rewrite the source.",
    "Exports use the full figure canvas. Source savefig file writes and crop options are not reproduced.",
)


def parse_request(path: Path) -> dict:
    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    if request.get("format") not in ("svg", "png", "pdf"):
        raise ValueError("format must be svg, png, or pdf")
    dpi = request.get("dpi", 300)
    if type(dpi) is not int or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    for key in ("width_mm", "height_mm"):
        if key in request:
            value = request[key]
            if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive number")
    overrides = request.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("overrides must map object IDs to text fields")
    for gid, fields in overrides.items():
        if not isinstance(fields, dict) or set(fields) != {"text"} or not isinstance(fields["text"], str):
            raise ValueError(f"override for {gid!r} must contain only a string text field")
    return {**request, "dpi": dpi, "overrides": overrides}


def load_engine():
    return bridgeboot.load_engine_modules(
        str(Path(bridgeboot.__file__).parent),
        (*bridgeboot.ENGINE_SIBLINGS, "figsession", "executionfact", "inputdeclaration",
         "previewbudget", "preview_complexity", "preview_hybrid", "teedraw"),
    )


def frozen_globals(source: Path, bindings) -> dict:
    """Restore the same typed data mapping as the Host's separated renderer."""
    import keyword
    import re

    if (not isinstance(bindings, list) or not 1 <= len(bindings) <= 8
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name)
                   or keyword.iskeyword(name) for name in bindings)
            or len(set(bindings)) != len(bindings)):
        raise ValueError("Invalid frozen data bindings")
    from citrus_figures.dataset_codec import decode_frame

    data = {}
    for index, binding in enumerate(bindings):
        with (source.parent / f"input_{index}.json").open(encoding="utf-8") as stream:
            data[binding] = decode_frame(json.load(stream))
    return {"data": data, "plt": plt, "matplotlib": matplotlib}


def capture_figures(source: Path, *, init_globals=None) -> list[Figure]:
    """Run a script and retain open, shown, or saved figures."""
    captured = {}
    original_savefig = Figure.savefig

    def capture_savefig(figure, fname, *args, **kwargs):
        captured[id(figure)] = figure
        # In-memory saves are useful to the script and don't generate stray files.
        if hasattr(fname, "write"):
            return original_savefig(figure, fname, *args, **kwargs)
        return None

    def capture_open(*_args, **_kwargs):
        for number in plt.get_fignums():
            figure = plt.figure(number)
            captured[id(figure)] = figure

    namespace = {}
    with (
        patch.object(Figure, "savefig", capture_savefig),
        patch.object(plt, "show", capture_open),
        patch.object(sys, "argv", [str(source)]),
        patch.object(sys, "path", [str(source.parent), *sys.path]),
        chdir(source.parent) if source.parent == Path("/inputs/project") else nullcontext(),
    ):
        try:
            namespace = runpy.run_path(str(source), run_name="__main__", init_globals=init_globals)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise ValueError(f"source exited with status {exc.code}") from exc
    capture_open()
    if not captured:
        captured = {id(value): value for value in namespace.values() if isinstance(value, Figure)}
    if not 1 <= len(captured) <= 8:
        raise ValueError(f"source must create one to eight Matplotlib Figures, found {len(captured)}")
    return list(captured.values())


def editable_entries(manifest: dict) -> dict:
    """Index native Tavotto gids and its advertised editable text fields."""
    result = {}
    for element in manifest["elements"]:
        role = element["role"]
        prop = "text" if role in TEXT_ROLES else "label" if role in SERIES_ROLES else None
        if prop is None:
            continue
        for field in element["editable"]:
            if field["prop"] == prop and isinstance(field.get("value"), str):
                result[element["gid"]] = (element, prop, field["value"])
                break
    return result


def make_patches(entries: dict, overrides: dict, native: object, patchspec) -> tuple[list[dict], dict]:
    _canonical, dropped = patchspec.canonicalize_with_diagnostics(native)
    if dropped:
        diagnostics = ", ".join(f"{row['index']}: {row['reason']}" for row in dropped)
        raise ValueError(f"invalid native_patches entries ({diagnostics})")
    # Preserve submitted order and upstream last-wins behavior, as the native
    # worker passes the actual list (not a sorted canonical list) to do_export.
    native_keys = {(row["gid"], row["prop"]) for row in native}
    replacement = {}
    for gid, fields in overrides.items():
        if gid not in entries:
            raise ValueError(f"object {gid!r} does not support text overrides")
        if (gid, entries[gid][1]) in native_keys:
            raise ValueError(f"object {gid!r} has both a text override and a native patch for the same property")
        replacement[gid] = fields["text"]

    # Matplotlib stores separate label strings on the series and on the legend.
    # Carry existing exact-match links through a rename, keeping custom captions
    # and explicit legend overrides independent.
    for gid, (element, _prop, text) in entries.items():
        link = element.get("legend_entry", {})
        source = link.get("source_gid")
        if (
            source in replacement and gid not in replacement and (gid, "text") not in native_keys
            and link.get("binding_default") == "follow_source" and text == entries[source][2]
        ):
            replacement[gid] = replacement[source]

    patches = list(native)
    for gid, value in replacement.items():
        element, prop, _text = entries[gid]
        change = {"gid": gid, "prop": prop, "value": value}
        if "identity" in element:
            change["identity"] = element["identity"]
        patches.append(change)
    return patches, replacement


def workbench_result(baseline: dict, rendered: dict, replacement: dict) -> dict:
    initial = editable_entries(baseline)
    updated = editable_entries(rendered)
    panels = {
        row["gid"]: {"id": row["gid"], "label": row["label"]}
        for row in rendered["elements"] if row["role"] in ("axes", "axes3d")
    }
    objects = []
    for gid in dict.fromkeys([*initial, *updated]):
        if gid in updated:
            element, prop, text = updated[gid]
        elif gid in replacement and replacement[gid] == "":
            # Tavotto omits empty, undrawn Text from the export manifest. Keep
            # this known object editable so a later request can restore its text.
            element, prop, _previous = initial[gid]
            text = ""
        else:
            continue
        panel_id = gid
        while panel_id not in panels and "." in panel_id:
            panel_id = panel_id.rsplit(".", 1)[0]
        if panel_id not in panels:
            panel_id = STEM
            panels.setdefault(STEM, {"id": STEM, "label": "Figure"})
        if element["role"] == "title" and gid == f"{panel_id}.title" and text:
            panels[panel_id]["label"] = text
        objects.append({
            "id": gid, "panel_id": panel_id, "kind": element["role"],
            "label": (text or element["role"]) if prop == "label" or not text else element["label"],
            "properties": {"text": text}, "editable": ["text"],
        })
    if not panels:
        panels[STEM] = {"id": STEM, "label": "Figure"}
    native_editability = "preview_only"
    if any(row.get("editable") for row in rendered["elements"]):
        native_editability = "partial" if rendered.get("unsupported") else "semantic"
    return {
        "renderer": RENDERER,
        "editability": native_editability,
        "native_editability": native_editability,
        "native_manifest": rendered,
        "panels": list(panels.values()),
        "objects": objects,
        "limitations": list(LIMITATIONS),
    }


def export_size_patches(patches, request, figure_gid):
    """Explicit export dimensions supersede only the matching saved axis size."""
    if not ("width_mm" in request or "height_mm" in request):
        return patches
    sized = []
    for patch in patches:
        if patch["gid"] == figure_gid and patch["prop"] == "size_mm":
            width, height = patch["value"]
            patch = {**patch, "value": [request.get("width_mm", width), request.get("height_mm", height)]}
        sized.append(patch)
    return sized


def replay_host_style(figure, replay):
    from citrus_figures.core import RenderedFigure
    from citrus_figures.manifest import assign_locators, build_manifest
    from citrus_figures.overrides import apply_overrides, validate_overrides
    from citrus_figures.styling import prepare_figure_fonts
    from citrus_figures.style_replay import (coordinate_sha256, replay_baseline,
                                              typed_payload_sha256, valid_baseline)

    if not isinstance(replay, dict) or set(replay) != {"baseline", "overrides", "overrides_sha256"}:
        raise ValueError("Invalid saved style receipt")
    expected = replay["baseline"]
    if not valid_baseline(expected) or not isinstance(replay["overrides"], list) or not replay["overrides"]:
        raise ValueError("Invalid saved style receipt")
    prepare_figure_fonts(figure)
    rendered = assign_locators(RenderedFigure(figure=figure, axes=list(figure.axes), qa_profile={}))
    manifest = build_manifest(rendered, source="script", include_qa=False)
    if replay_baseline(figure, manifest["objects"]) != expected:
        raise ValueError("The original figure could not be reproduced")
    edits = validate_overrides(replay["overrides"], manifest)
    if typed_payload_sha256(edits) != replay["overrides_sha256"]:
        raise ValueError("The saved style changes do not match the receipt")
    applied = apply_overrides(rendered, manifest, edits)
    if sorted(applied) != sorted(edit["gid"] for edit in edits):
        raise ValueError("The saved style changes were not applied")
    figure.canvas.draw()
    if coordinate_sha256(figure) != expected["coordinates_sha256"]:
        raise ValueError("The saved style changes altered figure data or geometry")


def render(source: Path, request: dict, output: Path) -> None:
    matplotlib.use("Agg")
    engine = load_engine()
    ordinal = request.get("figure_ordinal")
    if ordinal is not None and (type(ordinal) is not int or not 0 <= ordinal <= 7):
        raise ValueError("Invalid figure ordinal")
    if ordinal is not None:
        random.seed(1)
        np.random.seed(1)
    bindings = request.get("frozen_bindings")
    captured = (capture_figures(source) if bindings is None else
                capture_figures(source, init_globals=frozen_globals(source, bindings)))
    if ordinal is None and len(captured) != 1:
        raise ValueError(f"source must create one Matplotlib Figure, found {len(captured)}")
    if ordinal is not None and ordinal >= len(captured):
        raise ValueError("Figure ordinal not captured")
    figure = captured[ordinal or 0]
    if "host_style_replay" in request:
        if bindings is None or ordinal is not None:
            raise ValueError("Saved Host style is only supported with a frozen renderer")
        replay_host_style(figure, request["host_style_replay"])
    capture = None
    if ordinal is not None:
        # Witness the unmodified capture before a requested size or any native patch.
        with BytesIO() as witness:
            figure.savefig(witness, format="png", dpi=80, bbox_inches=figure.bbox_inches,
                           metadata={"Software": None})
            capture = {"ordinal": ordinal, "count": len(captured), "signature": sha256(witness.getbuffer()).hexdigest()}
    width, height = figure.get_size_inches()
    if "width_mm" in request:
        width = request["width_mm"] / 25.4
    if "height_mm" in request:
        height = request["height_mm"] / 25.4
    figure.set_size_inches(width, height, forward=False)
    fmt = request["format"]
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tavotto-", dir=output) as scratch:
        stage = Path(scratch)
        session = engine.figsession.LiveFigureSession(stage / "session")
        # Force the full canvas so savefig.bbox rcParams can't silently change
        # requested dimensions. Instrumentation and export use the same frame.
        with matplotlib.rc_context({"savefig.bbox": None}):
            session.add_figure(STEM, figure, engine.figcapture.SOURCE_PYPLOT)
            session.instrument_all(raise_if_none_live=True)
            baseline = session.manifest(STEM)
            patches, replacement = make_patches(
                editable_entries(baseline), request["overrides"], request.get("native_patches", []),
                engine.patchspec,
            )
            figure_gid = next((element["gid"] for element in baseline["elements"]
                               if element["role"] == "figure"), "figure")
            patches = export_size_patches(patches, request, figure_gid)
            artifact = stage / f"figure.{fmt}"
            exported = session.do_export(
                STEM, patches, str(artifact), fmt, request["dpi"],
                svg_fonttype="none" if fmt == "svg" else None,
                data_points=[],  # also returns the manifest measured on this exact export draw
            )
        warnings = exported["warnings"]
        future = exported.get("future_props", [])
        if warnings or future:
            details = [*warnings, *(f"unrecognized property: {prop}" for prop in future)]
            raise ValueError("Tavotto could not apply all overrides: " + " | ".join(details))
        result = workbench_result(baseline, exported["draw_manifest"], replacement)
        if capture is not None:
            result["capture"] = capture
        result_file = stage / "result.json"
        result_file.write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False, default=engine.figsession._json_default)
            + "\n", encoding="utf-8",
        )
        artifact.replace(output / artifact.name)
        result_file.replace(output / result_file.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        render(args.source.resolve(), parse_request(args.request), args.output.resolve())
    except Exception as exc:
        parser.exit(1, f"Python render failed: {exc}\n")


if __name__ == "__main__":
    main()
