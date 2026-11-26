"""F-07: the runtime capability table is the only tool-routing statement.

The table is a projection of host evidence, so these tests drive it through
evidence and assert the three properties that keep the Host prompt honest:

* a row only ever names a tool the turn actually exposes;
* a closed path is stated as closed, with the real reason, and never as a tool
  suggestion (CSV data audit, unauthorized Skill, blocked sandbox, withdrawn
  fallback);
* the facts a tool contract owns (artifacts, wait semantics) are read from that
  contract instead of being restated here.
"""
from __future__ import annotations

import json
import re
import sys
import types

import pytest

from src.agent.code_tools import CODE_TOOL_NAMES, EXECUTION_WAIT_TOOL_NAMES
from src.agent.fusion_tools import (CAPABILITY_NEW, FUSION_TOOL_NAMES, FUSION_WAIT_TOOLS,
                                    SKILL_IDS)
from src.agent.nature_skill_tools import NATURE_TOOL_NAMES
from src.agent.runtime_capability_table import (
    CSV_ROUTE_GENERIC,
    CSV_ROUTE_UNAVAILABLE,
    CSV_ROUTE_WORKER,
    FUSION_STATE_NEW,
    NOT_RUNNABLE,
    READ_ONLY,
    RUNNABLE,
    SKILL_DATA_AUDIT,
    SKILL_INTEGRITY,
    SKILL_LITERATURE,
    CapabilityRow,
    RuntimeCapabilityTable,
    build,
    normalize_csv_route,
    resolve_csv_route,
)
from src.tools.tool_specs import TOOL_DECLARATIONS

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KNOWN_TOOL_NAMES = (
    frozenset(spec.name for spec in TOOL_DECLARATIONS)
    | FUSION_TOOL_NAMES
    | NATURE_TOOL_NAMES
    | CODE_TOOL_NAMES
    | {"execute", "update_task_plan", "write_todos", "read_skill", "load_skill",
       "read_context_evidence", "workspace_read", "workspace_write"}
)
AUDIT_SURFACE = ("run_external_skill", "collect_external_skill")
LITERATURE_SURFACE = ("run_external_skill", "continue_external_literature",
                      "collect_external_skill")


def named_tools(text: str) -> set[str]:
    """Tool names that appear as whole tokens in one prompt block."""
    return {token for token in _TOKEN.findall(text) if token in KNOWN_TOOL_NAMES}


def assert_only_exposed_tools(test_table: RuntimeCapabilityTable) -> str:
    text = test_table.prompt_fragment()
    assert set(named_tools(text)) <= set(test_table.available_names), named_tools(text)
    for row in test_table.rows:
        assert set(row.tools) <= set(test_table.available_names), row.task
        if row.status == NOT_RUNNABLE:
            assert row.tools == (), row.task
        else:
            assert row.tools, row.task
    return text


def build_for(**overrides) -> RuntimeCapabilityTable:
    evidence = {"available_names": AUDIT_SURFACE,
                "authorized_skills": {SKILL_DATA_AUDIT, SKILL_LITERATURE, SKILL_INTEGRITY},
                "fusion_state": FUSION_STATE_NEW,
                "csv_route": CSV_ROUTE_UNAVAILABLE}
    evidence.update(overrides)
    return build(**evidence)


def fake_contract(monkeypatch, **attributes):
    """Replace the fusion contract with a controllable one for route resolution."""
    module = types.ModuleType("src.agent.fusion_worker_contract")
    module.analyzer_version = lambda release, skill: "0.2.0"
    module.csv_table_supported = lambda skill, version: False
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, "src.agent.fusion_worker_contract", module)
    return module


def test_every_row_only_names_exposed_tools():
    for available in (
        (),
        ("inspect_uploaded_table",),
        AUDIT_SURFACE,
        LITERATURE_SURFACE,
        tuple(sorted(FUSION_TOOL_NAMES)),
        tuple(sorted(FUSION_TOOL_NAMES | CODE_TOOL_NAMES | {"execute"})),
        ("resolve_external_report", "read_tool_result", "request_user_input"),
    ):
        test_table = build_for(available_names=available)
        assert_only_exposed_tools(test_table)


