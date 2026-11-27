"""Host tool contracts for method-only releases. Skill Markdown never grants access."""
from __future__ import annotations

import json
import os
import re
from typing import Annotated

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

METHOD_IDS = frozenset({"data-audit", "research-integrity", "literature-verification"})


def method_only_deployment():
    return os.environ.get("CITRUS_SCIENTIFIC_METHODS_ONLY") == "1"


def method_only_release(release, rows):
    return (method_only_deployment() and release is not None
            and (release / "scientific-methods.json").is_file()
            and not (release / "fusion").exists()
            and METHOD_IDS <= {row["id"] for row in rows})


def receipt(value):
    from src.research.delivery_reconciliation import delivery_outcome

    status = str(value.get("status") or "unknown")
    reason = str(value.get("reason_code") or value.get("reason") or "scientific_method_result")
    if status not in {"complete", "partial", "not_run", "failed", "unknown"}:
        status = "partial"
    return json.dumps(value, ensure_ascii=False, allow_nan=False), {
        **value, "_tool_outcome": delivery_outcome(status, reason, [])}


class ScientificArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


FieldText = Annotated[str, Field(max_length=500)]


class Reference(ScientificArgs):
    doi: FieldText = ""
    arxiv_id: FieldText = ""
    pmid: FieldText = ""
    title: FieldText = ""
    authors: FieldText | Annotated[list[FieldText], Field(max_length=30)] = ""
    year: int | None = None
    journal: FieldText = ""
    venue: FieldText = ""
    volume: FieldText = ""
    issue: FieldText = ""
    pages: FieldText = ""


class VerifyReferencesArgs(ScientificArgs):
    references: list[Reference] = Field(min_length=1, max_length=10)


@tool(args_schema=VerifyReferencesArgs, response_format="content_and_artifact")
def verify_literature_references(references):
    """一次核验 1–10 条用户给出的 DOI 或结构化题录。返回匹配字段、来源覆盖和错误状态。不要发送论文全文或生成报告，不要绕过本地/禁网约束。"""
    from src.core.capability_policy import allows
    from src.research.reference_verification_service import verify_references

    if not (method_only_deployment() and allows("bibliographic_lookup")
            and allows("skill:literature-verification")):
        return receipt({"status": "not_run", "reason_code": "bibliographic_lookup_not_authorized"})
    items = VerifyReferencesArgs(references=references).references
    try:
        return receipt(verify_references([item.model_dump(exclude_defaults=True) for item in items]))
    except ValueError:
        return receipt({"status": "not_run", "reason_code": "reference_input_invalid"})


class RequestedWorkbookScope(ScientificArgs):
    sheet: Annotated[str, Field(min_length=1, max_length=31)]
    bounds: Annotated[str, Field(max_length=32)] = ""
    header_rows: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=8)


class PrepareDatasetArgs(ScientificArgs):
    file_index: int = Field(default=0, ge=0, le=19)
    scope: RequestedWorkbookScope | None = None


def _dataset_context():
    from .code_tools import _identity
    from .fusion_tools import _delivery

    identity, _ = _identity()
    _, query, _, clarification = _delivery.get()
    return identity, query, clarification


@tool(args_schema=PrepareDatasetArgs, response_format="content_and_artifact")
def prepare_audit_dataset(file_index=0, scope=None):
    """读取当前轮已授权 XLSX/XLSM/CSV 附件，返回已冻结的数据引用。默认自动识别，只在用户明示工作表或精确区域时传 scope；没有歧义时不要求用户输入坐标。"""
    from src.core.capability_policy import allows
    from src.research.context import get_request_tables
    from src.research.dataset_audit import prepare_dataset
    from .dataset_tool_scope import authorized_scope

    if not (method_only_deployment() and allows("skill:data-audit")):
        return receipt({"status": "not_run", "reason_code": "dataset_preparation_not_authorized"})
    identity, query, clarification = _dataset_context()
    tables = get_request_tables()
    if file_index >= len(tables):
        return receipt({"status": "not_run", "reason_code": "dataset_attachment_required"})
    try:
        confirmed = authorized_scope(scope, query, clarification)
    except ValueError:
        return receipt({"status": "not_run", "reason_code": "dataset_explicit_scope_unconfirmed",
                        "next_action": "保留用户指定范围。明确工作表和范围后再准备数据，不得回退为全表。"})
    source = tables[file_index]
    reference = source.get("source_ref") or {}
    digest = reference.get("sha256") or source.get("sha256")
    file_id = source.get("file_id") or reference.get("file_id")
    return receipt(prepare_dataset(owner=identity.owner_user_id, session_id=identity.session_id,
        execution_id=identity.run_id, file_id=file_id, source_sha256=digest,
        source_name=source.get("name"), source_bytes=source.get("data"), scope=confirmed))


