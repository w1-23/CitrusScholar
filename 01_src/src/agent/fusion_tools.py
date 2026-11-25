"""Small AgentCore adapters for pinned external Skill execution and delivery."""
from __future__ import annotations

from functools import partial
import json
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

SKILL_IDS = frozenset({"literature-verification", "data-audit", "research-integrity"})
FUSION_WAIT_TOOLS = frozenset({"run_external_skill", "continue_external_literature"})
REPLACED_TOOLS = frozenset({"verify_uploaded_references", "audit_uploaded_data", "audit_research_images",
                            "fetch_and_audit_open_supplements"})


def installed_fusion(release, rows):
    return bool(release and SKILL_IDS <= {row["id"] for row in rows}
                and (release / "fusion" / "bundle.json").is_file())


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


FileID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
SkillID = Literal["literature-verification", "data-audit", "research-integrity"]


class InputRef(_Args):
    file_id: FileID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunArgs(_Args):
    skill_id: SkillID
    inputs: dict[str, InputRef] = Field(default_factory=dict, max_length=24)
    options: dict = Field(default_factory=dict, max_length=20)


class LiteratureArgs(_Args):
    execution_id: FileID


class CollectArgs(LiteratureArgs):
    skill_id: SkillID
    publish_excel: bool = False


def _bound(skill_id):
    from src.core.capability_policy import allows
    from .code_tools import _context, _enabled, _identity
    from .installed_skills import list_installed

    identity, call_id = _identity()
    release = _context.get()[2]
    if not (_enabled() and release and allows("skill:" + skill_id)
            and installed_fusion(Path(release), list_installed(Path(release)))):
        raise PermissionError("external_skill_unavailable")
    return identity, call_id, Path(release)


def _response(data, status="complete", reason="external_skill_ready", *, receipts=(), execution_id=None):
    from src.research.delivery_reconciliation import delivery_outcome

    artifact = {**data, "_tool_outcome": delivery_outcome(status, reason, list(receipts))}
    if execution_id:
        artifact["_code_execution_id"] = execution_id
    return json.dumps(data, ensure_ascii=False, allow_nan=False), artifact


def _queued(receipt):
    execution_id = receipt["execution_id"]
    return _response({"execution_id": execution_id, "status": receipt["status"], "instruction":
                      "Worker 已受理。等待框架恢复后，再读取执行结果。"}, execution_id=execution_id)


def _consumed(value):
    worker_status = value["status"]
    status = "unknown" if worker_status == "unknown" else "not_run" if worker_status in {
        "queued", "claimed", "preparing", "running", "collecting", "cancel_requested"} else "failed"
    return _response({"execution_id": value["execution_id"], "worker_status": worker_status},
                     status, "external_skill_result_not_available")


def _bounded_items(key, items, fields):
    # No raw observations, attached paths, provider bodies, logs or options
    # enter the model prompt. Large results preserve counts and coverage.
    rows = []
    for item in items[:36]:
        if not isinstance(item, dict):
            continue
        row = {name: value[:500] if isinstance(value, str) else value
               for name in fields if (value := item.get(name)) is not None}
        if key == "records":
            submitted = item.get("submitted") or {}
            row["submitted"] = {name: str(submitted[name])[:350] for name in ("doi", "title", "year") if name in submitted}
            row["attempts"] = [{"provider": a.get("provider"), "status": a.get("status"), "queried_at": a.get("queried_at")}
                               for a in item.get("attempts", [])[:8] if isinstance(a, dict)]
        if key == "findings":
            coordinates = ("sheet", "sheet_name", "region", "region_id", "cell", "cell_range", "range",
                           "scope", "group", "row", "row_index", "column", "column_index", "role")
            row["locations"] = [{field: loc[field] for field in coordinates if field in loc}
                                for loc in item.get("locations", [])[:8] if isinstance(loc, dict)]
        rows.append(row)
    return rows


def _summary(report):
    fields = {
        "findings": ("kind", "rule", "check", "severity", "message", "detail", "title"),
        "coverage": ("check", "status", "scope", "record_index", "region_id", "reason"),
        "evidence": ("evidence_id", "doi", "title", "url"),
        "records": ("record_index", "status", "evidence_ids", "evidence_grade", "problems"),
    }
    summary = {"status": report["status"], "limitations": [str(x)[:200] for x in report.get("limitations", [])[:10]]}
    for key, selection in fields.items():
        data = report.get(key)
        if isinstance(data, list):
            summary[key] = _bounded_items(key, data, selection)
            summary[key + "_total"] = len(data)
    execution = report.get("execution") or {}
    summary["execution"] = {name: execution[name] for name in ("skill", "version", "python") if name in execution}
    return summary


def _workbook_source(identity, reference):
    from src.session import files
    from hashlib import sha256

    metadata = files.get_task_input_file(identity.owner_user_id, identity.run_id,
                                         reference["file_id"], reference["sha256"])
    if not metadata or metadata.get("sha256") != reference["sha256"]:
        raise PermissionError("external_workbook_scope_changed")
    from .workspace_projection import bounded_bytes

    data = bounded_bytes(Path(metadata["path"]), 20 * 1024 * 1024)
    if len(data) != metadata["size_bytes"] or sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("external_workbook_source_changed")
    return data, metadata["filename"]


