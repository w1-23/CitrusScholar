"""Thin chat adapter for the same durable reference audit used by the PDF panel."""
from __future__ import annotations

import json
from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool


def _audit_status(payload):
    coverage = payload.get("coverage", {})
    action = payload.get("next_action")
    total, processed, pending = (coverage.get(key) for key in ("total", "processed", "pending"))
    scoped = (payload.get("schema") == "reference_audit/2"
              and all(type(count) is int and count >= 0 for count in (total, processed, pending))
              and total > 0 and total == processed + pending
              and coverage.get("processing_complete") is (pending == 0))
    ready = scoped and (payload.get("terminal") is True and pending == 0
                        or action == "budget_exhausted_export_partial")
    suggestions = ["report_saved_rows"] if ready else []
    if coverage.get("pending") and not ready:
        suggestions.append("check_pending_entries")
    return {
        "ready_for_report": ready,
        "processing_complete": scoped and pending == 0,
        "coverage_complete": scoped and coverage.get("complete") is True,
        "pending": coverage.get("pending", 0),
        "retryable": coverage.get("retryable", 0),
        "recommended_actions": suggestions,
        "evidence_scope": "bibliographic_identity_and_fields",
    }


def _identity_report_rows(rows):
    from citrus_refcheck import report_view

    entries = []
    for row in rows:
        if row.get("match_status") not in {"matching", "conflict", "unconfirmed", "unprocessed"}:
            continue
        entries.append({"claims": row.get("claims") or {}, "citation": row.get("citation", ""),
            "record_id": row.get("record_id"), "record_index": row.get("record_index"),
            "evidence": {"processing_status": row.get("processing_status"),
                         "match_status": row["match_status"], "reason": row.get("reason"),
                         "match": row.get("selected_record"), "differences": row.get("identity_differences") or []}})
    if not entries:
        return {}
    snapshot = {"schema_version": "reference-verification/1", "entries": entries}
    return {row["record_index"]: row for row in report_view(snapshot)["rows"]}


def _compact_audit(payload):
    """Project model observations only; durable audit/API/export rows stay intact."""
    from src.core.tool_result_view import REFERENCE_PAGE_BYTES, content_wire_bytes

    header = ("schema", "audit_id", "rule_version", "status", "terminal", "audit_status",
              "coverage", "counts", "verdict_counts", "budget", "offset", "run_reason",
              "result_refs", "result_ref", "export_requests", "export_status", "download_url", "_tool_outcome")
    rows = []
    response = {key: payload[key] for key in header if key in payload}
    response.update(rows=rows, result_view="compact", next_offset=payload.get("next_offset"))
    report_rows = _identity_report_rows(payload.get("rows", []))
    for row in payload.get("rows", []):
        if len(rows) == 10:
            response["next_offset"] = row["record_index"]
            break
        claims = row.get("claims") or {}
        verdict = row.get("verdict") or {}
        selected = row.get("selected_record") or {}
        processing = str(row.get("processing_status") or
                         ("processed" if row.get("status") not in (None, "", "not_checked") else "unprocessed"))
        identity_raw = str(row.get("identity") or verdict.get("identity") or
                           ("matched" if row.get("status") == "metadata_matched" else ""))
        if processing != "processed":
            identity = "unprocessed"
        elif identity_raw in {"identifier_match", "metadata_corroborated", "confirmed", "matched"}:
            identity = "matched"
        elif identity_raw in {"identifier_conflict_candidate", "conflict", "conflicting"}:
            identity = "conflict"
        else:
            identity = "unresolved"
        compact = {
            "record_id": row.get("record_id", ""),
            "record_index": row.get("record_index"),
            "title": str(claims.get("title") or row.get("citation") or "")[:180],
            "processing_status": processing,
            "identity": identity,
        }
        matched = {key: selected.get(key) or row.get(f"matched_record_{key}")
                   for key in ("doi", "title", "year", "source")}
        matched = {key: str(value)[:240] for key, value in matched.items() if value}
        if matched:
            compact["matched"] = matched
        for key in ("source_url", "provider"):
            value = row.get(key)
            if isinstance(value, str) and 0 < len(value) <= 240:
                compact[key] = value
        notes = []
        for item in (verdict.get("material_differences") or [])[:3]:
            if isinstance(item, dict):
                notes.append({"field": str(item.get("field") or "")[:80],
                              "submitted": str(item.get("submitted") or "")[:160],
                              "registered": str(item.get("registered") or "")[:160],
                              "reason": str(item.get("reason") or "")[:160]})
        for item in (verdict.get("benign_variants") or [])[:2]:
            if isinstance(item, dict):
                notes.append({"field": str(item.get("field") or "")[:80],
                              "variant": str(item.get("variant_type") or item.get("explanation") or "")[:160]})
        if kernel := report_rows.get(row.get("record_index")):
            compact.update(processing_status=kernel["processing_status"], identity=kernel["identity"])
            if kernel["citation"]:
                compact["citation"] = kernel["citation"]
            if kernel["field_notes"]:
                notes = [{key: str(note.get(key) or "")[:160]
                          for key in ("field", "submitted", "registered", "reason")}
                         for note in kernel["field_notes"][:5]]
        if notes:
            compact["field_notes"] = notes
        compact["retryable"] = bool(row.get("retryable"))
        reason = row.get("reason")
        if isinstance(reason, str) and 0 < len(reason) <= 160:
            compact["reason"] = reason
        if errors := row.get("errors"):
            compact["unavailable_sources"] = list(errors)[:3]
        rows.append(compact)
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if content_wire_bytes(encoded) > REFERENCE_PAGE_BYTES - 32:
            rows.pop()
            response["next_offset"] = row["record_index"]
            break
    return response



