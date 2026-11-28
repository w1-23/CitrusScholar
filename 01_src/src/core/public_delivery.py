"""Typed disclosure boundary for public turn delivery and metadata-only timings.

Public projection is separate from authority checks and private checkpoint storage.
Only named fields of known events cross this boundary; trusted producers still choose
which answer, source, question, or artifact the current user may receive.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import sqlite3

_PRIVATE_KEYS = frozenset("""
    systemprompt developerprompt privateprompt systemmessage developermessage rawrequest
    rawprompt rawmessages promptmessages providerrequest requestpayload providerprotocol promptinjection
    reasoning reasoningcontent reasoningtext chainofthought thinking analysisdraft hiddenreasoning
    draft draftanswer drafttext draftcontent draftmessages draftresponse completionenvelope assistantdraft
    incidentalpartials proposalrepairs completionproof reviewreceipt
""".split())
_ROLE_KEYS = {"role", "type", "msgtype"}
_PRIVATE_ROLES = {"system", "developer", "systemmessage", "developermessage", "reasoning", "thinking", "draft"}
_PRIVATE_VALUES = {"private", "internal", "draft", "intermediate"}
_TAG = re.compile(r"<\s*(think|thinking|analysis|reasoning|draft|system_prompt|developer_message)\b[^>]*>.*?(?:</\s*\1\s*>|\Z)", re.I | re.S)
_FENCE = re.compile(r"(?m)^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n(?P<body>[\s\S]*?)(?:^[ \t]{0,3}(?P=fence)[`~]*[ \t]*(?:\n|\Z)|\Z)")
_ANCHOR = re.compile(r'''["'](?:[^"'\n]{1,64})["']\s*:''')
_OPEN = re.compile(r'[\[{"\x27]')
_ESCAPED_QUOTE = re.compile(r'\\+["\x27]')
_PRIVATE_SERIAL_CLASS = re.compile(r"[\x22\x27](?:SystemMessage|DeveloperMessage)[\x22\x27]", re.I)
_PRIVATE_SHAPE = re.compile(r"[\[{,]\s*[\"']?(?:system[_-]?prompt|developer[_-]?(?:message|prompt)|reasoning[_-]?content|draft[_-]?(?:answer|text|content)|raw[_-]?(?:request|prompt))\s*[\"']?\s*:", re.I)
_DECODER = json.JSONDecoder()
_ID = re.compile(r"[A-Za-z0-9_.:@+-]{1,256}\Z")
_DURATION_LIMIT_MS = 7 * 24 * 60 * 60 * 1000
TIMING_KEYS = ("queue_wait_ms", "provider_first_packet_ms", "visible_answer_first_ms",
               "tool_elapsed_ms", "context_maintenance_ms")
MAINTENANCE_PURPOSES = {"context_hints", "context_summary", "turn_checkpoint"}


# Prefer preserving source for an ambiguous request about a figure or code.
# An ordinary “download SVG” is a file obligation, not a request for XML in chat.
_FIGURE_SOURCE_INTENT = re.compile(
    r"源码|源代码|源文件|绘图代码|生成代码|脚本|标记语言|XML|"
    r"\b(?:source(?:[ -]code|[ -]file)?|markup|code|script|inline[ -]SVG|"
    r"SVG[ -]text)\b", re.I)


def verified_figure_previews(query, answer, requirements, goals, verified, *, session_id, owner_user_id):
    """Return protected previews for exact SVG placeholders in a proven file obligation."""
    from src.core import figures
    from src.graph.task_contract import FIGURE_KINDS
    from src.session.files import get_file

    if owner_user_id <= 0 or _FIGURE_SOURCE_INTENT.search(query or "") or len(requirements) != 1:
        return {}
    req = requirements[0]
    if (req.get("phase", "initial") != "initial" or req.get("figure_id")
            or req.get("all_formats") is not True or "svg" not in (req.get("formats") or ())
            or not set(req.get("kinds") or ()).intersection(FIGURE_KINDS)):
        return {}
    if len(goals) != 1 or goals[0].kind != "deliverable" or len(goals[0].evidence_call_ids) != 1:
        return {}
    goal = goals[0]
    assets = {row["artifact_id"] for row in verified if row.get("valid") and row.get("request_matches")
              and row.get("mime") == "image/svg+xml" and row.get("producer_tool") == "run_research_figure_script"
              and row.get("artifact_id") and row.get("link") in goal.artifact_links}
    if not assets:
        return {}
    indices = set(figures.PLACEHOLDER_RE.findall(answer))
    previews = {}
    for index, figure in figures.snapshot().items():
        if str(index) not in indices:
            continue
        match = re.fullmatch(r"/api/v2/files/([a-f0-9]{32})/download", figure["download_url"])
        if not match:
            continue
        file_id = match[1]
        try:
            entry = get_file(session_id, file_id, owner_user_id=owner_user_id)
        except (OSError, ValueError, sqlite3.Error):
            continue
        digest = hashlib.sha256(figure["svg"].encode("utf-8")).hexdigest()
        if (entry and entry.get("artifact_id") in assets and entry.get("mime_type") == "image/svg+xml"
                and entry.get("sha256") == digest):
            previews[index] = f"/api/v2/files/{file_id}/preview"
    return previews


def _key(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold()) if isinstance(value, str) else ""


def private_key(value):
    """Recognize private structured names, including camelCase and snake_case."""
    return _key(value) in _PRIVATE_KEYS


def private_record(row):
    if not isinstance(row, dict):
        return False
    identity = row.get("id")
    return ((isinstance(identity, list) and bool(identity) and
             _key(identity[-1]) in ("systemmessage", "developermessage"))
            or any(row.get(key) is True for key in ("internal", "private", "is_draft", "draft"))
            or any(isinstance(row.get(key), str) and row[key].casefold() in _PRIVATE_VALUES
                   for key in ("visibility", "delivery", "phase"))
            or any(_key(value) in _PRIVATE_ROLES for key, value in row.items() if _key(key) in _ROLE_KEYS))


def _structured_private(value, depth=0):
    if depth >= 24:
        return True
    if isinstance(value, dict):
        return (private_record(value) or any(private_key(key) or _structured_private(item, depth + 1)
                                             for key, item in value.items()))
    if isinstance(value, list):
        return any(_structured_private(item, depth + 1) for item in value)
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in '[{"':
            try:
                decoded = json.loads(text)
            except (ValueError, RecursionError):
                return _has_private_shape(text)
            return _structured_private(decoded, depth + 1)
        return _has_private_shape(value)
    return False


def _has_private_shape(text):
    normal = _ESCAPED_QUOTE.sub('"', text)
    if _PRIVATE_SHAPE.search(normal) or _PRIVATE_SERIAL_CLASS.search(normal):
        return True
    for field in _ANCHOR.finditer(normal):
        name = field[0][1:field[0].rfind(field[0][0])]
        key = _key(name)
        value = normal[field.end():]
        if key in ("private", "internal", "isdraft") and re.match(r"\s*true\b", value, re.I):
            return True
        if key in ("visibility", "delivery", "phase") and re.match(
                r"\s*[\x22\x27](?:private|internal|draft|intermediate)[\x22\x27]", value, re.I):
            return True
        if private_key(name) or (key in _ROLE_KEYS and re.match(
                r'''\s*["'](?:system(?:_?message)?|developer(?:_?message)?|reasoning|thinking|draft)["']''', value, re.I)):
            return True
    return False


def _structured_end(text, start):
    """Return a balanced container span, respecting quoted values."""
    stack, quoted, escaped = [], None, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quoted:
                quoted = None
        elif char in ("\x22", "\x27"):
            quoted = char
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index + 1
    return None


def strip_private_text(text):
    """Remove structured prompt/protocol spill, preserving adjacent prose and evidence.

    Do not use a generic prose keyword blacklist: mentions of prompts, models,
    reasoning and JSON examples without private structure are user content.
    """
    if not isinstance(text, str):
        return ""
    text = _TAG.sub("", text)
    if not _has_private_shape(text) and "\\u" not in text:
        return text
    text = _FENCE.sub(lambda part: "" if _has_private_shape(part["body"])
                      or (_structured_private(part["body"].strip()) if "\\u" in part["body"] else False)
                      else part[0], text)
    kept, end, search = [], 0, 0
    while (candidate := _OPEN.search(text, search)) is not None:
        start = candidate.start()
        search = start + 1
        # Ordinary quotes in prose are not an outer serialized JSON document.
        if candidate[0] in "\"'" and (start + 1 == len(text) or text[start + 1] not in '[{\\"'):
            continue
        try:
            value, stop = _DECODER.raw_decode(text, start)
        except (ValueError, RecursionError):
            if candidate[0] not in "[{":
                continue
            stop = _structured_end(text, start)
            if stop is None:
                # A truncated structured tail cannot be considered public.
                line_end = text.find("\n", start)
                tail_end = len(text) if line_end < 0 else line_end
                if _has_private_shape(text[start:tail_end]):
                    kept.append(text[end:start])
                    end = search = tail_end
                continue
            span = text[start:stop]
            if not _has_private_shape(span):
                continue
            try:
                value = ast.literal_eval(span)
            except (SyntaxError, ValueError, RecursionError):
                kept.append(text[end:start])
                end = search = stop
                continue
        if not isinstance(value, (str, dict, list)):
            continue
        search = stop
        if _structured_private(value):
            kept.append(text[end:start])
            end = stop
    kept.append(text[end:])
    return "".join(kept)


# Stable codes confirmed to have escaped the internal retrieval/execution view.
# Do not filter generic words such as "partial", "budget" or "unavailable".
_ANSWER_TRACE = re.compile(
    r"(?i)(?<![a-z0-9_])(?:candidate_pool_missing|no_candidate_pool|"
    r"retrieval_(?:followup|call|token)_budget_exhausted|retrieval_deadline_exhausted|"
    r"invalid_tool_arguments|result_fields_missing|operation_outcome_unknown|"
    r"completion_proof_invalid|turn_reconcile_required)(?![a-z0-9_])"
)
_ANSWER_TRACE_SENTENCE = re.compile(r"(?<=[。！？；])|(?<=[.!?])(?=\s)|(?<=\n)")


_TRACE_PAREN = re.compile(r"\([^()\n]*\)|（[^（）\n]*）")


def _clear_answer_trace(text):
    if not _ANSWER_TRACE.search(text):
        return text
    # Parenthetical diagnostics must not erase the conclusion they annotate.
    text = _TRACE_PAREN.sub(lambda part: "" if _ANSWER_TRACE.search(part[0]) else part[0], text)
    # Remove whole diagnostic-only code fences, not their delimiters alone.
    text = _FENCE.sub(lambda part: "" if part["body"].strip() and all(
        not line.strip() or _ANSWER_TRACE.search(line) for line in part["body"].splitlines()) else part[0], text)
    # Drop the diagnostic sentence, not an entire paragraph or adjacent citations.
    # Keep list lines atomic so removing a trace can't leave a dangling bullet.
    output = []
    for line in text.splitlines(keepends=True):
        if _ANSWER_TRACE.search(line):
            clean = "".join(part for part in _ANSWER_TRACE_SENTENCE.split(line)
                            if not _ANSWER_TRACE.search(part))
            if clean.strip() in {"", "-", "*", ">"}:
                clean = ""
            elif line.endswith("\n") and not clean.endswith("\n"):
                clean += "\n"
            line = clean
        output.append(line)
    return "".join(output)


def public_answer_text(content):
    """Read only opted-in text, excluding structural and diagnostic spill."""
    if isinstance(content, str):
        return _clear_answer_trace(strip_private_text(content))
    if not isinstance(content, (list, tuple)):
        return ""
    return _clear_answer_trace(strip_private_text("".join(
        block if isinstance(block, str) else block["text"] for block in content
        if isinstance(block, str) or (isinstance(block, dict) and not private_record(block)
            and block.get("type") in ("text", "output_text") and isinstance(block.get("text"), str)))))


def _object(value):
    return value if isinstance(value, dict) and not private_record(value) else {}


def _number(value):
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def duration_ms(value):
    if not _number(value) or value < 0:
        return None
    return int(min(_DURATION_LIMIT_MS, value))


def timing_fields(value):
    """Use null for an unobserved metric, and cap a finite measured duration."""
    value = _object(value)
    return {key: duration_ms(value.get(key)) for key in TIMING_KEYS}


def project_fields(value, *, ids="", numbers="", bools="", texts="", nullable=""):
    """Typed opt-in scalar fields. No untrusted object rides a scalar slot."""
    value = _object(value)
    result = {key: item for key in ids.split() if isinstance(item := value.get(key), str) and
              (not item or _ID.fullmatch(item))}
    result.update((key, value[key]) for key in numbers.split() if _number(value.get(key)))
    result.update((key, value[key]) for key in bools.split() if type(value.get(key)) is bool)
    result.update((key, strip_private_text(value[key])) for key in texts.split() if isinstance(value.get(key), str))
    result.update((key, None) for key in nullable.split() if key in value and value[key] is None)
    return result


def _array(value, projector):
    return [item for element in value if (item := projector(element))] if isinstance(value, (tuple, list)) else []


def _ids(value):
    return _array(value, lambda item: item if isinstance(item, str) and _ID.fullmatch(item) else None)


def _set_array(dst, src, key, projector):
    if isinstance(src.get(key), (tuple, list)):
        dst[key] = projector(src[key])


def _set_object(dst, src, key, projector):
    if isinstance(src.get(key), dict) and not private_record(src[key]):
        dst[key] = projector(src[key])


def project_artifact_delivery(value):
    row = _object(value)
    result = project_fields(row, ids="status", numbers="unavailable_count", nullable="unavailable_count") | {
        "verified_file_ids": list(dict.fromkeys(_ids(row.get("verified_file_ids"))))}
    if row.get("schema_version") != "artifact-delivery/1":
        return result

    def asset(item):
        fields = project_fields(item, ids="asset_ref format sha256 render_id qa_status", numbers="size_bytes",
                                texts="filename mime_type", nullable="render_id")
        if not fields.get("asset_ref") or not fields.get("sha256"):
            return {}
        url = item.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"/api/v2/session/[A-Za-z0-9_-]{8,128}/artifacts/[a-f0-9]{32}", url):
            return {}
        validation = _object(item.get("validation"))
        ref = item.get("workbench_project_ref")
        version = item.get("workbench_render_version", 1)
        if (isinstance(ref, str) and re.fullmatch(r"[a-f0-9]{32}", ref) and validation.get("status") == "passed"
                and type(version) is int and 1 <= version <= 999_999_999):
            fields["workbench_project_ref"] = ref
            if version > 1:
                fields["workbench_render_version"] = version
        fields.update(url=url, validation={"status": project_fields(validation, ids="status").get("status", ""),
                                           "checks": _ids(validation.get("checks"))})
        return fields

    assets = _array(row.get("artifacts"), asset)
    return result | project_fields(row, ids="delivery_id logical_task_id delivery_status render_id", nullable="render_id") | {
        "schema_version": "artifact-delivery/1", "artifacts": assets,
        "user_links": list(dict.fromkeys(item["url"] for item in assets)),
        "limitations": _ids(row.get("limitations")),
        "missing_formats": [fmt for fmt in _ids(row.get("missing_formats"))
                            if fmt in {"svg", "png", "pdf", "tiff"}],
        "obligations": _array(row.get("obligations"), lambda item: project_fields(item, texts="label") | {
            "formats": _ids(_object(item).get("formats"))})}


def _source_ref(value):
    return project_fields(value, ids="source_family document_id version_id index_generation md_sha256 chunk_id",
                          numbers="start end")


def _citation(value):
    row = _object(value)
    item = project_fields(row, ids="kind ref_id reader_id record_key role type source_type source_family doi",
                          numbers="index year score rank ref_number", bools="cited",
                          texts="title authors journal url source snippet excerpt abstract description")
    _set_object(item, row, "read_ref", _source_ref)
    if isinstance(row.get("authors"), list):
        item["authors"] = _array(row["authors"], strip_private_text)
    return item


def project_citation_bubbles(value):
    """Projection only. Use the existing evidence authorization before projecting."""
    return _array(value, lambda item: _citation(item) if _object(item).get("kind") in ("local", "shared", "web") else {})


def _citations(row):
    item = project_fields(row, ids="display", bools="pre_renumbered")
    _set_array(item, row, "bubbles", project_citation_bubbles)
    _set_array(item, row, "dropped", _ids)
    for key in ("cited", "uncited", "historical", "web_separate"):
        _set_array(item, row, key, lambda rows: _array(rows, _citation))
    if isinstance(row.get("remap"), dict):
        item["remap"] = {key: val for key, val in row["remap"].items() if _ID.fullmatch(key) and
                         (type(val) is int or isinstance(val, str) and _ID.fullmatch(val))}
    return item


def _field_ref(value):
    row = _object(value)
    result = project_fields(row, ids="kind file_id sha256", texts="sheet column", nullable="dataset_ref file_id sha256 sheet")
    _set_object(result, row, "dataset_ref", lambda item: project_fields(item, ids="kind id sha256", numbers="version"))
    return result


def _question_option(value):
    row = _object(value)
    result = project_fields(row, ids="id", texts="label description")
    _set_array(result, row, "refs", lambda rows: _array(rows, _field_ref))
    return result


def project_public_question(value):
    row = _object(value)
    result = project_fields(row, ids="id job_id status child_job_id interrupt_id", texts="prompt", numbers="version expires_at recovery_cursor",
                            bools="multi_select allow_text", nullable="answer")
    _set_array(result, row, "options", lambda rows: _array(rows, _question_option))
    def answer(item):
        safe = project_fields(item, texts="text", bools="cancelled", numbers="version")
        _set_array(safe, item, "selected_option_ids", _ids)
        return safe
    _set_object(result, row, "answer", answer)
    return result


_USAGE_NUMBERS = "input_tokens output_tokens total_tokens cached_input_tokens cache_read cache_write cache_uncached cache_hit cache_miss cache_ratio"
_BUDGET_NUMBERS = "snapshot_version epoch estimated_input_tokens estimated_tokens estimated_total_tokens original_input_tokens model_window input_limit output_tokens working_soft_tokens working_target_tokens tool_message_count message_count image_count"
_BUDGET_IDS = "scope estimate_scope estimate_method model_window_status image_estimate_status change_reason"
_CONTEXT_IDS = "source model session_id request_id logical_call_id attempt_id parent_purpose status history_load_status history_load_reason"
_COMPONENTS = "system messages tool_arguments tool_schemas images protocol"
_OBSERVATION_FIELDS = "estimated_input_tokens max_output_tokens prepared_output_tokens model_window input_limit"
_OLD_PROJECTED = frozenset("""
    tool_call_start tool_executing tool_progress tool_result operation assistant_progress
    task_update agent_recovery permission_request permission_decision status task_state
    reasoning thinking draft plan tool_resolution agent_control context_projection
    reference_audit_progress browse_phase