from src.research.dataset_audit import AuditChecks, DatasetRef


class AuditDatasetArgs(ScientificArgs):
    dataset_ref: DatasetRef
    checks: AuditChecks | None = None
    scope: RequestedWorkbookScope | None = None


@tool(args_schema=AuditDatasetArgs, response_format="content_and_artifact")
def audit_prepared_dataset(dataset_ref, checks=None, scope=None):
    """对 prepare_audit_dataset 返回的 DatasetRef 做确定性数据质量检查。若准备时明确限定 scope，沿用原 scope。只读，不修改原件；仅在用户或明确 schema 提供阈值时使用范围规则。"""
    from src.core.capability_policy import allows
    from src.research.dataset_audit import audit_dataset
    from .dataset_tool_scope import authorized_scope

    if not (method_only_deployment() and allows("skill:data-audit")):
        return receipt({"status": "not_run", "reason_code": "dataset_audit_not_authorized"})
    identity, query, clarification = _dataset_context()
    try:
        confirmed = authorized_scope(scope, query, clarification)
    except ValueError:
        return receipt({"status": "not_run", "reason_code": "dataset_explicit_scope_unconfirmed"})
    return receipt(audit_dataset(owner=identity.owner_user_id, session_id=identity.session_id,
        dataset_ref=dataset_ref, checks=checks, required_scope=confirmed,
        required_execution_id=identity.run_id if confirmed is not None else None))


class NumericInput(ScientificArgs):
    file_index: int = Field(ge=0, le=19)
    benford_columns: list[FieldText] = Field(default_factory=list, max_length=24)


class NumericScanArgs(ScientificArgs):
    inputs: list[NumericInput] = Field(min_length=1, max_length=4)
    enable_benford: bool = False


class NumericCollectArgs(ScientificArgs):
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")


_NUMERIC_REQUEST = re.compile(
    r"科研诚信|数值复用|重复(?:数值|数字|数据)?(?:区段|区块|块|段)|末位数字|本福特|格里姆|"
    r"\b(?:research integrity|repeated numeric|duplicate (?:numeric |data )?blocks?|benford|grimmer?)\b", re.I)


