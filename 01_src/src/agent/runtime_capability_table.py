"""One runtime capability table: what this turn can really do, with which tool.

The Host prompt used to append tool routing whenever a tool *family* appeared in
``allowed_names``. That states one path per family even where the surface and the
path are decided by different facts:

* a CSV data audit may have no executable route at all while the data-audit
  surface is open, because the pinned analyzer need not ship the ``csv_table``
  adapter;
* the generic reviewer may have been withdrawn by the same retirement transition
  that opened the new surface;
* ``run_external_skill`` appears when **any** of ``SKILL_IDS`` is authorized, so
  "the generic tool is visible" never means "this particular Skill may run";
* the native ``execute`` gate (an isolation sandbox backend) is not the offline
  code worker gate (``code_execute``); no readiness fact is shared.

Skill documents describe method; they must not guess which tool exists this turn.
This module answers that once, as a projection of evidence the host already
computed. It owns no registry and no second copy of a tool contract:

``artifacts``  read from ``src.tools.tool_specs`` for declared tools;
``waits``      read from ``FUSION_WAIT_TOOLS`` / ``EXECUTION_WAIT_TOOL_NAMES``,
               with an explicit per-row override for synchronous adapters;
``tools``      strictly a subset of ``available_names`` (``host.allowed_names``).

Evidence
--------
``available_names``    authoritative allowed set for this turn
``authorized_skills``  per-Skill authorization, not the surface being open
``fusion_state``       the decision ``fusion_capability_state`` already made
``csv_route``          three-state CSV data-audit route (``CSV_ROUTE_*``)
``tool_arguments``     argument names per exposed tool (``catalog.schemas``)
``sandbox_ready``      ``execute`` backend readiness (not the code worker)
``read_only_report``   the host's report follow-up decision
``turn_inputs``        input kinds present, only used to surface a closed path

Invariant, tested: every row's ``tools`` is a subset of ``available_names`` and
``prompt_fragment()`` never names a tool outside it, so a closed path can only be
described as closed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from src.tools.tool_specs import spec_by_name

__all__ = [
    "CSV_ROUTES", "CSV_ROUTE_GENERIC", "CSV_ROUTE_UNAVAILABLE", "CSV_ROUTE_WORKER",
    "CapabilityRow", "FUSION_STATE_NEW", "NOT_RUNNABLE", "READ_ONLY", "RUNNABLE",
    "RuntimeCapabilityTable", "SKILL_DATA_AUDIT", "SKILL_INTEGRITY", "SKILL_LITERATURE",
    "build", "csv_analyzer_supported", "normalize_csv_route", "resolve_csv_route",
]

RUNNABLE = "runnable"
READ_ONLY = "read_only"
NOT_RUNNABLE = "not_runnable"
_STATUSES = frozenset({RUNNABLE, READ_ONLY, NOT_RUNNABLE})

# Three-state CSV data-audit route. One boolean cannot express this: a native
# Worker route waits for an execution id, a generic adapter returns in-process,
# and neither being installed is a closed path rather than a pending one.
CSV_ROUTE_WORKER = "worker"
CSV_ROUTE_GENERIC = "generic"
CSV_ROUTE_UNAVAILABLE = "unavailable"
CSV_ROUTES = frozenset({CSV_ROUTE_WORKER, CSV_ROUTE_GENERIC, CSV_ROUTE_UNAVAILABLE})

# Mirrors src.agent.fusion_tools.CAPABILITY_NEW; the test asserts the equality so
# this literal cannot drift from the fusion contract.
FUSION_STATE_NEW = "new_capability"

# Skill ids, mirroring src.agent.fusion_tools.SKILL_IDS (asserted by the test).
SKILL_DATA_AUDIT = "data-audit"
SKILL_LITERATURE = "literature-verification"
SKILL_INTEGRITY = "research-integrity"

FIGURE_TOOL = "run_research_figure_script"
FIGURE_TEMPLATE_ARGUMENTS = ("mode", "template_id", "template_parameters_json")
FIGURE_TEMPLATES = ("volcano", "ROC", "dotplot", "marginal", "paired")
INSPECT_TOOL = "inspect_uploaded_table"
FUSION_TOOL = "run_external_skill"
COLLECT_TOOL = "collect_external_skill"
CONTINUE_TOOL = "continue_external_literature"
FOLLOWUP_TOOL = "resolve_external_report"
LEGACY_AUDIT_TOOL = "audit_uploaded_data"
LEGACY_VERIFY_TOOL = "verify_uploaded_references"
SANDBOX_EXECUTE_TOOL = "execute"
CODE_TOOLS = (
    "code_execute", "code_workspace", "code_write", "code_bind_input",
    "code_status", "code_log", "code_read", "code_deliver", "code_cancel",
    "code_wait", "code_environment_install", "code_environment_publish",
    "code_environment_read",
)

# The one explicit CSV routing contract. The real Fusion dispatch reads the same
# function, so the table and the executor cannot disagree about the route.
_CSV_ROUTE_CONTRACT = "csv_data_audit_route"
_CSV_ROUTE_ALIASES = {
    "worker": CSV_ROUTE_WORKER, "native": CSV_ROUTE_WORKER,
    "generic": CSV_ROUTE_GENERIC, "generic_audit": CSV_ROUTE_GENERIC,
    "generic_data_audit": CSV_ROUTE_GENERIC,
    "unavailable": CSV_ROUTE_UNAVAILABLE, "none": CSV_ROUTE_UNAVAILABLE,
    "unsupported": CSV_ROUTE_UNAVAILABLE,
}


def normalize_csv_route(value) -> str:
    """Normalize one fusion route signal to ``CSV_ROUTE_*`` (``""`` if unknown)."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        value = value.get(SKILL_DATA_AUDIT)
        if value is None:
            return ""
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return _CSV_ROUTE_ALIASES.get(text, "")


