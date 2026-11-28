"""Offline acceptance for E6. Only synthetic public sources and scopes are used."""
from src.graph.retrieval_router import decide_retrieval, parse_request_constraints
from src.retrieval.evidence_adapter import _declared_source_hash
from src.retrieval.evidence_contract import evidence_contract
from src.retrieval.turn_budget import RetrievalLimits, TurnRetrievalBudget
from src.retrieval.turn_dedup import RetrievalLedger


_LOCAL = "local_hybrid_search"
_SEARCH = "deepseek_search"
_FETCH = "deepseek_fetch"
_URL = "https://example.org/paper"


def _local(generation="g1", include_ref=True):
    row = {"title": "Fixture", "text": "never duplicate the stored body", "citation_ref": "L1",
           "locator": {"corpus_id": "methods", "paper_id": "paper", "chunk_id": "chunk",
                       "index_generation": "g1", "input_sha256": "b" * 64}}
    if include_ref:
        row.update(source_ref="ev:fixture", read_entry={"ref": "ev:fixture", "turn_id": "owner-turn",
                                                        "generation": "g1"})
    return {"main_results": [row], "base_generation": generation}


def _web(url=_URL, **row):
    return {"web_results": [{"url": url, "title": "Fixture", "citation_ref": "W1", **row}]}


def test_pure_transform_does_not_start_retrieval_but_can_reuse_history():
    parsed = parse_request_constraints("把上面的说明翻译成英文。正文：请联网搜索")
    assert parsed.source_policy == "deny"
    decision = decide_retrieval("翻译上述说明", has_history_evidence=True, recent_turn_seqs=[4])
    assert (decision.mode, decision.policy, decision.history_turn_seqs) == ("reuse", "deny", (4,))
    mixed = decide_retrieval("翻译上述说明并查找最新公开文献")
    assert (mixed.mode, mixed.policy) == ("web", "required")
    assert decide_retrieval("计算最新利率的差额").mode == "web"
    assert decide_retrieval("What is quality control?", has_history_evidence=True, recent_turn_seqs=[4]).mode == "auto"


def test_local_contract_is_scoped_generation_bound_and_never_invents_source_hash():
    view = evidence_contract(_LOCAL, _local(), status="complete")
    assert view["index_snapshot"] == [{"corpus_ref": "methods", "generation": "g1"}]
    assert view["scope"] == ["shared_published"]
    row, = view["results"]
    assert (row["document_ref"], row["chunk_ref"], row["citation_ref"]) == ("paper", "chunk", "L1")
    assert row["source_hash"] is None and row["text_sha256"] == "b" * 64
    assert "source_hash_unavailable" in view["limitations"]
    assert "never duplicate" not in repr(view) and "owner-turn" not in repr(view)
    assert _declared_source_hash({"source_sha256": "A" * 64}) == "a" * 64
    assert _declared_source_hash({"source_hash": "not a sha256"}) is None
    assert not evidence_contract(_LOCAL, _local("changed"))["results"]
    assert not evidence_contract(_LOCAL, _local(include_ref=False))["results"]


def test_web_adapter_keeps_discovery_fetch_browser_and_paper_evidence_distinct():
    search = evidence_contract(_SEARCH, _web(content="snippet, not verified body"), provider="engine")
    fetch = evidence_contract(_FETCH, _web(content="fetched body"))
    browser = evidence_contract("browse_web", _web() | {"_browse_evidence": {"rendered_text_sha256": "a" * 64}})
    paper = evidence_contract("deepseek_fetch_paper", _web(text="abstract") | {"fetch_status": "abstract_only"})
    assert [view["results"][0]["source_status"] for view in (search, fetch, browser, paper)] == [
        "discovered", "fetched", "rendered", "partial"]
    assert [view["provider"] for view in (search, fetch, browser, paper)] == [
        "web_search:engine", "web_fetch", "browser", "paper_fetch"]
    assert "fetched body" not in repr(fetch)
    assert not evidence_contract(_SEARCH, _web("https://user:password@example.org"))["results"]
    assert evidence_contract(_FETCH, {}, status="failed", reason="network_timeout")["status"] == "timeout"
    assert evidence_contract(_LOCAL, {"main_results": [], "local_status": "no_authorized_corpus"})["status"] == "permission_denied"
    no_results = evidence_contract(_SEARCH, {"web_results": []}, status="no_results")
    assert (no_results["status"], no_results["scope"]) == ("no_results", ["public_web"])
    browser_error = {"action": "open", "error": "browse_timed_out", "_tool_outcome": {
        "status": "failed", "reason_code": "browse_timed_out",
    }}
    assert evidence_contract("browse_web", browser_error)["status"] == "timeout"
    assert evidence_contract("browse_web", browser_error | {"action": "download"}) is None
    assert evidence_contract("draw_svg", _web()) is None


