import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent.harness import HarnessRuntime
from src.agent.host import _same_repair_target
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import BatchObservation, FinalCheck, RunLimits
from src.core.task_result import effective_tool_records
from src.core.turn_outcome import artifact_goal_evidence
from src.graph.task_contract import contract_for_turn


def test_unconfigured_skill_release_never_activates_archived_prompts(monkeypatch):
    from src.agent.host_policy import system_prompt
    from src.prompts.loader import assemble_system_prompt, verify_figure_prompt_loading

    monkeypatch.delenv("CITRUS_SKILL_RELEASE", raising=False)
    tools = {"run_research_figure_script", "research_workspace"}
    assert assemble_system_prompt(mode="expert", available_tools=tools) == system_prompt(tools, native_skills=True)
    with pytest.raises(ValueError, match="skill_release_not_configured"):
        verify_figure_prompt_loading()


def test_request_to_check_both_sources_preserves_authorized_search(monkeypatch):
    from src.agent.host_policy import allowed_presets, system_prompt
    from src.core import tracing
    from src.core.capability_policy import CapabilityPolicy
    from src.graph import tool_availability
    from src.graph.retrieval_router import retrieval_denied, source_scope

    monkeypatch.setattr(tracing, "web_search_enabled", lambda: True)
    monkeypatch.setattr(tool_availability, "provider_denials", lambda settings: frozenset())
    query = "请简述内标法如何补偿基质效应。请分别查看本地资料和公开网络一次，只引用实际采用的证据。"
    scope = source_scope(query, exclusive_only=True)
    assert scope == "fusion"
    assert not retrieval_denied(query, scope)
    assert retrieval_denied("这轮别查本地或网络")
    state = {"query": query, "capability_snapshot": CapabilityPolicy().snapshot()}
    available = allowed_presets(state, has_tables=False, has_pool=False)
    assert {"local_hybrid_search", "deepseek_search"} <= available
    assert "用户明确要求本地资料和公开网络都查时" in system_prompt(available, native_skills=True)


def test_attachment_plot_needs_a_downloadable_svg():
    requirements = contract_for_turn("请你绘制这个数据", has_tables=True).deliverables
    assert requirements[0]["formats"] == ["svg"]
    assert not contract_for_turn("请描述这个数据，不要绘制图", has_tables=True).deliverables
    assert contract_for_turn("审查 Sheet1", has_tables=True).deliverables[0]["formats"] == ["xlsx"]
    assert not contract_for_turn("审查 Sheet1，只给文字", has_tables=True).deliverables
    goals, gaps = artifact_goal_evidence(requirements, [], set())
    assert not goals and gaps[0]["reason_code"] == "required_deliverable_missing"


@pytest.mark.parametrize("query", ["画图", "好，帮我画个图", "请继续出图"])
def test_short_drawing_requests_need_a_current_turn_figure(query):
    requirements = contract_for_turn(query, has_tables=False).deliverables
    assert [item["formats"] for item in requirements] == [["svg"]]
    goals, gaps = artifact_goal_evidence(requirements, [], set())
    assert not goals and gaps[0]["reason_code"] == "required_deliverable_missing"


@pytest.mark.parametrize("query", ["不要画图", "请解释如何画图", "出图方法是什么"])
def test_short_drawing_mentions_do_not_require_a_figure(query):
    assert not contract_for_turn(query, has_tables=False).deliverables


def test_verified_workbook_can_be_delivered_with_partial_audit_coverage():
    requirements = contract_for_turn("审查 Sheet1", has_tables=True).deliverables
    link = "/api/v2/session/fixture-session/artifacts/fixture"
    record = {"name": "audit_uploaded_data", "tool_call_id": "audit", "status": "partial",
              "artifact_validations": [{"link": link, "valid": True, "request_matches": True,
                                        "filename": "fixture_审查标注.xlsx", "producer_tool": "audit_uploaded_data",
                                        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}]}
    goals, gaps = artifact_goal_evidence(requirements, [record], {link})
    assert not gaps and goals[0].artifact_links == [link]
    goals, gaps = artifact_goal_evidence(requirements, [record], set())
    assert not goals and gaps[0]["reason_code"] == "required_deliverable_missing"


def test_rejected_audit_can_be_repaired_only_for_the_same_requested_scope():
    name = "audit_uploaded_data"
    base = {"file_ids_json": ["a" * 32], "file_indexes_json": [0], "sheet_name": "Sheet1"}
    region = {"bounds": "H15:I18", "sheet": "Sheet1", "header_rows": [15]}
    checks = {"profile": "on_demand", "requested_checks": ["duplicate_row"],
              "regions": [region | {"file_index": 0}]}
    corrected = {"name": name, "args": base | {"checks_json": checks}}
    for bad_checks in (checks | {"regions": [region]},
                       checks | {"regions": [region], "profile": "duplicates"}):
        rejected = {"name": name, "args": base | {"checks_json": bad_checks}}
        assert _same_repair_target(rejected, corrected)
    assert not _same_repair_target(rejected, corrected | {"args": corrected["args"] | {"file_ids_json": ["b" * 32]}})
    narrower = checks | {"regions": [region | {"file_index": 0, "bounds": "H15:I17"}]}
    assert not _same_repair_target(rejected, corrected | {"args": base | {"checks_json": narrower}})
    different = checks | {"requested_checks": ["missing_values"]}
    assert not _same_repair_target(rejected, corrected | {"args": base | {"checks_json": different}})


