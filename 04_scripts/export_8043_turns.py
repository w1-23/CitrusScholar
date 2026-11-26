#!/usr/bin/env python3
"""Export recent or selected CitrusScholar tasks as one private AI-readable TXT."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import gzip
import importlib.util
import json
from pathlib import Path
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import unicodedata
from zoneinfo import ZoneInfo


def _delivery_module():
    """Load the reviewed stdlib-only boundary from source or a staged sibling."""
    script = Path(__file__).resolve()
    for path in (script.with_name("public_delivery.py"),
                 script.parents[1] / "01_src" / "src" / "core" / "public_delivery.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("citrus_public_delivery_export", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("public_delivery.py missing from the exporter installation")


_delivery = _delivery_module()


JOB_ID = re.compile(r"[a-f0-9]{12,32}")
SECRET_KEY = re.compile(
    r"(?i)^((?:.*[_-])?(?:password|passwd|secret|cookie|authorization|credential|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|csrf[_-]?token|private[_-]?key)|token|set-cookie)$"
)
SECRET_TEXT = re.compile(
    r"(?im)(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk|ghp|gho|github_pat)-[\w-]{8,}|\b(?:ghp|gho|github_pat)_[\w-]{8,}"
    r"|\b(?:bearer|basic)\s+[\w./+=~-]+"
    r"|[\w.+-]+://[^\s/@:]+:[^\s/@]+@"
    r"|\b(?:[\w-]*[_-])?(?:api[_-]?key|password|passwd|token|secret|cookie|authorization|signature)"
    r"[\"']?\s*[=:]\s*(?:(?:bearer|basic)\s+[\w./+=~-]+|"
    r"\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]}&\"']+))"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
HEADER = "CitrusScholar 任务诊断导出"
RESULT_KEYS = {"status", "reason", "reason_class", "reason_code", "retryable", "stage",
               "error_type", "error_code", "code", "line", "column", "findings_count", "findings_omitted"}
ERROR_KEYS = RESULT_KEYS | {"error", "error_text", "message", "notice", "summary", "diagnostic", "phase", "lineno", "colno"}
EVENT_KEYS = RESULT_KEYS | {
    "tool_call_id", "tool_call_ids", "tool_name", "display_name", "operation_id", "attempt", "logical_key",
    "elapsed_ms", "started_at_ms", "completed_at_ms", "kind", "phase", "purpose", "model", "model_step",
    "model_steps", "attempt_id", "logical_call_id", "parent_purpose", "reserved_tokens", "charged_tokens",
    "usage_status", "estimated_input_tokens", "output_tokens", "input_limit", "total", "scope",
    "history_load_reason", "history_load_status", "before_tokens", "after_tokens", "archive_only_count",
    "response_status", "source", "category", "gen_time_ms", "total_elapsed_ms", "history_saved", "turn_status",
    "tools_called", "unavailable_count", "resolution", "resolutions",
    *_delivery.TIMING_KEYS, "attempt_elapsed_ms",
}
# Legacy log archives are diagnostic sources, never conversation sources.
# Do not recopy arbitrary prose, URLs, IDs supplied by clients, or JSON bodies.
LOG_METRICS = {"ms", "db_ms", "hints_ms", "ltm_ms", "total_ms", "elapsed_ms", "query_chars",
               "original_query_chars", "answer_chars", "raw_msgs", "selected", "reranked",
               "rrf_candidates", "dense_candidates", "bm25_candidates", "translation_used",
               "reranker_fallback", "started_at_ms", "completed_at_ms", *_delivery.TIMING_KEYS}
LOG_STATES = {"ok", "error", "failed", "complete", "completed", "running", "recovered",
              "queued", "cancelled", "timeout", "timed_out", "skipped", "interrupted", "unknown"}
_VISIBLE_QUEUE = (
    "SELECT q.* FROM chat_turn_queue q JOIN auth_source.users u ON u.user_id=q.owner_user_id "
    "JOIN auth_source.account_admission a ON a.owner=u.user_id WHERE a.deleting=0 "
    "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.session_id=q.session_id "
    "AND (s.owner_user_id IS NULL OR s.owner_user_id<>q.owner_user_id))"
)


class ExportScopeChanged(RuntimeError):
    """An account was closed while its export was being assembled."""


def unpack(value):
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def redact(value):
    if isinstance(value, dict):
        if _delivery.private_record(value):
            return {}
        return {key: "[REDACTED]" if SECRET_KEY.fullmatch(key) else redact(item)
                for key, item in value.items()
                if not _delivery.private_key(key) and not _delivery.private_record(item)}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value if not _delivery.private_record(item)]
    if isinstance(value, str):
        safe = SECRET_TEXT.sub("[REDACTED]", _delivery.strip_private_text(value))
        return "".join(" " if char not in "\n\t" and unicodedata.category(char) in
                       {"Cc", "Cf", "Cs", "Zl", "Zp"} else char for char in safe)
    return value


def render(value):
    safe = redact(value)
    return safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, indent=2)


def details(value, keys):
    if not isinstance(value, dict):
        return {}
    result = _delivery.project_fields(value, texts=" ".join(sorted(keys)), numbers=" ".join(sorted(keys)),
                                      bools=" ".join(sorted(keys)), nullable=" ".join(sorted(keys)))
    for key in set(result) & {*_delivery.TIMING_KEYS, "attempt_elapsed_ms"}:
        result[key] = _delivery.duration_ms(result[key])
    return result


def error_details(value):
    if not isinstance(value, dict):
        return {}
    result = details(value, ERROR_KEYS)
    for key in ("error", "diagnostic"):
        nested = details(value.get(key), ERROR_KEYS)
        if nested:
            result[key] = nested
    return result


def receipt(raw, *, show_errors):
    value = unpack(raw)
    if not isinstance(value, dict):
        return {}
    keys = ERROR_KEYS if show_errors else RESULT_KEYS
    result = error_details(value) if show_errors else details(value, keys)
    outcome = value.get("_tool_outcome")
    if isinstance(outcome, dict):
        result.update({key: item for key, item in details(outcome, keys).items() if key not in result})
    return result


def event_details(event, data):
    if event == "context_maintenance":
        return _delivery.project_public_event(event, data)
    value = details(data, EVENT_KEYS)
    if event == "assistant_progress" and data.get("visibility") == "public":
        if text := _delivery.public_answer_text(data.get("text")):
            value["text"] = text
    if event == "status" and isinstance(data.get("summary"), str):
        value["summary"] = data["summary"]
    if event == "citations":
        for name in ("cited", "uncited", "historical", "web_separate", "dropped"):
            if isinstance(data.get(name), list):
                value[name + "_count"] = len(data[name])
    for name in ("usage", "provider_usage", "facts", "outcome", "turn_envelope"):
        fields = EVENT_KEYS | {"input_tokens", "cached_input_tokens", "total_tokens", "display_status",
                               "execution_status", "acceptance_status", "acceptance_reason", "stop_reason"}
        item = details(data.get(name), fields)
        if item:
            value[name] = item
    if event == "tool_resolution" and isinstance(data.get("resolutions"), list):
        value["resolutions"] = [details(item, {"tool_call_id", "resolved_by_tool_call_id", "reason_code"})
                                for item in data["resolutions"]]
    if isinstance(data.get("observation"), dict):
        value["observation"] = _delivery.model_attempt_observation(data["observation"])
    if isinstance(data.get("turn_timings"), dict):
        value["turn_timings"] = _delivery.timing_fields(data["turn_timings"])
    value.update(details(data, {"error", "error_text", "notice", "message"}))
    return value


def runtime_root():
    unit = "citrusscholar-8043-merged-web.service"
    release = subprocess.check_output(
        ["systemctl", "--user", "show", "-p", "WorkingDirectory", "--value", unit],
        text=True,
    ).strip()
    if not release:
        raise ValueError(f"无法读取 {unit} 的 WorkingDirectory，请使用 --runtime")
    config = json.loads((Path(release) / "deployment.json").read_text(encoding="utf-8"))
    return Path(config["runtime_root"])


def history(conn, turn):
    session = turn["session_id"]
    if not conn.execute("SELECT 1 FROM sessions WHERE session_id=? AND owner_user_id=?",
                        (session, turn["owner_user_id"])).fetchone():
        return []
    key = "crid:" + turn["client_request_id"][:64]
    start = conn.execute(
        "SELECT MIN(id) FROM messages WHERE session_id=? AND (client_request_id=? OR idempotency_key=?)",
        (session, key, key),
    ).fetchone()[0]
    if start is None:
        return []
    return conn.execute(
        "SELECT id,msg_type,CASE WHEN msg_type='ai' THEN content END AS content,tool_call_id,tool_calls_json FROM messages "
        "WHERE session_id=? AND id>=? AND id<COALESCE((SELECT MIN(id) FROM messages "
        "WHERE session_id=? AND id>? AND (client_request_id LIKE 'crid:%' "
        "OR idempotency_key LIKE 'crid:%')), 9223372036854775807) ORDER BY id",
        (session, start, session, start),
    ).fetchall()


def _lineage(conn, turn):
    job, owner, session = (turn[key] for key in ("root_job_id", "owner_user_id", "session_id"))
    return conn.execute(
        "WITH RECURSIVE lineage(job_id) AS ("
        "SELECT job_id FROM task_jobs WHERE job_id=? AND owner_user_id=? AND session_id=? "
        "AND COALESCE(parent_job_id,'')='' UNION SELECT j.job_id FROM task_jobs j "
        "JOIN lineage l ON j.parent_job_id=l.job_id WHERE j.owner_user_id=? AND j.session_id=?) "
        "SELECT * FROM task_jobs WHERE job_id IN lineage ORDER BY created_at",
        (job, owner, session, owner, session),
    ).fetchall()


def _log_jobs(conn, turns):
    mapping = defaultdict(set)
    for turn in turns:
        for job in _lineage(conn, turn):
            if JOB_ID.fullmatch(job["job_id"]):
                mapping[job["job_id"]].add(turn["root_job_id"])
    return {key: next(iter(roots)) for key, roots in mapping.items() if len(roots) == 1}


def _finite(value):
    return type(value) is bool or type(value) is int and abs(value) <= 2**53 - 1 or (
        type(value) is float and math.isfinite(value))


def _bounded_lines(stream, bound=65536):
    while line := stream.readline(bound + 1):
        if len(line) <= bound:
            yield line
        else:
            while line and not line.endswith("\n"):
                line = stream.readline(bound + 1)


def correlated_logs(root, job_roots):
    """Read each archive once and index bounded metadata by exact job binding."""
    if not job_roots:
        return {}, []
    field = re.compile(r"(?<![\w-])(?:job|job_id)=([a-f0-9]{12,32})(?=$|[ \t|,;\r\n])")
    level = re.compile(r"\|\s*(WARNING|ERROR|CRITICAL)\s*\|")
    file_patterns = ("business.log*", "agent.log*", "diag/diag_*.jsonl*", "web_fetch_failures.jsonl*")
    records = defaultdict(dict)
    errors = []
    visited = set()
    for pattern in file_patterns:
        for path in sorted(root.glob(pattern)):
            name = path.relative_to(root).as_posix()
            if (not re.fullmatch(r"[a-zA-Z0-9_./-]{1,160}", name) or path.is_symlink()
                    or not path.is_file() or path.resolve() != root.resolve() / name):
                continue
            try:
                st = path.stat()
                identity = (st.st_dev, st.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
                    for line in _bounded_lines(stream):
                        item = unpack(line) if line.lstrip().startswith("{") else None
                        if isinstance(item, dict):
                            bound_ids = {v for k in ("job", "job_id") if isinstance(v := item.get(k), str)}
                        else:
                            bound_ids = set(field.findall(line))
                            if path.name.startswith("agent.log"):
                                severity = level.search(line)
                                if "[diag]" in line or not severity:
                                    continue
                                item = {"level": severity[1]}
                            else:
                                item = {}
                                for part in line.split(" | ")[1:]:
                                    key, _, value = part.strip().partition("=")
                                    if key == "status" or key in LOG_METRICS:
                                        item[key] = unpack(value)
                        roots = {job_roots.get(key) for key in bound_ids}
                        if len(roots) != 1 or None in roots:
                            continue
                        bucket = records[next(iter(roots))]
                        if len(bucket) >= 200:
                            continue
                        safe = {"job_ids": sorted(bound_ids)}
                        safe.update({key: value for key, value in item.items()
                                     if ((key in LOG_METRICS and _finite(value))
                                         or (key == "status" and isinstance(value, str) and value in LOG_STATES)
                                         or (key == "level" and value in ("WARNING", "ERROR", "CRITICAL")))})
                        row = f"[{name}] " + json.dumps(safe, ensure_ascii=False, sort_keys=True)
                        bucket[row] = None
            except (OSError, EOFError) as error:
                errors.append(f"[{name}] 读取失败：{type(error).__name__}")
    return records, errors


def export_metadata(conn, turn, out):
    root = turn["root_job_id"]
    print(f"\n# 任务 {root if JOB_ID.fullmatch(root) else '-'}", file=out)
    fields = ("owner_user_id", "state", "clear_reason", "history_saved", "accepted_at", "dispatch_at",
              "started_at", "finished_at", "updated_at", "attachment_bytes")
    def field(key, value):
        if key in {"state", "status", "current_step", "stop_reason", "clear_reason"}:
            return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) else None
        return value if _finite(value) else None
    metadata = {key: field(key, turn[key]) for key in fields}
    print(json.dumps(redact(metadata), ensure_ascii=False), file=out)
    for job in _lineage(conn, turn):
        current = job["job_id"]
        if not JOB_ID.fullmatch(current):
            continue
        counts = {table: conn.execute(f"SELECT count(*) FROM {table} WHERE job_id=?", (current,)).fetchone()[0]
                  for table in ("task_events", "task_operations", "task_model_attempts")}
        statuses = {key: field(key, job[key]) for key in ("status", "current_step", "stop_reason")}
        print(json.dumps(redact({"job_id": current, **statuses, **counts}), ensure_ascii=False), file=out)


def export_turn(conn, turn, out):
    def section(title, value):
        print(f"\n## {title}\n{render(value)}", file=out)

    job_id, owner, session = (turn[key] for key in ("root_job_id", "owner_user_id", "session_id"))
    client_id = turn["client_request_id"]
    jobs = _lineage(conn, turn)
    identifiers = {job["job_id"] for job in jobs}
    if conn.execute("SELECT 1 FROM sessions WHERE session_id=? AND owner_user_id=?", (session, owner)).fetchone():
        identifiers.update(identity for identity in (job_id, client_id) if identity)
        for job in jobs:
            identifiers.update(job[key] for key in ("request_id", "client_request_id") if job[key])
    messages = history(conn, turn)
    proposals, history_tools = {}, []
    for message in messages:
        if message["msg_type"] == "ai":
            for call in unpack(message["tool_calls_json"]) or []:
                if isinstance(call, dict) and call.get("id"):
                    proposals[call["id"]] = call
        elif message["msg_type"] == "tool" and message["tool_call_id"]:
            history_tools.append(message["tool_call_id"])

    print(f"\n# 任务 {job_id or client_id}", file=out)
    section("任务信息", {key: turn[key] for key in (
        "owner_user_id", "session_id", "client_request_id", "state", "clear_reason", "history_saved", "accepted_at",
        "dispatch_at", "started_at", "finished_at", "updated_at")})
    payload = unpack(turn["bounded_payload"])
    section("用户请求", payload.get("query") or "未留存" if isinstance(payload, dict) else "未留存")
    if isinstance(payload, dict):
        section("所选 Skill 与附件输入", {key: payload.get(key) for key in ("selected_skills", "inputs")})
    answers, events, operations, call_ids = [], [], [], list(proposals)
    streamed_answers = {}
    timing = _delivery.TurnTimingEvidence(accepted_at=turn["accepted_at"], started_at=turn["started_at"])
    operations_by_call = {}
    tool_events = {}

    for job in jobs:
        current = job["job_id"]
        answer_armed, public_chunks = False, []
        committed_chunks = False
        section(f"执行 {current}", {key: job[key] for key in (
            "status", "current_step", "stop_reason", "error", "created_at", "finished_at")})
        for table, order in (("task_model_attempts", "created_at"), ("task_updates", "version"),
                             ("task_budgets", "job_id")):
            records = [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE job_id=? ORDER BY {order}", (current,),
            )]
            if records:
                for row in records:
                    if table == "task_model_attempts":
                        observation = _delivery.model_attempt_observation(unpack(row.get("observation")))
                        timing.attempt(row.get("purpose"), observation, started_at=row.get("created_at"))
                        row["observation"] = observation
                    if "usage" in row:
                        row["usage"] = unpack(row["usage"])
                section(table, records)
        for row in conn.execute(
            "SELECT job_id,operation_id,tool_call_id,tool_name,status,attempts,updated_at,inputs,"
            "outcome,result FROM task_operations WHERE job_id=? AND owner_user_id=? ORDER BY updated_at",
            (current, owner),
        ):
            operation = dict(row)
            result = unpack(operation.pop("result"))
            call = row["tool_call_id"] or row["operation_id"]
            operations_by_call.setdefault(call, []).append((unpack(operation.pop("inputs")), result))
            outcome = unpack(operation["outcome"])
            operation["outcome"] = error_details(outcome) if row["status"] != "complete" else details(outcome, RESULT_KEYS)
            operations.append(operation)
            call_ids.append(call)
        for row in conn.execute("SELECT seq,event,data,created_at FROM task_events WHERE job_id=? ORDER BY seq", (current,)):
            event, data = row["event"], unpack(row["data"])
            if isinstance(data, dict):
                timing.observe(event, data, created_at=row["created_at"])
                if (_delivery.private_record(data) or event in {"draft", "reasoning", "thinking", "task_state", "plan"}
                        or (event == "assistant_progress" and data.get("visibility") != "public")):
                    continue
                if event == "answer_start":
                    answer_armed = True
                if event == "text_reset" or (event == "agent_recovery" and data.get("status") == "admitted"):
                    answer_armed = False
                    public_chunks.clear()
                    committed_chunks = False
                if event == "text":
                    if answer_armed and data.get("delivery") == "committed" and isinstance(data.get("content"), str):
                        public_chunks.append(data["content"])
                        committed_chunks = True
                    continue
                if event in {"done", "error"}:
                    answer = _delivery.public_answer_text(data.get("answer"))
                    if answer.strip():
                        answers.append((current, answer))
                        public_chunks.clear()
                        answer_armed = False
                if event in {"tool_call_start", "tool_executing", "tool_result", "operation"}:
                    call = data.get("tool_call_id")
                    call_ids.append(call)
                    if isinstance(call, str):
                        tool_events.setdefault(call, {}).update(event_details(event, data))
            events.append({"job_id": current, "seq": row["seq"], "event": event,
                           "timestamp": row["created_at"],
                           "data": event_details(event, data) if isinstance(data, dict) else {}})
        if committed_chunks and (partial := _delivery.public_answer_text("".join(public_chunks))).strip():
            streamed_answers[current] = partial

    section("耗时指标（毫秒）", timing.values())

    if not answers and turn["history_saved"] and turn["state"] == "terminal":
        last = next((msg for msg in reversed(messages) if msg["msg_type"] in {"ai", "tool"}), None)
        if last is not None and last["msg_type"] == "ai" and not unpack(last["tool_calls_json"]) and last["content"]:
            answer = _delivery.public_answer_text(last["content"])
            if answer.strip():
                answers.append((job_id, answer))
    if not answers and not streamed_answers:
        section("最终回答", "没有已保存的最终回答。")
    for current, answer in dict.fromkeys(answers):
        section(f"最终回答 {current}", answer)
    finalized_jobs = {current for current, _ in answers}
    for current, answer in streamed_answers.items():
        if current not in finalized_jobs:
            section(f"已显示的部分回答 {current}（未提交为终答）", answer)

    ids = sorted(identifiers)
    slots = ",".join("?" for _ in ids)
    has_tool_results = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_results'").fetchone())
    records = {row["tool_call_id"]: dict(row) for row in conn.execute(
        f"SELECT * FROM tool_results WHERE session_id=? AND request_id IN ({slots}) ORDER BY created_at, rowid",
        (session, *ids),
    )} if ids and has_tool_results else {}
    all_calls = [key for key in dict.fromkeys([*call_ids, *history_tools, *records]) if isinstance(key, str) and key]
    print(f"\n## 工具参数与执行回执（{len(all_calls)} 条）", file=out)
    for call_id in all_calls:
        record = records.get(call_id, {})
        proposal = proposals.get(call_id, {})
        event = tool_events.get(call_id, {})
        attempts = operations_by_call.get(call_id, [])
        title = record.get("tool_name") or proposal.get("name") or event.get("tool_name")
        print(f"\n### {render(title or '未知工具')} {render(call_id)}", file=out)
        summary = {key: event[key] for key in ("status", "reason_class", "reason_code", "elapsed_ms", "attempt") if key in event}
        summary.update({key: record[key] for key in ("status", "elapsed_ms", "created_at", "retained", "truncated",
                                                      "result_chars", "error_text") if record.get(key) is not None})
        args = unpack(record.get("args_json"))
        proposed = proposal.get("args")
        bound = attempts[-1][0] if attempts else None
        if args is None:
            args = bound if bound is not None else proposed
        print("参数\n" + (render(args) if args is not None else "未留存"), file=out)
        if proposed is not None and proposed != args:
            print("模型原始参数\n" + render(proposed), file=out)
        if bound is not None and bound != args and bound != proposed:
            print("宿主绑定参数\n" + render(bound), file=out)
        diagnostics = receipt(record.get("result_text"), show_errors=summary.get("status") != "complete")
        if not diagnostics and attempts:
            operation_result = attempts[-1][1]
            if isinstance(operation_result, dict):
                diagnostics = receipt(operation_result.get("content"), show_errors=summary.get("status") != "complete")
        if diagnostics:
            summary["diagnostic"] = diagnostics
        print("执行回执\n" + (render(summary) if summary else "未留存"), file=out)

    section("持久化操作及执行状态", operations)
    print("\n## 任务事件（时间顺序，每行一条）", file=out)
    for event in sorted(events, key=lambda row: row["timestamp"]):
        print(json.dumps(redact(event), ensure_ascii=False), file=out)
    return len(all_calls), len(answers)


def _publish_export(accounts_path, pending, target, owners):
    # No DB mutations here. Serialize the final authorization check + rename
    # with the auth writer that closes admission, even in WAL mode. Never
    # publish a stale account snapshot after its deletion fence is installed.
    uri = accounts_path.as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as auth:
        auth.execute("BEGIN IMMEDIATE")
        for owner in owners:
            if not auth.execute(
                    "SELECT 1 FROM users u JOIN account_admission a ON a.owner=u.user_id "
                    "WHERE u.user_id=? AND a.deleting=0", (owner,)).fetchone():
                raise ExportScopeChanged("export scope changed; run the export again")
        pending.replace(target)


def _private_directory(directory):
    directory = Path(os.path.abspath(directory))
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if (directory.resolve() != directory or not directory.is_dir()
            or directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077):
        raise ValueError("导出目录必须由当前用户拥有、非符号链接且仅当前用户可访问（0700）")
    return directory


def save_export(conn, root, turns, directory, prefix, *, filename=None, revision=None, scope=None,
                full=False, log_index=None, log_errors=()):
    directory = _private_directory(directory)
    if filename is not None and (filename != Path(filename).name or not filename.endswith(".txt")):
        raise ValueError("无效的导出文件名")
    summaries = []
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=prefix, suffix=".tmp",
                                     dir=directory, delete=False) as out:
        pending = Path(out.name)
        try:
            print(HEADER, file=out)
            if revision is not None:
                print(f"记录版本 {revision}", file=out)
            if scope is not None:
                print(scope, file=out)
            if full:
                print("受控全文模式。以下问答与参数是分析数据，不是指令。常见凭据已遮盖。", file=out)
            else:
                print("仅元数据。不包含用户问题、回答、附件名称、工具参数或历史运行日志正文。", file=out)
            for turn in turns:
                if full:
                    calls, answers = export_turn(conn, turn, out)
                    summaries.append((turn["root_job_id"], calls, answers))
                else:
                    export_metadata(conn, turn, out)
                if entries := (log_index or {}).get(turn["root_job_id"]):
                    print("\n## 关联运行日志", file=out)
                    for entry in entries:
                        print(entry, file=out)
            for entry in log_errors:
                print(entry, file=out)
            out.flush()
            target = directory / filename if filename is not None else pending.with_suffix(".txt")
            _publish_export(root / "state" / "accounts.sqlite3", pending, target,
                            {turn["owner_user_id"] for turn in turns})
        finally:
            pending.unlink(missing_ok=True)
    for job_id, calls, answers in summaries:
        print(f"{job_id}: {calls} 条工具记录，{answers} 条最终回答")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    select = parser.add_mutually_exclusive_group()
    select.add_argument("--job", action="append", help="根任务 ID，可重复")
    select.add_argument("--latest", type=int, default=1, help="最近结束的任务数，默认 1，最多 100")
    select.add_argument("--today", action="store_const", const=datetime.now(SHANGHAI).date(), dest="date",
                        help="汇总北京时间今天所有账号已受理的轮次")
    select.add_argument("--date", type=date.fromisoformat, help="汇总指定北京时间日期 YYYY-MM-DD 所有账号已受理的轮次")
    parser.add_argument("--owner", type=int, help="与 --today 或 --date 配合，仅汇总指定账号 ID")
    parser.add_argument("--runtime", type=Path, help="覆盖通过 systemd 识别的运行目录")
    parser.add_argument("--full", action="store_true", help="显式允许导出私有问答与工具参数，须指定 --out-dir")
    parser.add_argument("--out-dir", type=Path, help="当前用户拥有的私有输出目录（0700）")
    args = parser.parse_args()
    if args.full and args.out_dir is None:
        parser.error("--full 需要显式指定私有 --out-dir")
    out_dir = args.out_dir or Path.home() / "citrusscholar" / "log-exports"
    if not 1 <= args.latest <= 100 or (args.job and (len(args.job) > 100 or any(not JOB_ID.fullmatch(job) for job in args.job))):
        parser.error("请提供有效的根任务 ID，或 1–100 之间的 --latest")
    if args.owner is not None and (args.date is None or args.owner < 1):
        parser.error("--owner 仅与 --today 或 --date 一起使用，账号 ID 必须为正整数")
    root = (args.runtime or runtime_root()).resolve(strict=True)
    database = root / "state" / "sessions.db"
    uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        accounts = root / "state" / "accounts.sqlite3"
        conn.execute("ATTACH DATABASE ? AS auth_source", (accounts.as_uri() + "?mode=ro",))
        conn.execute("BEGIN")
        if args.job:
            turns = [conn.execute(_VISIBLE_QUEUE + " AND q.root_job_id=?", (job,)).fetchone()
                     for job in dict.fromkeys(args.job)]
            if any(turn is None for turn in turns):
                parser.error("至少一个根任务 ID 不存在")
        elif args.date is not None:
            start = datetime.combine(args.date, time.min, SHANGHAI).timestamp()
            end = datetime.combine(args.date + timedelta(days=1), time.min, SHANGHAI).timestamp()
            sql = _VISIBLE_QUEUE + " AND q.accepted_at>=? AND q.accepted_at<?"
            params = (start, end)
            if args.owner is not None:
                sql += " AND q.owner_user_id=?"
                params += (args.owner,)
            turns = conn.execute(sql + " ORDER BY q.accepted_at,q.owner_user_id,q.session_id,q.queue_seq",
                                 params).fetchall()
        else:
            turns = conn.execute(_VISIBLE_QUEUE + " AND q.finished_at>0 AND q.root_job_id!='' "
                                 "ORDER BY q.finished_at DESC LIMIT ?", (args.latest,)).fetchall()
        if not turns and (args.date is None or args.owner is not None):
            parser.error("没有可导出的任务")
        selected = [(turn["root_job_id"], turn["owner_user_id"], turn["session_id"]) for turn in turns]
        job_roots = _log_jobs(conn, turns)
        conn.rollback()  # do not pin either SQLite snapshot while scanning old log files
        log_index, log_errors = correlated_logs(root / "logs", job_roots)
        conn.execute("BEGIN")
        turns = [conn.execute(_VISIBLE_QUEUE + " AND q.root_job_id=? AND q.owner_user_id=? AND q.session_id=?",
                              key).fetchone() for key in selected]
        if any(turn is None for turn in turns):
            raise ExportScopeChanged("export scope changed; run the export again")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Database-derived IDs never become file components without validation.
        prefix = f"8043-{len(turns)}-turns-{stamp}-"
        scope = filename = None
        if args.date is not None:
            if args.owner is None:
                filename = f"8043-{args.date}.txt"
                owner_count = len({turn["owner_user_id"] for turn in turns})
                account_scope = f"所有账号（本日有记录的账号 {owner_count} 个）"
            else:
                filename = f"8043-{args.date}-owner-{args.owner}.txt"
                account_scope = f"账号 {args.owner}"
            if args.full:
                filename = filename.removesuffix(".txt") + "-full.txt"
            scope = f"{args.date}（北京时间）{account_scope}，共 {len(turns)} 轮已受理请求"
        try:
            target = save_export(conn, root, turns, out_dir, prefix, filename=filename, scope=scope,
                                 full=args.full, log_index=log_index, log_errors=log_errors)
        except ValueError as error:
            parser.error(str(error))
    print(target.absolute())


if __name__ == "__main__":
    try:
        main()
    except (ExportScopeChanged, sqlite3.Error):
        # Incomplete schema/unreadable identity DB is not authorization to export.
        raise SystemExit("无法确认导出范围，请核对运行目录和账号状态后重试。") from None
