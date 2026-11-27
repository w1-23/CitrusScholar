"""Host-side acceptance of declared sandbox outputs; never executes user code."""
from __future__ import annotations

import hashlib
import json
import re
from time import monotonic
import uuid
from typing import Any


_MULTI_FIGURE_HINT_RE = re.compile(
    r"(?i)(?:分别|各自|两张|两幅|多个|多张|"
    r"第[二三四五六七八九十]张|"
    r"和|与|以及|、|\band\b|\bplus\b|\balso\b|\btwo\b|\bthree\b)")


_FIGURE_NOUN_RE = re.compile(
    r"(?i)(?:箱线图|柱状图|折线图|散点图|"
    r"热图|直方图|误差棒图|流程图|示意图|"
    r"科研图|图件|图表|可视化|figure|chart|plot|diagram|visuali[sz]ation)")


def _accept_frozen_render(item: dict, manifest: dict, parents: dict, table_hashes: dict,
                          *, owner: int, session_id: str) -> tuple[dict, list]:
    """Bind worker claims to accepted typed output bytes before publishing figures."""
    from src.figures.projects import snapshot_script_inputs

    receipt = manifest.get("frozen_render_receipt")
    names = item["parents"]
    digest = lambda value: isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("renderer_sha256") != hashlib.sha256(item["render_script"].encode("utf-8")).hexdigest()
            or not names or any(name not in table_hashes for name in names)
            or receipt.get("data_sha256") != {name: table_hashes[name] for name in names}):
        raise ValueError("冻结渲染回执与已接收的分析结果或渲染代码不一致。")
    status, reasons = receipt.get("geometry_status"), receipt.get("geometry_unsupported")
    if not ((status == "supported" and digest(receipt.get("geometry_sha256")) and reasons == [])
            or (status == "unsupported" and receipt.get("geometry_sha256") is None
                and isinstance(reasons, list) and 0 < len(reasons) <= 64
                and all(isinstance(reason, str) and 0 < len(reason) <= 512 for reason in reasons))):
        raise ValueError("冻结渲染数值几何回执无效。")
    snapshots, refs = snapshot_script_inputs(
        {name: {"dataset_ref": parents[name]} for name in names}, owner, session_id)
    # Prepared datasets persist canonical typed JSON verbatim; the copied bytes
    # must have the exact digest acknowledged by both worker and host.
    if any(row["sha256"] != table_hashes[row["variable"]] for row in snapshots):
        raise ValueError("保存的冻结结果与工作器使用的数据不一致。")
    return {"version": 1, "input_snapshots": snapshots, "receipt": receipt}, refs


def _requested_figures(query: str, *, has_tables: bool) -> list[dict]:
    """Reuse the same user-turn deliverable contract as the host."""
    from src.graph.task_contract import FIGURE_KINDS, contract_for_turn

    return [item for item in contract_for_turn(query, has_tables=has_tables).deliverables
            if FIGURE_KINDS.intersection(item.get("kinds", []))]


def _singleton_figure_goal(*, query: str, has_tables: bool,
                           request_id: str, execution_id: str) -> str:
    """Return host evidence for exactly one requested figure, else ``""``.

    A one-figure *output contract* is not evidence that the user requested
    one figure: sequential delivery may call this tool twice with two
    one-figure contracts.  Only the host's turn-level contract may collapse
    attempts into one active goal.  Any parse gap/ambiguity returns empty and
    the caller falls back to the durable declared slot.
    """
    text = str(query or "").strip()
    if (not text or _MULTI_FIGURE_HINT_RE.search(text)
            or len(_FIGURE_NOUN_RE.findall(text)) != 1):
        return ""
    try:
        figures = _requested_figures(text, has_tables=has_tables)
    except Exception:
        return ""
    if len(figures) != 1:
        return ""
    turn = str(request_id or execution_id or "").strip()
    if not turn:
        return ""
    return f"{turn}|{text}"


