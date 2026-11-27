"""Validate model-proposed workbook scope against trusted user input."""
from __future__ import annotations

import re

from src.graph.intent_text import mask_literal_payload

_RANGE = re.compile(r"(?<![\w$])\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}\s*:\s*\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}(?!\w)")
_NARROW = re.compile(r"(?:(?:仅|只)(?:检查|审查|核查|看|读取)?[^，。！？\n]{0,48}(?:表|区|行|列)|"
                     r"\b(?:sheet\s*\d+|worksheet\s+['\"`]?[\w-]+|(?:rows?|columns?)\s+\d+|"
                     r"only\s+(?:(?:audit|read|scan|inspect|check|use)\s+)?(?:the\s+)?(?:selected\s+|specified\s+)?(?:sheet\b|range\b|(?:rows?|columns?)\s+\d+))|"
                     r"第\s*\d+\s*(?:张表|行|列)|工作表\s*[:：]|(?:工作表|sheet)\s+[A-Za-z0-9_]|(?:工作表|sheet)\s*[`'\"])", re.I)
_NEGATIVE = re.compile(r"排除|除外|不(?:要|检查|审查|核查|读取|使用)|示例|例如|候选|还是|"
                       r"\b(?:not|exclude|except|example|candidate|or)\b", re.I)


def restricted_request(text):
    clean = mask_literal_payload(text)
    return bool(_RANGE.search(clean) or _NARROW.search(clean))


def authorized_scope(proposed, request, clarification=""):
    if proposed is None:
        if restricted_request(request) or restricted_request(clarification):
            raise ValueError("dataset_explicit_scope_required")
        return None
    from src.research.dataset_audit import WorkbookScope

    value = proposed if isinstance(proposed, dict) else proposed.model_dump()
    bounds = value.get("bounds") or ""
    sheet = value["sheet"]
    query = mask_literal_payload(clarification or request)
    if bounds:
        from .fusion_worker_contract import confirmed_workbook_regions

        ranges = _RANGE.findall(query)
        if len(ranges) != 1 or ranges[0].replace("$", "").replace(" ", "").upper() != bounds.replace("$", "").upper():
            raise ValueError("dataset_explicit_scope_unconfirmed")
        headers = value.get("header_rows") or []
        if len(headers) > 1:
            raise ValueError("dataset_multirow_header_confirmation_required")
        item = {"id": "audit-user-range", "sheet": sheet, "range": bounds}
        if headers:
            item["header_row"] = headers[0]
        confirmed = confirmed_workbook_regions([item], query)[0]
        return WorkbookScope(sheet=confirmed["sheet"], bounds=confirmed["bounds"],
                             header_rows=tuple(confirmed["header_rows"]))
    if _RANGE.search(query) or _NEGATIVE.search(query):
        raise ValueError("dataset_explicit_scope_unconfirmed")
    exact = r"(?<![\w-])[`'\"]?" + re.escape(sheet) + r"[`'\"]?(?![\w-])"
    if not re.search(r"(?:工作表|worksheet|sheet)\s*[:：]?\s*" + exact + r"|" + exact + r"\s*工作表", query, re.I):
        raise ValueError("dataset_explicit_scope_unconfirmed")
    if value.get("header_rows"):
        raise ValueError("dataset_sheet_scope_cannot_override_headers")
    return WorkbookScope(sheet=sheet, bounds=None, header_rows=())


_PREVIEW = re.compile(r"预览|原始(?:值|数据|内容)|表头|列名|前.{0,8}行|其他文件|另一个文件|"
                      r"\b(?:preview|headers?|raw (?:data|values|rows)|sample rows|first [\w-]+ rows|"
                      r"columns? names?|(?:other|another|previous) (?:file|attachment))\b", re.I)
_AUDIT_CHECK_INTENT = {
    "missing": re.compile(r"缺失|空值|\b(?:missing|null|empty)\b", re.I),
    "duplicate": re.compile(r"重复|\bduplicat(?:e|es|ed|ion)\b", re.I),
}


def csv_audit_supplies_locations(query, tables, observations):
    """Recognize a complete, present-turn audit of its sole CSV attachment."""
    text = mask_literal_payload(query)
    requested = {check for check, pattern in _AUDIT_CHECK_INTENT.items() if pattern.search(text)}
    if len(tables) != 1 or not requested or _PREVIEW.search(text) or restricted_request(text):
        return False
    source = tables[0]
    pinned = source.get("source_ref") or {}
    source_ref = {"file_id": source.get("file_id") or pinned.get("file_id"),
                  "sha256": pinned.get("sha256") or source.get("sha256")}
    if not all(source_ref.values()):
        return False
    prepared, covered = [], False
    for message in observations:
        artifact = message.artifact or {}
        if message.name == "audit_prepared_dataset":
            checks = {item.get("check") for item in artifact.get("checks") or ()
                      if isinstance(item, dict) and item.get("status") == "checked"}
            ref = artifact.get("dataset_ref")
            covered = bool(artifact.get("status") == "complete"
                           and artifact.get("reason_code") == "dataset_audit_completed"
                           and ref and ref in prepared and requested <= checks)
        if artifact.get("status") != "complete":
            continue
        if (message.name == "prepare_audit_dataset" and artifact.get("reason_code") == "dataset_prepared"
                and artifact.get("format") == "csv" and artifact.get("source_ref") == source_ref
                and (artifact.get("scope") or {}).get("selection") == "single_csv_table"):
            prepared.append(artifact.get("dataset_ref"))
    return covered
