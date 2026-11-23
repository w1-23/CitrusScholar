"""Thin chat adapter for the same durable reference audit used by the PDF panel."""
from __future__ import annotations

import json
from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool


def _audit_status(payload):
    coverage = payload.get("coverage", {})
    action = payload.get("next_action")
    ready = bool(payload.get("terminal") or action in {
        "export", "export_and_review", "budget_exhausted_export_partial"})
    suggestions = ["report_saved_rows"] if ready else []
    if coverage.get("pending") and not ready:
        suggestions.append("check_pending_entries")
    return {
        "ready_for_report": ready,
        "pending": coverage.get("pending", 0),
        "retryable": coverage.get("retryable", 0),
        "recommended_actions": suggestions,
        "evidence_scope": "bibliographic_identity_and_fields",
    }


def _compact_audit(payload):
    """Project model observations only; durable audit/API/export rows stay intact."""
    from src.core.tool_result_view import REFERENCE_PAGE_BYTES, content_wire_bytes

    header = ("schema", "audit_id", "rule_version", "status", "terminal", "audit_status",
              "coverage", "counts", "verdict_counts", "budget", "offset", "run_reason",
              "result_refs", "result_ref", "export_requests", "export_status", "download_url", "_tool_outcome")
    rows = []
    response = {key: payload[key] for key in header if key in payload}
    response.update(rows=rows, result_view="compact", next_offset=payload.get("next_offset"))
    keys = ("record_id", "record_index", "status", "existence", "identity", "retryable")
    for row in payload.get("rows", []):
        if len(rows) == 10:
            response["next_offset"] = row["record_index"]
            break
        compact = {key: row[key] for key in keys if key in row}
        claims = row.get("claims") or {}
        verdict = row.get("verdict") or {}
        compact.update(title=str(claims.get("title") or row.get("citation") or "")[:180],
                       decision=verdict.get("decision", "not_checked"))
        selected = row.get("selected_record") or {}
        submitted_doi = row.get("doi") or claims.get("doi")
        for key, value in (("doi", submitted_doi),
                           ("matched_record_doi", selected.get("doi") if selected.get("doi") != submitted_doi else None),
                           ("source_url", row.get("source_url")),
                           ("provider", row.get("provider"))):
            if isinstance(value, str) and 0 < len(value) <= 240:
                compact[key] = value
        if fields := verdict.get("material_differences"):
            compact["differing_fields"] = [item["field"] for item in fields[:5]]
        if row.get("status") not in {"metadata_matched", "not_checked"} and row.get("metadata_title"):
            compact["registered_title"] = row["metadata_title"][:180]
        if errors := row.get("errors"):
            compact["unavailable_sources"] = list(errors)[:3]
        rows.append(compact)
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if content_wire_bytes(encoded) > REFERENCE_PAGE_BYTES - 32:
            rows.pop()
            response["next_offset"] = row["record_index"]
            break
    return response


