"""Bound the first model delivery without modifying stored tool evidence."""
from __future__ import annotations

import hashlib
import json
import re

INLINE_CHARS = 16000
FIRST_DELIVERY_BYTES = 3072
REFERENCE_PAGE_BYTES = 8192
SEARCH_DELIVERY_BYTES = 12288
# Readers have a separate per-page ceiling: an already-encoded 16 KB JSON
# page can need up to twice that space once escaped as a message string.
READER_DELIVERY_BYTES = 2 * INLINE_CHARS + 2
PREVIEW_CHARS = 768
TAIL_PREVIEW_CHARS = 256
_READERS = frozenset({"read_tool_result", "read_candidate", "read_skill", "load_skill", "read_context_evidence"})
_WORKSPACE_READS = frozenset({"read", "read_run", "read_skill", "read_skill_resource", "context_source"})
_CONTROL_PRIORITY = (
    "status", "next_action", "terminal", "review_required", "can_resume", "export_ready",
    "audit_id", "review_id", "_tool_outcome", "dataset_ref", "analysis_result_ref",
    "findings_page", "findings_total", "registry_coverage", "execution_id", "saved_report", "report_file_error",
    "audit_status", "continuation", "counts", "coverage", "export_requests", "review", "files", "exports",
    "execution_stage", "deliverable_stage", "figure_placeholders", "figure_placeholder", "figure_id",
    "project_registered", "inline_registered", "failures",
)


_SCRIPT_PRIORITY = (
    "_tool_outcome", "deliverable_stage", "execution_stage", "figure_delivery_notice",
    "render_data_summary", "exports", "figure_qa_summary", "figure_stages", "script_ref", "run_id", "failure_phase",
    "error_code", "dataset_refs", "error",
)


class _JsonNumber(str):
    """Keep the original spelling of numbers in general previews."""


def _asset_ref(value):
    # These identity tuples contain a version integer expected verbatim by the
    # workspace and script runner. Do not convert arbitrary scientific numbers.
    if not isinstance(value, dict) or set(value) != {"kind", "id", "version", "sha256"}:
        return None
    kind, identity, version, digest = (value[key] for key in ("kind", "id", "version", "sha256"))
    if (kind not in ("research_script", "builtin_script", "dataset_snapshot")
            or not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity)
            or not isinstance(version, _JsonNumber) or not re.fullmatch(r"[1-9][0-9]{0,18}", version)
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        return None
    return {**value, "version": int(version)}


def _figure_qa_summary(figure):
    """Separate verified publication from QA coverage, not a new verdict."""
    token = re.compile(r"[a-z][a-z0-9_]{0,49}")

    def statuses(source, keys):
        return {key: source[key] for key in keys if isinstance(source.get(key), str) and token.fullmatch(source[key])}

    def check_statuses(source, keys):
        checks = source.get("checks")
        if not isinstance(checks, dict):
            return {}
        return {key: state["status"] for key in keys if isinstance((check := checks.get(key)), dict)
                and (state := statuses(check, ("status",)))}

    result = {}
    for key in ("qa_receipt", "current_qa", "nature_skill_receipt"):
        record = figure.get(key)
        if not isinstance(record, dict):
            continue
        if key == "current_qa":
            overall = record.get("overall")
            if not isinstance(overall, dict):
                continue
            section = statuses(overall, ("execution", "conclusion"))
            for name in ("blocking", "advisory", "unexecuted"):
                items = overall.get(name)
                if isinstance(items, list):
                    section[name] = [item for item in items[:5] if isinstance(item, str) and token.fullmatch(item)]
        else:
            section = statuses(record, ("status",))
            keys = ("dimensions", "fonts", "linewidth", "canvas_text_bounds") if key == "qa_receipt" else (
                "source_preflight", "pdf_text", "pdf_collisions")
            checks = check_statuses(record, keys)
            if checks:
                section["checks"] = checks
        if section:
            result[key] = section
    return result