@tool(args_schema=RunArgs, response_format="content_and_artifact")
def run_external_skill(skill_id: SkillID, inputs=None, options=None):
    """Run the pinned external literature, XLSX data-audit, or research-integrity Skill. Use exact authorized file_id and sha256 per input role. Data audit needs workbook and scoped regions. Literature accepts bibliography and/or typed references. No raw observations or URLs are accepted."""
    from .fusion_worker import schedule_fusion

    try:
        identity, call_id, release = _bound(skill_id)
        refs = {role: ref.model_dump() if isinstance(ref, InputRef) else InputRef.model_validate(ref).model_dump()
                for role, ref in (inputs or {}).items()}
        receipt = schedule_fusion(identity, operation_id=call_id, skill_release=release,
                                  skill_id=skill_id, inputs=refs, options=options or {})
        return _queued(receipt)
    except (LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "输入、权限或固定的 Skill 发行不可用。"}, "not_run", "external_skill_not_admitted")


@tool(args_schema=LiteratureArgs, response_format="content_and_artifact")
def continue_external_literature(execution_id: str):
    """After the external literature Worker extracted or parsed the bibliography, obtain authorized host registry receipts and run the same pinned verifier. Give only its earlier execution_id. Do not call again for a completed verification; the host scopes and deduplicates lookup."""
    from src.research.fusion_observations import collect_observations
    from .fusion_worker import consume_fusion, find_fusion, schedule_fusion

    skill_id = "literature-verification"
    try:
        identity, _, release = _bound(skill_id)
        value = consume_fusion(identity, execution_id=execution_id, skill_release=release, skill_id=skill_id)
        if value["status"] != "succeeded":
            return _consumed(value)
        report = value["report"]
        if any(row.get("role") == "observations" for row in report.get("execution", {}).get("inputs", [])):
            return _response(_summary(report))
        operation_id = "registry:" + execution_id
        if existing := find_fusion(identity, operation_id=operation_id, skill_release=release, skill_id=skill_id):
            return _queued(existing)
        observations = collect_observations(report, identity=identity)
        receipt = schedule_fusion(identity, operation_id=operation_id, skill_release=release,
                                  skill_id=skill_id, inputs=value["inputs"], options=value["options"],
                                  observations=observations)
        return _queued(receipt)
    except (LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "登记补查不可用。可读取已有结果并标注覆盖缺口。"}, "not_run", "external_registry_unavailable")


@tool(args_schema=CollectArgs, response_format="content_and_artifact")
def collect_external_skill(execution_id: str, skill_id: SkillID, publish_excel: bool = False):
    """Read a settled external Skill result by exact execution_id. For data-audit, set publish_excel=true only when the user requests a workbook or audit report; CitrusScholar copies the source XLSX and marks exact findings in red with cell comments, plus the existing summary sheet."""
    from .fusion_worker import consume_fusion

    try:
        identity, _, release = _bound(skill_id)
        value = consume_fusion(identity, execution_id=execution_id, skill_release=release, skill_id=skill_id)
        if value["status"] != "succeeded":
            return _consumed(value)
        report = value["report"]
        summary = {"execution_id": execution_id, **_summary(report)}
        status = {"complete": "complete", "partial": "partial", "unsupported": "not_run", "failed": "failed"}[report["status"]]
        if not publish_excel or skill_id != "data-audit" or status not in {"complete", "partial"}:
            return _response(summary, status, "external_skill_report")
        try:
            from src.research.fusion_workbook import publish_fusion_workbook
            from src.core.account_leases import account_lease

            ref = value["inputs"]["workbook"]
            with account_lease(identity.owner_user_id):
                raw, name = _workbook_source(identity, ref)
                artifact = publish_fusion_workbook(report, source_file_id=ref["file_id"],
                    source_sha256=ref["sha256"], source_name=name, source_bytes=raw)
            summary["workbook"] = {"filename": artifact["filename"], "url": artifact["url"], "cells": artifact["cells"]}
            return _response(summary, status, "external_skill_report", receipts=[artifact])
        except (KeyError, LookupError, ValueError, PermissionError, OSError):
            summary["workbook_error"] = "没有生成正式标注工作簿，审查结论仍可用。"
            return _response(summary, "partial", "external_workbook_not_published")
    except (LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "执行结果不可用。"}, "not_run", "external_skill_report_unavailable")


_TOOLS = (run_external_skill, continue_external_literature, collect_external_skill)
FUSION_TOOL_NAMES = frozenset(item.name for item in _TOOLS)


def _load(item):
    from src.tools.registry import register_tool

    register_tool(item, category="research", is_readonly=False, concurrency_key="external_skill",
                  write_scope="session_artifact", recovery_mode="never")
    return item


def register_fusion_tools(catalog):
    from .capabilities import CapabilityDefinition

    for item in _TOOLS:
        catalog.register(CapabilityDefinition(item.name, item.description, item.args_schema.model_json_schema(),
            partial(_load, item), provider="external_skill", mutates=True, execution_mode="sequential"))