def test_prompt_lists_runnable_rows_and_states_closures():
    test_table = build_for()
    text = assert_only_exposed_tools(test_table)
    assert "本轮能力表" in text
    assert "不是工具白名单" in text
    assert "不得虚构、不得承诺完成" in text
    assert "数据审查（XLSX/XLSM）" in text
    assert "不可执行 · 数据审查（CSV 单表）" in text
    assert len(test_table.runnable()) >= 3


def test_csv_routes_differ_in_wait_semantics_and_wording():
    worker = build_for(csv_route=CSV_ROUTE_WORKER)
    generic = build_for(csv_route=CSV_ROUTE_GENERIC)
    closed = build_for(csv_route=CSV_ROUTE_UNAVAILABLE)

    workbook_inputs = " ".join(worker.row("audit.workbook").inputs)
    assert "1–16" in workbook_inputs or "1-16" in workbook_inputs
    assert "csv_table" not in workbook_inputs
    # plot_fields is only consumed by the CSV generic adapter (evidenced there).
    assert "plot_fields" not in workbook_inputs
    worker_row = worker.row("audit.csv")
    assert worker_row.status == RUNNABLE and worker_row.waits is True
    assert "execution_id" in worker_row.hint and "collect_external_skill" in worker_row.hint
    assert worker_row.source.startswith("csv_route:worker")

    generic_row = generic.row("audit.csv")
    assert generic_row.status == RUNNABLE and generic_row.waits is False
    assert "不产生 execution_id" in generic_row.hint
    assert "review_id" in generic_row.hint
    assert "不接受 sheet" in " ".join(generic_row.inputs)
    assert "plot_fields" in " ".join(generic_row.inputs)
    assert "plot_fields" not in " ".join(worker_row.inputs)
    assert generic_row.source.startswith("csv_route:generic")
    assert any("annotated_xlsx" in item for item in generic_row.artifacts)
    # Same tool surface, different route: only the route decides the wait.
    assert generic.row("audit.workbook").waits is True

    closed_row = closed.row("audit.csv")
    assert closed_row.status == NOT_RUNNABLE and closed_row.tools == ()
    assert closed_row.reason_code == "external_csv_table_audit_unavailable"
    assert closed_row.surface is True
    text = assert_only_exposed_tools(closed)
    assert "不可执行 · 数据审查（CSV 单表）" in text
    assert "不得把 CSV 伪装成工作簿或编造工作表名" in text


def test_csv_closure_offers_the_real_read_only_fallback():
    available = AUDIT_SURFACE + ("inspect_uploaded_table",)
    test_table = build_for(available_names=available, turn_inputs=("csv",))
    closed_row = test_table.row("audit.csv")
    assert closed_row.status == NOT_RUNNABLE
    assert "inspect_uploaded_table" in closed_row.hint
    assert test_table.row("table.inspect").status == RUNNABLE
    text = assert_only_exposed_tools(test_table)
    assert "不等于数据审查" in text


def test_csv_closure_without_inspect_tool_names_no_tool():
    test_table = build_for(available_names=AUDIT_SURFACE)
    closed_row = test_table.row("audit.csv")
    assert "inspect_uploaded_table" not in closed_row.hint
    assert_only_exposed_tools(test_table)


def test_resolve_csv_route_uses_the_one_contract_function(monkeypatch):
    fake_contract(monkeypatch, csv_data_audit_route=lambda skill, release: "worker")
    assert resolve_csv_route(available_names=AUDIT_SURFACE) == (
        CSV_ROUTE_WORKER, "fusion_signal:csv_data_audit_route")

    fake_contract(monkeypatch, csv_data_audit_route=lambda skill, release: "generic")
    assert resolve_csv_route(available_names=AUDIT_SURFACE) == (
        CSV_ROUTE_GENERIC, "fusion_signal:csv_data_audit_route")

    fake_contract(monkeypatch, csv_data_audit_route=lambda skill, release: "unavailable")
    assert resolve_csv_route(available_names=AUDIT_SURFACE) == (
        CSV_ROUTE_UNAVAILABLE, "fusion_signal:csv_data_audit_route")
    # ... and the retained legacy reviewer is the documented fallback.
    assert resolve_csv_route(available_names=AUDIT_SURFACE + ("audit_uploaded_data",)) == (
        CSV_ROUTE_GENERIC, "legacy_fallback")