def csv_analyzer_supported(release) -> bool | None:
    """Whether the pinned data-audit analyzer ships the formal CSV table adapter.

    ``None`` means the question could not be answered (no release or no contract),
    which is not the same as "not installed": callers must not promise a path.
    """
    try:
        contract = import_module("src.agent.fusion_worker_contract")
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        version = contract.analyzer_version(Path(release), SKILL_DATA_AUDIT) if release else ""
        return bool(contract.csv_table_supported(SKILL_DATA_AUDIT, version))
    except Exception:
        return None


def _fusion_csv_route(release) -> tuple[str, str] | None:
    """Ask the fusion contract for its one explicit CSV data-audit route.

    ``fusion_worker_contract.csv_data_audit_route(skill, release)`` is the single
    route contract; the real dispatch reads the same function. A contract that is
    not installed yet, or that cannot answer, returns ``None`` so the caller falls
    back to the explicit facts instead of guessing a signature or a return type.
    """
    try:
        contract = import_module("src.agent.fusion_worker_contract")
        resolver = getattr(contract, _CSV_ROUTE_CONTRACT, None)
        if resolver is None:
            return None
        route = normalize_csv_route(resolver(SKILL_DATA_AUDIT, release))
    except Exception:
        return None
    return (route, "fusion_signal:" + _CSV_ROUTE_CONTRACT) if route else None


def resolve_csv_route(*, release=None, fusion_state="", available_names=()) -> tuple[str, str]:
    """Resolve the installed CSV data-audit route and name the evidence used.

    Order: fusion's explicit signal, then the analyzer support bit (native
    Worker), then the generic reviewer being callable this turn, otherwise a
    closed path. The second element is reported as ``csv_route_source`` so a
    capability report can say exactly which fact decided the row.
    """
    available = frozenset(str(name) for name in (available_names or ()))
    signal = _fusion_csv_route(release)
    if signal is not None:
        route, source = signal
        if route == CSV_ROUTE_UNAVAILABLE and LEGACY_AUDIT_TOOL in available:
            # The contract says the Fusion CSV route is closed. A retained legacy
            # reviewer is the documented fallback, not a silent second route.
            return CSV_ROUTE_GENERIC, "legacy_fallback"
        return route, source
    supported = csv_analyzer_supported(release)
    if supported:
        # The pinned analyzer reads the table itself: a queued Worker job that
        # reports through an execution id.
        return CSV_ROUTE_WORKER, "analyzer_support_bit"
    if FUSION_TOOL in available:
        if supported is None:
            # The analyzer version could not be read, so even the execution shape
            # of a table review is unknown. Do not promise a path.
            return CSV_ROUTE_UNAVAILABLE, "csv_analyzer_version_unknown"
        # The surface is open and the adapter is not shipped: ``run_external_skill``
        # runs the real generic data-audit route in-process on the same bytes.
        return CSV_ROUTE_GENERIC, "generic_adapter"
    if LEGACY_AUDIT_TOOL in available:
        return CSV_ROUTE_GENERIC, "generic_audit_tool"
    return CSV_ROUTE_UNAVAILABLE, "csv_executor_absent"
@dataclass(frozen=True)
class CapabilityRow:
    """One task a turn can do, or cannot do, with the exact tools and evidence."""

    task: str
    label: str
    status: str
    tools: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    waits: bool = False
    hint: str = ""
    reason_code: str = ""
    source: str = ""
    surface: bool = True

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("invalid_capability_status:" + str(self.status))
        if not str(self.task).strip() or not str(self.label).strip():
            raise ValueError("capability_row_identity_required")
        closed = self.status == NOT_RUNNABLE
        if closed == bool(self.tools):
            raise ValueError("capability_row_tool_status_mismatch:" + self.task)
        if closed and not str(self.reason_code).strip():
            raise ValueError("capability_row_reason_required:" + self.task)
        if not closed and not self.artifacts:
            raise ValueError("capability_row_artifacts_required:" + self.task)


