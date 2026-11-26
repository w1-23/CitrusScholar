"""Publish scientific figures produced by the isolated script runner."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from langchain_core.tools import tool

from src.core.tool_errors import ToolArgumentError
from src.figures.nature_receipts import current_qa_summary

# The formats a figure project carries. SVG is the primary one because its
# text stays real text; PNG and PDF are what a journal asks for.
FIGURE_FORMATS = ("svg", "png", "pdf")

# The MIME type of each format.
MIME_BY_FORMAT = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
}

# A script longer than this is not a figure script; it is a program.
_MAX_SCRIPT_CHARS = 200_000


def _json_arg(raw: Any, name: str) -> Any:
    """Accept either a JSON string or an already-decoded value."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list, tuple)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            raise ToolArgumentError(
                f"{name} 需要 JSON 文本；例如 {{\"chart\": \"bar\"}}。") from None
    raise ToolArgumentError(f"{name} 需要 JSON 文本。")


def _requested_formats(raw: Any) -> tuple[str, ...] | None:
    formats = _json_arg(raw, "formats_json")
    if formats is None:
        return None
    if (not isinstance(formats, (list, tuple)) or not formats
            or any(not isinstance(fmt, str) or fmt.strip().lower() not in FIGURE_FORMATS for fmt in formats)):
        raise ToolArgumentError('formats_json 需要格式数组，例如 ["svg", "png", "pdf"]。')
    return tuple(dict.fromkeys(fmt.strip().lower() for fmt in formats))


def _owner() -> int:
    from src.core.tracing import get_user_id
    return int(get_user_id() or 0)


def _session() -> str:
    from src.core.tracing import get_session_id
    return str(get_session_id() or "")


def _error(code: str, message: str, *, execution_stage: str = "not_run") -> tuple[str, dict]:
    """A failed tool call, in the shape the executor reports.

    The reason code is what the execution ledger records, so it names the
    *kind* of failure rather than repeating the message.
    """
    result = {
        "error": message,
        "execution_stage": execution_stage,
        "_tool_outcome": {"status": "failed", "reason_code": code, "artifacts": []},
    }
    return f"[FIGURE_{code}] {message}", result


def _publish(filename: str, payload: bytes, mime: str,
             logical_task_id: str = "") -> dict[str, Any]:
    """Publish one file into the account library and describe it.

    The bytes that are stored are the bytes that were produced. The
    registration is verified against the stored file's digest before the
    link is handed back, so a link always points at the content the caller
    produced.
    """
    from src.research.artifacts import publish_artifact
    from src.core.tracing import get_client_request_id, get_current_tool_name

    if not isinstance(payload, bytes) or not payload:
        raise ValueError("图件导出为空。")
    entry = publish_artifact(
        owner=_owner(), session_id=_session(), filename=filename,
        content=payload, mime=mime,
        producer_tool=get_current_tool_name() or "run_research_figure_script",
        request_id=get_client_request_id() or "",
        logical_task_id=logical_task_id)
    if (not entry.get("file_id") or not entry.get("artifact_url") or entry.get("size") != len(payload)
            or entry.get("sha256") != hashlib.sha256(payload).hexdigest()):
        raise ValueError("图件登记未能证明与实际导出字节一致。")
    return entry


def _register_figure(svg_bytes: bytes, alt_text: str, export: dict, *, workbench_url: str = "") -> str:
    """Register the figure for the chat inline preview.

    The existing figure registry turns a rendered SVG into the
    ``[[FIGURE:n]]`` placeholder that the chat expands into an inline
    preview. The SVG here has already been flattened into the plain subset
    the browser renderer accepts, so the inline preview and the downloaded
    file are the same drawing.
    """
    from src.core.figures import register
    index, _instruction = register(svg_bytes.decode("utf-8"), alt_text,
        download_url=export.get("download_url") or export["artifact_url"], workbench_url=workbench_url)
    return f"[[FIGURE:{index}]]"


