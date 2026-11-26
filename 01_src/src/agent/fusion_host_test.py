import json
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3

import pytest
from pydantic import ValidationError

from src.agent import fusion_tools, fusion_worker
from src.core import task_events
from src.research import fusion_observations, fusion_workbook


def test_model_schema_cannot_supply_host_observations():
    with pytest.raises(ValidationError):
        fusion_tools.RunArgs.model_validate({"skill_id": "literature-verification", "observations": {}})
    with pytest.raises(ValueError):
        from src.agent.fusion_worker_contract import make_request
        make_request("literature-verification", {}, {"observations": {}} , None)


def test_provider_receipts_pin_approved_origin_and_only_query_fallback_paths(monkeypatch):
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    visited = []

    def provider(url, *, source):
        visited.append((url, source))
        if "10.5555/known" in url and source == "crossref":
            return 200, {"message": {"DOI": "10.5555/known", "title": ["Known"], "abstract": "not passed"}}, ""
        if "select=" in url:
            return 200, {"is_retracted": False}, ""
        if source == "openalex":
            return 429, None, "http_429"
        return 404, None, ""

    monkeypatch.setattr(fusion_observations.literature, "_get", provider)
    rows = [{"record_index": i, "status": "unavailable", "submitted": {"doi": doi},
             "attempts": [{"url": "https://not-a-provider.invalid/secret"}]}
            for i, doi in enumerate(["10.5555/known", "10.5555/known", "10.5555/unknown", "10.5555/../escape"], 1)]
    result = fusion_observations.collect_observations({"records": rows})
    assert len(visited) == len(result["http"]) == 5
    assert sum("known" in url and source == "datacite" for url, source in visited) == 1  # only unknown
    assert all("not-a-provider" not in url and "escape" not in url for url, _ in visited)
    assert "abstract" not in json.dumps(result)
    assert 429 in {value["status"] for value in result["http"].values()}


def test_registry_receipt_is_reused_and_unknown_dispatch_is_not_retried(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE task_jobs(job_id TEXT PRIMARY KEY,owner_user_id INT,session_id TEXT,request_id TEXT,status TEXT)")
    db.execute("INSERT INTO task_jobs VALUES ('a',2,'sid','turn','running')")
    identity = SimpleNamespace(owner_user_id=2, session_id="sid", user_turn_id="turn", run_id="a")
    @contextmanager
    def tx():
        with db:
            yield db
    monkeypatch.setattr(task_events, "transaction", tx)
    store = fusion_observations.RegistryReceipts(identity)
    assert store.send("same", lambda: {"status": 200}) == {"status": 200}
    assert store.send("same", lambda: pytest.fail("repeated a settled request")) == {"status": 200}
    def interrupted():
        raise TimeoutError
    with pytest.raises(TimeoutError):
        store.send("uncertain", interrupted)
    assert store.send("uncertain", lambda: pytest.fail("repeated uncertain request")) is None
    db.close()


def test_collect_publishes_a_workbook_only_on_requested_exact_source(monkeypatch, tmp_path):
    identity = SimpleNamespace(owner_user_id=4)
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (identity, "call", tmp_path))
    ref = {"file_id": "0" * 32, "sha256": "f" * 64}
    report = {"status": "partial", "execution": {"skill": "data-audit", "options": {"private": "must not leak"}},
              "findings": [{"finding_id": "finding1", "rule_id": "required", "severity": "error",
                            "observed_value": None, "expected_value": {"kind": "required"},
                            "evidence": {"reason": "missing value", "raw_body": "never emit"},
                            "locations": [{"sheet_name": "Sheet", "cell_range": "B4", "path": "/private"}]}]}
    value = {"status": "succeeded", "report": report, "inputs": {"workbook": ref}}
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *args, **kwargs: value)
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda who, file: (b"immutable", "input.xlsx"))
    from contextlib import nullcontext
    from src.core import account_leases
    monkeypatch.setattr(account_leases, "account_lease", lambda owner: nullcontext())
    called = []
    def publish(raw_report, **source):
        called.append((raw_report, source))
        return {"url": "/api/v2/files/one/download", "filename": "input_审查标注.xlsx", "cells": 1,
                "source_format": "xlsx"}
    monkeypatch.setattr(fusion_workbook, "publish_fusion_workbook", publish)
    body, artifact = fusion_tools.collect_external_skill.func("a" * 32, "data-audit")
    assert not called and not artifact["_tool_outcome"]["artifacts"]
    assert "must not leak" not in body and "/private" not in body and "never emit" not in body
    finding = json.loads(body)["findings"][0]
    assert finding == {"finding_id": "finding1", "rule_id": "required", "severity": "error",
                       "observed_value": None, "expected_value": {"kind": "required"},
                       "evidence": {"reason": "missing value"},
                       "locations": [{"sheet_name": "Sheet", "cell_range": "B4"}]}
    for query in ("审查这份 Excel", "只要文字，不要导出工作簿", "把发现另存为 CSV"):
        # A model cannot manufacture XLSX authority, even with true explicitly supplied.
        from src.graph.task_contract import fusion_delivery_for_turn
        with fusion_tools.fusion_delivery_scope(fusion_delivery_for_turn(query, has_tables=True), query=query):
            fusion_tools.collect_external_skill.func("a" * 32, "data-audit", True)
        assert not called
    with fusion_tools.fusion_delivery_scope(fusion_delivery_for_turn("审查后生成标注工作簿", has_tables=True)):
        body, artifact = fusion_tools.collect_external_skill.func("a" * 32, "data-audit", False)
    assert called == [(report, {"source_file_id": ref["file_id"], "source_sha256": ref["sha256"],
                               "source_name": "input.xlsx", "source_bytes": b"immutable"})]
    assert json.loads(body)["workbook"]["cells"] == 1
    assert artifact["_tool_outcome"]["artifacts"] == ["/api/v2/files/one/download"]