def _figure_publication_id(*, contract: dict, figure: dict, execution_id: str,
                           fmt: str, logical_task_id: str = "",
                           request_id: str = "", request_goal_id: str = "") -> str:
    """Return a durable artifact identity for one declared figure slot.

    With explicit host evidence that the turn has one figure goal, all its
    successful re-renders share ``request_goal_id`` and form one version
    chain.  Without that evidence, identity includes the declared figure
    name, parents and formats, plus the proven request scope.  Consequently
    two independent single-output calls in a multi-figure request stay
    separate.  Formats are always separate chains.
    """
    figures = contract.get("figures") if isinstance(contract, dict) else []
    root = str(logical_task_id or "").split(":", 1)[0].strip()
    root = root or str(request_id or "").strip() or str(execution_id or "").strip()
    if not root:
        raise ValueError("figure publication requires a non-empty request scope")
    request_scope = hashlib.sha256(root.encode("utf-8")).hexdigest()[:24]
    goal = str(request_goal_id or "").strip() if len(figures) == 1 else ""
    if goal:
        goal_hash = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:24]
        return f"figure:goal-{goal_hash}:{fmt}"
    slot = {"name": str(figure.get("name") or ""),
            "parents": list(figure.get("parents") or []),
            "formats": list(figure.get("formats") or [])}
    slot_hash = hashlib.sha256(json.dumps(slot, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return f"figure:slot-{request_scope}:{slot_hash}:{fmt}"


def publish_script_outputs(*, runner, text: str, title: str, output_name: str,
                           contract: dict, source: dict, data_refs: list,
                           profile: dict, settings, replay: tuple[dict, dict, dict]) -> tuple[str, dict]:
    from src.figures.sandbox import SandboxJob
    from src.figures.script_outputs import output_slots, strict_json, validate_receipt
    from src.research.figure_tools import (_owner, _session, _error, _publish, _record_project,
                                           _register_figure, _failed_qa_checks)
    from src.research.analysis_results import persist_prepared_dataset
    from src.research.tools import _safe_name
    from src.core.tracing import get_client_request_id, get_job_id, original_query

    owner = _owner()
    inputs, paths, hashes = replay
    execution_id = str(source.get("run_id") or uuid.uuid4().hex)
    logical_task_id = ""
    try:
        from src.research.script_assets import get_run
        logical_task_id = str(get_run(owner, _session(), execution_id).get("logical_task_id") or "")
    except Exception:
        # A retained run may already be unavailable after cleanup.  The
        # non-empty execution id below is the safe, turn-local fallback.
        pass
    request_id = str(get_client_request_id() or "")
    query = original_query()
    has_tables = bool(source.get("input_snapshots"))
    request_goal_id = _singleton_figure_goal(
        query=query, has_tables=has_tables,
        request_id=request_id, execution_id=execution_id)
    job = SandboxJob(script=text, inputs=inputs, sources=paths,
        expected_input_sha256=hashes, qa_profile=profile,
        outputs=output_slots(contract), output_contract=contract,
        timeout_sec=int(getattr(settings, "FIGURES_SANDBOX_TIMEOUT_SEC", 120) or 120),
        memory_mb=int(getattr(settings, "FIGURES_SANDBOX_MEMORY_MB", 1024) or 1024),
        cpus=str(getattr(settings, "FIGURES_SANDBOX_CPUS", "1.0") or "1.0"),
        image=str(getattr(settings, "FIGURES_SANDBOX_IMAGE", "") or ""),
        image_sha256=str(getattr(settings, "FIGURES_SANDBOX_IMAGE_SHA256", "") or ""),
        runtime=str(getattr(settings, "FIGURES_SANDBOX_RUNTIME", "") or ""))
    try:
        result = runner.run(job)
    except Exception as exc:
        return _error("SANDBOX_ERROR", f"隔离执行失败：{exc}", execution_stage="sandbox_failed")
    if not result.ok:
        from src.research.script_reconciliation import observe_failed_worker
        observe_failed_worker(result, source, explicit_outputs=True)
        described = result.describe() or "脚本未完成；未接收本次残留输出。"
        content, payload = _error("SCRIPT_ERROR", described, execution_stage="sandbox_failed")
        if result.diagnostic:
            payload["diagnostic"] = result.diagnostic
        if "RENDER_SCRIPT_EMIT_NOT_AVAILABLE" in described:
            payload["error_code"] = "render_script_emit_not_available"
        return content, payload
    started = monotonic()
    timing = dict(worker_execution_and_render=result.duration_ms, manifest_and_qa=0,
                  publication=0, project_initialization=0)
    try:
        receipt = validate_receipt(result.outputs["result"], contract, job_id=job.job_id)
    except (KeyError, TypeError, ValueError):
        return _error("WORKER_PROTOCOL", "沙箱未确认版本2输出协议，请更新工作器。",
                      execution_stage="output_verification_failed")

    timing["manifest_and_qa"] += int((monotonic() - started) * 1000)

    # Bind lineage to verified server inputs, not to caller-supplied owner/path.
    parents = {}
    publication_started = False
    for snapshot in source.get("input_snapshots", []):
        parents[snapshot["variable"]] = snapshot.get("dataset_ref") or {
            "kind": "input_snapshot", "file_id": snapshot["file_id"], "sha256": snapshot["sha256"]}
    input_names = set(parents)
    table_specs = {item["name"]: item for item in contract["tables"]}
    tables: list[dict[str, Any]] = []
    table_hashes: dict[str, str] = {}
    failed = set()
    pending = dict(table_specs)
    script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    while pending:
        advanced = False
        for name, item in list(pending.items()):
            declared = item.get("parents") or sorted(input_names)
            if any(parent in pending for parent in declared):
                continue
            del pending[name]
            advanced = True
            try:
                if any(parent not in parents or parent in failed for parent in declared):
                    raise ValueError("父数据不可用，未保存依赖它的中间表。")
                blob = result.outputs.get(f"table:{name}")
                if blob is None:
                    raise ValueError(receipt["slots"][f"table:{name}"].get("error") or "未生成声明的中间表。")
                payload = strict_json(blob)
                publication_started = True
                dataset = persist_prepared_dataset(owner, typed_payload=payload,
                    session_id=_session(), parents=[parents[parent] for parent in declared],
                    execution_id=execution_id, script_sha256=script_hash,
                    logical_task_id=get_job_id() or execution_id,
                    source={"output_name": name, "description": "由固定脚本产生的内部绘图数据"})
                from src.figures.frozen_render import typed_payload_sha256
                table_hashes[name] = typed_payload_sha256(payload)
                parents[name] = dataset.ref
                frame_info = dataset.to_dict()
                tables.append({"name": name, "status": "complete", "dataset_ref": dataset.ref,
                    "columns": frame_info.get("columns", []),
                    "rows": frame_info.get("row_count", frame_info.get("rows")), "visibility": "internal"})
            except Exception as exc:
                failed.add(name)
                tables.append({"name": name, "status": "failed", "reason_code": "dataset_unavailable", "error": str(exc)[:500]})
        if not advanced:
            for name in pending:
                failed.add(name)
                tables.append({"name": name, "status": "failed", "reason_code": "cyclic_dependencies",
                               "error": "中间表依赖成环或引用自身，未保存。"})
            break

    figures = []
    exports = []
    placeholders = []
    for item in contract["figures"]:
        name = item["name"]
        figure = {"name": name, "title": item.get("title") or title or name,
                  "status": "failed", "exports": [], "editable": False,
                  "delivery_stage": "render_missing", "project_registered": False, "inline_registered": False}
        figures.append(figure)
        try:
            required_parents = item.get("parents", sorted(input_names | set(table_specs)))
            if any(parent not in parents or parent in failed for parent in required_parents):
                figure.update(reason_code="figure_input_unavailable",
                              error="图件依赖的数据未成功保存。")
                continue
            started = monotonic()
            manifest_blob = result.outputs.get(f"figure:{name}:manifest")
            if not manifest_blob:
                slot = receipt["slots"].get(f"figure:{name}:manifest", {})
                detail = str(slot.get("error") or "")[:500]
                raise ValueError("未取得有效图件对象清单。" + (" 工作器诊断：" + detail if detail else ""))
            if len(manifest_blob) > 4 * 1024 * 1024:
                raise ValueError("图件对象清单超过大小限制。")
            manifest = strict_json(manifest_blob)
            figure["nature_skill_receipt"] = manifest.get("nature_skill_receipt", {})
            from src.figures.nature_receipts import accept_current_render_qa, current_qa_summary
            blobs = {fmt: result.outputs[f"figure:{name}:{fmt}"] for fmt in item["formats"]
                     if f"figure:{name}:{fmt}" in result.outputs}
            if not any(blobs.values()):
                raise ValueError("没有非空的图件导出。")
            figure["delivery_stage"] = "rendered"
            accept_current_render_qa(manifest, blobs, "initial")
            figure["current_qa"] = current_qa_summary(manifest)
            timing["manifest_and_qa"] += int((monotonic() - started) * 1000)
            from src.figures.spec import parse_qa_profile
            qa = manifest.get("qa_receipt") or {}
            if (not isinstance(manifest.get("objects"), list) or qa.get("version") != 1
                    or qa.get("profile") != parse_qa_profile(profile)):
                raise ValueError("工作器未确认图件QA。")
            figure["qa_receipt"] = qa
            figure["delivery_stage"] = "outputs_verified"
            # Retain actionable per-codepoint evidence in the model-visible
            # partial receipt; Nature PDF checks do not cover glyph availability.
            glyphs = (qa.get("checks") or {}).get("font_glyph_coverage", {})
            if glyphs.get("status") == "fail":
                figure["font_repair_required"] = glyphs
            selected_refs = [parents[parent] for parent in required_parents]
            figure_source = {**source, "version": 3, "output_contract": contract,
                             "output_key": name, "execution_id": execution_id}
            frozen_refs = []
            if "render_script" in item:
                publication_started = True
                frozen, frozen_refs = _accept_frozen_render(
                    item, manifest, parents, table_hashes, owner=owner, session_id=_session())
                figure_source["frozen_render"] = frozen
            qa_failures = _failed_qa_checks(qa)
            figure_errors = ([{"reason_code": "figure_qa_needs_attention", "checks": qa_failures}]
                             if qa_failures else [])
            for fmt in item["formats"]:
                key = f"figure:{name}:{fmt}"
                payload = result.outputs.get(key)
                if payload is None:
                    detail = (manifest.get("render_receipt") or {}).get("export_errors", {}).get(fmt, {})
                    figure_errors.append({"format": fmt,
                        "error": receipt["slots"][key].get("error", "格式导出缺失"), **detail})
                    continue
                try:
                    mime = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf", "tiff": "image/tiff"}[fmt]
                    publication_started = True
                    suffix = f"_{name}.{fmt}"
                    stem = _safe_name(output_name or title, fallback="figure", max_len=80 - len(suffix))
                    publication_id = _figure_publication_id(
                        contract=contract, figure=item, execution_id=execution_id,
                        fmt=fmt, logical_task_id=logical_task_id,
                        request_id=request_id, request_goal_id=request_goal_id)
                    started = monotonic()
                    try:
                        entry = _publish(f"{stem}{suffix}", payload, mime, logical_task_id=publication_id)
                    finally:
                        timing["publication"] += int((monotonic() - started) * 1000)
                    record = {"format": fmt, **entry}
                    figure["exports"].append(record)
                    exports.append(record)
                except Exception as exc:
                    figure_errors.append({"format": fmt, "error": str(exc)[:500]})
            if not figure["exports"]:
                raise ValueError("没有成功发布任何请求格式。")
            figure["delivery_stage"] = "figure_published"
            manifest["data_provenance"] = {"input_snapshots": source.get("input_snapshots", []),
                                          "dataset_refs": selected_refs, "execution_id": execution_id}
            presentation_warnings = []
            started = monotonic()
            try:
                project = _record_project(title=figure["title"], source_kind="script", source=figure_source,
                    manifest=manifest, exports=figure["exports"], data_refs=data_refs + selected_refs + frozen_refs)
                if not project:
                    raise LookupError("figure project was not persisted")
                from src.figures.projects import _can_edit
                figure.update(figure_id=project["figure_id"], project_registered=True,
                              editable=_can_edit({**project, "source_kind": "script", "source_json": figure_source}),
                              workspace_url=f"/figures?project={project['figure_id']}")
                if not figure["editable"]:
                    figure["edit_limitation"] = "已保存并可下载；仅样式编辑需要独立渲染和受支持的数值几何校验。"
            except Exception:
                presentation_warnings.append({"reason_code": "project_persistence_failed",
                                              "error": "图件可下载，工作台项目未能登记。"})
            finally:
                timing["project_initialization"] += int((monotonic() - started) * 1000)
            svg_export = next((entry for entry in figure["exports"] if entry["format"] == "svg"), None)
            if svg_export:
                try:
                    figure["figure_placeholder"] = _register_figure(result.outputs[f"figure:{name}:svg"],
                        figure["title"], svg_export, workbench_url=figure.get("workspace_url", ""))
                    if not figure["figure_placeholder"]:
                        raise LookupError("inline figure was not registered")
                    placeholders.append(figure["figure_placeholder"])
                    figure["inline_registered"] = True
                except Exception:
                    presentation_warnings.append({"reason_code": "inline_registration_failed",
                                                  "error": "图件可下载，内联预览未登记。"})
            figure.update(status="partial" if figure_errors else "complete", failures=figure_errors,
                          presentation_warnings=presentation_warnings)
        except Exception as exc:
            stage = figure["delivery_stage"]
            reason = {"render_missing": "figure_render_missing", "rendered": "figure_verification_failed",
                      "outputs_verified": "figure_publication_failed", "figure_published": "project_persistence_failed"}[stage]
            figure.update(status="partial" if figure["exports"] else "failed", reason_code=reason, error=str(exc)[:500])

    successes = sum(item["status"] == "complete" for item in tables + figures)
    failures = any(item["status"] != "complete" for item in tables + figures)
    if failures:
        status = "partial" if successes or exports else "failed"
    else:
        status = "complete"
    deliverable_stage = ("analysis_only" if not figures else
                         "figure_published" if exports else "figure_unpublished")
    reason = deliverable_stage if status == "complete" else "script_outputs_incomplete"
    summary = {"execution_id": execution_id, "protocol_version": 2,
        "execution_stage": "worker_receipt_verified", "deliverable_stage": deliverable_stage,
        "script_sha256": script_hash, "backend": result.backend, "duration_ms": result.duration_ms,
        "timings_ms": timing, "tables": tables, "dataset_refs": [item["dataset_ref"] for item in tables if item.get("dataset_ref")],
        "figures": figures, "exports": exports, "figure_placeholders": placeholders,
        "stdout": receipt.get("stdout", ""),
        "note": "中间表为内部资产；正式附件仅包括已发布图件。执行成功不等于研究结论成立。",
        "_tool_outcome": {"status": status, "reason_code": reason,
                          "artifacts": [item["artifact_url"] for item in exports if item.get("artifact_url")]}}
    if not figures and query and any(requirement.get("phase", "initial") != "edit"
                                     for requirement in _requested_figures(query, has_tables=has_tables)):
        summary["figure_delivery_notice"] = (
            "本次仅分析，未生成图件。若仍缺本轮请求的图件，下次应声明 outputs_json.figures "
            "后实际渲染，不要再次做同样的只分析步骤。匹配的模板或脚本优先按 script_ref 执行，"
            "已产出的中间表可通过 dataset_ref 绑定。改变输出契约须发起新运行，不用 repair_from_run_id。")
    failed_formats = [receipt["slots"][f"figure:{item['name']}:{fmt}"]
                      for item in contract["figures"] for fmt in item["formats"]]
    if (status == "failed" and not publication_started and failed_formats
            and all(slot["status"] == "error" for slot in failed_formats)):
        from src.research.script_reconciliation import observe_failed_worker
        observe_failed_worker(result, source, validated_output_failure=True)
    report_blob = result.outputs.get("report")
    if report_blob:
        # The report is model-written observations, never trusted authorization.
        summary["script_report"] = report_blob.decode("utf-8", "replace")
    return json.dumps(summary, ensure_ascii=False, allow_nan=False), summary
