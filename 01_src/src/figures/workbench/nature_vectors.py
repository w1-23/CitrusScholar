"""Presentation groups for source-locked Nature plot vector derivatives."""
from __future__ import annotations

import math

from src.figures import projects, svg_projects
from src.figures.plot_vectors import PLOT_PROPERTIES, plot_objects
from src.figures.workbench import artifacts
from src.figures.workbench.groups import _group_id

_PICKABLE_SHAPES = frozenset({"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"})


def is_plot(project):
    source = project.get("source_json")
    return (project.get("source_kind") == "svg" and isinstance(source, dict)
            and isinstance(source.get("nature_plot"), dict))


def _snapshot(project, owner, new_svg=None):
    source = project["source_json"]
    provenance = source["nature_plot"]
    manifest = project.get("manifest_json") or {}
    registered = artifacts.entries(project, owner)
    blob = artifacts.read_entry(registered.get("svg"), owner, "svg")
    if (provenance.get("mode") != "locked_vector_paint" or provenance.get("version") != 1
            or provenance != manifest.get("nature_plot") or blob.digest != source.get("svg_sha256")
            or not isinstance(source.get("svg"), str) or blob.body != source["svg"].encode("utf-8")):
        raise projects.ProjectError("plot_source_unverified", "无法验证原图与当前样式副本的绑定。", 409)
    groups, targets, hits = [], {}, []
    for ident, tag, label, values in plot_objects(new_svg if new_svg is not None else source["svg"]):
        group_id = _group_id(project["figure_id"], ("nature_plot_native", ident))
        targets[group_id] = ident, values
        # The browser's detached SVG hit scene handles these native shapes.
        # Do not proxy text (svglite textLength differs from Canvas metrics),
        # resources, clips or a raster with approximate hit boxes.
        pick_ids = [ident] if tag in _PICKABLE_SHAPES else []
        if pick_ids:
            hits.append({"gid": ident, "role": "vector", "artist": tag})
        groups.append({"id": group_id, "kind": "vector", "label": f"{label} · {ident}" if label else ident,
                       "parent": None, "pick_ids": pick_ids, "properties": sorted(values), "values": values,
                       "reason": "", "line_marker_fill": False, "native_tag": tag})
    note = "仅可调整原图对象的颜色和线宽。数据、几何、原始文字和原始文件不变。"
    if provenance.get("origin") == "uploaded":
        note = "来自资料库上传（" + provenance["vector_dialect"] + " 样式）。平台未执行绘图或验证原始分析。" + note
    elif provenance.get("origin") == "code_output":
        note = "来自已验收的代码 SVG 输出。平台未验证原始分析。" + note
    return {"figure_id": project["figure_id"], "title": project.get("title") or "科研图件",
            "render_version": project["render_version"], "read_only_reason": "" if groups else "未发现可编辑的原生对象。",
            "groups": groups, "format_capabilities": artifacts.capabilities(project, owner, replayable=False),
            "hit_manifest": {"objects": hits}, "figure_size": None, "options": {}, "marker_fill_states": [],
            "original_exports": provenance["original_exports"],
            "presentation_note": note}, targets


def detail(project, owner):
    result, _ = _snapshot(project, owner)
    return result


def edit(project, owner, commands, *, draft_revision, save):
    _, targets = _snapshot(project, owner)
    edits = []
    for command in commands:
        ident, values = targets.get(command["group_id"], (None, {}))
        prop, value = command["property"], command["value"]
        if not ident or prop not in values:
            raise projects.ProjectError("plot_edit_unsupported", "所选对象不支持这个样式。", 422)
        if prop == "linewidth":
            if type(value) not in {float, int} or not 0.2 <= value <= 8 or not math.isfinite(value):
                raise projects.ProjectError("plot_edit_unsupported", "线宽超出允许范围。", 422)
            # Closed SVG edits allow up to four decimal places. Refuse, do
            # not silently round or report a different accepted draft value.
            exact = f"{value:.4f}".rstrip("0").rstrip(".")
            if float(exact) != value:
                raise projects.ProjectError("plot_edit_unsupported", "线宽最多允许四位小数。", 422)
            value = exact
        elif not isinstance(value, str):
            raise projects.ProjectError("plot_edit_unsupported", "颜色必须使用十六进制色值。", 422)
        if len(value) == 4 and value.startswith("#"):
            value = "#" + "".join(ch * 2 for ch in value[1:])
        edits.append({"op": "set_attribute", "target": ident, "attribute": PLOT_PROPERTIES[prop], "value": value})
    figure_id, revision = project["figure_id"], project["render_version"]
    if not save:
        svg = svg_projects.apply_svg_edits(project["source_json"]["svg"], edits, plot=True)
        projects._assert_current(project, owner)
        result, _ = _snapshot(project, owner, svg)
        return {**result, "svg": svg, "draft_revision": draft_revision}
    svg_projects.apply_svg_project_edits(figure_id=figure_id, owner=owner, edits=edits, expected_revision=revision)
    committed = projects._load_revision(figure_id, owner, revision + 1)
    return detail(committed, owner)