""".split())
_TERMINAL_NOTICES = frozenset((
    "本轮回答已完成，但历史写入未确认；刷新后可能无法恢复。",
    "本轮没有形成可交付的回答；终态以 turn_status 为准。",
    "主模型尚未配置，请联系管理员配置模型后重试。附件仍可在资料库中查看。",
    "处理请求时发生内部错误，请稍后重试", "处理请求时发生内部错误，请稍后重试。",
    "附件未通过检查，请检查文件后重试。", "附件处理队列繁忙，请稍后重试。",
    "附件解析超过处理时限，请缩小文件或重试。", "附件处理失败，请检查文件格式后重试。",
    "服务器请求队列已满，请稍后重试。", "等待处理超时，请稍后重试。",
))


def _usage(value):
    row = _object(value)
    result = project_fields(row, ids="cache_status scope logical_call_id attempt_id",
                            numbers=_USAGE_NUMBERS, nullable=_USAGE_NUMBERS + " cache_ratio_basis")
    if row.get("cache_ratio_basis") == "reported_read/(reported_read+reported_uncached)":
        result["cache_ratio_basis"] = row["cache_ratio_basis"]
    _set_object(result, row, "cache_sources", lambda item: project_fields(item, ids="read write uncached",
                nullable="read write uncached"))
    return result


def model_attempt_observation(value):
    row = _object(value)
    result = project_fields(row, ids="provider stream_state finish_reason finish_reason_status response_type output_limit_status",
                            numbers=_OBSERVATION_FIELDS, bools="streaming", nullable="finish_reason max_output_tokens")
    for key in ("provider_first_packet_ms", "attempt_elapsed_ms"):
        if key in row:
            result[key] = duration_ms(row[key])
    return result


def _context(event, row):
    if event == "context_usage":
        result = project_fields(row, ids=_CONTEXT_IDS + " scope observation_scope", numbers="total epoch",
                                nullable="total epoch")
        _set_object(result, row, "call", _usage)
    elif event in ("context_snapshot", "context_budget"):
        result = project_fields(row, ids=_CONTEXT_IDS + " " + _BUDGET_IDS + " usage_status error",
                                numbers=_BUDGET_NUMBERS, nullable=_BUDGET_NUMBERS, bools="fits tool_results_shortened")
        _set_object(result, row, "components", lambda item: project_fields(item, numbers=_COMPONENTS, nullable=_COMPONENTS))
        _set_object(result, row, "provider_usage", _usage)
        if "actual_output_tokens" in row:
            result.update(project_fields(row, numbers="actual_output_tokens", nullable="actual_output_tokens"))
        if "attempt_elapsed_ms" in row:
            result["attempt_elapsed_ms"] = duration_ms(row["attempt_elapsed_ms"])
        _set_object(result, row, "task_budget",
                    lambda item: project_fields(item, numbers="calls max_calls tokens max_tokens"))
    elif event == "context_status":
        result = project_fields(row, ids="history_load_status history_load_reason estimate_scope memory_status format_hint",
                                numbers="history_msgs history_chars ltm_chars estimated_tokens max_tokens soft_threshold hard_threshold compression_len",
                                bools="ltm_recalled resident_cards user_preferences compressed", texts="estimate_label")
        _set_array(result, row, "suggestions", lambda rows: _array(rows, strip_private_text))
    else:
        result = project_fields(row, ids="stage status reason_code tool_call_id", texts="message")
        def readback(value):
            item = _object(value)
            reply = project_fields(item, ids="tool_call_id full_sha256 offset_basis", numbers="start_char end_char total_chars next_start_char",
                                   nullable="next_start_char")
            for key in ("retry", "retry_same_selection"):
                _set_object(reply, item, key, lambda item: project_fields(item, numbers="start_char max_chars"))
            return reply
        _set_object(result, row, "last_readback_page", readback)
    return result


def _terminal_event(row):
    from src.core.progress_bus import public_tool_reason
    from src.core.task_events import _public_terminal
    from src.core.turn_notices import stopped_notice

    result = project_fields(row, ids="job_id session_id turn_status recovery",
                            numbers="gen_time_ms total_elapsed_ms citation_count cited_count",
                            bools="history_saved citation_supported retrieval_supported")
    if "answer" in row:
        result["answer"] = public_answer_text(row["answer"])
    for key in ("notice", "warning"):
        if isinstance(row.get(key), str) and row[key] in _TERMINAL_NOTICES:
            result[key] = row[key]
    if row.get("stop_notice"):
        result["stop_notice"] = stopped_notice()
    if "stop_reason" in row:
        result["stop_reason"] = public_tool_reason(row["stop_reason"])
    for key in ("question", "pending_question"):
        _set_object(result, row, key, project_public_question)
    _set_array(result, row, "artifact_ids", _ids)
    _set_object(result, row, "turn_envelope", _public_terminal)
    return result


_NARRATION_STAGES = {
    "local_search": "我先查找相关的本地资料。",
    "evidence_read": "我会核对原文，确认找到的资料能支持哪些结论。",
    "web_search": "我再核对相关的公开来源。",
    "table_read": "我先检查数据表的结构和可用字段。",
    "data_check": "接下来检查数据质量，找出会影响判断的问题。",
    "figure_render": "接下来根据已选数据生成图件并检查可用的输出。",
}


def project_execution_narration(row):
    # Deterministic mode: trust only a Host-selected stage. Never echo arbitrary prose.
    stage = row.get("stage")
    if (not isinstance(stage, str) or stage not in _NARRATION_STAGES or
            row.get("visibility") != "public" or private_record(row)):
        return {}
    result = project_fields(row, ids="event_id run_id job_id thread_id client_request_id", numbers="created_at")
    if (not all(result.get(k) for k in ("event_id", "run_id", "job_id", "thread_id", "client_request_id"))
            or result["job_id"] != result["run_id"] or "created_at" not in result):
        return {}
    return result | {"type": "execution_narration", "visibility": "public", "stage": stage,
                     "text": _NARRATION_STAGES[stage]}


def project_public_event(event, data):
    """Project a live or persisted event. Unknown event payloads fail closed."""
    seq = data.get("seq") if isinstance(data, dict) else None
    cursor = {"seq": seq} if type(seq) is int else {}
    row = _object(data)
    if not row:
        return cursor
    if event in _OLD_PROJECTED:
        from src.core.progress_bus import public_progress_payload
        return public_progress_payload(event, row) | cursor
    if event == "execution_narration":
        result = project_execution_narration(row)
    elif event == "text":
        text = public_answer_text(row.get("content"))
        # Ignore discarded blocks but keep meaningful inter-chunk whitespace.
        result = {"content": text} if text else {}
        if result and row.get("delivery") == "committed":
            result["delivery"] = "committed"
    elif event == "done":
        result = _terminal_event(row)
    elif event == "error":
        from src.core.progress_bus import public_progress_payload
        if row.get("code") == "turn_reconcile_required":
            result = {"code": "turn_reconcile_required", "message": "任务执行状态待核对。"}
        else:
            result = public_progress_payload(event, row)
    elif event == "citations":
        result = _citations(row)
    elif event == "request_user_input":
        result = {}
        _set_object(result, row, "question", project_public_question)
    elif event == "artifact_delivery":
        result = project_artifact_delivery(row)
    elif event in ("context_usage", "context_snapshot", "context_budget", "context_status", "context_degraded"):
        result = _context(event, row)
    elif event == "model_attempt":
        result = project_fields(row, ids="attempt_id purpose status usage_status error", numbers="reserved_tokens charged_tokens")
        _set_object(result, row, "usage", _usage)
        _set_object(result, row, "observation", model_attempt_observation)
    elif event == "job":
        result = project_fields(row, ids="job_id session_id request_id client_request_id status", numbers="recovery_cursor")
        for field, value in (("protocol", "citrus-stream/1"), ("task_protocol", "citrus-task/1")):
            if row.get(field) == value:
                result[field] = value
        if result.get("job_id") and row.get("events_url") == f"/jobs/{result['job_id']}/events":
            result["events_url"] = row["events_url"]
    elif event in ("execution_segment", "retrieval_cache"):
        result = project_fields(row, ids="job_id phase reason_code kind status")
    elif event == "context_maintenance":
        result = {"elapsed_ms": duration_ms(row.get("elapsed_ms"))}
        if row.get("status") in ("compacted", "failed"):
            result["status"] = row["status"]
    else:
        result = {}
    return result | cursor


def project_diagnostic_progress(event, value):
    """Stdlib-only progress projection for private operator archives.

    Exporting structured evidence never requires rendering the browser's
    richer labels or loading the LangGraph runtime on the operator host.
    """
    if event == "context_maintenance":
        return project_public_event(event, value)
    row = _object(value)
    cursor = project_fields(row, numbers="seq")
    if event != "assistant_progress" or private_record(row) or row.get("kind") == "model_preview":
        return cursor
    return cursor | project_fields(row, ids="schema event_id job_id run_id tool_call_id operation_id logical_step_id "
                                         "note_id kind status reason_code tool_name")


def _since_ms(start, end):
    if not _number(start) or not _number(end) or start <= 0 or end < start:
        return None
    return duration_ms(round((end - start) * 1000, 3))


class TurnTimingEvidence:
    """Reduce ordered event metadata without retaining raw answer or prompt text."""
    def __init__(self, *, accepted_at=None, started_at=None):
        self._accepted = accepted_at
        self._queue = _since_ms(accepted_at, started_at)
        self._provider = None
        self._visible = None
        self._reset_answer()
        self._tool = None
        self._maintenance = None
        self._post_terminal_maintenance = None

    def _reset_answer(self):
        self._armed = False
        self._pending_visible = None
        self._legacy_visible = None
        self._legacy_digest = hashlib.sha256()

    def attempt(self, purpose, observation, *, started_at=None):
        metrics = model_attempt_observation(observation)
        if purpose in MAINTENANCE_PURPOSES:
            elapsed = metrics.get("attempt_elapsed_ms")
            if elapsed is not None:
                self._maintenance = (self._maintenance or 0) + elapsed
        else:
            first = metrics.get("provider_first_packet_ms")
            if first is not None:
                # Preserve physical-call TTFT. For parallel attempts select the
                # earliest observed packet by start + latency, not minimum TTFT.
                stamp = (started_at + first / 1000) if _number(started_at) else math.inf
                if self._provider is None or stamp < self._provider[0]:
                    self._provider = (stamp, first)

    def observe(self, event, data, *, created_at):
        row = _object(data)
        if private_record(data):
            return
        if event == "answer_start":
            self._armed = True
        elif event in ("text_reset", "agent_recovery"):
            self._reset_answer()
        elif event == "text" and self._armed:
            text = public_answer_text(row.get("content"))
            if row.get("delivery") == "committed":
                if text.strip() and self._pending_visible is None:
                    self._pending_visible = created_at
            else:
                self._legacy_digest.update(text.encode("utf-8", errors="surrogatepass"))
                if text.strip() and self._legacy_visible is None:
                    self._legacy_visible = created_at
        elif event == "done":
            status = row.get("turn_status")
            if self._visible is None and status not in ("awaiting_input", "cancelled"):
                stamp = self._pending_visible
                answer = public_answer_text(row.get("answer"))
                if stamp is None and answer.strip():
                    # A legacy text frame was not marked at the producer. Give
                    # it credit only if the accumulated text matches the answer.
                    matches = (self._legacy_digest.digest() ==
                               hashlib.sha256(answer.encode("utf-8", errors="surrogatepass")).digest())
                    stamp = self._legacy_visible if matches and self._legacy_visible is not None else created_at
                self._visible = _since_ms(self._accepted, stamp)
            self._reset_answer()
        elif event == "tool_result" and (elapsed := duration_ms(row.get("elapsed_ms"))) is not None:
            self._tool = (self._tool or 0) + elapsed
        elif event == "context_maintenance" and row.get("status") in ("compacted", "failed"):
            if (elapsed := duration_ms(row.get("elapsed_ms"))) is not None:
                self._post_terminal_maintenance = (self._post_terminal_maintenance or 0) + elapsed

    def values(self):
        return timing_fields({"queue_wait_ms": self._queue,
            "provider_first_packet_ms": self._provider[1] if self._provider else None,
            "visible_answer_first_ms": self._visible, "tool_elapsed_ms": self._tool,
            "context_maintenance_ms": (self._post_terminal_maintenance if self._post_terminal_maintenance is not None
                                       else self._maintenance)})