@tool(args_schema=NumericScanArgs, response_format="content_and_artifact")
def run_numeric_integrity(inputs, enable_benford=False):
    """仅在用户明确请求数值科研诚信检查时提交离线 Worker。用本轮附件的零基序号选择 CSV/XLSX 原件。禁止图像和超出用户范围，不把运行受理说成检查完成。等待原 execution_id 后调用 collect_numeric_integrity。"""
    from src.core.capability_policy import allows
    from src.graph.intent_text import mask_literal_payload
    from src.research.context import get_request_tables
    from .code_tools import _identity
    from .dataset_tool_scope import restricted_request
    from .numeric_integrity_worker import schedule_numeric_integrity

    identity, query, clarification = _dataset_context()
    text = mask_literal_payload(query)
    if not (method_only_deployment() and allows("skill:research-integrity") and _NUMERIC_REQUEST.search(text)):
        return receipt({"status": "not_run", "reason_code": "numeric_integrity_explicit_request_required"})
    if restricted_request(query) or restricted_request(clarification):
        return receipt({"status": "not_run", "reason_code": "numeric_bounded_regions_not_supported"})
    if enable_benford and not re.search(r"benford|本福特", text, re.I):
        return receipt({"status": "not_run", "reason_code": "numeric_benford_explicit_request_required"})
    tables, sources, seen = get_request_tables(), [], set()
    for term in NumericScanArgs(inputs=inputs, enable_benford=enable_benford).inputs:
        index = term.file_index
        if index >= len(tables) or index in seen:
            return receipt({"status": "not_run", "reason_code": "numeric_admitted_sources_required"})
        seen.add(index)
        ref = tables[index].get("source_ref") or {}
        sources.append({"file_id": ref.get("file_id"), "sha256": ref.get("sha256"),
                        "benford_columns": term.benford_columns})
    _, call_id = _identity()
    try:
        accepted = schedule_numeric_integrity(identity, operation_id=call_id, sources=sources,
                                              enable_benford=enable_benford)
    except (ValueError, PermissionError, LookupError) as exc:
        reason = str(exc)
        known_preflight = reason in {"numeric_file_reference_invalid", "numeric_file_not_admitted_to_run",
            "numeric_pinned_worker_unavailable", "numeric_pinned_worker_release_required",
            "numeric_explicit_admitted_source_refs_required", "numeric_bounded_regions_not_supported"}
        return receipt({"status": "not_run" if known_preflight else "unknown",
                        "reason_code": reason if known_preflight else "numeric_submission_requires_reconciliation"})
    except OSError:
        return receipt({"status": "unknown", "reason_code": "numeric_submission_requires_reconciliation"})
    data = {"status": "complete", "reason_code": "numeric_worker_accepted",
            "execution_id": accepted["execution_id"], "worker_status": accepted["status"],
            "completion_scope": "submission_only", "collect_required": True}
    content, artifact = receipt(data)
    return content, {**artifact, "_code_execution_id": data["execution_id"]}


@tool(args_schema=NumericCollectArgs, response_format="content_and_artifact")
def collect_numeric_integrity(execution_id):
    """只读读取本轮原数值筛查的已验证回执。用原 execution_id，不重新提交。零发现不是数据真实或不存在问题；图像从不在范围内。"""
    from src.core.capability_policy import allows
    from .numeric_integrity_worker import collect_numeric_integrity as collect

    if not (method_only_deployment() and allows("skill:research-integrity")):
        return receipt({"status": "not_run", "reason_code": "numeric_integrity_not_authorized"})
    identity, _, _ = _dataset_context()
    try:
        value = collect(identity, execution_id=execution_id)
    except (LookupError, ValueError, OSError, PermissionError):
        return receipt({"status": "not_run", "reason_code": "numeric_receipt_not_available"})
    report = value["report"]
    if report is None:
        status = "failed" if value["status"] in {"failed", "timed_out", "cancelled"} else "unknown"
        return receipt({"status": status, "reason_code": "numeric_worker_result_unconfirmed",
                        "execution_id": execution_id, "image_review": "not_in_scope"})
    # Keep full structured evidence in the receipt/readback. The model input
    # stays bounded to a preview and points to the exact stored operation.
    data = {"status": report["status"], "reason_code": "numeric_integrity_report",
            "execution_id": execution_id, "report": report, "worker_receipt": value["receipt"]}
    _, artifact = receipt(data)
    findings = report.get("findings") or []
    visible = {**data, "report": {**report, "findings": findings[:12],
                                  "findings_omitted_from_preview": max(0, len(findings) - 12)}}
    return json.dumps(visible, ensure_ascii=False, allow_nan=False), artifact


TOOL_SCOPES = {
    "data-audit": frozenset({prepare_audit_dataset.name, audit_prepared_dataset.name}),
    "research-integrity": frozenset({run_numeric_integrity.name, collect_numeric_integrity.name}),
    "literature-verification": frozenset({verify_literature_references.name}),
}


METHOD_CONTEXT_TOOLS = frozenset({prepare_audit_dataset.name, audit_prepared_dataset.name,
                                  "run_numeric_integrity", "collect_numeric_integrity"})
METHOD_WAIT_TOOLS = frozenset({"run_numeric_integrity"})