@dataclass(frozen=True)
class RuntimeCapabilityTable:
    """This turn's capability rows plus the evidence digest that produced them."""

    rows: tuple[CapabilityRow, ...]
    available_names: frozenset[str]
    diagnostics: tuple[str, ...] = ()
    read_only_report: bool = False
    fusion_state: str = ""
    csv_route: str = CSV_ROUTE_UNAVAILABLE

    def row(self, task: str) -> CapabilityRow | None:
        for item in self.rows:
            if item.task == task:
                return item
        return None

    def runnable(self) -> tuple[CapabilityRow, ...]:
        return tuple(row for row in self.rows if row.status != NOT_RUNNABLE)

    def tools(self) -> frozenset[str]:
        """Every tool the table offers; always a subset of ``available_names``."""
        return frozenset(name for row in self.runnable() for name in row.tools)

    def as_dict(self) -> dict:
        return {
            "version": 1,
            "fusion_state": self.fusion_state,
            "csv_route": self.csv_route,
            "read_only_report": self.read_only_report,
            "available_tool_count": len(self.available_names),
            "rows": [{
                "task": row.task, "label": row.label, "status": row.status,
                "tools": list(row.tools), "modes": list(row.modes),
                "inputs": list(row.inputs), "artifacts": list(row.artifacts),
                "waits": row.waits, "hint": row.hint, "reason_code": row.reason_code,
                "source": row.source, "surface": row.surface,
            } for row in self.rows],
            "diagnostics": list(self.diagnostics),
        }

    def prompt_fragment(self) -> str:
        """The Host prompt block; names no tool the turn does not expose."""
        lines = [self._header()]
        for row in self.runnable():
            lines.append("· " + row.label + "：" + self._tool_clause(row) + " — " + self._detail(row))
        for row in self.rows:
            if row.status != NOT_RUNNABLE or not row.surface:
                continue
            if self.read_only_report and not row.task.startswith("literature."):
                # A read-only report turn forbids everything else as a rule; a
                # per-capability negative would only add noise.
                continue
            reason = ("（" + row.reason_code + "）") if row.reason_code else ""
            lines.append("· 不可执行 · " + row.label + "：" + row.hint + reason)
        lines.append(self._rule())
        return "\n".join(lines)

    def _header(self) -> str:
        if self.read_only_report:
            return "【本轮能力表 · 只读报告：仅可读取已接受的既有报告，不重跑、不重派发】"
        return "【本轮能力表：上述任务的真实执行路径；表外能力以本轮工具清单为准】"

    def _rule(self) -> str:
        if self.read_only_report:
            return ("规则：本表只说明可读取既有报告的路径，不是工具白名单；本轮只读既有报告，"
                    "不得重新执行审查、核验或绘图；按 findings_page.next_offset 续读分页，"
                    "不得编造缺失位置或数量。")
        return ("规则：本表只说明上述任务的执行路径，不是工具白名单；其他工具以本轮实际清单为准，"
                "但表中标注“不可执行”的路径不得虚构、不得承诺完成，也不替换成旧路径；"
                "工具回执是唯一完成证据。")

    @staticmethod
    def _tool_clause(row: CapabilityRow) -> str:
        clause = ", ".join(row.tools)
        if row.modes:
            clause += "（" + " / ".join(row.modes) + "）"
        return clause

    @staticmethod
    def _detail(row: CapabilityRow) -> str:
        parts = ["输入：" + "；".join(row.inputs), "产物：" + "、".join(row.artifacts)]
        parts.append("需要等待" if row.waits else "同步执行")
        if row.hint:
            parts.append(row.hint)
        return "；".join(parts)


@dataclass(frozen=True)
class _Facts:
    available: frozenset[str]
    skills: frozenset[str]
    fusion_state: str
    csv_route: str
    csv_source: str
    arguments: Mapping[str, frozenset[str]]
    sandbox_ready: bool | None
    read_only: bool
    inputs: frozenset[str]
    wait_tools: frozenset[str]

    def has(self, *names: str) -> bool:
        return any(name in self.available for name in names)

    def first(self, *names: str) -> tuple[str, ...]:
        return tuple(name for name in names if name in self.available)

    def args_of(self, name: str) -> frozenset[str]:
        return self.arguments.get(name, frozenset())

    def waits_for(self, tools: Sequence[str]) -> bool:
        return any(name in self.wait_tools for name in tools)