def test_duplicate_results_stop_only_their_lane_and_keep_readback_available():
    ledger = RetrievalLedger()
    assert not ledger.admit(_SEARCH, "a", {})
    assert not ledger.admit(_LOCAL, "b", {})
    ledger.observe(_SEARCH, "a", _web("https://EXAMPLE.org:443/paper#top"), status="complete", reason="")
    ledger.observe(_LOCAL, "b", _local(), status="complete", reason="")
    assert not ledger.admit(_SEARCH, "new-words", {})
    ledger.observe(_SEARCH, "new-words", _web(), status="complete", reason="")
    assert ledger.admit(_SEARCH, "stop", {}) == "retrieval_no_new_evidence"
    assert not ledger.admit(_FETCH, "read-source", {})
    assert not ledger.admit("browse_web", "browser-source", {"action": "open"})
    assert not ledger.admit("read_candidate", "read-local", {})
    assert not ledger.admit("read_tool_result", "existing-receipt", {})
    assert ledger.summary()["status"] == "partial"
    assert ledger.summary()["unresolved_lanes"] == ["web_search"]
    assert ledger.summary()["coverage"] == {"local": 1, "web_search": 1}


def test_discovery_cap_and_global_cap_do_not_treat_failures_as_empty_results():
    ledger = RetrievalLedger(budget=TurnRetrievalBudget(RetrievalLimits(calls=4)))
    for i in range(3):
        assert ledger.admit(_SEARCH, str(i), {}) is None
        ledger.observe(_SEARCH, str(i), {}, status="failed", reason="timeout")
    assert ledger.admit(_SEARCH, "too-many", {}) == "retrieval_followup_budget_exhausted"
    assert ledger.admit(_LOCAL, "local", {}) is None
    assert ledger.admit(_FETCH, "over-call-budget", {}) == "retrieval_call_budget_exhausted"
    assert ledger.admit("browse_web", "cleanup", {"action": "close"}) is None
    assert ledger.admit("browse_web", "authorized-download", {"action": "download"}) is None
    assert ledger.summary()["limitations"] == ["timeout"]


