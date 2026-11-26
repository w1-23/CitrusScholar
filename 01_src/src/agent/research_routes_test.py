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
    assert "已知论文身份用目录精确核验" not in prompt
    assert "只有缺少实质正文证据才 Fetch" not in prompt
    assert "缺少公开事实时用获准的 Search" not in prompt
    assert "结合任务和事实缺口选择已授权能力" in prompt
    assert "要求本地和网络都查，就分别查证" in prompt
    from src.graph.retrieval_router import decide_retrieval, required_route_groups

    def routes(query):
        return required_route_groups(decide_retrieval(query))

    assert len(routes("请分别查看本地资料和公开网络，核对柑橘溃疡病。")) == 2
    assert len(routes("只查本地库中的柑橘溃疡病文献，不用联网")) == 1
    assert not routes("请用附件画箱线图。")
    assert "宿主会在终答前核对本轮两路覆盖" not in prompt



def test_scientific_scope_separates_drawing_and_csv_audit():
    from src.agent.research_routes import accepts_method, scientific_scope, scoped_tools

    candidates = {"inspect_uploaded_table", "request_user_input", "read_tool_result",
                  "deliver_research_artifact", "run_external_skill", "collect_external_skill",
                  "run_research_figure_script", "deepseek_search", "code_execute"}
    figures = scientific_scope("把这个数据绘制成图", has_tables=True)
    assert figures == {"nature-figure"}
    assert scoped_tools(candidates, figures) == {
        "inspect_uploaded_table", "request_user_input", "read_tool_result",
        "deliver_research_artifact", "run_research_figure_script",
    }
    audit = scientific_scope("审查这个数据有没有异常", has_tables=True)
    assert audit == {"data-audit"}
    assert scoped_tools(candidates, audit) == {
        "inspect_uploaded_table", "request_user_input", "read_tool_result",
        "deliver_research_artifact", "run_external_skill", "collect_external_skill",
    }
    assert accepts_method("run_external_skill", {"skill_id": "data-audit"}, audit)
    assert not accepts_method("run_external_skill", {"skill_id": "research-integrity"}, audit)
    assert not accepts_method("collect_external_skill", {"skill_id": "literature-verification"}, audit)
    assert "deepseek_search" in scoped_tools(candidates, audit, retrieval={"deepseek_search"})


def test_conservative_scope_keeps_general_and_combined_requests():
    from src.agent.research_routes import scientific_scope

    assert not scientific_scope("转座子是什么？")
    assert not scientific_scope("这份表里某个字段是什么意思？", has_tables=True)
    assert not scientific_scope("不要审查数据", has_tables=True)
    assert not scientific_scope("审查数据并结合论文公开资料", has_tables=True)
    assert scientific_scope("请分析数据并绘制这个数据图", has_tables=True) == {"nature-figure"}
    assert scientific_scope("请检查数据异常并绘制这个数据图", has_tables=True) == {
        "data-audit", "nature-figure",
    }
    assert scientific_scope("请检查附件", selected=["research-integrity"], has_tables=True) == {"research-integrity"}
    assert not scientific_scope("检索并绘制", selected=["nature-figure", "research-question-answering"])



def test_audit_completion_requires_the_correct_report_not_the_submission():
    from src.agent.research_routes import has_data_audit_report

    calls = {"csv": {"args": {"skill_id": "data-audit"}},
             "worker": {"args": {"skill_id": "data-audit"}},
             "other": {"args": {"skill_id": "research-integrity"}}}
    start = row("run_external_skill", tool_call_id="worker", reason="external_skill_ready")
    wrong_method = row("collect_external_skill", tool_call_id="other", reason="external_skill_report")
    report = row("collect_external_skill", tool_call_id="worker", reason="external_skill_report")
    assert not has_data_audit_report([row("inspect_uploaded_table"), start, wrong_method], calls)
    assert not has_data_audit_report([{**report, "status": "partial"}], calls)
    assert has_data_audit_report([start, report], calls)
    assert has_data_audit_report([row("run_external_skill", tool_call_id="csv", reason="audit_review_completed")], calls)



def test_scoped_tools_reach_stock_deepagents_model_node():
    import asyncio

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langchain.agents.middleware import AgentState
    from src.agent.deep_agent_runtime import create_framework_agent
    from src.agent.research_routes import scoped_tools

    seen = []

    class Model(BaseChatModel):
        model_name: str = "offline-scientific-route"

        @property
        def _llm_type(self):
            return "offline-scientific-route"

        def bind_tools(self, tools, **kwargs):
            schemas = [convert_to_openai_tool(t) for t in tools]
            return self.bind(tools=schemas, **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append({spec["function"]["name"] for spec in kwargs.get("tools", [])})
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

    @tool
    def inspect_uploaded_table() -> str:
        """Inspect the supplied table."""
        return "not invoked"

    @tool
    def run_research_figure_script() -> str:
        """Render the requested figure."""
        return "not invoked"

    @tool
    def run_external_skill() -> str:
        """Run the requested audit method."""
        return "not invoked"

    catalog = {t.name: t for t in (inspect_uploaded_table, run_research_figure_script, run_external_skill)}
    allow = scoped_tools(catalog, {"data-audit"})
    graph = create_framework_agent(Model(), [catalog[name] for name in sorted(allow)], [],
        InMemorySaver(), AgentState, execute_ready=False, task_subagent=False)
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="Review the supplied data.")]},
        {"configurable": {"thread_id": "offline-scope"}}))
    assert result["messages"][-1].content == "done"
    assert seen and {"inspect_uploaded_table", "run_external_skill"} <= seen[0]
    assert not {"execute", "task", "run_research_figure_script"} & seen[0]