def _wait_tool_names() -> frozenset[str]:
    """The authoritative wait semantics, read from the contracts that own them."""
    names: set[str] = set()
    try:
        names |= set(import_module("src.agent.fusion_tools").FUSION_WAIT_TOOLS)
    except Exception:  # pragma: no cover - optional surface
        pass
    try:
        names |= set(import_module("src.agent.code_tools").EXECUTION_WAIT_TOOL_NAMES)
    except Exception:  # pragma: no cover - optional surface
        pass
    return frozenset(names)


def _artifacts(name: str) -> tuple[str, ...]:
    """The declared output kinds of a tool, from its own ToolSpec row."""
    spec = spec_by_name(name)
    return tuple(spec.produces) if spec is not None else ()


def _argument_map(tool_arguments) -> Mapping[str, frozenset[str]]:
    if not tool_arguments:
        return {}
    out: dict[str, frozenset[str]] = {}
    for name, value in dict(tool_arguments).items():
        if isinstance(value, str):
            out[str(name)] = frozenset(part.strip() for part in value.split(",") if part.strip())
        else:
            out[str(name)] = frozenset(str(part) for part in value)
    return out


def _authorized_skills(value) -> frozenset[str]:
    if not value:
        return frozenset()
    if isinstance(value, Mapping):
        return frozenset(str(key) for key, allowed in value.items() if allowed)
    return frozenset(str(item) for item in value)


def _closed(task: str, label: str, reason_code: str, hint: str, *,
            surface: bool = False, source: str = "") -> CapabilityRow:
    return CapabilityRow(task=task, label=label, status=NOT_RUNNABLE, reason_code=reason_code,
                         hint=hint, surface=bool(surface), source=source)


def _collector_advice(facts: _Facts, *,
                      prefix: str = "按返回的 execution_id 取结果") -> str:
    if facts.has(COLLECT_TOOL):
        return ("用返回的 execution_id 调 " + COLLECT_TOOL +
                " 取本轮结果；追问或交付不要再派发")
    return prefix
def _figure_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    tools = facts.first(FIGURE_TOOL)
    artifacts = _artifacts(FIGURE_TOOL) or ("图件包（SVG/PNG/PDF）",)
    arguments = facts.args_of(FIGURE_TOOL)
    evidence = bool(facts.arguments.get(FIGURE_TOOL))
    template_declared = all(name in arguments for name in FIGURE_TEMPLATE_ARGUMENTS)
    legacy_forward = facts.has("run_nature_plot", "collect_nature_plot")
    forward_note = ("；旧入口仅兼容转发到本入口，不产生第二套执行记录，一律用本入口"
                    if legacy_forward else "")
    if not tools:
        return (
            _closed("figure.script", "科研绘图（脚本模式）", "figure_tool_unavailable",
                    "本轮没有可用的绘图执行工具；不要承诺出图，如实说明限制",
                    surface="figure" in facts.inputs, source="available_names"),
            _closed("figure.template", "科研绘图（固定图型模板）", "figure_tool_unavailable",
                    "本轮没有可用的绘图执行工具；不要以固定模板名义执行或承诺图件",
                    surface="figure" in facts.inputs, source="available_names"),
        )
    rows = [CapabilityRow(
        task="figure.script", label="科研绘图（脚本模式）", status=RUNNABLE, tools=tools,
        modes=("script",) if "mode" in arguments else (),
        inputs=("本账号授权表（CSV/XLSX/PDF，按 file_id+sha256 绑定）与完整脚本或 script_ref",),
        artifacts=artifacts, waits=facts.waits_for(tools),
        hint="沙箱内一次性执行，渲染与 QA 由 Worker 完成，以工具回执与工作台项目为准" + forward_note,
        source="tool_specs+available_names")]
    template_hint = ("固定图型 " + "/".join(FIGURE_TEMPLATES)
                     + " 与脚本模式共用同一冻结输入、Worker、超时、输出合同、QA 与工作台项目")
    if not evidence:
        rows.append(CapabilityRow(
            task="figure.template", label="科研绘图（固定图型模板）", status=RUNNABLE,
            tools=tools, modes=("template",),
            inputs=("单张表 inputs_json（或 analysis_result_ref_json）与类型化 template_parameters_json",),
            artifacts=artifacts, waits=facts.waits_for(tools),
            hint=template_hint + "；模板参数支持以工具参数为准" + forward_note,
            source="tool_contract"))
    elif template_declared:
        rows.append(CapabilityRow(
            task="figure.template", label="科研绘图（固定图型模板）", status=RUNNABLE,
            tools=tools, modes=("template",),
            inputs=("单张表 inputs_json（或 analysis_result_ref_json）与类型化 template_parameters_json",),
            artifacts=artifacts, waits=facts.waits_for(tools),
            hint=template_hint + "，不需要另开入口" + forward_note,
            source="tool_arguments"))
    else:
        rows.append(_closed(
            "figure.template", "科研绘图（固定图型模板）", "figure_template_mode_not_declared",
            "本轮安装的绘图工具参数未声明模板模式（mode/template_id/template_parameters_json）；"
            "不要以固定模板名义执行，改用脚本模式实现或如实说明限制",
            surface="figure" in facts.inputs, source="tool_arguments"))
    return tuple(rows)