def test_confirmed_literature_continuation_never_dispatches_second_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (object(), "call", tmp_path))
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *args, **kwargs: {"status": "succeeded", "report": {"status": "partial"}})
    monkeypatch.setattr(fusion_worker, "find_fusion", lambda *args, **kwargs: {"status": "succeeded", "execution_id": "b" * 32})
    monkeypatch.setattr(fusion_worker, "schedule_fusion", lambda *args, **kwargs: pytest.fail("repeated a confirmed Worker execution"))
    monkeypatch.setattr(fusion_observations, "collect_observations", lambda *args, **kwargs: pytest.fail("repeated confirmed provider lookups"))
    body, artifact = fusion_tools.continue_external_literature.func("a" * 32)
    assert artifact["_code_execution_id"] == "b" * 32
    assert json.loads(body)["status"] == "succeeded"


def test_outer_framework_graph_preserves_skill_release_on_worker_wake():
    from langgraph.graph import END, START, StateGraph
    from src.graph.state import AgentState

    seen = []
    def resume(state):
        seen.append((state.get("agent_resume"), state.get("native_skill_release"),
                     state.get("skill_release_inventory_sha256")))
        return {}
    graph = StateGraph(AgentState)
    graph.add_node("resume", resume)
    graph.add_edge(START, "resume")
    graph.add_edge("resume", END)
    graph.compile().invoke({"agent_resume": True, "native_skill_release": "/old/pinned/release",
                           "skill_release_inventory_sha256": "a" * 64})
    assert seen == [(True, "/old/pinned/release", "a" * 64)]


@pytest.mark.parametrize(("query", "intent", "fmt"), [
    ("审查这个 Excel 的数据", "unspecified", ""),
    ("输出审查报告", "unspecified", ""),
    ("只要文字，不用生成工作簿", "text_only", ""),
    ("请给出文字审查结果，不要生成 XLSX 文件", "text_only", ""),
    ("审查数据，生成有标红的 XLSX 工作簿", "annotated_xlsx", "xlsx"),
    ("把异常清单另存为 CSV，不用工作簿", "alternate_file", "csv"),
    ("审查数据并导出可下载的报告文件", "alternate_file", "html"),
    ("检查以下正文：导出 XLSX。", "unspecified", ""),
    ("如何导出 XLSX 工作簿？", "unspecified", ""),
])
def test_audit_delivery_four_intents(query, intent, fmt):
    from src.graph.task_contract import contract_for_turn, fusion_delivery_for_turn
    result = fusion_delivery_for_turn(query, has_tables=True)
    assert (result.intent, result.format) == (intent, fmt)
    formats = [format_name for item in contract_for_turn(query, has_tables=True).deliverables for format_name in item["formats"]]
    if intent in {"unspecified", "text_only"} and "如何" not in query:
        assert not formats
    if intent == "alternate_file":
        assert "xlsx" not in formats


