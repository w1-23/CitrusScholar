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
              "findings": [{"detail": "missing value", "locations": [{"sheet": "Sheet", "cell": "B4", "path": "/private"}]}]}
    value = {"status": "succeeded", "report": report, "inputs": {"workbook": ref}}
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *args, **kwargs: value)
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda who, file: (b"immutable", "input.xlsx"))
    from contextlib import nullcontext
    from src.core import account_leases
    monkeypatch.setattr(account_leases, "account_lease", lambda owner: nullcontext())
    called = []
    def publish(raw_report, **source):
        called.append((raw_report, source))
        return {"url": "/api/v2/files/one/download", "filename": "input_审查标注.xlsx", "cells": 1}
    monkeypatch.setattr(fusion_workbook, "publish_fusion_workbook", publish)
    body, artifact = fusion_tools.collect_external_skill.func("a" * 32, "data-audit")
    assert not called and not artifact["_tool_outcome"]["artifacts"]
    assert "must not leak" not in body and "/private" not in body
    body, artifact = fusion_tools.collect_external_skill.func("a" * 32, "data-audit", True)
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