def _inspect_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    tools = facts.first(INSPECT_TOOL)
    if not tools:
        return (_closed("table.inspect", "只读查看表格结构", "inspect_tool_unavailable",
                        "本轮没有只读看表工具；不得声称已查看列名、类型或覆盖范围",
                        surface="table" in facts.inputs or "csv" in facts.inputs,
                        source="available_names"),)
    return (CapabilityRow(
        task="table.inspect", label="只读查看表格结构", status=RUNNABLE, tools=tools,
        inputs=("本账号授权表（file_id 或本轮附件；CSV/TSV/Excel/PDF/Word）",),
        artifacts=_artifacts(INSPECT_TOOL) or ("结构摘要（只读，不是交付物）",),
        waits=facts.waits_for(tools),
        hint="只读结构、列名、类型、少量样例与覆盖范围；这不等于数据审查，不能据此声称已完成审查",
        source="tool_specs+available_names"),)


def _legacy_audit_artifacts() -> tuple[str, ...]:
    declared = _artifacts(LEGACY_AUDIT_TOOL) or _artifacts("audit_research_images")
    return declared or ("finding report", "标注工作簿（仅在用户明确要求文件时）")


def _fusion_audit_artifacts() -> tuple[str, ...]:
    """The pinned data-audit contract: a report, and a workbook only on request.

    Publication belongs to the host: only an ``annotated_xlsx`` delivery contract
    produces the annotated workbook, so a text-only request is never promised one.
    """
    return ("审查报告（findings/coverage/review_id）",
            "标注工作簿（仅当本轮交付合同为 annotated_xlsx）")


def _audit_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    worker = facts.first(FUSION_TOOL)
    authorized = SKILL_DATA_AUDIT in facts.skills
    legacy = facts.first(LEGACY_AUDIT_TOOL)
    artifacts = _legacy_audit_artifacts()
    fusion_artifacts = _fusion_audit_artifacts()
    unauthorized = "skill_capability_unauthorized:" + SKILL_DATA_AUDIT
    unauthorized_hint = (SKILL_DATA_AUDIT + " Skill 本轮未授权：不要用它做审查，"
                         "也不要换成旧分析器或通用工具冒充审查")
    rows: list[CapabilityRow] = []
    if worker and authorized:
        rows.append(CapabilityRow(
            task="audit.workbook", label="数据审查（XLSX/XLSM）", status=RUNNABLE, tools=worker,
            inputs=("恰好一个工作簿 inputs={'workbook': {file_id, sha256}}（.xlsx/.xlsm）",
                    "options.regions：1–16 个已由用户本轮确认真实工作表与 A1 的区域，"
                    "header_row 非首行需显式确认表头行"),
            artifacts=fusion_artifacts, waits=facts.waits_for(worker),
            hint=(_collector_advice(facts) + "；XLSM 先做宏安全检查；发布权归宿主，"
                  "只有 annotated_xlsx 交付合同才出标注工作簿"),
            source="fusion_surface+skill"))
    elif legacy:
        rows.append(CapabilityRow(
            task="audit.workbook", label="数据审查（XLSX/XLSM）", status=RUNNABLE, tools=legacy,
            inputs=("本账号授权工作簿（file_ids_json/file_indexes_json）与真实工作表、有界 A1 区域",),
            artifacts=artifacts, waits=facts.waits_for(legacy),
            hint="不能自行确认候选区域，导出复用已保存审查结果",
            source="legacy_tool+available_names"))
    elif worker:
        rows.append(_closed("audit.workbook", "数据审查（XLSX/XLSM）", unauthorized,
                            unauthorized_hint, surface=True, source="skill_authorization"))
    else:
        rows.append(_closed("audit.workbook", "数据审查（XLSX/XLSM）", "data_audit_unavailable",
                            "本轮没有可用的数据审查执行入口；不得声称已完成审查",
                            surface="workbook" in facts.inputs or "table" in facts.inputs,
                            source="available_names"))
    rows.append(_csv_row(facts, worker=worker, authorized=authorized, legacy=legacy,
                         artifacts=artifacts, fusion_artifacts=fusion_artifacts,
                         unauthorized=unauthorized, unauthorized_hint=unauthorized_hint))
    return tuple(rows)


