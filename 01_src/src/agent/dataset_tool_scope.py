"""Validate model-proposed workbook scope against trusted user input."""
from __future__ import annotations

import re

from src.graph.intent_text import mask_literal_payload

_RANGE = re.compile(r"(?<![\w$])\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}\s*:\s*\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}(?!\w)")
_NARROW = re.compile(r"(?:(?:仅|只)(?:检查|审查|核查|看|读取)?[^，。！？\n]{0,48}(?:表|区|行|列)|"
                     r"\b(?:sheet\s*\d+|worksheet\s+['\"`]?[\w-]+|(?:rows?|columns?)\s+\d+|"
                     r"only\b[^,;.!?\n]{0,48}(?:sheet|range|rows?|columns?))|"
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
