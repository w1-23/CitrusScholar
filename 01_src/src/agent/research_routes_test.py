"""Deterministic tests for the bounded research dual-route gate."""
from src.agent.research_routes import research_route_gate

BOTH = {"local_hybrid_search", "deepseek_search"}


def row(name, status="complete", **extra):
    return {"name": name, "status": status, **extra}


def test_no_discovery_is_not_gated_so_direct_reuse_can_finish():
    gate = research_route_gate([], BOTH)
    assert not gate.active


def test_local_discovery_requires_one_deepseek_search():
    gate = research_route_gate([row("local_hybrid_search")], BOTH)
    assert gate.active and gate.missing == ("web",)
    assert not gate.challenge and not gate.message
    assert "DeepSeek 网络 Search" in gate.notice


def test_deepseek_search_requires_one_local_attempt():
    gate = research_route_gate([row("deepseek_search")], BOTH)
    assert gate.active and gate.missing == ("local",)
    assert not gate.challenge and not gate.message


def test_both_routes_attempted_is_satisfied():
    gate = research_route_gate(
        [row("local_hybrid_search"), row("deepseek_search")], BOTH)
    assert not gate.active


def test_failed_route_counts_as_one_bounded_attempt():
    gate = research_route_gate(
        [row("local_hybrid_search"), row("deepseek_search", "failed")], BOTH)
    assert not gate.active


def test_partial_route_counts_as_one_bounded_attempt():
    gate = research_route_gate(
        [row("local_hybrid_search", "partial"), row("deepseek_search")], BOTH)
    assert not gate.active


def test_not_run_route_does_not_count():
    gate = research_route_gate(
        [row("local_hybrid_search"), row("deepseek_search", "not_run")], BOTH)
    assert gate.active and gate.missing == ("web",)
    assert not gate.challenge


def test_successful_read_candidate_satisfies_local_reuse():
    gate = research_route_gate([row("read_candidate"), row("deepseek_search")], BOTH)
    assert not gate.active


def test_read_candidate_alone_does_not_start_a_discovery_turn():
    gate = research_route_gate([row("read_candidate")], BOTH)
    assert not gate.active


def test_fetch_and_fetch_paper_are_not_deepseek_search():
    fetch_only = research_route_gate([row("deepseek_fetch")], BOTH)
    paper_only = research_route_gate([row("deepseek_fetch_paper")], BOTH)
    assert not fetch_only.active and not paper_only.active


def test_browse_is_not_deepseek_search():
    gate = research_route_gate([row("browse_web")], BOTH)
    assert not gate.active


def test_literature_metadata_lookup_is_not_deepseek_search():
    metadata_only = research_route_gate([row("search_literature")], BOTH)
    local_plus_metadata = research_route_gate(
        [row("local_hybrid_search"), row("search_literature")], BOTH)
    assert not metadata_only.active
    assert local_plus_metadata.active and local_plus_metadata.missing == ("web",)


def test_unauthorized_route_is_not_required():
    gate = research_route_gate([row("local_hybrid_search")], {"local_hybrid_search"})
    assert not gate.active


def test_web_only_authorized_does_not_require_local():
    gate = research_route_gate([row("deepseek_search")], {"deepseek_search"})
    assert not gate.active


def test_explicit_dual_source_notice_without_available_tools():
    gate = research_route_gate([], set(), explicit=True)
    assert gate.missing == ("local", "web")
    assert gate.notice and not gate.message and not gate.challenge


def test_notice_only_for_explicit_dual_source_request():
    from src.agent.research_routes import explicitly_requests_both_sources

    assert explicitly_requests_both_sources("请分别查看本地资料和公开网络")
    assert explicitly_requests_both_sources("本地和网络都要")
    assert not explicitly_requests_both_sources("只查本地，别联网")
    assert not explicitly_requests_both_sources("本地和网络有什么区别？")


def test_inherited_attempts_do_not_start_this_turn():
    gate = research_route_gate(
        [row("deepseek_search", inherited_from_job_id="parent")], BOTH)
    assert not gate.active


def test_policy_has_no_candidate_final_alias_and_keeps_host_projection():
    from src.agent.host_policy import system_prompt
    prompt = system_prompt({"local_hybrid_search", "deepseek_search"})
    assert "本地候选也可用" not in prompt
    assert "[cNN]" not in prompt
    assert "宿主发布前会移除这些标记" in prompt
    assert "本地：题名" in prompt and "联网：" in prompt
    assert "尝试一轮" not in prompt
    examples = prompt.split("<source_routing_examples>", 1)[1].split("</source_routing_examples>", 1)[0]
    assert examples.count("正确：") == examples.count("反例：") == 3
    assert all(example in examples for example in ("分别查看本地资料和公开网络", "只查本地资料", "画箱线图"))
    assert "宿主会在终答前核对本轮两路覆盖" not in prompt


def test_no_query_heuristics_in_route_module():
    import inspect
    import src.agent.research_routes as routes
    source = inspect.getsource(routes)
    assert "is_research_turn" not in source
    assert "browse_web" not in source
    assert "deepseek_fetch" not in source
    assert "search_literature" not in source
