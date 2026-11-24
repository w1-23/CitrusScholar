"""Authorize and persist the standalone spreadsheet audit for the chat tool."""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re

from src.core.tool_errors import ToolArgumentError

# `duplicates` is a package group alias. An explicit requested_checks list is
# a demand for concrete checks, so resolve it to the canonical row check rather
# than activating key/PaperConan prerequisites that may not exist.
_CHECK_ALIASES = {"duplicates": "duplicate_row", "duplicate_rows": "duplicate_row"}


def _selected_checks(checks, query):
    selected = checks.get("requested_checks")
    if selected is not None:
        return list(dict.fromkeys(
            _CHECK_ALIASES.get(str(item).strip().lower(), str(item).strip())
            for item in selected))
    if re.search(r"(?:仅|只)(?:检查|审查|查|看)?(?:数据)?(?:完全)?重复", query):
        return ["duplicate_row"]
    return None


def text_only(query: str) -> bool:
    return bool(re.search(r"(?:只|仅)(?:要|给|用|输出|提供)?(?:文字|文本)|"
                          r"(?:不要|无需|不用)(?:生成|交付|导出)?(?:文件|工作簿)|\btext only\b", query, re.I))


def _sheet_scope(raw, name, proposed, query):
    if not proposed:
        match = re.search(r"\bsheet\s*\d+\b|第\s*\d+\s*张表", query, re.I)
        proposed = match[0] if match else ""
    if not proposed or Path(name).suffix.lower() not in {".xlsx", ".xlsm"}:
        return proposed
    from citrus_audit.readers import open_xlsx_readonly

    with closing(open_xlsx_readonly(raw, name, data_only=True)) as workbook:
        if proposed in workbook.sheetnames:
            return proposed
        number = re.fullmatch(r"(?:sheet\s*(\d+)|第\s*(\d+)\s*张表)", proposed, re.I)
        if number and (index := int(number[1] or number[2])) in range(1, len(workbook.sheetnames) + 1):
            return workbook.sheetnames[index - 1]
    raise ToolArgumentError("工作表不存在，请使用结构读取返回的实际工作表名。")


