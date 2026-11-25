"""Deterministic tests for observational, user-requested source coverage."""
import pytest

from src.agent.research_routes import dual_source_notice, explicitly_requests_both_sources

BOTH = "请分别查看本地资料和公开网络"


def row(name, status="complete", **extra):
    return {"name": name, "status": status, **extra}


@pytest.mark.parametrize("query", [BOTH, "本地和网络都要", "Check both local corpus and web"])
def test_explicit_dual_source_request(query):
    assert explicitly_requests_both_sources(query)
    assert "本地检索、公开网络检索未完成" in dual_source_notice(query, [])


@pytest.mark.parametrize("query", ["只查本地，别联网", "本地和网络有什么区别？", "请在本地或公开网络搜索质量控制资料",
                                    "rwp基因如何调控胚性", "查找我给出的 DOI"])
def test_optional_source_has_no_coverage_or_parity_requirement(query):
    assert not explicitly_requests_both_sources(query)
    assert not dual_source_notice(query, [row("deepseek_search")])


@pytest.mark.parametrize("tool", ["deepseek_search", "deepseek_fetch", "deepseek_fetch_paper", "browse_web", "search_literature"])
def test_actual_authorized_public_fallback_counts_without_provider_parity(tool):
    assert not dual_source_notice(BOTH, [row("read_candidate"), row(tool)])


def test_coverage_distinguishes_not_run_failed_or_inherited_from_observed():
    for observation in (row("deepseek_search", "failed"), row("deepseek_search", "not_run"),
                        row("deepseek_search", inherited_from_job_id="earlier-job")):
        assert "公开网络检索未完成" in dual_source_notice(BOTH, [row("local_hybrid_search"), observation])
    assert not dual_source_notice(BOTH, [row("local_hybrid_search", "partial"), row("deepseek_search", "no_results")])


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
    from src.graph.retrieval_router import decide_retrieval, required_route_groups

    def routes(query):
        return required_route_groups(decide_retrieval(query))

    assert len(routes("请分别查看本地资料和公开网络，核对柑橘溃疡病。")) == 2
    assert len(routes("只查本地库中的柑橘溃疡病文献，不用联网")) == 1
    assert not routes("请用附件画箱线图。")
    assert "宿主会在终答前核对本轮两路覆盖" not in prompt