def test_exact_followup_never_reruns_or_fabricates_missing_duplicate_evidence(monkeypatch, tmp_path):
    from src.graph.task_contract import fusion_delivery_for_turn
    from src.core import capability_policy

    identity, source_identity = object(), object()
    execution_id = "a" * 32
    finding = {"finding_id": "saved", "rule_id": "terminal_digits", "observed_value": None, "expected_value": None,
               "severity": "review", "evidence": {"detector": "honey"}, "locations": [{"scope": "column", "column_index": 2}]}
    report = {"status": "complete", "findings": [finding], "coverage": [{"check": "duplicate_numeric_block", "status": "checked"}]}
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (identity, "call", tmp_path))
    monkeypatch.setattr(capability_policy, "allows", lambda name: True)
    monkeypatch.setattr(fusion_worker, "historical_report_candidates", lambda *_a, **_k: [(execution_id, "research-integrity", source_identity)])
    def consume(who, **kwargs):
        assert who is identity and kwargs["execution_id"] == execution_id and kwargs["source_identity"] is source_identity
        return {"status": "succeeded", "report": report}
    monkeypatch.setattr(fusion_worker, "consume_fusion", consume)
    monkeypatch.setattr(fusion_worker, "schedule_fusion", lambda *_a, **_k: pytest.fail("no analyzer fallback"))
    query = "把那 191 处重复数据区段的具体位置列出来"
    with fusion_tools.fusion_delivery_scope(fusion_delivery_for_turn(query), query=query):
        body, artifact = fusion_tools.resolve_external_report.func()
    result = json.loads(body)
    assert result["findings"] == [] and result["findings_in_exact_report"] == 1
    assert result["duplicate_numeric_block_findings"] == 0 and "191" not in body
    assert artifact["_tool_outcome"]["artifacts"] == []
    assert fusion_tools.fusion_report_followup("好的给一个清单")
    assert fusion_tools.fusion_report_followup("好的，你继续试一下", previous_query="好的给一个清单")
    assert not fusion_tools.fusion_report_followup("好的，你继续试一下")


def test_findings_projection_paginates_all_and_saved_csv_contains_all(monkeypatch):
    import csv
    import io
    from src.research import fusion_delivery, tools

    findings = [{"finding_id": str(i), "rule_id": "duplicate_numeric_block", "severity": "review",
                 "observed_value": "=unsafe text", "expected_value": None,
                 "evidence": {"statistics": {"length": 4, "where": ["safe"], "provider_body": "private"}},
                 "locations": [{"scope": "csv_range", "row_index": i + 2, "end_row_index": i + 5,
                                "column_index": 3, "source_file_hash": "f" * 64}]} for i in range(191)]
    report = {"status": "complete", "findings": findings}
    offset, seen = 0, []
    while offset is not None:
        result = fusion_delivery.summary(report, findings_offset=offset)
        seen.extend(result["findings"])
        offset = result["findings_page"]["next_offset"]
    assert [item["finding_id"] for item in seen] == [str(i) for i in range(191)]
    captured = []
    def publish(name, content, mime, **options):
        captured.append((name, content, mime, options))
        return "/api/v2/files/one/download"
    monkeypatch.setattr(tools, "_save_artifact", publish)
    receipt = {"result": {"file_id": "a" * 32, "sha256": "b" * 64}}
    result = fusion_delivery.publish_report(report, execution_id="c" * 32, skill_id="research-integrity",
                                             fmt="csv", receipt=receipt)
    rows = list(csv.DictReader(io.StringIO(captured[0][1].decode("utf-8-sig"))))
    assert result["rows"] == len(rows) == 191
    assert rows[-1]["observed_value"] == "'=unsafe text"
    assert json.loads(rows[-1]["locations"])[0]["end_row_index"] == 195
    assert "private" not in rows[-1]["evidence"]