def test_the_installed_contract_answers_without_a_release():
    # fusion_worker_contract.csv_data_audit_route is the real dispatch contract;
    # without a release it must answer "unavailable" rather than guess.
    assert resolve_csv_route(available_names=AUDIT_SURFACE) == (
        CSV_ROUTE_UNAVAILABLE, "fusion_signal:csv_data_audit_route")


def test_resolve_csv_route_uses_the_support_bit_only_when_no_signal(monkeypatch):
    fake_contract(monkeypatch, csv_table_supported=lambda skill, version: True)
    assert resolve_csv_route(available_names=()) == (CSV_ROUTE_WORKER, "analyzer_support_bit")


def test_resolve_csv_route_falls_back_to_the_real_generic_routes(monkeypatch):
    fake_contract(monkeypatch)
    # The generic adapter runs inside run_external_skill on the same bytes, so an
    # open fusion surface is a real synchronous route (no execution id).
    assert resolve_csv_route(available_names=("run_external_skill",)) == (
        CSV_ROUTE_GENERIC, "generic_adapter")
    assert resolve_csv_route(available_names=("audit_uploaded_data",)) == (
        CSV_ROUTE_GENERIC, "generic_audit_tool")
    assert resolve_csv_route(available_names=()) == (
        CSV_ROUTE_UNAVAILABLE, "csv_executor_absent")


def test_resolve_csv_route_fails_closed_when_the_contract_cannot_answer(monkeypatch):
    def boom(release, skill):
        raise ValueError("fusion_bundle_schema_mismatch")

    fake_contract(monkeypatch, analyzer_version=boom)
    release = "/nonexistent/release"
    # An unreadable analyzer version makes even the execution shape unknown: the
    # open surface must not promise a path, and no surface is a closed path.
    assert resolve_csv_route(release=release, available_names=("run_external_skill",),
                             fusion_state=CAPABILITY_NEW) == (
        CSV_ROUTE_UNAVAILABLE, "csv_analyzer_version_unknown")
    assert resolve_csv_route(release=release, available_names=(),
                             fusion_state=CAPABILITY_NEW) == (
        CSV_ROUTE_UNAVAILABLE, "csv_executor_absent")


def test_skill_authorization_is_per_skill_not_per_surface():
    test_table = build_for(available_names=LITERATURE_SURFACE,
                           authorized_skills={SKILL_LITERATURE},
                           csv_route=CSV_ROUTE_WORKER)
    assert test_table.row("literature.verify").status == RUNNABLE
    for task in ("audit.workbook", "audit.csv", "research.integrity"):
        row = test_table.row(task)
        assert row.status == NOT_RUNNABLE, task
        assert row.tools == (), task
        assert row.reason_code == "skill_capability_unauthorized:" + (
            SKILL_INTEGRITY if task == "research.integrity" else SKILL_DATA_AUDIT)
        assert row.surface is True, task
    text = assert_only_exposed_tools(test_table)
    assert "Skill 本轮未授权" in text

    everything = build_for(available_names=LITERATURE_SURFACE, csv_route=CSV_ROUTE_WORKER)
    assert {"audit.workbook", "audit.csv", "literature.verify",
            "research.integrity"} <= {row.task for row in everything.runnable()}
