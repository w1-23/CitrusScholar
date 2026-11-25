"""Adapt authorized figure revisions into the workbench's group vocabulary."""
from __future__ import annotations

from citrus_figures.workbench_styles import COLORMAPS
from src.figures import projects, store
from src.figures.declarative_snapshot import SnapshotError, load_snapshot
from src.figures.document import SCHEMA, _digest
from src.figures.workbench import artifacts, nature_vectors
from src.figures.workbench.groups import Inventory, invalid

_OPTIONS = {"fontfamily": "font_families", "fontweight": "font_weights", "fontstyle": "font_styles",
            "horizontalalignment": "horizontalalignment_options", "loc": "legend_loc_options",
            "marker": "marker_options", "linestyle": "linestyle_options"}


def list_assets(owner, *, limit, offset, query="", kind="", chart="", access=""):
    result = []
    consumed = 0
    source_offset = 0
    needle = query.strip().casefold()
    while len(result) < limit + 1:
        rows = store.list_projects(owner, limit=100, offset=source_offset)
        if not rows:
            break
        source_offset += len(rows)
        for row in rows:
            source_kind = row.get("source_kind") or "unknown"
            title = str(row.get("title") or "未命名图件")
            if (kind and source_kind != kind) or (needle and needle not in title.casefold()):
                continue
            source = row.get("source_json")
            spec = source.get("spec") if source_kind == "spec" and isinstance(source, dict) else None
            chart_type = ("multipanel" if spec.get("panels") else spec.get("chart")) if isinstance(spec, dict) else None
            if chart and chart != chart_type:
                continue
            editable = None
            if access:
                if nature_vectors.is_plot(row):
                    try:
                        editable = bool(nature_vectors.detail(row, owner)["groups"])
                    except projects.ProjectError:
                        editable = False
                else:
                    _, inventory = _inventory(row, owner)
                    editable = not inventory.disabled and any(group.props for group in inventory.groups.values())
                if editable != (access == "editable"):
                    continue
            consumed += 1
            if consumed <= offset:
                continue
            exports = row.get("exports_json")
            result.append({"figure_id": row["figure_id"], "title": title, "render_version": row["render_version"],
                           "source_kind": source_kind, "chart_type": chart_type, "editable": editable,
                           "exports": {fmt: True for fmt in ("svg", "png") if isinstance(exports, dict) and exports.get(fmt)}})
            if len(result) == limit + 1:
                break
    return {"projects": result[:limit], "has_more": len(result) > limit}


def _document_matches(document, objects):
    bindings = document.get("objects")
    if not isinstance(bindings, list) or len(bindings) != len(objects):
        return False
    for index, (binding, row) in enumerate(zip(bindings, objects)):
        if (not isinstance(binding, dict) or binding.get("manifest_ref") != f"/manifest_json/objects/{index}"
                or binding.get("gid") != row.get("gid") or binding.get("role") != row.get("role")
                or (binding.get("identity_scope") == "semantic"
                    and binding.get("semantic_key") != row.get("semantic_key"))):
            return False
    return True


def _read_only(project, manifest, owner, frozen, *, check_document=True):
    source = project.get("source_json")
    if project.get("source_kind") == "svg":
        return "此图件是静态 SVG，未登记系列和子图编辑信息。"
    if not isinstance(source, dict):
        return "图件的已保存来源无法验证。"
    if not projects._can_edit(project):
        try:
            return projects.edit_context(project).get("edit_limitation") or "此图件没有可用的样式重放环境。"
        except (TypeError, KeyError, AttributeError):
            return "图件的已保存来源无法验证。"
    if not projects._is_script(project) and frozen is None:
        return "历史图件没有可验证的冻结数据快照。"
    document = project.get("document_json")
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA
            or document.get("revision") != project["render_version"]
            or document.get("document_id") != project["figure_id"] or not manifest.get("objects")
            or (check_document and not _document_matches(document, manifest["objects"]))):
        return "图件没有对应版本的已保存对象清单。"
    try:
        if _digest(source) != document.get("source_sha256"):
            return "已保存图件来源与此版本的对象清单不匹配。"
        projects._verify_sources(project, owner)
    except projects.ProjectError as exc:
        return exc.message
    except (TypeError, ValueError, KeyError, AttributeError):
        return "图件的已保存来源无法验证。"
    return ""