def run_review(tables, indexes, validated, *, sheet_name, query):
    from citrus_audit import discover_regions, review_sheet
    from src.core.cancel import raise_if_cancelled
    from src.core.tracing import get_session_id, get_user_id
    from src.research.delivery_reconciliation import delivery_outcome
    from src.research.delivery_tools import publish_audit_workbooks
    from src.research.integrity_review import record_report

    checks = validated["checks"]
    roles = dict(checks.get("field_roles") or {})
    for old, new in (("groups", "group_columns"), ("values", "value_columns"), ("key_columns", "sample_keys"),
                     ("required_fields", "required_fields"), ("optional_fields", "optional_fields"), ("ranges", "ranges")):
        if old in checks:
            roles.setdefault(new, checks[old])
    selected = _selected_checks(checks, query)
    profile = checks.get("profile") or "balanced"
    if profile == "on_demand":
        if selected is None:
            raise ToolArgumentError("按需检查需提供 requested_checks，或选择 balanced 默认审查。")
        profile = "custom"
    if any(index >= len(tables) for index in indexes):
        raise ToolArgumentError("请选择已授权的真实附件序号。")
    stats = {index: {} for index in indexes}
    for key in ("reported_summaries", "reported_p_values"):
        for binding in checks.get(key, ()):
            if not isinstance(binding, dict):
                raise ToolArgumentError(f"{key} 的每条映射必须是对象。")
            file_index = binding.get("file_index", indexes[0] if len(indexes) == 1 else None)
            if type(file_index) is not int or file_index not in stats:
                raise ToolArgumentError(f"{key} 需通过 file_index 绑定本次审查的原件。")
            stats[file_index].setdefault(key, []).append({k: v for k, v in binding.items() if k != "file_index"})
    snapshots, targets, inputs, errors, plans, layouts = [], [], {}, [], [], []
    for index in indexes:
        raise_if_cancelled("data_audit_review")
        source = tables[index]
        raw, name = source["data"], source["name"]
        file_id = source.get("file_id") or ""
        digest = sha256(raw).hexdigest()
        regions = [{key: value for key, value in row.items() if key != "file_index"}
                   for row in validated["regions"] or () if row["file_index"] == index]
        if validated["regions"] and not regions:
            errors.append(f"{name} 未指定审查区域")
            continue
        actual_sheet = _sheet_scope(raw, name, sheet_name, query)
        if not regions and Path(name).suffix.lower() in {".xlsx", ".xlsm"}:
            sheets = [actual_sheet] if actual_sheet else []
            if not sheets:
                from citrus_audit.readers import open_xlsx_readonly
                with closing(open_xlsx_readonly(raw, name, data_only=True)) as workbook:
                    sheets = list(workbook.sheetnames)
            for sheet in sheets:
                layout = discover_regions(raw, name, sheet_name=sheet, header_rows=validated["header_rows"])
                if layout["region_count"] > 1 or layout["regions_truncated"]:
                    layouts.append({"file_index": index, **layout})
        options = {"sheet_scope": actual_sheet, "regions": regions or None, "field_roles": roles,
                   "requested_checks": selected, "profile": profile,
                   "header_rows": None if regions else validated["header_rows"], **stats[index]}
        identity = {"file_index": index, "source_file_id": file_id, "source_sha256": digest, "source_name": name}
        plans.append((identity, options))
        inputs[index] = raw

    if layouts:
        message = "未执行审查：发现多区块或区域列表不完整。请先确认 layout，再传入 checks_json.regions。"
        artifact = {"layouts": layouts, "_tool_outcome": delivery_outcome("not_run", "audit_regions_required", [])}
        return message + "\n" + json.dumps({"layouts": layouts}, ensure_ascii=False), artifact

    from src.core.tracing import get_client_request_id, get_job_id
    from src.research.integrity_review import get_review

    owner, session = int(get_user_id() or 0), str(get_session_id() or "")
    request = get_client_request_id() or get_job_id()
    review_id = sha256(json.dumps(["standalone-audit-review/1", owner, session, request, plans],
                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32] if request else None
    previous = get_review(owner, review_id) if review_id else None
    if previous and previous["session_id"] != session:
        raise ToolArgumentError("保存的审查快照不属于本会话。")

    for identity, options in plans if not previous else ():
        raise_if_cancelled("data_audit_review")
        name = identity["source_name"]
        try:
            snapshot = review_sheet(inputs[identity["file_index"]], name, **options)
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError(str(exc)) from exc
        if snapshot.report["status"] == "error":
            errors.append(f"{name} 没有可审查的已解析区域")
            continue
        snapshots.append({**identity, "report": snapshot.report, "report_sha256": snapshot.report_sha256})
        targets.append({**identity, "ok": True, "file": name})
    if not snapshots and not previous:
        text = "未完成审查。" + "；".join(errors)
        return text, {"_tool_outcome": delivery_outcome("failed", "audit_source_unavailable", [])}

    if previous:
        report = previous["report"]
        result = {"review_id": review_id, "review_url": "/integrity-review?review=" + review_id}
        snapshots = report["snapshots"]
    else:
        report = {"schema": "standalone-audit-review/1", "table_results": targets, "snapshots": snapshots,
                  "input_errors": errors.copy()}
        result = record_report(owner, session, report, review_id=review_id)
    errors = list(report.get("input_errors", ()))
    findings = []
    coverage = []
    for snapshot in snapshots:
        saved = snapshot["report"]
        for table in saved["tables"]:
            location = table["table"]
            outcome = table["result"]
            data = outcome["data"]
            findings.extend(item for item in data["findings"] if not item.get("coverage_only"))
            coverage.append({"file_index": snapshot["file_index"], "sheet": location["sheet"],
                "bounds": location.get("region_bounds"), "status": outcome["status"]})
    coverage_notes = list(errors)
    for item in snapshots:
        coverage_notes.extend((item["report"].get("review") or {}).get("coverage_gaps") or [])
    receipts = []
    publication_failed = False
    if not text_only(query):
        published, failures = publish_audit_workbooks(report, load_source=lambda target: inputs[target["file_index"]],
                                                       review_id=result["review_id"])
        receipts = list(published.values())
        publication_failed = bool(failures) and not receipts
        errors.extend(failures)
        coverage_notes.extend(failures)
    status = "partial" if errors or any(item["report"]["status"] == "partial" for item in snapshots) else "complete"
    reason = "audit_review_coverage_partial" if status == "partial" else "audit_review_completed"
    if publication_failed:
        status, reason = "failed", "audit_workbook_not_published"
    artifact = {"integrity_review": result, "coverage": coverage, "finding_count": len(findings),
                "findings": findings[:6], "coverage_gaps": coverage_notes[:10], "publication_receipts": receipts}
    artifact["_tool_outcome"] = delivery_outcome(status, reason, receipts)
    return json.dumps(artifact, ensure_ascii=False, allow_nan=False, separators=(",", ":")), artifact