def test_budget_restore_counts_verified_observations_once_and_keeps_deadline(monkeypatch):
    from src.retrieval import turn_budget
    monotonic, epoch = [10.0], [1000.0]
    monkeypatch.setattr(turn_budget.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(turn_budget.time, "time", lambda: epoch[0])
    prior = RetrievalLedger()
    assert prior.admit(_SEARCH, "saved", {}) is None
    monotonic[0], epoch[0] = 20.0, 1061.0
    ledger = RetrievalLedger()
    ledger.restore_timer_state(prior.timer_state())
    for _ in range(2):
        ledger.restore_admission(_SEARCH, "saved", {})
        ledger.observe(_SEARCH, "saved", _web(), status="complete", reason="", content="abc")
    assert ledger.summary()["calls"] == {"web_search": 1}
    assert ledger.summary()["estimated_observed_tokens"] == 1
    assert ledger.admit(_FETCH, "too-late", {}) == "retrieval_deadline_exhausted"
    assert RetrievalLedger().admit(_FETCH, "new-turn", {}) is None


def test_observed_token_cap_stops_new_work_without_truncating_receipt():
    ledger = RetrievalLedger(budget=TurnRetrievalBudget(RetrievalLimits(observed_tokens=3)))
    assert not ledger.admit(_FETCH, "large", {})
    artifact = _web(text="full original receipt")
    ledger.observe(_FETCH, "large", artifact, status="complete", reason="")
    assert ledger.admit(_LOCAL, "stopped", {}) == "retrieval_token_budget_exhausted"
    assert artifact["web_results"][0]["text"] == "full original receipt"
    assert not ledger.admit("read_local_evidence", "read-back", {})


def test_budget_denial_never_creates_a_false_existing_result():
    ledger = RetrievalLedger(budget=TurnRetrievalBudget(RetrievalLimits(calls=1)))
    assert not ledger.admit(_LOCAL, "used-budget", {})
    args = {"query": "distinct research topic"}
    for call_id in ("blocked", "blocked-again"):
        assert ledger.claim(_SEARCH, args, call_id) is None
        assert ledger.admit(_SEARCH, call_id, args) == "retrieval_call_budget_exhausted"



def test_preflight_release_keeps_discovery_credit_and_exact_tool_visibility():
    ledger = RetrievalLedger()
    names = {_LOCAL, _SEARCH, _FETCH, "browse_web", "read_local_evidence", "inspect_uploaded_table"}
    args = {"query": "queries only charge on verified dispatch"}
    assert ledger.claim(_LOCAL, args, "proposal") is None
    assert ledger.reserve(_LOCAL, "proposal", args) is None
    assert ledger.timer_state() is None and ledger.summary()["calls"] == {}
    ledger.release_unexecuted(_LOCAL, "proposal", args)
    assert ledger.claim(_LOCAL, args, "fixed") is None
    assert ledger.reserve(_LOCAL, "fixed", args) is None
    ledger.restore_admission(_LOCAL, "fixed", args)
    for i in ("followup-1", "followup-2"):
        assert ledger.reserve(_LOCAL, i, {}) is None
    assert _LOCAL not in ledger.budget.visible_tools(names)
    ledger.release_unexecuted(_LOCAL, "followup-1", {})
    assert ledger.budget.visible_tools(names) == names
    ledger.restore_admission(_LOCAL, "followup-2", {})
    assert ledger.reserve(_LOCAL, "followup-3", {}) is None
    ledger.restore_admission(_LOCAL, "followup-3", {})
    assert ledger.budget.visible_tools(names) == names - {_LOCAL}
    assert ledger.summary()["calls"] == {"local": 3}
    assert ledger.timer_state() is not None


def test_global_exhaustion_preserves_existing_evidence_and_browser_cleanup():
    ledger = RetrievalLedger(budget=TurnRetrievalBudget(RetrievalLimits(calls=1)))
    assert ledger.reserve(_LOCAL, "first", {}) is None
    ledger.restore_admission(_LOCAL, "first", {})
    names = {_LOCAL, _SEARCH, _FETCH, "browse_web", "read_candidate", "read_local_evidence",
             "read_tool_result", "run_research_figure_script"}
    assert ledger.budget.visible_tools(names) == names - {_LOCAL, _SEARCH, _FETCH}
    assert ledger.reserve("browse_web", "navigate", {"action": "open"}) == "retrieval_call_budget_exhausted"
    assert ledger.reserve("browse_web", "cleanup", {"action": "close"}) is None
    assert ledger.reserve("read_local_evidence", "same-passage", {}) is None
    assert ledger.summary()["calls"] == {"local": 1}


def test_uncitable_passages_are_not_a_verified_empty_search():
    ledger = RetrievalLedger()
    for i in ("excerpt-only-1", "excerpt-only-2"):
        assert ledger.admit(_LOCAL, i, {}) is None
        ledger.observe(_LOCAL, i, {"corpus_contract": 1, "main_results": [], "selected_result_count": 1},
                       status="complete", reason="")
    assert ledger.admit(_LOCAL, "additional", {}) is None