def _select_script_runner():
    from src.config import settings
    from src.figures.sandbox import select_backend
    return select_backend(
        backend=str(settings.FIGURES_SANDBOX_BACKEND or ""),
        image=str(settings.FIGURES_SANDBOX_IMAGE or ""),
        image_sha256=str(settings.FIGURES_SANDBOX_IMAGE_SHA256 or ""),
        runtime=str(settings.FIGURES_SANDBOX_RUNTIME or ""),
        allow_local=bool(settings.FIGURES_SANDBOX_ALLOW_LOCAL),
        wsl_distribution=str(settings.FIGURES_SANDBOX_WSL_DISTRO))


def _run_research_figure_script(
    script: str = "",
    title: str = "",
    formats_json: str | list[str] = "",
    output_name: str = "",
    inputs_json: str | dict = "",
    qa_profile_json: str | dict = "",
    analysis_result_ref_json: str | dict = "",
    outputs_json: str | dict = "",
    script_record: dict | None = None,
    _sandbox_runner=None,
) -> tuple[str, dict]:
    """在一次性隔离Python环境中探索/整理数据，生成命名科研图（SVG/PNG/PDF）。

    首选的科研绘图路径，配合 figure-export 的 nature-figure 工作流。
    仅在服务端已配置隔离沙箱时可用；未配置时本工具不会出现。
    三种隔离环境：Docker、WSL-Docker、Apptainer；脚本仍不联网、不建子进程。
    工作簿请先只读探查：用 inspect_uploaded_table 查看工作表、列名、类型和少量样例，
    不要把整本工作簿、全表或分页内容写进脚本字面量；预览截短不代表源数据截短。
    Matplotlib 和产生 Matplotlib Artist 的 Seaborn 均支持，不承诺其他后端。
    先按已加载的 figure-export/nature-figure 工作流确定问题、变量与单位；
    高唯一率编号不默认作处理组，不能默取第一数值列或截前 N 行。多指标按
    研究目的分面/热图，保留原始点及明确的聚合/变换；缺少关键设计时询问。
    单套底部/左侧刻度，实际 draw 后验证标签密度及固定画布边界；布局不合格
    会返回可修正错误，不能以文件生成代替科学表达与视觉检查。
    inputs_json 是变量名到当前账号文件的 JSON 映射，例如
    {"data":{"file_id":"..."}}；脚本中 pd.read_csv(data) 或 pd.read_excel(data,
    sheet_name=..., header=...) 读取完整固定字节快照。模型只需列/类型/维度与
    少量样例的结构摘要；不要分页读出、复述全表或把全表手写进脚本字面量。
    预览截短不等于源数据截短，不用 nrows/head 代替全表；纯绘图不需先做推断检验。
    不允许路径；快照保存摘要并固定引用，原文件变更需新建图件来源。
    analysis_result_ref_json 接收统计工具返回的 analysis_result_ref，按账号核验；
    analysis_result 变量是全精度结果 JSON Path，analysis_data 是对应 CSV Path。
    qa_profile_json 支持 name(scientific/nature_single/nature_double)、final_size_mm、
    min_font_pt、min_linewidth_pt、caption，返回实际测量及明确的 unsupported 检查。
    inputs_json也可绑定 {"tidy":{"dataset_ref":上一执行返回的dataset_ref}}，
    用 df=load_dataset(tidy) 读取保留类型/精度的内部表；普通文件仍用pandas读取。
    可选 outputs_json.input_contract 版本 1 区分原始文件和读入的表。raw 声明 Path 变量。tables 逐项声明 name、input、format（csv/tsv/excel/dataset）及需要时的 sheet、header，主脚本收到对应 DataFrame。独立 renderer 只收到冻结表映射 data。可用 render 指定 renderer 键到父表的映射。emit.table 的 parents 使用 inputs_json 中的原始绑定名。省略 input_contract 时沿用旧变量合同。
    默认用冻结表与独立渲染的outputs_json版本2协议，支持后续保真样式编辑。例如：
    {"version":2,"tables":[{"name":"tidy","parents":["data"]}],
    "figures":[{"name":"relations","title":"变量关系","formats":["svg"],
    "parents":["tidy"],"render_script":"frame=data['tidy']; fig,ax=plt.subplots(); ax.scatter(frame['x'],frame['y'])"}]}。
    主script只读取、整理、分析输入，用emit.table('tidy',df,parents=['data'])保存完整结果。
    render_script通过data['tidy']取得冻结DataFrame，仅构造并保留一个Figure；不调用emit、
    不读取主脚本变量/输入路径、不重新抽样或分析。渲染所需数值及标签先保存在表内。
    最多8表/8图。每张独立图声明不同的 name；只有用户明确要求科学组合图时才共用一个多面板 Figure。
    导出和Nature审核由worker完成，不自行savefig。
    旧混写v2仍兼容：未声明render_script的图必须emit.figure('relations',fig)明确登记，
    只创建Figure或末尾写fig不算登记；该路径可下载，但没有保真样式编辑凭证。
    emit.report(value)最多调用一次，集中提交结构化观察；过程日志用print，不把报告接口当追加日志。
    emit.table的单元格须为标量（字符串、数值、布尔或空），dict/list应拆列或显式编码为JSON字符串。
    用户要求图件时声明至少一张图，实际导出并登记后才有交付凭据。
    纯探索/整理可声明空figures，emit.report返回有界观察；中间表不作默认附件，
    下一执行使用返回的dataset_ref；一次执行也可整理并绘图，不机械分步。
    用户已指定 Python 或图型时直接沿用，只有影响科学含义的未知选择才询问。
    有匹配的内置模板时优先按引用和参数执行，仅传必要参数。
    修复失败时先读取已保存run_id/tool_call_id的具体审核失败项，并复用真实输入与脚本版本；
    不用演示数据、占位图或降低字号/审核要求替代用户要的图件，不把调试图当正式成果。
    图件parents只引用输入绑定名或命名中间表；未声明时保守依赖全部输入与表。
    outputs_json省略时兼容旧协议，最后恰好保留一张Figure。所有脚本必须使用Agg、
    不联网、不创建子进程、不访问沙箱以外文件；类型不明或关键设计不明先探索/必要提问。

    script 是完整的 Python 源码，例如：
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.bar(['CK', 'T1'], [10.1, 12.2], color='#0F4D92', label='果实重量')
    ax.set_ylabel('g'); ax.set_title('果实重量')

    formats_json：覆盖各输出的请求格式。省略时沿用输出契约，没有契约时默认 SVG。
    output_name：文件名（不含扩展名）。

    失败时返回 [FIGURE_...] 形式的说明，指出问题与改法；沙箱不会执行任何
    需要网络或特权的操作。
    """
    from src.figures.sandbox import SandboxJob, SandboxUnavailable, select_backend
    from src.config import settings

    text = str(script or "")
    if not text.strip():
        return _error("ARG", "script 不能为空。")
    if len(text) > _MAX_SCRIPT_CHARS + (65000 if script_record else 0):
        return _error("ARG", f"脚本超过 {_MAX_SCRIPT_CHARS} 字符上限，请精简后重试。")

    try:
        requested_formats = _requested_formats(formats_json)
    except ToolArgumentError as exc:
        return _error("ARG", str(exc))
    formats = requested_formats or ("svg",)

    runner = _sandbox_runner if _sandbox_runner is not None else _select_script_runner()
    if runner is None:
        return _error("SANDBOX_UNAVAILABLE",
                      "当前环境没有配置绘图脚本沙箱，无法执行自定义绘图脚本；"
                      "请说明当前环境无法执行绘图脚本。")

    from src.figures.projects import snapshot_script_inputs, script_replay_inputs, ProjectError
    from src.figures.spec import parse_qa_profile, SpecError
    try:
        from src.figures.script_outputs import normalize_output_contract
        contract = normalize_output_contract(_json_arg(outputs_json, "outputs_json"))
        if contract and requested_formats is not None:
            contract["figures"] = [{**figure, "formats": list(requested_formats)}
                                   for figure in contract["figures"]]
        bindings = _json_arg(inputs_json, "inputs_json") or {}
        profile = parse_qa_profile(_json_arg(qa_profile_json, "qa_profile_json"))
        internal_file_ids = []
        replay_snapshots = (script_record or {}).get("replay_input_snapshots")
        analysis_ref = _json_arg(analysis_result_ref_json, "analysis_result_ref_json")
        analysis_meta = {}
        if analysis_ref:
            from src.research.analysis_results import load_analysis_result, load_dataset_snapshot
            analysis = load_analysis_result(_owner(), analysis_ref)
            result_payload = analysis.to_dict()
            dataset_ref = result_payload["dataset_ref"]
            dataset = load_dataset_snapshot(_owner(), dataset_ref)
            # These are actual persisted full-precision outputs, never values
            # copied from a language-model explanation of the analysis.
            if replay_snapshots is None:
                from src.session.files import register_file
                analysis_file = register_file(session_id=_session(), owner_user_id=_owner(),
                    filename="figure_analysis_result.json", visibility="internal",
                    content=json.dumps(result_payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                    mime="application/json", producer_tool="run_research_figure_script")
                data_file = register_file(session_id=_session(), owner_user_id=_owner(),
                    filename="figure_analysis_data.csv", visibility="internal",
                    content=dataset.to_frame().to_csv(index=False).encode("utf-8"), mime="text/csv",
                    producer_tool="run_research_figure_script")
                internal_file_ids = [analysis_file["file_id"], data_file["file_id"]]
                if not isinstance(bindings, dict) or set(bindings) & {"analysis_result", "analysis_data"}:
                    raise ToolArgumentError("analysis_result / analysis_data 是分析结果保留的输入变量。")
                bindings = {**bindings, "analysis_result": {"file_id": analysis_file["file_id"]},
                            "analysis_data": {"file_id": data_file["file_id"]}}
            analysis_meta = {"analysis_result_ref": analysis.ref, "dataset_ref": dataset_ref}
        if replay_snapshots is None:
            snapshots, data_refs = snapshot_script_inputs(bindings, _owner(), _session(),
                internal_file_ids=tuple(internal_file_ids))
        else:
            # Only the runtime can provide these, after resolving an owned run.
            # Validate again at the execution boundary before using its bytes.
            snapshots = replay_snapshots
            data_refs = [{"kind": "input_snapshot", "file_id": item["file_id"],
                          "sha256": item["sha256"], "source_file_id": item["source_file_id"]}
                         for item in snapshots]
            data_refs.extend(item["dataset_ref"] for item in snapshots if item.get("dataset_ref"))
        if script_record is not None:
            script_record["input_snapshots"] = snapshots
        source = {"kind": "script", "version": 2, "script": text,
                  "input_snapshots": snapshots, "qa_profile": profile,
                  "snapshot_policy": "immutable_bytes_new_source_for_changed_data", **analysis_meta,
                  **({key: script_record[key] for key in ("script_ref", "run_id", "parameters")
                      if key in script_record} if script_record else {})}
        inputs, sources, hashes = script_replay_inputs(source, _owner())
    except (ToolArgumentError, ProjectError, SpecError) as exc:
        return _error("INPUT", str(exc))
    except Exception as exc:
        return _error("INPUT", f"脚本数据或分析引用无法验证：{exc}")
    if contract:
        from src.research.script_execution import publish_script_outputs
        return publish_script_outputs(runner=runner, text=text, title=title,
            output_name=output_name, contract=contract, source=source,
            data_refs=data_refs, profile=profile, settings=settings,
            replay=(inputs, sources, hashes))
    outputs = {fmt: f"figure.{fmt}" for fmt in formats}
    outputs["manifest"] = "objects.json"
    job = SandboxJob(
        script=text, inputs=inputs, sources=sources, expected_input_sha256=hashes,
        qa_profile=profile,
        outputs=outputs,
        timeout_sec=int(getattr(settings, "FIGURES_SANDBOX_TIMEOUT_SEC", 120) or 120),
        memory_mb=int(getattr(settings, "FIGURES_SANDBOX_MEMORY_MB", 1024) or 1024),
        cpus=str(getattr(settings, "FIGURES_SANDBOX_CPUS", "1.0") or "1.0"),
        image=str(getattr(settings, "FIGURES_SANDBOX_IMAGE", "") or ""),
        image_sha256=str(getattr(settings, "FIGURES_SANDBOX_IMAGE_SHA256", "") or ""),
        runtime=str(getattr(settings, "FIGURES_SANDBOX_RUNTIME", "") or ""))
    try:
        result = runner.run(job)
    except SandboxUnavailable as exc:
        return _error("SANDBOX_UNAVAILABLE", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SANDBOX_ERROR", f"沙箱执行失败：{exc}", execution_stage="sandbox_failed")

    if not result.ok:
        from src.research.script_reconciliation import observe_failed_worker
        observe_failed_worker(result, source)
        message = result.describe() or "[FIGURE_SCRIPT_ERROR] 绘图脚本执行失败。"
        code = {"timeout": "SCRIPT_TIMEOUT", "oom": "SCRIPT_OOM",
                "forbidden": "SCRIPT_FORBIDDEN",
                "output": "SCRIPT_OUTPUT"}.get(result.failure_kind, "SCRIPT_ERROR")
        content, payload = _error(code, message, execution_stage="sandbox_failed")
        if result.diagnostic:
            payload["diagnostic"] = result.diagnostic
        return content, payload

    try:
        manifest_bytes = result.outputs["manifest"]
        if len(manifest_bytes) > 4 * 1024 * 1024:
            raise ValueError("manifest too large")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("objects"), list):
            raise ValueError("invalid manifest")
        from src.figures.nature_receipts import accept_current_render_qa
        accept_current_render_qa(manifest, {fmt: result.outputs[fmt] for fmt in formats
                                           if fmt in result.outputs}, "initial")
        receipt = manifest.get("qa_receipt") or {}
        if receipt.get("version") != 1 or receipt.get("profile") != parse_qa_profile(profile):
            raise ValueError("worker did not acknowledge profile QA")
    except (KeyError, TypeError, ValueError):
        return _error("WORKER_PROTOCOL", "绘图沙箱没有返回完整对象清单，请更新沙箱镜像后重试。",
                      execution_stage="output_verification_failed")

    manifest["data_provenance"] = {"input_snapshots": snapshots,
                                   "snapshot_policy": source["snapshot_policy"], **analysis_meta}
    stem = output_name or title or "figure"
    # One requested figure keeps one logical identity across re-renders of
    # the same turn. Title is the only figure-level label the model owns, so
    # distinct figures (which are requested with distinct titles) stay apart.
    # The format is part of the identity: SVG and PDF never merge.
    figure_logical = ""
    if title.strip():
        import hashlib as _hashlib
        from src.core.tracing import get_client_request_id as _request_id
        _seed = f"{_request_id() or ''}|{title.strip()}"
        figure_logical = "figure:" + _hashlib.sha256(_seed.encode("utf-8")).hexdigest()[:32]
    published: list[dict[str, Any]] = []
    publish_failures = []
    for fmt in formats:
        payload = result.outputs.get(fmt)
        if payload is None:
            publish_failures.append({"format": fmt, "error": "请求格式未导出"})
            continue
        try:
            entry = _publish(f"{stem}.{fmt}", payload, MIME_BY_FORMAT[fmt],
                             logical_task_id=f"{figure_logical}:{fmt}" if figure_logical else "")
        except Exception as exc:  # noqa: BLE001
            publish_failures.append({"format": fmt, "error": str(exc)[:500]})
            continue
        published.append({"format": fmt, **entry})
    if not published:
        return _error("PUBLISH", "请求格式均未交付。", execution_stage="figure_publication_failed")

    project = _record_project(
        title=title or stem, source_kind="script",
        source=source, manifest=manifest, exports=published, data_refs=data_refs)
    workspace_url = f"/figures?project={project['figure_id']}" if project else ""
    if project:
        published.append({"format": "project", "figure_id": project["figure_id"],
                          "workspace_url": workspace_url})

    placeholder = ""
    stage_failures = [] if project else [{"reason_code": "project_persistence_failed"}]
    svg_export = next((item for item in published if item["format"] == "svg"), None)
    if svg_export:
        try:
            placeholder = _register_figure(result.outputs["svg"], title or "科研图件", svg_export,
                                           workbench_url=workspace_url)
        except Exception:
            stage_failures.append({"reason_code": "inline_registration_failed"})

    summary = {
        "figure_id": project["figure_id"] if project else "",
        "execution_stage": "outputs_verified",
        "deliverable_stage": "figure_published",
        "project_registered": bool(project),
        "inline_registered": bool(placeholder),
        "title": title or stem,
        "objects": len(manifest.get("objects", [])),
        "qa_receipt": manifest.get("qa_receipt", {}),
        "nature_skill_receipt": manifest.get("nature_skill_receipt", {}),
        "current_qa": current_qa_summary(manifest),
        "exports": published,
        "figure_placeholder": placeholder,
        "backend": result.backend,
        "duration_ms": result.duration_ms,
        "editable": False,
        "failures": publish_failures + stage_failures,
        "note": ("脚本图件已保存，可在「图表工作台」查看和下载（/figures）。"
                 "这份脚本尚未分离分析与渲染；样式编辑需要冻结中间表和独立render_script，避免重算改变数值。"
                 if project else "图件已导出，但未能保存工作台项目；当前只能下载。"),
    }
    qa_failures = _failed_qa_checks(receipt)
    summary["qa_failures"] = qa_failures
    summary["_tool_outcome"] = {
        "status": "partial" if publish_failures or qa_failures or stage_failures else "complete",
        "reason_code": ("format_publication_failed" if publish_failures else
                        "figure_qa_needs_attention" if qa_failures else
                        stage_failures[0]["reason_code"] if stage_failures else "figure_published"),
        "artifacts": [item["artifact_url"] for item in published
                      if item.get("artifact_url")],
    }
    return json.dumps(summary, ensure_ascii=False, default=str), summary


def _failed_qa_checks(receipt: dict) -> list[str]:
    """Measured failures need repair; unsupported checks are not certification."""
    failures = [name for name, check in (receipt.get("checks") or {}).items()
                if isinstance(check, dict) and check.get("status") == "fail"]
    if receipt.get("status") == "needs_attention" and not failures:
        failures.append("unspecified")
    return failures


def _record_project(*, title: str, source_kind: str, source: Any,
                    manifest: dict[str, Any], exports: list[dict[str, Any]],
                    data_refs: list[Any] | None = None) -> dict[str, Any]:
    """Save the project so the workspace can reopen and edit it."""
    from src.figures import store

    owner = _owner()
    if not owner:
        return {}
    project = {}
    try:
        project = store.create_project(
            owner_user_id=owner, title=title, source_kind=source_kind,
            source=source, session_id=_session(), data_refs=data_refs or [])
        from src.figures.projects import FIGURE_FORMATS as PROJECT_FORMATS
        export_map = {item["format"]: item for item in exports
                      if item.get("format") in PROJECT_FORMATS}
        committed = store.update_project(
            project["figure_id"], owner, expected_revision=0,
            manifest_json=manifest, exports_json=export_map,
            render_version=1)
        if not committed:
            raise RuntimeError("figure project publication was not committed")
        project["manifest_json"] = manifest
        project["exports_json"] = export_map
        return project
    except Exception:
        if project.get("figure_id"):
            # Keep published artifacts, but remove the incomplete project and
            # release its pins. A cleanup failure must not advertise success.
            try:
                store.delete_project(project["figure_id"], owner)
            except Exception:
                logging.getLogger(__name__).exception("Incomplete figure project cleanup failed")
        return {}


def run_research_figure_script(
    script: str = "", title: str = "", formats_json: str | list[str] = "",
    output_name: str = "", inputs_json: str | dict = "", qa_profile_json: str | dict = "",
    analysis_result_ref_json: str | dict = "", outputs_json: str | dict = "",
    script_ref_json: str | dict = "", parameters_json: str | dict = "",
    repair_from_run_id: str = "",
) -> tuple[str, dict]:
    # Pure caller-option checks precede source/run/input persistence. A rejected
    # option therefore cannot be mistaken for an uncertain worker dispatch.
    # Repair options are inherited from a retained run by script_runtime and
    # checked again by the execution adapter before snapshotting or dispatch.
    from src.figures.spec import parse_qa_profile
    from src.figures.script_outputs import (RenderScriptEmitUnavailable,
                                            normalize_output_contract)
    try:
        parse_qa_profile(_json_arg(qa_profile_json, "qa_profile_json"))
        normalize_output_contract(_json_arg(outputs_json, "outputs_json"))
        _requested_formats(formats_json)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, RenderScriptEmitUnavailable):
            # Pure, repeatable rejection: no script save, run reservation or
            # worker dispatch. The execution ledger still records not_run.
            payload = {"error": f"[RENDER_SCRIPT_EMIT_NOT_AVAILABLE] {exc}",
                       "error_code": "render_script_emit_not_available",
                       "_tool_outcome": {"status": "not_run",
                                         "reason_code": "script_execution_rejected",
                                         "artifacts": []}}
        else:
            payload = {"error": f"绘图参数未通过执行前校验：{exc}", "_tool_outcome": {
                "status": "not_run", "reason_code": "script_execution_rejected", "artifacts": []}}
        from src.research.script_reconciliation import observe_preflight_rejection
        observe_preflight_rejection(payload)
        return json.dumps(payload, ensure_ascii=False), payload
    from src.figures.sandbox import SandboxUnavailable
    try:
        runner = _select_script_runner()
    except SandboxUnavailable:
        runner = None
    if runner is None:
        payload = {"error": "绘图沙箱当前不可用；本次未启动worker、未保存脚本或运行。请先恢复沙箱就绪，不能将其当成脚本错误反复执行。",
                   "_tool_outcome": {"status": "not_run", "reason_code": "script_execution_rejected", "artifacts": []}}
        from src.research.script_reconciliation import observe_preflight_rejection
        observe_preflight_rejection(payload)
        return json.dumps(payload, ensure_ascii=False), payload
    # Pass the host-owned runner directly to the adapter, never through script
    # options, persisted environment, model arguments, or worker JSON.
    from functools import partial
    from src.research.script_runtime import execute_script_request
    executor = partial(_run_research_figure_script, _sandbox_runner=runner)
    return execute_script_request(executor, script=script,
        script_ref_json=script_ref_json, parameters_json=parameters_json,
        repair_from_run_id=repair_from_run_id,
        title=title, formats_json=formats_json, output_name=output_name,
        inputs_json=inputs_json, qa_profile_json=qa_profile_json,
        analysis_result_ref_json=analysis_result_ref_json, outputs_json=outputs_json)