def _csv_row(facts: _Facts, *, worker: tuple[str, ...], authorized: bool,
             legacy: tuple[str, ...], artifacts: tuple[str, ...],
             fusion_artifacts: tuple[str, ...], unauthorized: str,
             unauthorized_hint: str) -> CapabilityRow:
    """CSV is a single logical table: no sheet, no A1 rectangle, no workbook."""

    label = "数据审查（CSV 单表）"
    inspect_note = ("；可先用 " + INSPECT_TOOL + " 只读报告列名与记录范围"
                    if facts.has(INSPECT_TOOL) else "")
    replay = ("重复导出走 deliver_research_artifact(kind=\"data_audit\", review_id=...)，不重扫"
              if facts.has("deliver_research_artifact")
              else "重复导出复用返回的 review_id，不重扫")
    csv_inputs = ("恰好一个单表 CSV inputs={'csv_table': {file_id, sha256}}"
                  "（不接受 sheet、A1 或 regions）",)
    # Only the generic adapter freezes dataset columns from plot_fields; the
    # Worker path does not consume it, so a Worker row never advertises it.
    generic_inputs = csv_inputs + (
        "可选 options.plot_fields：1–64 个列名的冻结 dataset 列投影",)
    if facts.csv_route == CSV_ROUTE_WORKER:
        if worker and authorized:
            return CapabilityRow(
                task="audit.csv", label=label, status=RUNNABLE, tools=worker,
                inputs=csv_inputs, artifacts=fusion_artifacts, waits=True,
                hint=_collector_advice(facts) + "；CSV 不接受 regions；发布权归宿主",
                source="csv_route:worker+" + facts.csv_source)
        if worker:
            return _closed("audit.csv", label, unauthorized,
                           unauthorized_hint + inspect_note, surface=True,
                           source="csv_route:worker+skill_authorization")
        return _closed("audit.csv", label, "csv_worker_tool_unavailable",
                       "CSV 的 Worker 路由已具备但本轮工具面未开放；不得声称已审查" + inspect_note,
                       surface=True, source="csv_route:worker+available_names")
    if facts.csv_route == CSV_ROUTE_GENERIC:
        if facts.csv_source == "legacy_fallback" and legacy:
            return CapabilityRow(
                task="audit.csv", label=label, status=RUNNABLE, tools=legacy,
                inputs=("单表 CSV（file_ids_json）与列名、记录范围（不编造工作表名或单元格矩形）",),
                artifacts=artifacts, waits=facts.waits_for(legacy),
                hint="Fusion CSV 路由本轮未开放，回退既有通用审查工具：按列名与记录范围说明覆盖，"
                     + replay,
                source="csv_route:legacy_fallback")
        if worker and authorized:
            return CapabilityRow(
                task="audit.csv", label=label, status=RUNNABLE, tools=worker,
                inputs=generic_inputs, artifacts=fusion_artifacts, waits=False,
                hint=("复用通用审查：真实执行并返回 status/route/review_id/findings/coverage/"
                      "result_refs，冻结 dataset 原样透传；不产生 execution_id，不需要等待；" + replay),
                source="csv_route:generic+" + facts.csv_source)
        if legacy:
            return CapabilityRow(
                task="audit.csv", label=label, status=RUNNABLE, tools=legacy,
                inputs=("单表 CSV（file_ids_json）与列名、记录范围（不编造工作表名或单元格矩形）",),
                artifacts=artifacts, waits=facts.waits_for(legacy),
                hint="按列名与记录范围说明覆盖，" + replay,
                source="csv_route:generic+legacy_tool")
        if worker:
            return _closed("audit.csv", label, unauthorized,
                           unauthorized_hint + inspect_note, surface=True,
                           source="csv_route:generic+skill_authorization")
        return _closed("audit.csv", label, "csv_generic_route_tool_unavailable",
                       "CSV 的通用审查路由已具备但本轮工具面未开放；不得声称已审查" + inspect_note,
                       surface=True, source="csv_route:generic+available_names")
    surface = facts.has(FUSION_TOOL, LEGACY_AUDIT_TOOL) or "csv" in facts.inputs
    return _closed(
        "audit.csv", label, "external_csv_table_audit_unavailable",
        "本轮没有可用的 CSV 审查执行入口（既没有可读单表的 native Worker，也没有可用的通用审查路由）；"
        "不得声称已完成审查，不得把 CSV 伪装成工作簿或编造工作表名" + inspect_note
        + "，并如实说明限制",
        surface=surface, source="csv_route:unavailable+" + facts.csv_source)


