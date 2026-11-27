"""One turn's verified artifact delivery, derived from existing registries."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3

SCHEMA = "artifact-delivery/1"
_FIGURE_TOOL = "run_research_figure_script"
_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")


def _render_receipts(rows, messages):
    """Never infer a render from filenames or merge assets across tool calls."""
    for row in rows:
        edited = (row.get("name") == "research_workspace" and row.get("status") == "complete"
                  and row.get("reason") == "figure_edit_saved")
        if (row.get("name") != _FIGURE_TOOL and not edited) or row.get("inherited_from_job_id"):
            continue
        artifact = getattr(messages.get(row.get("tool_call_id")), "artifact", None)
        if not isinstance(artifact, dict):
            continue
        figures = artifact.get("figures")
        for figure in figures if isinstance(figures, list) else [artifact]:
            if not isinstance(figure, dict):
                continue
            qa = figure.get("qa_receipt") or {}
            qa_failed = bool(figure.get("qa_failures")) or row.get("reason") == "figure_qa_needs_attention"
            if isinstance(qa, dict):
                checks = qa.get("checks") or {}
                qa_failed = qa_failed or qa.get("status") in {"fail", "failed"} or (
                    isinstance(checks, dict) and any(isinstance(v, dict) and v.get("status") in {"fail", "failed"}
                                                     for v in checks.values()))
            tested = "failed" if qa_failed else "passed" if qa and row.get("status") == "complete" else "not_verified"
            for export in figure.get("exports") or ():
                if isinstance(export, dict) and export.get("artifact_id"):
                    yield export["artifact_id"], figure.get("figure_id"), export, tested, (
                        figure.get("render_version") if edited else 1)


def _render_metadata(owner, session, project_id, export, checksum, lookup, revision=1):
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        return {}
    try:
        project = lookup(project_id, owner)
    except (OSError, sqlite3.Error):
        return {}
    if not project:
        return {}
    registered = (project.get("exports_json") or {}).get(export.get("format"))
    document = project.get("document_json") or {}
    key = document.get("render_id")
    if (not isinstance(registered, dict) or registered.get("artifact_id") != export.get("artifact_id")
            or registered.get("sha256") != checksum):
        return {}
    result = {"render_id": key} if isinstance(key, str) and _ID.fullmatch(key) else {}
    frozen = (project.get("source_json") or {}).get("frozen_render")
    if (os.getenv("CITRUS_WORKBENCH_BRIDGE_ENABLED") == "1" and project.get("session_id") == session
            and project.get("source_kind") == "script" and type(revision) is int
            and 1 <= revision <= 999_999_999 and project.get("render_version") == revision
            and isinstance(frozen, dict)):
        if revision > 1:
            if os.getenv("CITRUS_WORKBENCH_STYLED_IMPORT") != "1":
                return result
            from src.figures.projects import ProjectError
            from src.figures.workbench_source import _saved_style

            receipt = frozen.get("receipt")
            if not isinstance(receipt, dict):
                return result
            try:
                _saved_style(project, receipt)
            except ProjectError:
                return result
            result["workbench_render_version"] = revision
        result["workbench_project_ref"] = project_id
    return result


def build_delivery(*, identity, request_id, rows, messages, requirements=(), select_links=None,
                   gaps=(), delivery_uncertain=False, validate=None, project_lookup=None, verified=None):
    if validate is None:
        from src.research.tools import validate_artifact_links as validate
    if project_lookup is None:
        from src.figures.store import get_project as project_lookup
    links = list(dict.fromkeys(item["link"] for row in rows if not row.get("inherited_from_job_id")
        for item in row.get("artifact_validations", ()) if item.get("link") and item.get("request_matches") is True))
    result = verified if verified is not None else (validate(links, session_id=identity.session_id,
             owner_user_id=identity.owner_user_id, request_id=request_id) if links else [])
    receipts = {asset_id: (project_id, export, qa, revision)
                for asset_id, project_id, export, qa, revision in _render_receipts(rows, messages)}
    selected = set(select_links) if select_links is not None else None
    assets, unavailable, seen = [], 0, set()
    for ref in result:
        link, asset_id, checksum = ref.get("link"), ref.get("artifact_id"), ref.get("sha256")
        if asset_id in seen or (selected is not None and link not in selected):
            continue
        if not (ref.get("valid") and ref.get("request_matches") is True and ref.get("reason_code") == "verified"
                and _ID.fullmatch(str(asset_id or "")) and _SHA.fullmatch(str(checksum or ""))
                and type(ref.get("size_bytes")) is int and ref["size_bytes"] > 0):
            unavailable += 1
            continue
        seen.add(asset_id)
        filename = str(ref.get("filename") or "")
        asset = {"asset_ref": asset_id, "filename": filename,
                 "format": Path(filename).suffix.lower().lstrip("."), "mime_type": ref.get("mime", ""),
                 "sha256": checksum, "size_bytes": ref["size_bytes"], "url": link,
                 "validation": {"status": "passed", "checks": ["stored_bytes", "sha256", "format", "owner", "turn"]}}
        if asset_id in receipts:
            project_id, export, qa, revision = receipts[asset_id]
            asset["qa_status"] = qa
            asset["render_id"] = None
            asset.update(_render_metadata(identity.owner_user_id, identity.session_id, project_id, export,
                                          checksum, project_lookup, revision))
        assets.append(asset)
    if not assets and not unavailable and not requirements and not delivery_uncertain:
        return {"status": "none", "verified_file_ids": [], "unavailable_count": 0}
    renders = {a["render_id"] for a in assets if a.get("render_id")}
    qa_gaps = any(a.get("qa_status") in {"failed", "not_verified"} for a in assets)
    incompatible = bool(gaps or unavailable or qa_gaps)
    state = ("unknown" if delivery_uncertain else "partial" if assets and incompatible else "complete"
             if assets else "failed")
    limitations = [str(g.get("reason_code") or "required_deliverable_missing") for g in gaps]
    if qa_gaps:
        limitations.append("figure_qa_incomplete")
    if unavailable:
        limitations.append("asset_validation_failed")
    if delivery_uncertain:
        limitations.append("delivery_reconciliation_required")
    missing_formats = []
    if gaps and len(requirements) == 1:
        supported = {"svg", "png", "pdf", "tiff"}
        formats = {a["format"] for a in assets}
        missing_formats = [fmt for fmt in requirements[0].get("formats", ())
                           if fmt in supported and fmt not in formats]
    delivery_id = hashlib.sha256((identity.run_id + "\0" + request_id).encode()).hexdigest()[:32]
    return {"schema_version": SCHEMA, "delivery_id": delivery_id, "logical_task_id": identity.run_id,
            "status": ("unknown" if state == "unknown" else "available" if state == "complete"
                        else "partial" if assets else "unavailable"),
            "delivery_status": state, "verified_file_ids": [a["asset_ref"] for a in assets],
            "unavailable_count": unavailable, "render_id": next(iter(renders)) if len(renders) == 1 else None,
            "missing_formats": missing_formats,
             "artifacts": assets, "user_links": [a["url"] for a in assets], "limitations": list(dict.fromkeys(limitations)),
            "obligations": [{"formats": list(r.get("formats") or ()), "label": str(r.get("label") or "")}
                            for r in requirements]}


def missing_answer_links(manifest, answer):
    return [link for link in manifest.get("user_links", ()) if link not in answer]
