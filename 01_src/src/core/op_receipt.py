"""Normalize host tool results before lossy model-context projection.

Raw result storage remains in tool_results. This bounded metadata receipt carries
only evidence identity, coverage and outputs; it never interprets model prose.
"""
from __future__ import annotations
import json
import re
from typing import Any


# A first-party artifact identity and its content hash. Everything else (host
# paths, foreign hosts, unbounded text) is not publication evidence.
_ARTIFACT_URL = re.compile(r"/api/v2/session/[A-Za-z0-9_-]{1,128}/artifacts/[a-f0-9]{32}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_FILENAME = re.compile(r"[\w .()\[\]-]{1,200}", re.UNICODE)
MAX_PUBLICATION_FILES = 32


def publication_stages(payload: dict) -> list[dict]:
    """Project per-file publication stages as evidence, never as authority.

    Only server-observed identities are kept, and each stage is derived from
    real evidence instead of a claim: ``confirmed`` needs both a first-party
    artifact URL and the content hash, so a summary that merely says
    "confirmed" stays unconfirmed. Host paths, foreign URLs and unbounded text
    are dropped. No field here authorizes a resend; those stay with the caller.
    """
    outcome = payload.get("_tool_outcome")
    files = outcome.get("files") if isinstance(outcome, dict) else None
    if not isinstance(files, list):
        return []
    projected = []
    for item in files[:MAX_PUBLICATION_FILES]:
        if not isinstance(item, dict):
            continue
        index = item.get("file_index")
        if type(index) is not int or not 1 <= index <= 1000:
            continue
        filename = item.get("filename")
        filename = filename if isinstance(filename, str) and _FILENAME.fullmatch(filename) else ""
        url = item.get("url")
        url = url if isinstance(url, str) and _ARTIFACT_URL.fullmatch(url) else ""
        digest = item.get("sha256")
        digest = digest if isinstance(digest, str) and _SHA256.fullmatch(digest) else ""
        if item.get("confirmed") is True and url and digest:
            stage, next_stage = "confirmed", None
        elif item.get("committed") is True and digest:
            stage, next_stage = "committed", "confirmed"
        elif item.get("started") is True:
            stage, next_stage = "started", "committed"
        else:
            continue
        projected.append({"file_index": index, "filename": filename, "stage": stage,
                          "url": url if stage == "confirmed" else "", "sha256": digest,
                          "next_stage": next_stage})
    return projected


def result_payload(content: Any, artifact: Any) -> dict:
    try:
        parsed = json.loads(content) if isinstance(content, str) else {}
    except (ValueError, TypeError):
        parsed = {}
    data = dict(parsed) if isinstance(parsed, dict) else {}
    if isinstance(artifact, dict):
        data.update(artifact)
    return data


def make_receipt(name: str, call_id: str, content: Any, artifact: Any) -> dict:
    from src.tools.outcome import normalize_tool_outcome
    outcome = normalize_tool_outcome(name, str(content or ""), artifact)
    payload = result_payload(content, artifact)
    targets = {}
    if name == "verify_uploaded_references":
        targets = {"coverage": payload.get("coverage", {}), "rows": [
            {k: r[k] for k in ("record_id", "record_index", "source_sha256", "verification_rule",
                               "status", "doi", "input_title", "detail") if k in r}
            for r in payload.get("rows", []) if isinstance(r, dict)]}
        if payload.get("schema") == "reference_audit/2":
            targets.update({key: payload[key] for key in (
                "schema", "audit_id", "rule_version", "terminal", "export_status", "run_reason"
            ) if key in payload})
    elif name == "audit_uploaded_data":
        targets = {"results": [{k: r[k] for k in ("file_index", "source_file_id", "source_sha256",
            "checks_sha256", "audit_mode", "requested_sheet", "file", "ok", "sheets", "skipped",
            "coverage", "issues", "reason_code", "rule_execution", "annotation_errors") if k in r}
            for r in payload.get("results", payload.get("audit_results", [])) if isinstance(r, dict)]}
    elif name in {"run_research_figure_script"} or (
            name == "research_workspace" and outcome.reason_code == "figure_edit_saved"):
        fields = ("figure_id", "title", "exports", "editable", "qa_receipt", "qa_failures",
                  "nature_skill_receipt", "initial_nature_skill_receipt", "edit_provenance", "render_receipt", "status", "failures")
        targets = {k: payload[k] for k in fields if k in payload}
        targets["dataset_refs"] = payload.get("dataset_refs", [])
        if isinstance(payload.get("figures"), list):
            targets["figures"] = [{k: f[k] for k in fields if k in f}
                                  for f in payload["figures"] if isinstance(f, dict)]
    publication = publication_stages(artifact) if isinstance(artifact, dict) else []
    if publication:
        targets["publication"] = publication
    targets["_tool_outcome"] = outcome.as_dict()
    receipt = {"schema_version": 1, "tool_call_id": call_id, "tool_name": name,
               "status": outcome.status, "reason_code": outcome.reason_code,
               "coverage": targets, "result_refs": payload.get("result_refs", [])}
    if name == "run_research_figure_script" and payload.get("repair_identity") and payload.get("run_id"):
        receipt["repair_identity"] = payload["repair_identity"]
        receipt["run_id"] = payload["run_id"]
    return receipt