def _literature_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    tools = facts.first(FUSION_TOOL, CONTINUE_TOOL, COLLECT_TOOL)
    authorized = SKILL_LITERATURE in facts.skills
    legacy = facts.first(LEGACY_VERIFY_TOOL)
    fusion_artifacts = ("核验报告（findings/evidence/locations/coverage/limitations）",)
    artifacts = _artifacts(LEGACY_VERIFY_TOOL) or fusion_artifacts
    unauthorized = "skill_capability_unauthorized:" + SKILL_LITERATURE
    rows: list[CapabilityRow] = []
    if FUSION_TOOL in tools and authorized:
        continuing = ("同一执行批量补查只用 " + CONTINUE_TOOL + " 且仅当回执 can_continue 为真"
                      if CONTINUE_TOOL in tools else "不要重复派发同一执行")
        collecting = ("，最后用 " + COLLECT_TOOL + " 收证据与缺口"
                      if COLLECT_TOOL in tools else "")
        rows.append(CapabilityRow(
            task="literature.verify", label="文献核验", status=RUNNABLE, tools=tools,
            inputs=("bibliography（.bib/.pdf）与/或类型化 references；不接受原始观察或 URL",),
            artifacts=fusion_artifacts, waits=facts.waits_for(tools),
            hint=(_collector_advice(facts)
                  + "；" + continuing + collecting + "；通用搜索结果不能替代核验回执"),
            source="fusion_surface+skill"))
    elif legacy:
        rows.append(CapabilityRow(
            task="literature.verify", label="文献核验", status=RUNNABLE, tools=legacy,
            inputs=("本轮书目文件或类型化条目",), artifacts=artifacts,
            waits=facts.waits_for(legacy),
            hint="以工具回执为准，通用搜索结果不能替代核验回执",
            source="legacy_tool+available_names"))
    elif tools:
        rows.append(_closed("literature.verify", "文献核验", unauthorized,
                            SKILL_LITERATURE + " Skill 本轮未授权：不要用它核验文献，"
                            "也不要用通用搜索冒充核验回执", surface=True,
                            source="skill_authorization"))
    else:
        rows.append(_closed("literature.verify", "文献核验", "literature_surface_unavailable",
                            "本轮没有文献核验执行入口；不得声称已核验或给出未经验证的核验结论",
                            surface="bibliography" in facts.inputs, source="available_names"))
    followup = facts.first(FOLLOWUP_TOOL)
    if followup:
        rows.append(CapabilityRow(
            task="literature.followup", label="既有报告追问（只读）", status=READ_ONLY,
            tools=followup, inputs=("本轮已接受的既有报告与 findings_offset",),
            artifacts=("报告分页（findings_page）",), waits=False,
            hint="只读：不重跑算法、不重派发、不改写历史；按 findings_page.next_offset 续读，"
                 "不得编造缺失位置或数量",
            source="report_followup+available_names"))
    elif facts.read_only:
        rows.append(_closed("literature.followup", "既有报告追问（只读）",
                            "report_followup_requires_accepted_report",
                            "本轮没有可读取的既有报告；不得重新执行审查或编造报告内容",
                            surface=True, source="report_followup"))
    return tuple(rows)


def _integrity_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    tools = facts.first(FUSION_TOOL)
    authorized = SKILL_INTEGRITY in facts.skills
    artifacts = ("诚信核查报告（findings/evidence/coverage）",)
    if tools and authorized:
        return (CapabilityRow(
            task="research.integrity", label="科研诚信核查", status=RUNNABLE, tools=tools,
            inputs=("授权图像/数据/报告文件（按 file_id+sha256 绑定对应角色）",),
            artifacts=artifacts, waits=facts.waits_for(tools),
            hint=_collector_advice(facts) + "；结论以回执证据为准",
            source="fusion_surface+skill"),)
    if tools:
        return (_closed("research.integrity", "科研诚信核查",
                        "skill_capability_unauthorized:" + SKILL_INTEGRITY,
                        SKILL_INTEGRITY + " Skill 本轮未授权：不要用它核查，"
                        "也不要用通用工具冒充诚信结论", surface=True,
                        source="skill_authorization"),)
    return (_closed("research.integrity", "科研诚信核查", "integrity_surface_unavailable",
                    "本轮没有科研诚信核查入口；不得给出未经验证的诚信结论",
                    surface="image" in facts.inputs or "table" in facts.inputs,
                    source="available_names"),)