def _script_downloads(exports):
    """Fold repeated IDs/digests out of the model view, never out of evidence."""
    if not isinstance(exports, list):
        return None
    result = []
    for row in exports[:24]:
        if not isinstance(row, dict) or row.get("format") not in ("svg", "png", "pdf", "tiff"):
            continue
        link = row.get("download_url")
        if not isinstance(link, str) or not re.fullmatch(r"/api/v2/files/[a-f0-9]{32}/download", link):
            continue
        # Distinguish several figures with the same requested format.
        filename = row.get("filename")
        result.append({"format": row["format"], "download_url": link,
                       **({"filename": filename} if isinstance(filename, str) and len(filename) <= 90 else {})})
    return result


def _script_controls(value):
    if downloads := _script_downloads(value.get("exports")):
        value["exports"] = downloads
    report = value.get("script_report")
    if isinstance(report, str) and len(report) <= 8_192:
        try:
            report = json.loads(report)
        except (ValueError, TypeError, RecursionError):
            report = None
    if isinstance(report, dict):
        def count(item):
            return type(item) is int and 0 <= item <= 1_000_000_000

        summary = {key: report[key] for key in ("rows_read", "rows_plotted", "rows_excluded")
                   if count(report.get(key))}
        groups = report.get("n_per_group")
        if isinstance(groups, dict) and len(groups) <= 12 and all(
                isinstance(key, str) and 0 < len(key) <= 60 and key.isprintable() and count(item)
                for key, item in groups.items()):
            summary["n_per_group"] = groups
        if summary:
            value["render_data_summary"] = summary
    value["script_ref"] = _asset_ref(value.get("script_ref"))
    if isinstance(value.get("dataset_refs"), list):
        value["dataset_refs"] = [ref for item in value["dataset_refs"] if (ref := _asset_ref(item))]
    if isinstance(value.get("figures"), list) and value["figures"]:
        # First figure covers the common single-plot delivery. Additional
        # figures retain their bounded stage and references to original QA.
        first = value["figures"][0]
        if isinstance(first, dict) and (summary := _figure_qa_summary(first)):
            value["figure_qa_summary"] = {"figure_index": 0, **summary}
        value["figure_stages"] = [
            {key: item[key] for key in ("name", "status", "delivery_stage", "reason_code")
             if isinstance(item.get(key), str)}
            for item in value["figures"][:8] if isinstance(item, dict)]
    return value


def _workspace_overview(value):
    def rows(items, *, builtin):
        for item in items:
            if not isinstance(item, dict) or not (ref := _asset_ref(item.get("script_ref"))):
                continue
            if builtin:
                parameters = item.get("parameters")
                yield {"script_ref": ref, "parameter_names": list(parameters) if isinstance(parameters, dict) else []}
            else:
                name = item.get("name")
                yield {"script_ref": ref, "name": name if isinstance(name, str) else ""}

    return {key: list(rows(value[key], builtin=key == "presets"))
            for key in ("presets", "scripts") if isinstance(value.get(key), list)}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def content_wire_bytes(value) -> int:
    # Include the escaping required when the provider serializes content.
    return len(_json(value).encode("utf-8"))


def _verified_reference(message, text: str):
    reference = message.additional_kwargs.get("result_reference")
    storage = reference.get("storage") if isinstance(reference, dict) else None
    if not isinstance(storage, dict) or storage.get("complete") is not True:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    from src.core.tool_results import fetch_for_current_request, observation_receipt_key, storage_integrity
    key = observation_receipt_key(message)
    if (reference.get("tool_call_id") != key
            or reference.get("original_chars") != len(text) or reference.get("full_sha256") != digest):
        return None
    row = fetch_for_current_request(key)
    try:
        if (not row or row.get("tool_name") != message.name or row.get("full_sha256") != digest
                or not storage_integrity(row)["complete"]):
            return None
    except (ValueError, TypeError):
        return None
    return {"tool_call_id": key, "full_sha256": digest,
            "original_chars": len(text), "stored_chars": len(text)}