def test_failed_renderer_and_rejected_repair_can_be_replaced_by_same_goal_svg():
    name = "run_research_figure_script"
    link = "/api/v2/session/fixture-session/artifacts/fixture"
    rejected = {"name": name, "id": "rejected", "args": {"repair_from_run_id": "old-run"}}
    corrected = {"name": name, "id": "finished", "args": {}}

    def receipt(call_id, status, run_id, identity="a" * 64):
        return {"name": name, "tool_call_id": call_id, "status": status, "reason_code": "render",
                "op_receipt": {"schema_version": 1, "tool_name": name, "tool_call_id": call_id,
                               "status": status, "reason_code": "render", "run_id": run_id,
                               "repair_identity": identity}}

    failed = receipt("failed", "failed", "old-run")
    success = receipt("finished", "complete", "new-run")
    success["artifact_validations"] = [{"asset_kind": "figure", "valid": True, "link": link,
        "request_matches": True, "filename": "boxplot.svg", "mime": "image/svg+xml",
        "producer_tool": name, "exists": True, "owner_matches": True, "hash_matches": True,
        "format_matches": True}]
    assert _same_repair_target(rejected, corrected, records=[failed, success])
    assert effective_tool_records([failed, success]) == [success]
    direct_repair = corrected | {"args": {"repair_from_run_id": "old-run"}}
    assert _same_repair_target(rejected, direct_repair, records=[failed, success])
    assert not _same_repair_target(rejected, direct_repair,
                                   records=[failed, success | {"artifact_validations": []}])
    non_figure = success["artifact_validations"][0] | {"filename": "summary.csv", "mime": "text/csv"}
    assert not _same_repair_target(rejected, direct_repair,
                                   records=[failed, success | {"artifact_validations": [non_figure]}])
    mismatched = receipt("finished", "complete", "new-run", "b" * 64) | {
        "artifact_validations": success["artifact_validations"]}
    assert not _same_repair_target(rejected, corrected, records=[failed, mismatched])
    assert not _same_repair_target(rejected, corrected, records=[failed, success | {"artifact_validations": []}])
    assert not _same_repair_target(rejected, corrected, records=[failed | {"status": "unknown"}, success])


@pytest.mark.parametrize(("query", "expected"), [
    ("只核验以下十条文献是否真实存在，报告结果，无须查全文。", True),
    ("核验参考文献是否存在，并阅读全文。", False),
    ("核验这些文献，然后比较每篇的方法并给出结论。", False),
    ("核验参考文献，不用导出；也请绘图。", False),
    ("核验参考文献，不用导出。", True),
])
def test_identity_snapshot_finalizes_only_the_requested_scope(query, expected):
    from src.research.reference_tools import identity_report_requested

    assert identity_report_requested(query) is expected


def test_saved_identity_report_separates_rate_limit_from_unprocessed(monkeypatch):
    from src.research import reference_audit, reference_tools

    rows = [{"record_index": index, "citation": f"Fixture {index}",
             "processing_status": "processed" if index < 9 else "unprocessed",
             "match_status": "unconfirmed" if index < 9 else "unprocessed",
             "reason": "rate_limited" if index < 9 else "request_budget_exhausted"}
            for index in range(10)]
    view = {"schema": "reference_audit/2", "terminal": False, "rows": rows,
            "next_action": "budget_exhausted_export_partial", "next_offset": None,
            "coverage": {"processed": 9, "total": 10, "pending": 1, "processing_complete": False,
                         "complete": False, "retryable": 9}}
    monkeypatch.setattr(reference_audit, "get_audit", lambda *_args, **_kwargs: view)
    report = reference_tools.saved_identity_report(1, "fixture-session", "fixture-audit")
    assert "已处理 9/10" in report
    assert report.count("| 已处理 | 尚未确认 |") == 9
    assert report.count("| 未处理 | 未处理 |") == 1
    view["coverage"]["processing_complete"] = True
    assert reference_tools.saved_identity_report(1, "fixture-session", "fixture-audit") is None


@pytest.mark.parametrize("native", [False, True])
def test_saved_reference_batch_finishes_before_next_model_request(native, tmp_path):
    async def exercise():
        identity = ExecutionIdentity(1, "fixture-session", "fixture-turn", "fixture-run")
        calls = []
        observations = []
        call = {"name": "verify_uploaded_references", "id": "batch", "args": {}}

        async def model(history, current_identity, **kwargs):
            calls.append(current_identity)
            assert len(calls) == 1
            return AIMessage(content="", tool_calls=[call])

        async def execute(response, current_identity):
            observations.append(ToolMessage(content="report ready", name=call["name"], tool_call_id=call["id"]))
            return BatchObservation(messages=(observations[-1],))

        async def report():
            return "十条已处理，三条尚未确认。" if observations else None

        async def accept(response, history, current_identity):
            assert response.content == await report()
            return FinalCheck()

        schemas = [{"type": "function", "function": {"name": call["name"], "description": "Fixture",
                   "parameters": {"type": "object", "properties": {}}}}]
        runtime = HarnessRuntime(model, execute, lambda *_: None, accept, RunLimits(), schemas,
                                 final_from_observations=report)
        options = {"skill_root": tmp_path, "system_prompt": "fixture", "summary_model": None} if native else None
        human = HumanMessage("请核验文献是否存在", additional_kwargs=identity.message_metadata("user"))
        result = await runtime.run(identity, [human], saver=InMemorySaver(), native_options=options)
        assert result.status == "complete"
        assert result.messages[-1].content == "十条已处理，三条尚未确认。"
        assert len(calls) == len(observations) == 1

    asyncio.run(exercise())