def test_figure_template_is_claimed_only_when_declared_or_unknown():
    arguments = {"run_research_figure_script": {"mode", "template_id",
                                                "template_parameters_json", "script"}}
    declared = build_for(available_names=("run_research_figure_script",),
                         tool_arguments=arguments)
    row = declared.row("figure.template")
    assert row.status == RUNNABLE and row.modes == ("template",)
    assert row.source == "tool_arguments"
    assert declared.row("figure.script").modes == ("script",)

    script_only = build_for(
        available_names=("run_research_figure_script",),
        tool_arguments={"run_research_figure_script": {"script", "inputs_json"}})
    closed_row = script_only.row("figure.template")
    assert closed_row.status == NOT_RUNNABLE
    assert closed_row.reason_code == "figure_template_mode_not_declared"
    assert "不要以固定模板名义执行" in closed_row.hint
    assert script_only.row("figure.script").status == RUNNABLE

    unknown = build_for(available_names=("run_research_figure_script",))
    assert unknown.row("figure.template").status == RUNNABLE
    assert unknown.row("figure.template").source == "tool_contract"
    assert "tool_arguments_missing" in unknown.diagnostics


def test_figure_template_names_every_supported_form():
    test_table = build_for(available_names=("run_research_figure_script",),
                           tool_arguments={"run_research_figure_script": {
                               "mode", "template_id", "template_parameters_json"}})
    hint = test_table.row("figure.template").hint
    for form in ("volcano", "ROC", "dotplot", "marginal", "paired"):
        assert form in hint
    assert "同一冻结输入" in hint and "工作台项目" in hint


def test_deprecated_figure_tools_are_never_offered():
    available = ("run_research_figure_script",) + tuple(sorted(NATURE_TOOL_NAMES))
    test_table = build_for(available_names=available)
    for row in test_table.rows:
        for name in NATURE_TOOL_NAMES:
            assert name not in row.tools, row.task
    text = assert_only_exposed_tools(test_table)
    for name in NATURE_TOOL_NAMES:
        assert name not in text
    assert "旧入口仅兼容转发" in text


def test_read_only_report_turn_offers_only_the_existing_report():
    available = ("resolve_external_report", "read_tool_result", "request_user_input")
    test_table = build_for(available_names=available, read_only_report=True,
                           turn_inputs=("csv", "figure"))
    assert [row.task for row in test_table.runnable()] == ["literature.followup"]
    followup = test_table.row("literature.followup")
    assert followup.status == READ_ONLY and followup.waits is False
    assert "不重跑算法" in followup.hint and "next_offset" in followup.hint
    text = assert_only_exposed_tools(test_table)
    assert "只读报告" in text
    assert "不得重新执行审查、核验或绘图" in text
    # A read-only turn states its rule once; per-capability negatives are noise.
    assert "不可执行 · 数据审查" not in text
    assert "不可执行 · 科研绘图" not in text
    for task in ("figure.script", "audit.csv", "audit.workbook", "table.inspect"):
        assert test_table.row(task).status == NOT_RUNNABLE, task


def test_noise_is_not_surfaced_without_a_matching_input():
    quiet = build_for(available_names=("request_user_input",), csv_route=CSV_ROUTE_UNAVAILABLE)
    assert quiet.row("audit.csv").surface is False
    assert "不可执行 · 数据审查" not in quiet.prompt_fragment()
    speaking = build_for(available_names=("request_user_input",), turn_inputs=("csv",),
                         csv_route=CSV_ROUTE_UNAVAILABLE)
    assert speaking.row("audit.csv").surface is True
    assert "不可执行 · 数据审查（CSV 单表）" in speaking.prompt_fragment()


def test_report_followup_without_a_report_is_closed():
    test_table = build_for(available_names=("read_tool_result", "request_user_input"),
                           read_only_report=True)
    row = test_table.row("literature.followup")
    assert row.status == NOT_RUNNABLE
    assert row.reason_code == "report_followup_requires_accepted_report"
    assert row.surface is True


def test_sandbox_execute_and_code_worker_are_separate_gates():
    available = ("execute", "code_execute", "code_status")
    blocked = build_for(available_names=available, sandbox_ready=False)
    sandbox = blocked.row("sandbox.execute")
    assert sandbox.status == NOT_RUNNABLE
    assert sandbox.reason_code == "sandbox_unavailable"
    assert sandbox.surface is True
    worker = blocked.row("code.worker")
    assert worker.status == RUNNABLE and worker.waits is True
    text = assert_only_exposed_tools(blocked)
    assert "不得回退宿主 shell" in text

    ready = build_for(available_names=available, sandbox_ready=True)
    assert ready.row("sandbox.execute").status == RUNNABLE
    assert "不是同一门禁" in ready.row("sandbox.execute").hint
    assert "不是同一门禁" in ready.row("code.worker").hint
    assert ready.row("code.worker").tools == ("code_execute", "code_status")