@tool
def verify_uploaded_references(file_index: int = 0, references_json: str | list | dict = "",
                               allow_web: Annotated[bool, InjectedToolArg] = True,
                               batch_size: Annotated[int, InjectedToolArg] = 10,
                               start_index: Annotated[int, InjectedToolArg] = 0, max_rows: int = 0,
                               publish: bool = False, audit_id: str = "", text: str = "",
                               file_id: str = "", operation: str = "run",
                               resume: bool = False, offset: int = 0,
                               include_evidence: bool = False,
                               column_map: dict[str, str] | None = None, encoding: str = "") -> str:
    """批量核实引文的存在性与书目身份，默认只读登记元数据和搜索片段。

    首次传 references_json、text、file_id 或本轮附件 file_index。提供 CSV/TSV
    列映射时用 column_map，非 UTF-8 / 带 BOM 的 UTF-16 需声明 encoding。
    有标识先查登记库，无标识按原题名用 DeepSeek Search 定位，身份明确即停。
    未确认和来源错误均可写入报告。返回原序紧凑结果与 audit_id；read 复用结果，
    include_evidence 读取单条完整证据。create/run/read/list/cancel 共用同一库存。
    run 默认最多核查 20 条，服从任务截止、历史请求预算与联网授权。禁止读取全文或 PDF 仍使用 run，run 只查书目信息。离线复用使用 read。
    ready_for_report=true 时直接呈现结果与缺口。只有用户另行要求重查时才传 resume=True。
    publish=True 导出已存 XLSX，其它格式用 deliver_research_artifact。
    """
    from src.core.tracing import get_user_id, get_session_id, get_client_request_id
    from src.research import reference_audit as service
    from src.core.tool_errors import ToolArgumentError

    def rejected(message, reason):
        return json.dumps({"summary": message, "rows": [],
            "_tool_outcome": {"status": "not_run", "reason_code": reason, "artifacts": []}}, ensure_ascii=False)

    owner, session = get_user_id(), get_session_id()
    if type(owner) is not int or owner <= 0 or not session:
        return rejected("需要已认证的账号和会话。", "reference_identity_required")
    if operation not in {"create", "run", "read", "list", "cancel"}:
        return rejected("operation 仅支持 create/run/read/list/cancel。", "invalid_reference_operation")
    if start_index:
        return rejected("不再使用 start_index 跳过库存；请传 audit_id 继续未核验条目。", "audit_id_required")
    if type(max_rows) is not int or not 0 <= max_rows <= 100:
        return rejected("max_rows 需要 0–100 的整数（0 使用默认20）。", "invalid_reference_limit")
    try:
        if include_evidence and operation != "read":
            raise ToolArgumentError("include_evidence 仅用于 operation=read；运行返回紧凑结果。")
        if operation == "list":
            payload = {"audits": service.list_audits(owner, session),
                "_tool_outcome": {"status": "complete", "reason_code": "reference_audits_listed", "artifacts": []}}
        else:
            supplied = references_json
            if isinstance(supplied, str):
                supplied = json.loads(supplied) if supplied.strip() else None
            if supplied is not None and not isinstance(supplied, list):
                raise ToolArgumentError("references_json 必须是条目数组。")
            if audit_id and (supplied or text or file_id or column_map is not None or encoding):
                raise ToolArgumentError("已有 audit_id 不能同时创建新条目或文件来源。")
            if not audit_id:
                if operation in {"read", "cancel"}:
                    raise ToolArgumentError("读取或取消需要明确 audit_id。")
                if sum(bool(value) for value in (supplied, text, file_id)) > 1:
                    raise ToolArgumentError("请选择 references_json、text 或 file_id 中的一项。")
                content, name, source_ref = None, "references.txt", None
                if file_id:
                    from src.session import files
                    entry = files.get_account_file(file_id, owner)
                    if not entry:
                        raise ToolArgumentError("账号文件不存在或无权读取。")
                    if int(entry.get("size_bytes") or 0) > 20 * 1024 * 1024:
                        raise ToolArgumentError("文献文件超过20MB限制。")
                    from pathlib import Path
                    with Path(entry["path"]).open("rb") as source:
                        content = source.read(20 * 1024 * 1024 + 1)
                    if len(content) > 20 * 1024 * 1024:
                        raise ToolArgumentError("文献文件超过20MB限制。")
                    import hashlib
                    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                        raise ToolArgumentError("文件摘要不匹配，未创建审查。")
                    name, source_ref = entry["filename"], {"file_id": file_id, "sha256": entry["sha256"]}
                elif not supplied and not text:
                    from src.research.context import get_request_tables
                    attachments = get_request_tables()
                    if type(file_index) is not int or not 0 <= file_index < len(attachments):
                        raise ToolArgumentError("请提供参考文献文件、原文或条目数组。")
                    item = attachments[file_index]
                    content, name = item.get("data"), item.get("name", name)
                    source_ref = item.get("source_ref")
                    if not content:
                        raise ToolArgumentError("附件没有可读取字节；请使用实际 file_id。")
                created = service.create_audit(owner, session, name=name, content=content,
                    text=text, references=supplied, source_ref=source_ref, column_map=column_map, encoding=encoding)
                audit_id = created["audit_id"]
                payload = created
            if operation == "cancel":
                payload = service.cancel_audit(owner, session, audit_id)
            elif operation == "read":
                payload = service.get_audit(owner, session, audit_id, offset=offset, limit=1 if include_evidence else (max_rows or 10))
            elif operation == "run":
                from src.core.progress_bus import emit_progress
                if allow_web:
                    payload = service.run_audit(owner, session, audit_id, max_rows=max_rows or 20,
                        resume=resume, progress=lambda event: emit_progress("reference_audit_progress", event))
                else:
                    payload = service.get_audit(owner, session, audit_id)
                    payload["run_reason"] = "network_disabled"
            elif operation == "create" and "payload" not in locals():
                payload = service.get_audit(owner, session, audit_id)
            payload["result_ref"] = {"kind": "reference_audit", "audit_id": audit_id}
            payload["read_more"] = ({"operation": "read", "audit_id": audit_id,
                                     "offset": payload["next_offset"], "max_rows": 10}
                                    if payload.get("next_offset") is not None else None)
            payload["export_requests"] = [
                {"kind": "bibliography", "audit_id": audit_id, "output_format": fmt}
                for fmt in ("json", "xlsx")]
            payload["export_status"] = "not_requested"
            if publish:
                try:
                    from src.research.artifacts import publish_artifact
                    content, mime, filename = service.export_audit(owner, session, audit_id, format="xlsx")
                    receipt = publish_artifact(owner=owner, session_id=session, filename=filename,
                        content=content, mime=mime, producer_tool="export_literature_evidence",
                        request_id=str(get_client_request_id() or ""))
                    payload.update(download_url=receipt["artifact_url"], export_status="complete")
                    payload["_tool_outcome"]["artifacts"] = [receipt["artifact_url"]]
                except (OSError, ValueError) as exc:
                    payload.update(export_status="failed", export_error=str(exc))
                    payload["_tool_outcome"].update(status="partial", reason_code="publication_failed")
        if operation != "list":
            payload["audit_status"] = _audit_status(payload)
            coverage = payload.get("coverage", {})
            outcome = payload["_tool_outcome"]
            counts = payload.get("counts", {})
            has_results = any(counts.get(status) for status in (
                "metadata_matched", "metadata_mismatch", "metadata_uncertain", "title_candidate",
                "not_found_in_checked_registries"))
            if (operation == "run" and allow_web and payload.get("retry_optional")
                    and payload.get("run_reason") in {"batch_finished", "all_entries_attempted_use_explicit_resume"}
                    and coverage.get("extraction_complete") is True and has_results
                    and outcome.get("reason_code") == "reference_coverage_incomplete"):
                outcome.update(status="complete", reason_code="reference_identity_report_ready_with_source_gaps",
                               coverage="全部条目已尝试；来源缺口与未确认结论保留在报告中")
            payload.pop("next_action", None)
            payload = ({**payload, "result_view": "full_evidence"} if include_evidence
                       else _compact_audit(payload))
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, LookupError, PermissionError, ToolArgumentError) as exc:
        return rejected(str(exc), "reference_audit_rejected")