run_research_figure_script.__doc__ = inspect.cleandoc(_run_research_figure_script.__doc__ or "") + "\n" + """

复用优先：先用research_workspace发现适用内置工具/脚本和任务脚本。
script与script_ref_json二选一；源码执行前自动暂存，返回script_ref及run_id，
失败也保留源码。parameters_json是JSON对象，脚本内通过parameters读取，不拼接代码。
修复已保存的failed/partial运行时，只提供修改后的script或script_ref_json与repair_from_run_id；
其余参数留空。宿主继承该运行的固定输入、参数、输出约定和审核选项，重新校验后创建新运行，沿用原运行的交付格式。
改变输出契约（如改为独立render_script）需显式绑定授权输入发起新运行，不传repair_from_run_id。
先read_run查看具体失败；running或状态未知的运行不能重放。旧记录缺少完整修复输入时须显式绑定授权来源。
内置脚本可自动使用其声明的输出契约；任务脚本需要明确绑定输入和输出。
同一任务局部修改用research_workspace.patch创建子版本，再按引用执行。
已有工具或参数化脚本能完成时，不从头重写算法。中间表和脚本默认不是下载附件。
"""
run_research_figure_script = tool(response_format="content_and_artifact")(run_research_figure_script)

__all__ = ["run_research_figure_script"]