def test_partial_code_surface_cannot_submit_execution():
    test_table = build_for(available_names=("code_workspace", "code_write"))
    row = test_table.row("code.worker")
    assert row.status == NOT_RUNNABLE
    assert row.reason_code == "code_execute_unavailable"
    assert_only_exposed_tools(test_table)


def test_waits_follow_the_tool_wait_sets():
    test_table = build_for()
    assert test_table.row("audit.workbook").waits is (("run_external_skill" in FUSION_WAIT_TOOLS))
    assert test_table.row("literature.verify").waits is ("run_external_skill" in FUSION_WAIT_TOOLS)
    code_table = build_for(available_names=("code_execute", "code_status"))
    assert code_table.row("code.worker").waits is (
        "code_execute" in EXECUTION_WAIT_TOOL_NAMES)
    figure = build_for(available_names=("run_research_figure_script",))
    assert figure.row("figure.script").waits is False


def test_artifacts_follow_the_declared_tool_contract():
    figure = build_for(available_names=("run_research_figure_script",))
    assert figure.row("figure.script").artifacts == ("figure_package",)
    legacy = build_for(available_names=("audit_uploaded_data", "verify_uploaded_references"),
                       fusion_state="legacy_tools_retained", csv_route=CSV_ROUTE_GENERIC)
    assert legacy.row("audit.workbook").artifacts == ("annotation_table", "report_json")
    assert legacy.row("literature.verify").artifacts == ("verification_sheet",)


def test_unknown_csv_route_fails_closed():
    with pytest.raises(ValueError):
        build_for(csv_route="semi-generic")
    assert normalize_csv_route("GENERIC") == CSV_ROUTE_GENERIC
    assert normalize_csv_route("generic_data_audit") == CSV_ROUTE_GENERIC
    assert normalize_csv_route("NATIVE") == CSV_ROUTE_WORKER
    assert normalize_csv_route("Unsupported") == CSV_ROUTE_UNAVAILABLE
    assert normalize_csv_route("mystery") == ""


def test_row_invariants_are_enforced():
    with pytest.raises(ValueError):
        CapabilityRow(task="x", label="x", status=NOT_RUNNABLE, tools=("a",),
                      reason_code="r", artifacts=("a",))
    with pytest.raises(ValueError):
        CapabilityRow(task="x", label="x", status=RUNNABLE, tools=("a",), artifacts=())
    with pytest.raises(ValueError):
        CapabilityRow(task="x", label="x", status=NOT_RUNNABLE, hint="h", artifacts=("a",))
    with pytest.raises(ValueError):
        CapabilityRow(task="", label="x", status=RUNNABLE, tools=("a",), artifacts=("a",))


def test_constants_match_the_contracts_they_mirror():
    assert FUSION_STATE_NEW == CAPABILITY_NEW
    assert {SKILL_DATA_AUDIT, SKILL_LITERATURE, SKILL_INTEGRITY} <= set(SKILL_IDS)
    assert "run_research_figure_script" in {spec.name for spec in TOOL_DECLARATIONS}


def test_as_dict_is_stable_and_json_serializable():
    first = build_for().as_dict()
    second = build_for().as_dict()
    assert first == second
    assert json.loads(json.dumps(first, ensure_ascii=False)) == first
    assert first["csv_route"] == CSV_ROUTE_UNAVAILABLE
    assert {row["task"] for row in first["rows"]} >= {"audit.csv", "audit.workbook",
                                                      "figure.script", "table.inspect"}


def test_explicit_evidence_skips_route_discovery():
    explicit = build_for(csv_route=CSV_ROUTE_GENERIC)
    assert explicit.diagnostics[0].startswith("csv_route=generic (explicit)")
    assert explicit.row("audit.csv").source.startswith("csv_route:generic+explicit")