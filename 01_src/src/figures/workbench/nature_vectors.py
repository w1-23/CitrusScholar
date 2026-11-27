"""Presentation groups for source-locked Nature plot vector derivatives."""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from src.figures import projects, svg_projects
from src.figures.nature_plot_project import _file_blob
from src.figures.plot_vectors import PLOT_PROPERTIES, normalize_nature_svg, plot_objects
from src.figures.workbench.object_contract import contract
from src.session import files
from src.figures.workbench import artifacts
from src.figures.workbench.groups import _group_id

_PICKABLE_SHAPES = frozenset({"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"})


def is_plot(project):
    source = project.get("source_json")
    return (project.get("source_kind") == "svg" and isinstance(source, dict)
            and isinstance(source.get("nature_plot"), dict))


def _verify_frozen_source(project, owner, provenance):
    try:
        descriptor = provenance["original_exports"][provenance["primary_svg"]]
        row = files.get_account_file(descriptor["file_id"], owner)
        if not row or row.get("sha256") != descriptor["sha256"]:
            raise ValueError("frozen SVG is unavailable")
        original = _file_blob(row, descriptor["size"], descriptor["sha256"], capture=True)
        if original is None:
            raise ValueError("frozen SVG is too large")
        source = project["source_json"]
        base = normalize_nature_svg(original, provenance["backend"])
        edits = source.get("accepted_edits", [])
        if edits != (project.get("overrides_json") or []):
            raise ValueError("stored SVG overrides do not match")
        replayed = svg_projects.apply_svg_edits(base, edits, plot=True) if edits else base
        # Compare parsed canonical content. Whitespace between tags and stable
        # ID/order/attributes remain part of the frozen vector identity.
        if ET.tostring(ET.fromstring(replayed)) != ET.tostring(ET.fromstring(source["svg"])):
            raise ValueError("current SVG does not replay from the frozen source")
        return descriptor["sha256"]
    except (KeyError, TypeError, ValueError, OSError, ET.ParseError, projects.ProjectError) as exc:
        raise projects.ProjectError("plot_source_unverified", "无法从冻结原件重放本图件的覆盖样式。", 409) from exc


def _snapshot(project, owner, new_svg=None, *, draft_revision=None, frozen_sha=None):
    source = project["source_json"]
    provenance = source["nature_plot"]
    manifest = project.get("manifest_json") or {}
    registered = artifacts.entries(project, owner)
    blob = artifacts.read_entry(registered.get("svg"), owner, "svg")
    if (provenance.get("mode") != "locked_vector_paint" or provenance.get("version") != 1
            or provenance != manifest.get("nature_plot") or blob.digest != source.get("svg_sha256")
            or not isinstance(source.get("svg"), str) or blob.body != source["svg"].encode("utf-8")):
        raise projects.ProjectError("plot_source_unverified", "无法验证原图与当前样式副本的绑定。", 409)
    source_reason = ""
    if not frozen_sha:
        try:
            frozen_sha = _verify_frozen_source(project, owner, provenance)
        except projects.ProjectError as exc:
            source_reason = exc.message
    groups, targets, hits, geometry = [], {}, [], {}
    text = new_svg if new_svg is not None else source["svg"]
    root = ET.fromstring(text)
    nodes = {node.get("id"): (i, node) for i, node in enumerate(root.iter()) if node.get("id")}
    parents = {child: node for node in root.iter() for child in node}
    if len(nodes) != sum(bool(node.get("id")) for node in root.iter()):
        raise projects.ProjectError("plot_source_unverified", "SVG 对象标识存在歧义。", 409)
    for ident, tag, label, values in plot_objects(text):
        group_id = _group_id(project["figure_id"], ("nature_plot_native", ident))
        targets[group_id] = ident, values
        # The browser's detached SVG hit scene handles these native shapes.
        # Do not proxy text (svglite textLength differs from Canvas metrics),
        # resources, clips or a raster with approximate hit boxes.
        pick_ids = [ident] if tag in _PICKABLE_SHAPES else []
        role = "text" if tag in {"text", "tspan"} else "shape"
        order, node = nodes[ident]
        clips = set()
        while node is not None:
            clip = node.get("clip-path", "")
            if clip.startswith("url(#") and clip.endswith(")"):
                clips.add(clip[5:-1])
            node = parents.get(node)
        geometry[group_id] = (role, pick_ids, sorted(clips), order)
        if pick_ids:
            hits.append({"gid": ident, "role": "vector", "artist": tag})
        fill = nodes[ident][1].get("fill", "")
        paint_note = "原始数据、几何和字体保持锁定。"
        if fill == "none":
            paint_note = "没有填充。" + paint_note
        elif fill.startswith("url("):
            paint_note = "渐变或引用填充保持锁定。" + paint_note
        groups.append({"id": group_id, "kind": "vector", "label": f"{label} · {ident}" if label else ident,
                       "parent": None, "pick_ids": pick_ids, "properties": [] if source_reason else sorted(values), "values": values,
                       "reason": source_reason or paint_note, "line_marker_fill": False, "native_tag": tag})
    note = "仅可调整原生文字的颜色，以及受支持形状的颜色和线宽。数据、几何、文字内容、字体和原件保持冻结。"
    if provenance.get("backend") == "r":
        note = "R / svglite 原生对象。文字样式从对象树选择。" + note
    if provenance.get("origin") == "uploaded":
        note = "来自资料库上传（" + provenance["vector_dialect"] + " 样式）。平台未执行绘图或验证原始分析。" + note
    elif provenance.get("origin") == "code_output":
        note = "来自已验收的代码 SVG 输出。平台未验证原始分析。" + note
    return {"figure_id": project["figure_id"], "title": project.get("title") or "科研图件",
            "render_version": project["render_version"], "read_only_reason": source_reason or ("" if groups else "未发现可编辑的原生对象。"),
            "return_session_id": project.get("session_id"),
            "groups": groups, "format_capabilities": artifacts.capabilities(project, owner, replayable=False),
            **contract(groups, project["render_version"], svg=text.encode("utf-8"), draft_revision=draft_revision,
                       source_sha256=frozen_sha, geometry=geometry),
            "hit_manifest": {"objects": hits}, "figure_size": None, "options": {}, "marker_fill_states": [],
            "original_exports": provenance["original_exports"],
            "presentation_note": note}, targets


def detail(project, owner):
    result, _ = _snapshot(project, owner)
    return result


def edit(project, owner, commands, *, draft_revision, save, source_sha256=None):
    snapshot, targets = _snapshot(project, owner)
    frozen_sha = snapshot["source_sha256"]
    if not frozen_sha:
        raise projects.ProjectError("plot_source_unverified", snapshot["read_only_reason"], 409)
    if source_sha256 is not None and source_sha256 != frozen_sha:
        raise projects.ProjectError("source_conflict", "草稿绑定的冻结图件来源已变化。", 409)
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
        result, _ = _snapshot(project, owner, svg, draft_revision=draft_revision, frozen_sha=frozen_sha)
        return {**result, "svg": svg, "draft_revision": draft_revision}
    svg_projects.apply_svg_project_edits(figure_id=figure_id, owner=owner, edits=edits, expected_revision=revision,
        workbench_edit_binding={"source_sha256": frozen_sha, "expected_revision": revision, "draft_revision": draft_revision})
    committed = projects._load_revision(figure_id, owner, revision + 1)
    return detail(committed, owner)
