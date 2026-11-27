"""AgentCore entrypoints for executable pinned Nature Figure templates."""
from __future__ import annotations

from functools import partial
import json
from pathlib import Path

from langchain_core.tools import tool

from src.research.nature_skill_contract import CollectArgs, RunArgs, SKILL_ID, pinned_template

NATURE_WAIT_TOOLS = frozenset({"run_nature_plot"})


def _pin(release, rows):
    if not release:
        raise ValueError("nature_plot_installed_release_required")
    version = next((row["version"] for row in rows if row["id"] == SKILL_ID), None)
    return pinned_template(Path(release), version)


def installed_nature(release, rows):
    """Allow discovery only for the verified executable release, never archived docs."""
    try:
        _pin(release, rows)
        return True
    except (ImportError, KeyError, LookupError, OSError, TypeError, ValueError):
        return False


def _bound():
    from src.core.capability_policy import allows
    from .code_tools import _context, _identity
    from .installed_skills import list_installed

    identity, _ = _identity()
    release = _context.get()[2]
    if release is None or not allows("skill:" + SKILL_ID):
        raise PermissionError("nature_plot_capability_unavailable")
    return identity, release, _pin(release, list_installed(Path(release)))


def _response(data, status, reason, *, receipts=(), wait_id=None):
    from src.research.delivery_reconciliation import delivery_outcome

    artifact = {**data, "_tool_outcome": delivery_outcome(status, reason, list(receipts))}
    if wait_id:
        artifact["_code_execution_id"] = wait_id
    return json.dumps(data, ensure_ascii=False, allow_nan=False), artifact


@tool(args_schema=RunArgs, response_format="content_and_artifact")
def run_nature_plot(backend, input, plot, title=None):
    """[DEPRECATED — forwards to run_research_figure_script mode="template"] 固定模板绘图统一入口已迁移。新任务请直接用 run_research_figure_script：mode="template"、template_id（volcano/ROC/dotplot/marginal/paired）、typed template_parameters_json 和单张表 inputs_json。本工具在兼容窗口内只把同一请求转发给统一绘图执行，返回的 execution_id 就是统一运行 id，不创建第二套执行记录。R 后端尚未统一，按 unsupported 返回，不自动切换 Python。"""
    from src.research.nature_skill_worker import NatureSubmissionUncertain, schedule_plot

    try:
        identity, release, pin = _bound()
        spec = RunArgs(backend=backend, input=input, plot=plot, title=title)
        row = schedule_plot(identity, release=release, pin=pin, spec=spec)
        execution_id = row["execution_id"]
        status = str(row.get("status") or "queued")
        artifact = row.get("artifact") if isinstance(row.get("artifact"), dict) else {}
        figures = [item for item in (artifact.get("figures") or []) if isinstance(item, dict)]
        figure = figures[0] if figures else {}
        settled = status in {"succeeded", "failed", "partial"}
        data = {"execution_id": execution_id, "worker_status": status, "backend": backend,
                "template": spec.plot.template, "deprecated": True,
                "superseded_by": "run_research_figure_script mode=template",
                "forwarded_to": "run_research_figure_script" if row.get("forwarded") else "",
                "completion_scope": "complete" if settled else "submission_only",
                "instruction": ("固定模板已通过统一绘图入口执行；无需再次提交。"
                                if settled else "等待框架恢复后，以 execution_id 调用 collect_nature_plot。")}
        receipts = []
        if settled:
            data.update(figure_id=figure.get("figure_id") or "",
                        workspace_url=figure.get("workspace_url") or "",
                        exports=[item for item in (figure.get("exports") or [])])
            receipts = [{"url": item["artifact_url"]} for item in data["exports"]
                        if isinstance(item, dict) and item.get("artifact_url")]
        if status == "failed":
            return _response({**data, "error": "统一绘图执行未发布图件；具体阶段见失败回执。"},
                             "failed", "nature_plot_worker_failed", receipts=receipts)
        return _response(data, "complete",
                         "nature_plot_forwarded_to_unified_execution" if settled
                         else "nature_plot_submitted",
                         receipts=receipts, wait_id=None if settled else execution_id)
    except NatureSubmissionUncertain:
        return _response({"error": "当前无法确认 Worker 是否已受理。请先对账，不要再次提交。",
                          "deprecated": True, "superseded_by": "run_research_figure_script mode=template"},
                         "unknown", "nature_plot_submission_requires_reconciliation")
    except (LookupError, PermissionError, ValueError, OSError) as exc:
        reason = str(exc)
        if reason in {"nature_plot_r_runtime_unavailable", "nature_plot_r_template_unavailable"}:
            detail = ("当前未启用本版本验证的隔离 R Worker 镜像。"
                      if reason == "nature_plot_r_runtime_unavailable"
                      else "固定 R 图型当前仅支持散点图与两轴边缘分布（marginal）。")
            return _response({"backend": "r", "status": "unsupported",
                              "detail": detail + "未运行，也未切换绘图后端。"}, "not_run", reason)
        if reason == "nature_plot_r_backend_not_unified":
            return _response({"backend": "r", "status": "unsupported",
                              "detail": "R 固定模板尚未接入统一绘图后端；共享身份、预算、deadline、"
                                        "网络策略、回执与工作台合同全部通过前不会对 Agent 宣称通用 R 绘图可用。"
                                        "未运行，也未切换 Python。",
                              "deprecated": True},
                             "not_run", "nature_plot_r_backend_not_unified")
        if reason == "nature_plot_code_worker_unavailable":
            return _response({"status": "unsupported", "detail": "当前无可用的隔离 Code Worker。"},
                             "not_run", reason)
        if reason == "nature_plot_requires_nonempty_utf8_csv_within_worker_limit":
            return _response({"status": "unsupported", "detail": "此固定图型仅接受当前任务已授权附加的非空 UTF-8 CSV（最大 32 MiB）。"},
                             "not_run", reason)
        if reason in {"nature_plot_forward_to_unified_execution_unavailable", ""}:
            return _response({"error": "统一绘图执行当前不可用；请改用 run_research_figure_script "
                                       "mode=\"template\" 绑定同一数据后重试。",
                              "deprecated": True}, "not_run", "nature_plot_forward_unavailable")
        return _response({"error": "未通过输入、权限或固定版本校验，未获得执行回执。"},
                         "not_run", "nature_plot_not_admitted")