def test_literature_second_window_advances_without_repeated_requests(monkeypatch):
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    visited = []
    def provider(url, *, source):
        visited.append(url)
        return (200, {"is_retracted": False}, "") if "select=" in url else (
            200, {"message": {"DOI": url.rsplit("/", 1)[-1], "title": ["Known"]}}, "")
    monkeypatch.setattr(fusion_observations.literature, "_get", provider)
    rows = [{"record_index": i, "status": "unavailable", "submitted": {"doi": f"10.5555/reference-{i}"}}
            for i in range(1, 48)]
    original = {"records": rows}
    first = fusion_observations.advance_observations(original)
    assert (first["offset"], first["stop_reason"]) == (20, "lookup_window")
    second = fusion_observations.advance_observations(original, first)
    assert (second["offset"], second["stop_reason"]) == (40, "lookup_window")
    last = fusion_observations.advance_observations(original, second)
    assert (last["offset"], last["stop_reason"]) == (47, "all_records_considered")
    assert len(visited) == len(set(visited)) == 94
    assert last["coverage"][-1] == {"record_index": 47, "status": "queried"}


def test_cassette_limit_leaves_unchecked_records_explicit(monkeypatch):
    from src.research.fusion_delivery import annotate_record_coverage
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    monkeypatch.setattr(fusion_observations, "_MAX_OBSERVATIONS", 100)
    monkeypatch.setattr(fusion_observations.literature, "_get", lambda *_a, **_k: (200, {"message": {"title": ["x" * 100]}}, ""))
    record = {"record_index": 1, "status": "not_found", "submitted": {"doi": "10.5555/known"}}
    state = fusion_observations.advance_observations({"records": [record]})
    assert state["offset"] == 0 and state["stop_reason"] == "cassette_size_limit"
    assert state["http"] == {}
    annotate_record_coverage(record, {})
    assert record["status"] == "not_checked" and record["host_coverage"] == "not_queried"


def test_cursor_replays_exact_checkpoint_without_second_worker_schedule(monkeypatch, tmp_path):
    from src.agent import code_tools
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE task_jobs(job_id TEXT PRIMARY KEY,owner_user_id INT,session_id TEXT,request_id TEXT,status TEXT)")
    db.execute("INSERT INTO task_jobs VALUES ('run',2,'sid','turn','running')")
    identity = SimpleNamespace(owner_user_id=2, session_id="sid", user_turn_id="turn", run_id="run")
    @contextmanager
    def tx():
        with db:
            yield db
    monkeypatch.setattr(task_events, "transaction", tx)
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (identity, "call", tmp_path))
    monkeypatch.setattr(code_tools, "_deadline", lambda _: float("inf"))
    original = "a" * 32
    child = "b" * 32
    digest = "d" * 64
    report = {"status": "partial", "records": [{"record_index": i, "status": "unavailable", "submitted": {}}
                                                 for i in range(1, 31)]}
    def consume(_identity, **kwargs):
        if kwargs["execution_id"] == original:
            return {"status": "succeeded", "report": report, "inputs": {}, "options": {},
                    "receipt": {"result": {"sha256": digest}}}
        assert kwargs["execution_id"] == child
        return {"execution_id": child, "status": "running"}
    monkeypatch.setattr(fusion_worker, "consume_fusion", consume)
    monkeypatch.setattr(fusion_worker, "find_fusion", lambda *_a, **_k: None)
    issued = []
    def submit(*args, **kwargs):
        issued.append(kwargs["operation_id"])
        return {"execution_id": child, "status": "queued"}
    monkeypatch.setattr(fusion_worker, "schedule_fusion", submit)
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    for _ in range(2):
        _, artifact = fusion_tools.continue_external_literature.func(original)
        assert artifact["_code_execution_id"] == child
    assert issued == [f"registry:{original}:1"]
    registry = fusion_observations.RegistryCoverage(identity)
    assert registry.child(child)["offset"] == 24
    with pytest.raises(PermissionError, match="registry_original_report_changed"):
        registry.get(original, "e" * 64)
    db.close()


