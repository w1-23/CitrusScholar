"""Open strictly provenance-bound derivatives of actual Nature plot outputs.

This host-only entry is called after a trusted runner's outputs are individually
verified and published. It does not execute or alter any plot source or artifact.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
import re

from src.figures import projects, store, svg_projects
from src.figures.plot_vectors import normalize_nature_svg

PRODUCER = "run_nature_plot"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.(?:svg|png|pdf|tiff)")
_FILE_ID = re.compile(r"[a-f0-9]{32}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _file_blob(registered, size, sha, *, capture):
    path = Path(registered["path"])
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise ValueError("Nature plot original file has changed")
    with path.open("rb") as stream:
        if capture and size <= projects.MAX_PREVIEW_BYTES:
            blob = stream.read(projects.MAX_PREVIEW_BYTES + 1)
            current_sha = hashlib.sha256(blob).hexdigest()
        else:
            blob = None
            current_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if current_sha != sha:
        raise ValueError("Nature plot original digest mismatch")
    return blob


def _descriptor(file_id, filename, sha, size, origin):
    return {"file_id": file_id, "filename": filename, "sha256": sha, "size": size,
            "origin": origin, "download_url": f"/api/v2/files/{file_id}/download"}


def _originals(owner, sid, entries, primary, *, execution_id=None, request_id=None):
    from src.session import files

    if not isinstance(entries, dict) or not 1 <= len(entries) <= 4 or primary not in entries:
        raise ValueError("Missing Nature plot originals")
    safe, primary_blob = {}, None
    total = 0
    stem = primary.rsplit(".", 1)[0]
    for filename, entry in entries.items():
        if (not isinstance(filename, str) or not _NAME.fullmatch(filename) or not isinstance(entry, dict)
                or filename.rsplit(".", 1)[0] != stem):
            raise ValueError("Invalid Nature plot artifact name")
        file_id, sha, size = (entry.get(key) for key in ("file_id", "sha256", "size"))
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id) or not isinstance(sha, str) or not _DIGEST.fullmatch(sha):
            raise ValueError("Missing Nature plot artifact identity")
        if type(size) is not int or size <= 0 or (total := total + size) > projects.MAX_RENDER_BYTES:
            raise ValueError("Nature plot originals exceed size bounds")
        registered = files.get_account_file(file_id, owner)
        expected_mime = projects.MIME_BY_FORMAT[filename.rsplit(".", 1)[-1]]
        if (not registered or registered.get("session_id") != sid or registered.get("producer_tool") != PRODUCER
                or registered.get("origin") != files.ORIGIN_GENERATED or registered.get("filename") != filename
                or registered.get("sha256") != sha or registered.get("size_bytes") != size
                or registered.get("mime_type") != expected_mime):
            raise ValueError("Unverified or inaccessible Nature plot original")
        if execution_id is not None:
            from src.research.artifacts import verify_nature_original
            verify_nature_original(owner, sid, execution_id, request_id, filename, registered, entry)
        blob = _file_blob(registered, size, sha, capture=filename == primary)
        if filename == primary:
            primary_blob = blob
        safe[filename] = _descriptor(file_id, filename, sha, size, files.ORIGIN_GENERATED)
    return safe, primary_blob


def _link_result(originals, primary, *, origin):
    preview = next((item for name, item in originals.items() if name.endswith(".png")), originals[primary])
    return {"origin": origin, "editable": False, "original_exports": originals,
            "preview_url": f"/api/v2/files/{preview['file_id']}/preview"}


def _unsupported(result):
    return {**result, "publication_state": "read_only", "reason_code": "plot_svg_unsupported",
            "edit_limitation": "此 SVG 不兼容或未通过安全校验。可继续隔离预览或下载未修改的原件。"}


def _retryable(result):
    return {**result, "publication_state": "retryable", "reason_code": "plot_workspace_unavailable",
            "edit_limitation": "可编辑副本暂未登记成功，可重试收集。原件未修改，仍可预览或下载。"}


def _editable(result, backend, figure_id, receipt):
    revision = receipt["render_version"]
    return {**result, "editable": True, "publication_state": "ready",
            "vector_dialect": "svglite" if backend == "r" else "matplotlib", "figure_id": figure_id,
            "render_version": revision, "workspace_url": f"/figure-workbench?project={figure_id}",
            "workbench_url": f"/figure-workbench?project={figure_id}",
            "preview_url": f"/api/v2/figure-workbench/projects/{figure_id}/preview/svg?revision={revision}",
            "exports": receipt["exports"]}


def _bound_derivative(result, owner, sid, backend, binding):
    if binding is None:
        return None
    execution_id, fingerprint = binding
    chosen = store.get_nature_binding(owner, sid, execution_id)
    if chosen is None:
        return None
    if chosen["fingerprint"] != fingerprint:
        raise projects.ProjectError("plot_execution_binding_mismatch", "绘图执行原件与已登记副本不一致。", 409)
    figure_id = chosen["figure_id"]
    if store.get_project(figure_id, owner) is None:
        return {**result, "publication_state": "read_only", "reason_code": "plot_workspace_deleted",
                "edit_limitation": "该执行的可编辑副本已删除，原件仍可预览或下载。"}
    projects.complete_render_publication(figure_id, owner)
    from src.figures.workbench.artifacts import read_entry
    read_entry(chosen["receipt"]["exports"]["svg"], owner, "svg")
    response = _editable(result, backend, figure_id, chosen["receipt"])
    # The execution receipt is immutable. The workbench opens the current copy;
    # preview the receipt bytes directly after any later edit.
    response["preview_url"] = f"/api/v2/files/{response['exports']['svg']['file_id']}/preview"
    return response


def _create_derivative(*, result, owner, sid, title, backend, vector, primary, vendor_root,
                       binding=None, request_id=None):
    dialect = "svglite" if backend == "r" else "matplotlib"
    provenance = {"version": 1, "backend": backend, "vector_dialect": dialect,
                  "origin": result["origin"], "mode": "locked_vector_paint",
                  "primary_svg": primary, "original_exports": result["original_exports"]}
    try:
        try:
            created = svg_projects.create_svg_project(owner=owner, session_id=sid, svg=vector,
                title=title, alt_text=title, vendor_root=vendor_root, plot_provenance=provenance,
                nature_binding=binding, request_id=request_id)
        except store.NatureBindingConflict:
            return _bound_derivative(result, owner, sid, backend, binding)
        if binding is not None:
            return _bound_derivative(result, owner, sid, backend, binding)
    except (projects.ProjectError, ValueError, OSError, sqlite3.Error) as exc:
        if getattr(exc, "code", "") in {"plot_svg_unsupported", "svg_invalid", "svg_security"}:
            return _unsupported(result)
        logging.getLogger(__name__).warning("Nature editable copy registration deferred", exc_info=True)
        return _retryable(result)
    return _editable(result, backend, created["figure_id"], created)


def open_nature_plot(*, owner, session_id, title, backend, original_exports, primary_svg, vendor_root=None,
                     execution_id=None, request_id=None):
    """Return the exact execution's editable copy or honest original-only state."""
    owner = svg_projects._valid_owner(owner)
    sid = svg_projects._require_session(owner, session_id)
    if backend not in {"python", "r"} or not isinstance(primary_svg, str) or not primary_svg.endswith(".svg"):
        raise projects.ProjectError("invalid_nature_plot", "没有明确的已批准绘图后端和 SVG 产物。", 422)
    if execution_id is not None and (not isinstance(execution_id, str) or not _FILE_ID.fullmatch(execution_id)
                                     or not isinstance(request_id, str) or not request_id):
        raise projects.ProjectError("invalid_nature_plot", "没有明确的绘图执行与发起请求。", 422)
    try:
        originals, blob = _originals(owner, sid, original_exports, primary_svg,
                                     execution_id=execution_id, request_id=request_id)
    except (ValueError, KeyError, OSError) as exc:
        raise projects.ProjectError("plot_original_unverified", "无法验证绘图原件的归属和摘要。", 409) from exc
    result = {**_link_result(originals, primary_svg, origin="worker_output"), "backend": backend}
    binding = None
    if execution_id is not None:
        fingerprint = hashlib.sha256(json.dumps([backend, primary_svg, request_id, originals],
                                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        binding = execution_id, fingerprint
        try:
            prior = _bound_derivative(result, owner, sid, backend, binding)
        except (projects.ProjectError, ValueError, OSError, sqlite3.Error) as exc:
            if getattr(exc, "code", "") == "plot_execution_binding_mismatch":
                raise
            logging.getLogger(__name__).warning("Nature editable copy recovery deferred", exc_info=True)
            return _retryable(result)
        if prior is not None:
            return prior
    if blob is None:
        return _unsupported(result)
    try:
        vector = normalize_nature_svg(blob, backend)
    except ValueError:
        return _unsupported(result)
    return _create_derivative(result=result, owner=owner, sid=sid, title=title, backend=backend,
                              vector=vector, primary=primary_svg, vendor_root=vendor_root,
                              binding=binding, request_id=request_id)


def import_uploaded_nature_svg(owner, file_id, *, vendor_root=None):
    """Opt-in derivative of one owned upload, not proof of a plotting run."""
    from src.session import files

    owner = svg_projects._valid_owner(owner)
    row = files.get_account_file(file_id, owner) if isinstance(file_id, str) and _FILE_ID.fullmatch(file_id) else None
    if not row or row.get("origin") != files.ORIGIN_UPLOADED:
        raise projects.ProjectError("plot_upload_not_found", "找不到可访问的上传原件。", 404)
    filename, sha, size = (row.get(key) for key in ("filename", "sha256", "size_bytes"))
    if (not isinstance(filename, str) or not filename.lower().endswith(".svg")
            or row.get("mime_type") != "image/svg+xml"):
        raise projects.ProjectError("plot_upload_not_svg", "只支持从资料库中的上传 SVG 创建样式副本。", 422)
    sid = svg_projects._require_session(owner, row.get("session_id"))
    if not isinstance(sha, str) or not _DIGEST.fullmatch(sha) or type(size) is not int or not 0 < size <= projects.MAX_RENDER_BYTES:
        raise projects.ProjectError("plot_upload_unverified", "上传原件大小超限或缺少摘要。", 422)
    try:
        blob = _file_blob(row, size, sha, capture=True)
    except (KeyError, OSError, ValueError) as exc:
        raise projects.ProjectError("plot_original_unverified", "无法验证上传原件的摘要。", 409) from exc
    originals = {filename: _descriptor(file_id, filename, sha, size, files.ORIGIN_UPLOADED)}
    result = _link_result(originals, filename, origin="uploaded")
    if blob is not None:
        for backend in ("r", "python"):
            try:
                vector = normalize_nature_svg(blob, backend)
            except ValueError:
                continue
            return _create_derivative(result=result, owner=owner, sid=sid, title=Path(filename).stem,
                                      backend=backend, vector=vector, primary=filename, vendor_root=vendor_root)
    return _unsupported(result)