def delivery_issue(message):
    """Expose only known delivery states, without publishing tool output."""
    code = message.additional_kwargs.get("model_delivery_issue")
    messages = {
        "result_storage_unavailable": "回执原文暂时无法验证或读取。请保留现有操作记录。",
        "result_page_too_large": "该页超过单次交付上限。请缩小原文页或字段范围。",
        "result_reference_too_large": "回执引用超过交付上限。",
        "result_readback_unavailable": "回执已保存，当前任务无法调用原文读取工具。",
    }
    return (code, messages[code]) if isinstance(code, str) and code in messages else None


def last_readback_range(observations):
    """Range of the latest verified read page for a budget-stop notice."""
    for message in reversed(observations):
        if message.name != "read_tool_result" or not message.additional_kwargs.get("host_publication"):
            continue
        try:
            data = json.loads(message.content)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or data.get("status") != "ok":
            continue
        storage, source, page = (data.get(key) for key in ("storage", "tool_result_ref", "page"))
        if not all(isinstance(part, dict) for part in (storage, source, page)) or storage.get("complete") is not True:
            continue
        digest, call_id = source.get("full_sha256"), source.get("tool_call_id")
        start, end, total = (page.get(key) for key in ("start_char", "end_char", "total_chars"))
        basis = page.get("offset_basis")
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(call_id, str) or not 0 < len(call_id) <= 256
                or any(type(value) is not int for value in (start, end, total))
                or not 0 <= start < end <= total or basis not in ("stored_text", "selected_fields_json")):
            continue
        cursor = {"tool_call_id": call_id, "full_sha256": digest, "start_char": start,
                  "end_char": end, "total_chars": total, "offset_basis": basis,
                  "next_start_char": end if end < total else None}
        max_chars = min(4000, max(1, (end - start) // 2))
        if basis == "stored_text":
            cursor["retry"] = {"start_char": start, "max_chars": max_chars}
        else:
            # Public progress carries no private JSON field names. Reuse the
            # original selection; these offsets cannot address stored text.
            cursor["retry_same_selection"] = {"start_char": start, "max_chars": max_chars}
        return cursor
    return None


def _failure(message, code: str, *, verified: bool = False):
    metadata = dict(message.additional_kwargs)
    metadata.pop("first_delivery_reference", None)
    metadata["model_delivery_issue"] = code
    if not verified:
        metadata.pop("result_reference", None)
    notice = ("请求较小的原文页或更窄的字段范围，不要重做原操作。" if code == "result_page_too_large"
              else "未交付原文。这是回执交付限制，不代表工具未执行；不要据此重复执行。")
    return message.model_copy(update={"content": _json({"delivery": code, "complete": False, "notice": notice}),
                                      "additional_kwargs": metadata})


def _table_overview(value):
    rows, columns, fields = (value.get(key) for key in ("rows", "columns", "fields"))
    if (not all(isinstance(count, str) and count.isascii() and count.isdecimal() and len(count) < 12
                for count in (rows, columns)) or not isinstance(fields, list)):
        return None
    descriptors = []
    for field in fields[:10]:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        descriptors.append({key: field[key][:100] for key in (
            "name", "inferred_type", "missing_count", "unique_count", "numeric_count", "min", "median", "max")
            if isinstance(field.get(key), str)})
        if isinstance(field.get("samples"), list):
            descriptors[-1]["samples"] = [sample[:60] for sample in field["samples"][:2] if isinstance(sample, str)]
    source_ref = value.get("source_ref")
    source = ({key: source_ref[key] for key in ("file_id", "sha256", "filename", "sheet", "sheet_name")
               if isinstance(source_ref.get(key), str) and len(source_ref[key]) <= 180}
              if isinstance(source_ref, dict) else {})
    summary = {"rows": int(rows), "columns": int(columns), "reported_fields": len(fields),
               "fields": descriptors, "source_ref": source}
    digest = value.get("source_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        summary["source_sha256"] = digest
    if isinstance(value.get("inputs_binding"), dict) and source.get("file_id"):
        summary["inputs_binding"] = {"data": {"file_id": source["file_id"],
                                              **({"sha256": digest} if "source_sha256" in summary else {})}}
    for key in ("source_rows", "source_columns", "source_rows_exact", "truncated", "truncation_reasons",
                "scan_complete", "header_rows", "sheet"):
        info = value.get(key)
        if isinstance(info, (bool, str, list)) and content_wire_bytes(info) <= 160:
            summary[key] = info
    sheets = value.get("sheets")
    if isinstance(sheets, list) and sheets:
        summary["sheet_count"] = len(sheets)
        samples = [item[:40] for item in sheets[:5] if isinstance(item, str)]
        if content_wire_bytes(samples) <= 160:
            summary["sheet_names_preview"] = samples
    # Keep identities, dimensions and field counts before optional samples.
    while descriptors and content_wire_bytes(_json(summary)) > 1500:
        descriptors.pop()
    summary["fields_omitted"] = max(0, len(fields) - len(descriptors))
    summary["samples_are_not_row_counts"] = True
    return summary


def first_delivery(message, *, call: dict, reader_available: bool):
    text = message.content
    is_reader = message.name in _READERS or (
        message.name == "research_workspace" and (call.get("args") or {}).get("operation") in _WORKSPACE_READS)
    try:
        size = content_wire_bytes(text)
    except (ValueError, TypeError, RecursionError):
        return _failure(message, "result_storage_unavailable")
    limit = READER_DELIVERY_BYTES if is_reader else FIRST_DELIVERY_BYTES
    if message.name == "verify_uploaded_references":
        args = call.get("args") or {}
        limit = (READER_DELIVERY_BYTES if args.get("operation") == "read" and args.get("include_evidence") is True
                 else REFERENCE_PAGE_BYTES)
    elif message.name in {"local_hybrid_search", "deepseek_search"}:
        limit = SEARCH_DELIVERY_BYTES
    if size <= limit:
        return message
    reference = _verified_reference(message, text) if isinstance(text, str) else None
    if is_reader:
        return _failure(message, "result_page_too_large", verified=reference is not None)
    if not reference:
        return _failure(message, "result_storage_unavailable")
    if not reader_available:
        return _failure(message, "result_readback_unavailable", verified=True)

    index = []
    control = {}
    total_controls = 0
    table_overview = workspace_overview = None
    figure_read_fields = []
    try:
        # Keep the original spelling of numbers; the reference, not this view,
        # is authoritative evidence for scientific quantities and units.
        value = json.loads(text, parse_float=_JsonNumber, parse_int=_JsonNumber, parse_constant=_JsonNumber)
        if isinstance(value, dict):
            if message.name == "inspect_uploaded_table":
                table_overview = _table_overview(value)
            index = [{"pointer": "/" + key.replace("~", "~0").replace("/", "~1"),
                      "type": "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "scalar"}
                     for key, item in list(value.items())[:12] if len(key) <= 80]
            try:
                content_wire_bytes(index)
            except UnicodeError:
                index = []
            priority = _CONTROL_PRIORITY
            if message.name == "run_research_figure_script":
                figure_read_fields = [f"/{key}" for key in ("script_report", "exports") if key in value]
                figures = value.get("figures")
                if isinstance(figures, list) and figures and isinstance(figures[0], dict):
                    figure_read_fields += [f"/figures/0/{key}" for key in ("exports", "qa_receipt", "current_qa", "nature_skill_receipt")
                                           if key in figures[0]]
                value = _script_controls(value)
                priority = tuple(dict.fromkeys((*_SCRIPT_PRIORITY, *_CONTROL_PRIORITY)))
            elif message.name == "research_workspace" and (call.get("args") or {}).get("operation", "list") == "list":
                workspace_overview = _workspace_overview(value)
                content_wire_bytes(workspace_overview)  # Fail closed on malformed text below.
            for key in priority:
                if key in value and value[key] is not None:
                    total_controls += 1
                    try:
                        if content_wire_bytes(_json(value[key])) <= FIRST_DELIVERY_BYTES:
                            control[key] = value[key]
                    except (ValueError, TypeError, RecursionError):
                        continue
    except (ValueError, TypeError, RecursionError):
        workspace_overview = None
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    receipt = artifact.get("op_receipt")
    status = receipt.get("status") if isinstance(receipt, dict) else None
    if not isinstance(status, str) or len(status) > 64 or not status.isascii():
        status = "unknown"
    head_end = min(PREVIEW_CHARS, len(text))
    tail_start = max(head_end, len(text) - TAIL_PREVIEW_CHARS)
    previews = [{"start_char": 0, "end_char": head_end, "text": text[:head_end]}]
    if tail_start < len(text):
        previews.append({"start_char": tail_start, "end_char": len(text), "text": text[tail_start:]})
    payload = {
        "tool_result_ref": reference,
        "delivery": "stored_result_with_partial_preview",
        "execution_status": status,
        "read_with": {"tool": "read_tool_result", "tool_call_id": reference["tool_call_id"]},
        "complete": False,
        "field_index": index,
        "control_observation": control,
        "previews": previews,
        "notice": "原文已完整保存。缺少关键结果时只读必要字段；如需续读，使用返回的 next_cursor。",
    }

    if table_overview is not None:
        payload["delivery"] = "stored_table_overview"
        payload["table_overview"] = table_overview
        payload.pop("previews")
        payload["notice"] = "table_overview 是合成预览，不是原文 JSON 路径，且不代表全量。回读只用 field_index 中的原文路径。绘图复用已绑定的源文件。"

    if figure_read_fields:
        payload["read_with"]["available_fields"] = figure_read_fields
        payload.pop("previews")
        payload["notice"] = ("已有可用交付卡片时不重复贴链接或 SVG 源码。只说明对用户目标有影响的验证缺口。"
                             "仅缺必要证据时从 available_fields 选字段。control_observation 是摘要，不是原文 JSON 路径。")

    if workspace_overview:
        payload["delivery"] = "stored_result_reference"
        payload["workspace_overview"] = workspace_overview
        payload.pop("previews")
        payload["notice"] = ("内置预设可直接按 script_ref 执行。只需查参数说明时，按字段读取；"
                             "仅修改源码时才调用 research_workspace.read。")
        for group in ("scripts", "presets"):
            workspace_overview[group + "_omitted"] = 0

    def render():
        payload["control_fields_omitted"] = total_controls - len(control)
        return _json(payload)

    rendered = render()
    if content_wire_bytes(rendered) > FIRST_DELIVERY_BYTES and "previews" in payload:
        payload["delivery"] = "stored_result_reference"
        payload.pop("previews")
        rendered = render()
    for field in (("field_index",) if figure_read_fields else ("field_index", "notice")):
        if content_wire_bytes(rendered) <= FIRST_DELIVERY_BYTES:
            break
        payload.pop(field)
        rendered = render()
    while content_wire_bytes(rendered) > FIRST_DELIVERY_BYTES and control:
        control.pop(next(reversed(control)))
        rendered = render()
    if workspace_overview:
        for group in ("scripts", "presets"):
            while content_wire_bytes(rendered) > FIRST_DELIVERY_BYTES and workspace_overview.get(group):
                workspace_overview[group].pop()
                workspace_overview[group + "_omitted"] += 1
                rendered = render()
    if content_wire_bytes(rendered) > FIRST_DELIVERY_BYTES and figure_read_fields:
        payload.pop("notice", None)
        rendered = render()
    if content_wire_bytes(rendered) > FIRST_DELIVERY_BYTES:
        return _failure(message, "result_reference_too_large", verified=True)
    metadata = {**message.additional_kwargs, "first_delivery_reference": True}
    metadata.pop("model_delivery_issue", None)
    return message.model_copy(update={"content": rendered, "additional_kwargs": metadata})