@tool(args_schema=CollectArgs, response_format="content_and_artifact")
def collect_nature_plot(execution_id):
    """[DEPRECATED — reads the unified execution receipt] Collect exactly this turn's settled execution by its unified execution_id. Do not pass paths, URLs, data or a title. Returns the verified unchanged SVG/PDF links and the editable Figure Workbench route published by the one figure chain. It publishes nothing itself and cannot create a second execution record. Do not call again after verified collection."""
    from src.research.nature_skill_worker import collect_plot

    receipts = []
    try:
        identity, release, pin = _bound()
        data, status, reason = collect_plot(identity, release=release, pin=pin,
                                               execution_id=execution_id, receipts=receipts)
        data.setdefault("deprecated", True)
        data.setdefault("superseded_by", "run_research_figure_script mode=template")
        if status == "failed":
            data["detail"] = "统一绘图执行未生成完整结果。核对 CSV 格式、列映射和该图型的数据条件；未发布图件。"
        return _response(data, status, reason, receipts=receipts)
    except (LookupError, PermissionError, ValueError, OSError):
        if receipts:
            return _response({"execution_id": execution_id,
                "verified_original_links": [item["url"] for item in receipts],
                "error": "并非所有原件和工作台结果均可核验。请使用已确认链接。"},
                "partial", "nature_plot_delivery_incomplete", receipts=receipts)
        return _response({"error": "没有可核验的当前执行结果。", "deprecated": True},
                         "not_run", "nature_plot_collection_unavailable")


_TOOLS = (run_nature_plot, collect_nature_plot)
NATURE_TOOL_NAMES = frozenset(item.name for item in _TOOLS)


def _load(item):
    from src.tools.registry import register_tool

    register_tool(item, category="research", is_readonly=False, concurrency_key="nature_plot",
                  write_scope="session_artifact", recovery_mode="never")
    return item


def register_nature_tools(catalog):
    from .capabilities import CapabilityDefinition

    for item in _TOOLS:
        catalog.register(CapabilityDefinition(item.name, item.description, item.args_schema.model_json_schema(),
            partial(_load, item), provider="nature_plot", mutates=True, skill_id=SKILL_ID, execution_mode="sequential"))