def identity_report_requested(query):
    import re
    from src.graph.agent_operations import _explicit_reference_check
    from src.graph.intent_text import intent_with_negation

    if not _explicit_reference_check(query):
        return False
    stripped = re.sub(r"https?://[^\s，。；！？]+", "", query)
    other_goals = re.compile(
        r"(?:精校|逐字段|(?:查看|查阅|读取|阅读|读|查|获取|核对|核验)?\s*全文|"
        r"论点|摘要|综述|绘图|画图|画.{0,8}(?:箱线图|热图|柱状图|散点图)|"
        r"(?:(?:比较|对比|解释|分析|评价|评述|归纳|提炼).{0,24}(?:方法|结论|局限|观点|创新))|"
        r"分析数据|审查数据|翻译|改写|导出|下载|投稿|"
        r"\b(?:(?:read\s+|fetch\s+)?full\s*text|plot|summari[sz]e|export|translate|"
        r"(?:then|also|and)\s+compare)\b)", re.I)
    return not intent_with_negation(stripped, other_goals)


def saved_identity_report(owner, session, audit_id):
    from html import escape
    from src.core.turn_source_ledger import safe_http_url
    from src.research.reference_audit import get_audit

    payload = get_audit(owner, session, audit_id, limit=5000)
    if not _audit_status(payload)["ready_for_report"] or payload.get("next_offset") is not None:
        return None
    rows = []
    coverage = payload["coverage"]
    labels = {"matched": "已匹配", "conflict": "身份冲突", "unresolved": "尚未确认", "unprocessed": "未处理"}

    def cell(value):
        return escape(" ".join(str(value or "").split())).replace("|", "&#124;").replace("[", "&#91;")

    for original in payload["rows"]:
        compact = _compact_audit({"rows": [original]})["rows"]
        if not compact:
            return None
        row = compact[0]
        record = row.get("matched") or {}
        title = cell(record.get("title") or record.get("doi") or "—")
        url = safe_http_url(row.get("source_url"))
        if not url and record.get("doi") and record["doi"].startswith("10."):
            url = safe_http_url("https://doi.org/" + record["doi"])
        source = f"[{cell(row.get('provider') or record.get('source') or '来源')}](<{url}>)" if url else "—"
        notes = [cell(f"{note['field']} {note.get('submitted', '')} → {note.get('registered', '')} {note.get('reason', '')}")
                 if "submitted" in note else cell(f"{note['field']} {note.get('variant', '')}")
                 for note in row.get("field_notes", [])]
        detail = "；".join(notes) or cell(row.get("reason") or "—")
        ordinal = int(row["record_index"]) + 1
        citation = cell(row.get("citation") or original.get("citation") or row["title"])
        handling = "已处理" if row["processing_status"] == "processed" else "未处理"
        rows.append(f"| {ordinal} | {citation} | {handling} | {labels[row['identity']]} | {title} · {source} | {detail} |")
    if not rows:
        return None
    summary = f"已处理 {coverage['processed']}/{coverage['total']} 条文献。"
    if not coverage.get("complete"):
        summary += "未处理条目、尚未确认的身份与来源缺口见下表。"
    return "\n".join([summary, "", "| 序号 | 提交引文 | 处理 | 身份 | 匹配对象与来源 | 备注 |",
                     "|---|---|---|---|---|---|", *rows])


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
    """按原序批量核对作品身份，返回紧凑行、来源和 audit_id。

    首次传 references_json、text、file_id 或附件 file_index。CSV/TSV 可用
    column_map 和 encoding。run 使用授权书目查询；read 从快照分页，不联网。
    ready_for_report 表示快照可报告。只有显式要求重核才用 resume=True；
    publish=True 从已存记录导出 XLSX。include_evidence 仅用于 read，
    run 会忽略该兼容参数并返回紧凑结果。
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
        if include_evidence and operation not in {"read", "run"}:
            raise ToolArgumentError("include_evidence 仅用于 operation=read；其他操作不接受该参数。")
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
            payload = ({**payload, "result_view": "full_evidence"}
                       if include_evidence and operation == "read"
                       else _compact_audit(payload))
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, LookupError, PermissionError, ToolArgumentError) as exc:
        return rejected(str(exc), "reference_audit_rejected")
