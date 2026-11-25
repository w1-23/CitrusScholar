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
    """Run one pinned Nature Figure Python template (volcano, ROC, dotplot, marginal scatter, or paired) on the whole authorized UTF-8 CSV. Use the exact file_id and SHA-256 and explicitly map known columns by their research meaning, not position. It does not run hypothesis tests, accept code or paths, render demos, auto-convert workbooks, or execute R. Set drop_incomplete only after an explicit scientific decision to exclude observations. The Worker runs offline and this first call only submits. After its trusted wait settles, call collect_nature_plot with the returned execution_id. Do not retry a still-working execution."""
    from src.research.nature_skill_worker import NatureSubmissionUncertain, schedule_plot

    try:
        identity, release, pin = _bound()
        spec = RunArgs(backend=backend, input=input, plot=plot, title=title)
        if backend == "r":
            return _response({"backend": "r", "status": "unsupported", "detail":
                "当前隔离 Worker 没有固定的 R 运行时和绘图依赖。未运行 R，也没有改用 Python 代画。"},
                "not_run", "nature_plot_r_runtime_unavailable")
        row = schedule_plot(identity, release=release, pin=pin, spec=spec)
        execution_id = row["execution_id"]
        return _response({"execution_id": execution_id, "worker_status": row["status"],
                          "backend": backend, "template": spec.plot.template, "completion_scope": "submission_only",
                          "instruction": "等待框架恢复后，以 execution_id 调用 collect_nature_plot。"},
                         "complete", "nature_plot_submitted", wait_id=execution_id)
    except NatureSubmissionUncertain:
        return _response({"error": "当前无法确认 Worker 是否已受理。请先对账，不要再次提交。"},
                         "unknown", "nature_plot_submission_requires_reconciliation")
    except (LookupError, PermissionError, ValueError, OSError) as exc:
        reason = str(exc)
        if reason == "nature_plot_code_worker_unavailable":
            return _response({"status": "unsupported", "detail": "当前无可用的隔离 Code Worker。"},
                             "not_run", reason)
        if reason == "nature_plot_requires_nonempty_utf8_csv_within_worker_limit":
            return _response({"status": "unsupported", "detail": "此固定图型仅接受当前任务已授权附加的非空 UTF-8 CSV（最大 32 MiB）。"},
                             "not_run", reason)
        return _response({"error": "未通过输入、权限或固定版本校验，未获得执行回执。"},
                         "not_run", "nature_plot_not_admitted")


@tool(args_schema=CollectArgs, response_format="content_and_artifact")
def collect_nature_plot(execution_id):
    """Collect exactly this turn's settled Nature template execution. Do not pass file paths, URLs, data or a title. Returns verified unchanged SVG/PDF/600 dpi TIFF, upstream row-count QA, and the editable Figure Workbench route if supported, otherwise download-only. Row-count QA does not certify visual or scientific correctness. Do not call again after verified collection or claim a workbench link when the result says download-only."""
    from src.research.nature_skill_worker import collect_plot

    receipts = []
    try:
        identity, release, pin = _bound()
        data, status, reason = collect_plot(identity, release=release, pin=pin,
                                               execution_id=execution_id, receipts=receipts)
        if status == "failed":
            data["detail"] = "隔离 Worker 未生成完整结果。核对 CSV 格式、列映射和该图型的数据条件；未发布图件。"
        return _response(data, status, reason, receipts=receipts)
    except (LookupError, PermissionError, ValueError, OSError):
        if receipts:
            return _response({"execution_id": execution_id,
                "verified_original_links": [item["url"] for item in receipts],
                "error": "并非所有原件和工作台结果均可核验。请使用已确认链接。"},
                "partial", "nature_plot_delivery_incomplete", receipts=receipts)
        return _response({"error": "没有可核验的当前执行结果。"},
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