def _code_rows(facts: _Facts) -> tuple[CapabilityRow, ...]:
    rows: list[CapabilityRow] = []
    native = facts.first(SANDBOX_EXECUTE_TOOL)
    if native:
        if facts.sandbox_ready is False:
            rows.append(_closed(
                "sandbox.execute", "原生代码执行（隔离沙箱）", "sandbox_unavailable",
                "沙箱后端不可用：不得回退宿主 shell，也不得声称已执行；如实说明限制",
                surface=True, source="sandbox_ready"))
        else:
            note = ("；沙箱不可用时只返回 not_run，绝不回退宿主 shell"
                    if facts.sandbox_ready is None else "")
            rows.append(CapabilityRow(
                task="sandbox.execute", label="原生代码执行（隔离沙箱）", status=RUNNABLE,
                tools=native, inputs=("run-scoped 工作区中已写入的脚本与已绑定输入",),
                artifacts=("退出码、日志引用与输出引用",), waits=facts.waits_for(native),
                hint="只在隔离沙箱内执行，与离线代码 Worker 不是同一门禁" + note,
                source="available_names+sandbox_ready"))
    codes = facts.first(*CODE_TOOLS)
    if "code_execute" in codes:
        pollers = facts.first("code_status", "code_log")
        poll = ("用 " + "/".join(pollers) + " 读回执" if pollers
                else "按 execution_id 取回执")
        deliverers = facts.first("code_read", "code_deliver")
        deliver = ("，" + "/".join(deliverers) + " 取产物" if deliverers else "")
        rows.append(CapabilityRow(
            task="code.worker", label="离线代码 Worker", status=RUNNABLE, tools=codes,
            inputs=("任务工作区中的脚本与 /input 快照（按工作区版本绑定）",),
            artifacts=("执行回执、受限日志与已声明输出",), waits=True,
            hint=("离线 Worker：先建脚本再提交，" + poll + deliver
                  + "；与原生沙箱执行不是同一门禁"),
            source="code_tool_surface"))
    elif codes:
        rows.append(_closed("code.worker", "离线代码 Worker", "code_execute_unavailable",
                            "本轮只开放了部分代码工具，不能提交执行；不得声称已运行代码",
                            surface="code" in facts.inputs, source="code_tool_surface"))
    return tuple(rows)


def _diagnostics(facts: _Facts, rows: Sequence[CapabilityRow]) -> tuple[str, ...]:
    out = ["csv_route=" + facts.csv_route + " (" + facts.csv_source + ")"]
    if not facts.csv_source.startswith(("fusion_signal", "legacy_fallback", "explicit")):
        # No route contract answered: the route was derived from readiness facts.
        out.append("csv_route_contract_missing")
    if facts.fusion_state == FUSION_STATE_NEW and facts.csv_route == CSV_ROUTE_UNAVAILABLE:
        out.append("csv_route_signal_missing")
    if not facts.arguments and facts.has(FIGURE_TOOL):
        out.append("tool_arguments_missing")
    if facts.sandbox_ready is None and facts.has(SANDBOX_EXECUTE_TOOL):
        out.append("sandbox_readiness_unknown")
    closed = [row.task for row in rows if row.status == NOT_RUNNABLE]
    if closed:
        out.append("closed=" + ",".join(closed))
    surfaced = [row.task for row in rows if row.status == NOT_RUNNABLE and row.surface]
    if surfaced:
        out.append("surfaced=" + ",".join(surfaced))
    return tuple(out)


def build(*, available_names=(), authorized_skills=(), fusion_state="", csv_route=None,
          tool_arguments=None, sandbox_ready=None, read_only_report=False,
          turn_inputs=(), release=None) -> RuntimeCapabilityTable:
    """Build this turn's capability table from host evidence.

    ``csv_route`` is the explicit three-state fact; when it is omitted the route
    is resolved from fusion's signal, then the analyzer support bit, then the
    generic reviewer. An unknown explicit value fails closed instead of being
    silently downgraded.
    """
    available = frozenset(str(name) for name in (available_names or ()))
    if csv_route is None:
        route, route_source = resolve_csv_route(
            release=release, fusion_state=str(fusion_state or ""), available_names=available)
    else:
        route = normalize_csv_route(csv_route)
        if route not in CSV_ROUTES:
            raise ValueError("unsupported_csv_route:" + str(csv_route))
        route_source = "explicit"
    facts = _Facts(
        available=available, skills=_authorized_skills(authorized_skills),
        fusion_state=str(fusion_state or ""), csv_route=route, csv_source=route_source,
        arguments=_argument_map(tool_arguments), sandbox_ready=sandbox_ready,
        read_only=bool(read_only_report),
        inputs=frozenset(str(kind) for kind in (turn_inputs or ())),
        wait_tools=_wait_tool_names())
    rows = (*_figure_rows(facts), *_inspect_rows(facts), *_audit_rows(facts),
            *_literature_rows(facts), *_integrity_rows(facts), *_code_rows(facts))
    return RuntimeCapabilityTable(
        rows=rows, available_names=available, diagnostics=_diagnostics(facts, rows),
        read_only_report=facts.read_only, fusion_state=facts.fusion_state,
        csv_route=facts.csv_route)