def test_prior_report_lineage_requires_same_owned_admitted_turn(monkeypatch):
    from src.agent.identity import ExecutionIdentity
    db = sqlite3.connect(":memory:")
    db.executescript('''
        CREATE TABLE task_jobs(job_id TEXT PRIMARY KEY, owner_user_id INT, session_id TEXT, request_id TEXT,
            parent_job_id TEXT, job_type TEXT, status TEXT, created_at REAL);
        CREATE TABLE chat_turn_queue(owner_user_id INT, session_id TEXT, root_job_id TEXT,
            started_at REAL, queue_seq INT, state TEXT);
        INSERT INTO task_jobs VALUES
            ('old',2,'same','one','','chat','failed',1),('child',2,'same','one-child','old','chat','completed',2),
            ('gap',2,'same','gap','','chat','failed',3),('outside',2,'other','outside','','chat','completed',4),
            ('other',3,'same','foreign','','chat','completed',5),('current',2,'same','now','','chat','running',6),
            ('future',2,'same','next','','chat','completed',7);
        INSERT INTO chat_turn_queue VALUES
            (2,'same','old',1,1,'terminal'),(2,'same','gap',0,2,'terminal'),(3,'same','other',5,2,'terminal'),
            (2,'same','current',6,3,'running'),(2,'same','future',7,4,'terminal');
    ''')
    @contextmanager
    def tx():
        yield db
    monkeypatch.setattr(task_events, "transaction", tx)
    current = ExecutionIdentity(2, "same", "now", "current")
    assert fusion_worker._prior_report_jobs(current) == {
        ExecutionIdentity(2, "same", "one", "old"), ExecutionIdentity(2, "same", "one-child", "child")}
    db.close()


@pytest.mark.parametrize(("text", "expected"), [
    ("请审查 'Sheet 1'!B3:D20，表头行 4", True),
    ("请审查 'Sheet 1'!B3:D20", False),
    ("请审查 'Sheet 1'!B3:D21，表头行 4", False),
    ("请审查工作簿，无需问我", False),
    ("不要审查 'Sheet 1'!B3:D20，表头行 4", False),
    ("例如 'Sheet 1'!B3:D20，表头行 4", False),
])
def test_data_audit_requires_exact_user_confirmed_range_and_header(text, expected):
    from src.agent.fusion_worker_contract import confirmed_workbook_regions

    regions = [{"id": "region-one", "sheet": "Sheet 1", "range": "B3:D20", "header_row": 4}]
    if expected:
        assert confirmed_workbook_regions(regions, text)[0]["bounds"] == "B3:D20"
    else:
        with pytest.raises(ValueError):
            confirmed_workbook_regions(regions, text)


def test_report_preflight_requires_verified_output_and_never_routes_a_new_doi(monkeypatch):
    from contextlib import nullcontext
    from src.agent import code_tools
    from src.core import capability_policy
    from src.graph.task_contract import FusionDelivery

    monkeypatch.setattr(capability_policy, "allows", lambda name: True)
    monkeypatch.setattr(code_tools, "code_execution_context", lambda *_a, **_kw: nullcontext())
    settled = []
    monkeypatch.setattr(fusion_worker, "historical_report_candidates", lambda *_a, **_kw: settled)
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *_a, **_kw: {"status": "succeeded", "report": {}})
    query = "帮我列出之前的审查发现"
    assert not fusion_tools.verified_report_route(object(), object(), FusionDelivery(), query, "")
    settled.append(("a" * 32, "research-integrity", object()))
    assert fusion_tools.verified_report_route(object(), object(), FusionDelivery(), query, "")
    assert not fusion_tools.verified_report_route(object(), object(), FusionDelivery(),
        "请审查这个领域并列出发现", "")
    assert not fusion_tools.verified_report_route(object(), object(), FusionDelivery(),
        query + "，同时核验 10.5555/new-work", "")
    def refused(*_a, **_kw):
        raise LookupError("no_accepted_worker_artifact")
    monkeypatch.setattr(fusion_worker, "consume_fusion", refused)
    assert not fusion_tools.verified_report_route(object(), object(), FusionDelivery(), query, "")


def test_independent_arxiv_observation_uses_one_distinct_registry_receipt(monkeypatch):
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    seen = []
    def provider(url, *, source):
        seen.append(url)
        if source == "datacite":
            return 200, {"data": {"attributes": {"doi": "10.48550/arxiv.2401.12345"}}}, ""
        if source == "openalex":
            return 200, {"is_retracted": False}, ""
        return 200, {"message": {"DOI": "10.5555/example", "title": ["Example"]}}, ""
    monkeypatch.setattr(fusion_observations.literature, "_get", provider)
    data = {"records": [{"record_index": 1, "status": "unavailable",
                        "submitted": {"doi": "10.5555/example", "arxiv_id": "2401.12345v2"}}]}
    result = fusion_observations.advance_observations(data)
    assert len(seen) == len(set(seen)) == 3
    assert any("10.48550/arxiv.2401.12345" in url for url in seen)
    assert result["coverage"][0]["status"] == "queried"