def _inventory(project, owner, *, check_preview=True):
    raw = project.get("manifest_json")
    try:
        manifest = projects._normalise_manifest(raw)
    except (TypeError, ValueError, KeyError):
        manifest = {"objects": []}
    # Reject a partially normalized inventory. A legacy invalid bounding box
    # can still be read through the sidebar with its geometry warning.
    frozen = None
    snapshot_error = False
    if project.get("source_kind") == "spec" and isinstance(project.get("source_json"), dict):
        try:
            source = project["source_json"]
            snapshot = load_snapshot(source)
            if snapshot is not None:
                frozen, _ = snapshot
                if manifest.get("resolved_data_sha256") != source["resolved_snapshot"]["resolved_sha256"]:
                    snapshot_error = True
        except SnapshotError:
            snapshot_error = True
    reason = ("图件的冻结数据快照与渲染对象无法验证。" if snapshot_error else
              _read_only(project, manifest, owner, frozen, check_document=check_preview))
    if not reason and (not isinstance(raw, dict) or not isinstance(raw.get("objects"), list)
                       or len(raw["objects"]) != len(manifest["objects"])):
        reason = "图件的已保存对象清单无法验证。"
    if not reason and check_preview:
        try:
            artifacts.read_entry(artifacts.entries(project, owner).get("svg"), owner, "svg")
        except projects.ProjectError as exc:
            reason = exc.message
    try:
        return manifest, Inventory(project, manifest, disabled=reason, frozen=frozen)
    except (projects.ProjectError, TypeError, AttributeError):
        return {"objects": []}, Inventory({**project, "overrides_json": []}, {"objects": []},
                                            disabled="图件的已保存对象清单无法验证。")


def _present(project, manifest, inventory, *, formats):
    return {"figure_id": project["figure_id"], "title": project.get("title") or "未命名图件",
            "render_version": project["render_version"], "read_only_reason": inventory.disabled,
            "groups": inventory.public(), "format_capabilities": formats,
            "hit_manifest": {"objects": manifest["objects"]}, "figure_size": manifest.get("figure_size_px"),
            "options": {**{prop: manifest.get(key, []) for prop, key in _OPTIONS.items()}, "cmap": list(COLORMAPS)},
            "marker_fill_states": (["auto", "none", "custom"] if project.get("source_kind") == "spec" else ["custom"])}


def detail(figure_id, owner, revision):
    project = projects._load_revision(figure_id, owner, revision)
    if nature_vectors.is_plot(project):
        result = nature_vectors.detail(project, owner)
        projects._assert_current(project, owner)
        return result
    manifest, inventory = _inventory(project, owner)
    capabilities = artifacts.capabilities(project, owner, replayable=not inventory.disabled and bool(project.get("session_id")))
    result = _present(project, manifest, inventory, formats=capabilities)
    projects._assert_current(project, owner)
    return result


def edit(figure_id, owner, revision, commands, *, draft_revision=0, save=False):
    project = projects._load_revision(figure_id, owner, revision)
    if nature_vectors.is_plot(project):
        return nature_vectors.edit(project, owner, commands, draft_revision=draft_revision, save=save)
    _, inventory = _inventory(project, owner)
    submitted, expected = inventory.translate(commands)

    def confirm(manifest, accepted, changed):
        try:
            # Use the same normalized value acknowledgement as script replay.
            # A child process reporting the right ids but dropping or changing
            # the edits must never make it to publication or a draft success.
            projects._check_worker_overrides(accepted, projects.merge_overrides(
                project.get("overrides_json") or [], submitted), manifest)
        except (projects.OverrideError, TypeError, KeyError, ValueError) as exc:
            raise invalid("group_edit_unconfirmed", "渲染器未确认提交的组样式。", 409) from exc
        if not {gid for gid, _ in expected} <= set(changed):
            raise invalid("group_edit_unconfirmed", "渲染器未应用全部组样式。", 409)
        inventory.confirm(expected, manifest, accepted)

    if not save:
        result = projects.preview_overrides(figure_id, owner, submitted, expected_revision=revision,
                                            draft_revision=draft_revision, confirm_targets=confirm)
        preview_project = {**project, "manifest_json": result["manifest"], "overrides_json": result["overrides"]}
        manifest, projected = _inventory(preview_project, owner, check_preview=False)
        return {**_present(project, manifest, projected, formats={}),
                "svg": result["svg"], "draft_revision": result["draft_revision"]}
    result = projects.save_overrides(figure_id, owner, submitted, expected_revision=revision,
                                     confirm_targets=confirm)
    committed = {**project, "render_version": result["render_version"], "manifest_json": result["manifest"],
                 "document_json": result["document"], "exports_json": result["exports"],
                 "overrides_json": result["overrides"]}
    manifest, updated = _inventory(committed, owner)
    return _present(committed, manifest, updated, formats=artifacts.capabilities(
        committed, owner, replayable=not updated.disabled and bool(committed.get("session_id"))))


def download(figure_id, owner, revision, fmt, *, generate=False):
    if generate:
        project = projects.get_for_owner(figure_id, owner)
        if project["render_version"] == revision:
            _, inventory = _inventory(project, owner)
            registered = artifacts.entries(project, owner)
            if fmt not in registered and (inventory.disabled or not project.get("session_id")):
                raise invalid("format_unavailable", "此版本没有可用的图件导出渲染。", 409)
    return artifacts.download(figure_id, owner, revision, fmt, generate=generate)
