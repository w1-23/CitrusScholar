"""Single-job Workbench adapter for the pinned Tavotto Matplotlib engine.

The caller must isolate the process. Loading a source runs arbitrary Python, and
capture of a Matplotlib save call is not a security boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import matplotlib
from matplotlib import pyplot as plt
from matplotlib.figure import Figure
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


def capture_figure(source: Path) -> Figure:
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
    ):
        try:
            namespace = runpy.run_path(str(source), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise ValueError(f"source exited with status {exc.code}") from exc
    capture_open()
    if not captured:
        captured = {id(value): value for value in namespace.values() if isinstance(value, Figure)}
    if len(captured) != 1:
        raise ValueError(f"source must create one Matplotlib Figure, found {len(captured)}")
    return next(iter(captured.values()))


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


def render(source: Path, request: dict, output: Path) -> None:
    matplotlib.use("Agg")
    engine = load_engine()
    figure = capture_figure(source)
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
