"""The figure workspace's service layer.

A *figure project* is a drawing the user can keep and adjust. This module is
everything the web workspace needs beyond raw storage: re-rendering a project
with the user's edits applied, publishing the result into the account's file
library, and importing a project out of the library.

Why re-rendering lives here
---------------------------
Saved previews and downloads share one authoritative render path. Browser
style projections are disposable drafts; unmappable changes request an SVG
correction. Formal saves reconstruct the source, apply the same closed edit
records, validate outputs and publish with a revision compare-and-swap.
Initial Nature audit evidence is retained separately from manual edit QA.

Why the source is kept
----------------------
A figure made from a specification can be re-rendered because the
specification is kept. A figure made from a script can be re-rendered because
the script is kept. Either way the *data* is re-read from its original source
— the uploaded table or the account-library file — so the numbers stay
traceable to the rows they came from.

The rule that is not negotiable
--------------------------------
Re-rendering must never change the data. The edits that reach the server are
filtered through :func:`src.figures.overrides.validate_overrides` against the
project's own manifest, so the only things that can change are colours,
font sizes, line widths and positions. A figure whose bars grew would be a
different claim, and there is no code path that can do that.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from src.figures.overrides import (
    OverrideError, merge_overrides, validate_overrides,
)
from src.figures.store import (
    create_project, delete_project, get_project, list_projects,
    finish_publications, get_revision_export, list_revision_exports, pending_publication,
    record_revision_export, update_project, update_project_committed,
)

# The formats a figure project carries.
FIGURE_FORMATS = ("svg", "png", "pdf", "tiff")

MIME_BY_FORMAT = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "tiff": "image/tiff",
}

# Newly published project revisions use SVG.
DEFAULT_FORMATS = ("svg",)
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RENDER_BYTES = 64 * 1024 * 1024


class ProjectError(RuntimeError):
    """A project operation that could not be carried out."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = str(code or "figure_error")
        self.message = str(message)
        self.status = int(status)


# ── Re-rendering ──────────────────────────────────────────────────────────

def _render_source(source: dict[str, Any],
                   owner_user_id: int | None = None) -> tuple[Any, dict[str, Any]]:
    """Build a declarative figure; scripts must use the worker byte path."""
    # Older script projects omitted kind; the script key is reliable evidence
    # for that legacy shape. Never mistake it for a declarative source.
    kind = str(source.get("kind") or ("script" if "script" in source else "spec"))
    if kind == "script":
        raise ProjectError(
            "script_requires_worker",
            "脚本图件必须通过隔离绘图工作进程重放。")
    if kind != "spec":
        raise ProjectError("unknown_source_kind", "不支持的图件来源类型。")
    return _render_spec(source, owner_user_id)


def _render_spec(source: dict[str, Any],
                 owner_user_id: int | None = None) -> tuple[Any, dict[str, Any]]:
    """Resolve account data only; Artist construction belongs to the child."""
    from src.figures.resolve import resolve_spec
    from src.figures.spec import SpecError, parse_spec

    from src.figures.declarative_snapshot import load_snapshot, SnapshotError
    try:
        frozen = load_snapshot(source)
    except SnapshotError as exc:
        raise ProjectError("source_unverified", "创建时的解析结果快照无效，拒绝重新分析后替代。") from exc
    if frozen is not None:
        return frozen
    payload = source.get("spec")
    if not isinstance(payload, dict):
        raise ProjectError("missing_spec", "这张图缺少规格定义，无法重新渲染。")
    try:
        spec = parse_spec(payload)
        resolved, info = resolve_spec(spec, owner_user_id=owner_user_id)
    except SpecError as exc:
        raise ProjectError("spec_invalid", exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - report, never crash the request
        raise ProjectError("data_unavailable",
                           f"图件所引用的数据现在无法读取：{exc}") from exc
    return resolved, info


def normalize_script_bindings(bindings: Any) -> dict:
    """Resolve only exact references in this request; never search an account by name."""
    import re
    from src.research.context import get_request_tables
    if not isinstance(bindings, dict) or len(bindings) > 8:
        raise ProjectError("invalid_inputs", "inputs 需要最多8个授权引用。")
    tables = get_request_tables()
    resolved = {}
    for variable, raw in bindings.items():
        if not isinstance(raw, dict):
            raise ProjectError("invalid_inputs", "输入需使用file_id或dataset_ref，不能提供路径。")
        if set(raw) == {"dataset_ref"}:
            resolved[variable] = raw
            continue
        if set(raw) == {"file_id"} and isinstance(raw["file_id"], str) and re.fullmatch(r"[a-f0-9]{32}", raw["file_id"]):
            resolved[variable] = raw
            continue
        candidates = []
        if set(raw) == {"file_index"} and type(raw["file_index"]) is int:
            if 0 <= raw["file_index"] < len(tables):
                candidates = [tables[raw["file_index"]].get("source_ref") or {}]
        elif set(raw) in ({"file_id"}, {"name"}):
            name = raw.get("file_id", raw.get("name"))
            candidates = [item.get("source_ref") or {} for item in tables
                          if isinstance(name, str) and item.get("name") == name]
        identities = {str(ref.get("file_id")): ref for ref in candidates if ref.get("file_id")}
        if len(identities) != 1:
            raise ProjectError("invalid_inputs", "输入引用无效或名称不唯一；请使用inspect返回的inputs_binding和真实file_id。")
        resolved[variable] = {"file_id": next(iter(identities))}
    return resolved


def snapshot_script_inputs(bindings: Any, owner: int, session_id: str, *,
                           internal_file_ids: tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copy authorized account inputs once; replay uses pinned immutable bytes.

    Changed or removed original files do not change this figure. A new data
    version requires a new figure source. Snapshot corruption fails closed.
    Neither source JSON nor provenance stores service filesystem paths.
    """
    import hashlib
    import keyword
    import re
    from src.session import files
    from src.core.account_leases import account_lease
    bindings = normalize_script_bindings(bindings)
    snapshots, refs, total = [], [], 0
    with account_lease(owner):
        for index, (variable, raw) in enumerate(bindings.items()):
            if (not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", variable)
                    or keyword.iskeyword(variable) or variable in {"plt", "matplotlib", "show", "emit", "load_dataset"}):
                raise ProjectError("invalid_inputs", "输入变量名无效或与绘图变量冲突。")
            dataset_ref = None
            if set(raw) == {"dataset_ref"}:
                from src.research.analysis_results import load_dataset_snapshot
                dataset = load_dataset_snapshot(owner, raw["dataset_ref"])
                dataset_ref = dataset.ref
                file_id = dataset.to_dict().get("payload_file_ref")
                if not file_id:
                    from src.figures.dataset_codec import encode_frame
                    payload = json.dumps(encode_frame(dataset.to_frame()), ensure_ascii=False, allow_nan=False).encode("utf-8")
                    entry = files.register_file(session_id=session_id, owner_user_id=owner,
                        filename="dataset_input.json", content=payload, mime="application/json",
                        visibility="internal", producer_tool="run_research_figure_script")
                    file_id = entry["file_id"]
                original = files.get_account_file(file_id, owner, allow_internal=True)
            else:
                file_id = raw["file_id"]
                original = files.get_account_file(file_id, owner,
                    allow_internal=file_id in internal_file_ids)
            if not original:
                raise ProjectError("data_unavailable", "输入文件不存在或不属于当前账号。")
            path = Path(original["path"])
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                raise ProjectError("invalid_inputs", "输入必须是大小受限的普通文件。")
            with path.open("rb") as handle:
                data = handle.read(32 * 1024 * 1024 + 1)
            total += len(data)
            if len(data) > 32 * 1024 * 1024 or total > 64 * 1024 * 1024:
                raise ProjectError("invalid_inputs", "脚本输入超过体积上限。")
            digest = hashlib.sha256(data).hexdigest()
            if original.get("sha256") and original["sha256"] != digest:
                raise ProjectError("source_changed", "原始输入已改变，请登记新的数据版本。")
            filename = str(original.get("filename") or "input.bin")
            suffix = Path(filename).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            sandbox_name = f"input_{index}{suffix}"
            snapshot = files.register_file(session_id=session_id, owner_user_id=owner,
                filename=f"figure_snapshot_{digest[:16]}{suffix}", content=data,
                mime=str(original.get("mime") or "application/octet-stream"),
                producer_tool="run_research_figure_script", dedupe_key=f"figure-snapshot:{digest}",
                visibility="internal")
            metadata = {"variable": variable, "sandbox_name": sandbox_name,
                        "file_id": str(snapshot["file_id"]), "source_file_id": file_id,
                        "sha256": digest, "size_bytes": len(data), "filename": filename}
            if dataset_ref:
                metadata["dataset_ref"] = dataset_ref
                refs.append(dataset_ref)
            snapshots.append(metadata)
            refs.append({"kind": "input_snapshot", "file_id": metadata["file_id"],
                         "sha256": digest, "source_file_id": file_id})
    return snapshots, refs


def script_replay_inputs(source: dict[str, Any], owner: int) -> tuple[dict, dict, dict]:
    import hashlib
    import re
    from src.session import files
    snapshots = source.get("input_snapshots")
    if snapshots is None:
        if source.get("inputs") or source.get("sources"):
            raise ProjectError("script_inputs_unavailable", "脚本原始输入映射无法重建，不能重放。")
        return {}, {}, {}
    if not isinstance(snapshots, list) or len(snapshots) > 8:
        raise ProjectError("source_unverified", "脚本输入快照合同无效。")
    inputs, sources, hashes = {}, {}, {}
    for item in snapshots:
        if not isinstance(item, dict):
            raise ProjectError("source_unverified", "脚本输入快照合同无效。")
        variable, name, digest = item.get("variable"), item.get("sandbox_name"), item.get("sha256")
        if (not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", variable)
                or variable in inputs or not isinstance(name, str)
                or not re.fullmatch(r"input_[0-7]\.[a-z0-9]{1,10}", name) or name in hashes
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ProjectError("source_unverified", "脚本输入快照名称或摘要无效。")
        entry = files.get_account_file(str(item.get("file_id") or ""), owner, allow_internal=True)
        if not entry:
            raise ProjectError("data_unavailable", "脚本输入快照不存在或不属于当前账号。")
        path = Path(entry["path"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.get("size_bytes"):
            raise ProjectError("source_changed", "脚本输入快照已损坏。")
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != digest:
            raise ProjectError("source_changed", "脚本输入快照摘要不匹配，拒绝重放。")
        inputs[variable], sources[variable], hashes[name] = name, str(path), digest
    return inputs, sources, hashes


def _frozen_render_state(source: dict[str, Any]) -> dict:
    """Validate the renderer identity and immutable table bindings."""
    import hashlib
    import re
    from citrus_figures.style_replay import REPLAY_ERRORS
    from src.figures.script_outputs import normalize_output_contract

    frozen = source.get("frozen_render")
    if not isinstance(frozen, dict):
        raise ProjectError("analysis_not_frozen", "脚本图缺少冻结分析结果和独立渲染脚本。", status=409)
    try:
        contract = normalize_output_contract(source.get("output_contract"))
        entry = next(item for item in contract["figures"] if item["name"] == source.get("output_key"))
        renderer = entry["render_script"]
        parents = entry["parents"]
        receipt = frozen["receipt"]
        snapshots = frozen["input_snapshots"]
        if frozen["version"] != 1 or receipt["version"] != 1 or not parents:
            raise ValueError("unsupported freeze record")
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
        raise ProjectError("frozen_render_invalid", "保存的冻结渲染合同或回执无效。", status=409) from None
    if receipt.get("renderer_sha256") != hashlib.sha256(renderer.encode("utf-8")).hexdigest():
        raise ProjectError("renderer_changed", REPLAY_ERRORS["renderer_changed"], status=409)
    hashes = receipt.get("data_sha256")
    if (not isinstance(hashes, dict) or set(hashes) != set(parents)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values())
            or not isinstance(snapshots, list) or len(snapshots) != len(parents)
            or any(not isinstance(row, dict) or not isinstance(row.get("variable"), str)
                   or row["variable"] not in hashes or row.get("sha256") != hashes[row["variable"]] for row in snapshots)
            or {row["variable"] for row in snapshots} != set(parents)):
        raise ProjectError("frozen_data_changed", REPLAY_ERRORS["frozen_data_changed"], status=409)
    return frozen


def _require_replay_evidence(receipt):
    import re
    from citrus_figures.style_replay import REPLAY_ERRORS, REPLAY_UNAVAILABLE, valid_baseline

    baseline = receipt.get("style_replay")
    if baseline is None:
        legacy_digest = receipt.get("geometry_sha256")
        if (receipt.get("geometry_status") != "supported" or receipt.get("geometry_unsupported") != []
                or not isinstance(legacy_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", legacy_digest)):
            code = "legacy_replay_evidence_unavailable"
            raise ProjectError(code, REPLAY_ERRORS[code], status=409)
    elif not valid_baseline(baseline):
        reason = baseline.get("unavailable") if isinstance(baseline, dict) else None
        code = reason if isinstance(reason, str) and reason in REPLAY_UNAVAILABLE else "replay_evidence_unavailable"
        raise ProjectError(code, REPLAY_ERRORS[code], status=409)


def _render_script(source: dict[str, Any], overrides: list[dict[str, Any]],
                   *, owner_user_id: int = 0, existing: list[dict[str, Any]] | None = None,
                   formats: tuple[str, ...] = DEFAULT_FORMATS, render_mode: str = "edit"
                   ) -> tuple[dict[str, bytes], dict[str, Any], list[dict[str, Any]]]:
    """Replay only the independent renderer over verified frozen tables.

    The one-shot sandbox receives no original analysis script or raw inputs.
    The worker must verify the saved baseline and preserve its raw coordinates.
    An unavailable sandbox fails explicitly instead of serving a stale preview.
    """
    from src.figures.sandbox import SandboxJob, SandboxUnavailable, select_backend

    frozen = _frozen_render_state(source)
    _require_replay_evidence(frozen["receipt"])
    # Style replay receives only the separated renderer and frozen tables.
    # The original analysis source remains provenance, never replay input.
    text = "pass"

    from src.config import settings

    runner = select_backend(
        backend=str(getattr(settings, "FIGURES_SANDBOX_BACKEND", "") or ""),
        image=str(getattr(settings, "FIGURES_SANDBOX_IMAGE", "") or ""),
        image_sha256=str(getattr(settings, "FIGURES_SANDBOX_IMAGE_SHA256", "") or ""),
        runtime=str(getattr(settings, "FIGURES_SANDBOX_RUNTIME", "") or ""),
        allow_local=bool(getattr(settings, "FIGURES_SANDBOX_ALLOW_LOCAL", False)),
        wsl_distribution=str(getattr(settings, "FIGURES_SANDBOX_WSL_DISTRO", "Ubuntu-24.04")))
    if runner is None:
        raise ProjectError(
            "sandbox_unavailable",
            "当前环境没有配置绘图脚本沙箱，无法重新渲染脚本来源的图件；"
            "请改用声明式图件工具重新出图。",
            status=503)

    contract = source.get("output_contract") or {}
    output_key = str(source.get("output_key") or "")
    if contract:
        from src.figures.script_outputs import normalize_output_contract, output_slots
        contract = normalize_output_contract(contract)
        contract = {**contract, "figures": [
            {**item, "formats": list(formats)} if item["name"] == output_key else item
            for item in contract["figures"]]}
        outputs = output_slots(contract, output_key=output_key)
    else:
        outputs = {fmt: f"figure.{fmt}" for fmt in formats}
        outputs["manifest"] = "objects.json"
        outputs["overrides"] = "overrides.json"
    inputs, sources, hashes = script_replay_inputs(
        {"input_snapshots": frozen["input_snapshots"]}, owner_user_id)
    job = SandboxJob(
        script=text, inputs=inputs, sources=sources, expected_input_sha256=hashes,
        qa_profile=source.get("qa_profile") or {}, outputs=outputs, overrides=overrides,
        output_contract=contract, output_key=output_key,
        base_overrides=existing or [], replay_overrides=True, render_mode=render_mode, dpi=_figure_dpi(),
        expected_frozen_receipt=frozen["receipt"],
        timeout_sec=int(getattr(settings, "FIGURES_SANDBOX_TIMEOUT_SEC", 120) or 120),
        memory_mb=int(getattr(settings, "FIGURES_SANDBOX_MEMORY_MB", 1024) or 1024),
        cpus=str(getattr(settings, "FIGURES_SANDBOX_CPUS", "1.0") or "1.0"),
        image=str(getattr(settings, "FIGURES_SANDBOX_IMAGE", "") or ""),
        image_sha256=str(getattr(settings, "FIGURES_SANDBOX_IMAGE_SHA256", "") or ""),
        runtime=str(getattr(settings, "FIGURES_SANDBOX_RUNTIME", "") or ""))
    try:
        result = runner.run(job)
    except SandboxUnavailable as exc:
        raise ProjectError("sandbox_unavailable", str(exc), status=503) from exc
    except Exception as exc:  # noqa: BLE001
        raise ProjectError("render_failed", f"沙箱执行失败：{exc}") from exc
    if not result.ok:
        raise ProjectError("render_failed", result.describe() or "绘图脚本执行失败。")

    # A versioned acknowledgement is mandatory: an older worker silently
    # ignoring overrides must never publish an unchanged figure as a success.
    try:
        output_bytes = result.outputs
        if contract:
            from src.figures.script_outputs import validate_receipt
            report = validate_receipt(output_bytes["result"], contract,
                                      job_id=job.job_id, output_key=output_key)
            from citrus_figures.style_replay import REPLAY_ERRORS
            import re
            for key in ("manifest", "overrides", *formats):
                state = report["slots"][f"figure:{output_key}:{key}"]
                if state["status"] != "ok":
                    detail = state.get("error") or "工作器未返回图件输出。"
                    known = {**REPLAY_ERRORS, "prop_not_editable": "", "unsupported_artist": "",
                             "shared_legend_conflict": "", "ambiguous_legend": "", "unresolved_artist": "",
                             "apply_failed": "", "override_acknowledgement_invalid": ""}
                    match = re.fullmatch(r"\[([a-z_]+)\] (.+)", detail)
                    if match and match[1] in known:
                        raise ProjectError(match[1], known[match[1]] or match[2], status=409)
                    raise ProjectError("render_output_failed", detail, status=409)
            output_bytes = {key: output_bytes[f"figure:{output_key}:{key}"]
                            for key in (*formats, "manifest", "overrides")}
        else:
            report = json.loads(result.stdout)
        manifest_bytes = output_bytes["manifest"]
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest budget exceeded")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        from citrus_figures.style_replay import REPLAY_ERRORS, replay_mismatch, typed_payload_sha256
        actual_receipt = manifest.get("frozen_render_receipt") if isinstance(manifest, dict) else None
        refusal = replay_mismatch(frozen["receipt"], actual_receipt)
        if refusal:
            raise ProjectError(refusal, REPLAY_ERRORS[refusal], status=409)
        if (not isinstance(report, dict) or (not contract and report.get("override_protocol") != 1)
                or report.get("applied_count") != len(merge_overrides(existing or [], overrides))
                or not isinstance(manifest, dict)
                or not isinstance(manifest.get("objects"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("gid"), str)
                       for item in manifest["objects"])):
            raise ValueError("worker protocol mismatch")
        override_bytes = output_bytes["overrides"]
        if len(override_bytes) > 256 * 1024:
            raise ValueError("override response budget exceeded")
        accepted = json.loads(override_bytes.decode("utf-8"))
        _check_worker_overrides(accepted, merge_overrides(existing or [], overrides), manifest)
        if manifest.get("style_replay_proof") != {
                "coordinates_sha256": actual_receipt["style_replay"]["coordinates_sha256"],
                "overrides_sha256": typed_payload_sha256(accepted)}:
            raise ProjectError("override_acknowledgement_invalid", "工作器未确认坐标与样式修改的对应关系。", status=409)
        if render_mode != "preview" and source.get("version", 0) >= 2:
            from src.figures.spec import parse_qa_profile
            receipt = manifest.get("qa_receipt") or {}
            if receipt.get("version") != 1 or receipt.get("profile") != parse_qa_profile(source.get("qa_profile") or {}):
                raise ValueError("worker did not acknowledge profile QA")
        blobs = {fmt: output_bytes[fmt] for fmt in formats}
        _validate_render_receipt(manifest, blobs, render_mode)
        if any(not isinstance(blob, bytes) or not blob for blob in blobs.values()):
            raise ValueError("invalid output bytes")
        if "svg" in blobs:
            blobs["svg"].decode("utf-8")
    except (KeyError, TypeError, ValueError, OverrideError) as exc:
        raise ProjectError("worker_protocol_invalid", "绘图沙箱未确认样式重放；请更新沙箱镜像。") from exc
    manifest["data_provenance"] = {key: source[key] for key in (
        "input_snapshots", "snapshot_policy", "analysis_result_ref", "dataset_ref") if key in source}
    return blobs, manifest, accepted



def _check_worker_overrides(accepted: Any, requested: list[dict[str, Any]],
                            manifest: dict[str, Any]) -> None:
    """Check bounded worker data without resolving container fonts on the host."""
    from src.figures.manifest import EDITABLE_BY_ROLE
    if (not isinstance(accepted, list) or len(accepted) > 200
            or any(not isinstance(item, dict) or set(item) != {"gid", "prop", "value"}
                   or not isinstance(item["gid"], str) or not isinstance(item["prop"], str)
                   for item in accepted)):
        raise ValueError("invalid accepted overrides")
    if [(item["gid"], item["prop"]) for item in accepted] != [(item["gid"], item["prop"]) for item in requested]:
        raise ValueError("worker did not acknowledge the requested edit set")
    nonfonts = [item for item in requested if item["prop"] != "fontfamily"]
    if validate_overrides(nonfonts, manifest) != [item for item in accepted if item["prop"] != "fontfamily"]:
        raise ValueError("worker substituted edit values")
    by_gid = {item.get("gid"): item for item in manifest.get("objects", []) if isinstance(item, dict)}
    families = set(manifest.get("font_families") or [])
    for actual, expected in zip(accepted, requested):
        if actual["prop"] != "fontfamily":
            continue
        target = by_gid.get(actual["gid"], {})
        value = actual["value"]
        raw = expected.get("value")
        if (not isinstance(value, str) or not isinstance(raw, str)
                or value not in families or value.casefold() != raw.strip().casefold()
                or "fontfamily" not in EDITABLE_BY_ROLE.get(target.get("role"), ())
                or "fontfamily" not in target.get("editable", [])):
            raise ValueError("invalid worker font acknowledgement")


# ── Applying edits ────────────────────────────────────────────────────────

def _normalise_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {"version": 1, "objects": []}
    import math
    # Legacy scatter manifests may contain Infinity from Bbox.null(). Keep the
    # object editable via its selector, but do not draw an invented overlay.
    objects = []
    for item in manifest.get("objects", []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        bbox = item.get("bbox")
        anchor = item.get("anchor")
        invalid_bbox = bbox is not None and (
            not isinstance(bbox, dict)
            or not {"x0", "y0", "x1", "y1"}.issubset(bbox)
            or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in bbox.values())
            or bbox["x0"] > bbox["x1"] or bbox["y0"] > bbox["y1"])
        invalid_anchor = anchor is not None and (
            not isinstance(anchor, (list, tuple)) or len(anchor) != 2
            or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in anchor))
        if invalid_bbox or invalid_anchor:
            item.update(bbox=None, anchor=None, geometry_warning="legacy_bounds_unavailable")
        objects.append(item)
    return {**manifest, "objects": objects}


# ── Publishing ────────────────────────────────────────────────────────────

@contextmanager
def stage_rendered(owner, session_id, stem, blobs, *, request_id=None):
    """Keep renders private until the figure transaction records this batch."""
    from src.core.account_leases import account_lease
    from src.core.cancel import raise_if_cancelled
    from src.core.tracing import get_client_request_id
    from src.research.artifacts import discard_staged_figures, stage_figure_artifact

    if not session_id:
        raise ProjectError("no_session", "没有有效会话，无法把图件登记到资料库。")
    request_id = str(get_client_request_id() or "") if request_id is None else request_id
    batch = uuid.uuid4().hex
    exports = {}
    with account_lease(owner):
        try:
            for fmt in FIGURE_FORMATS:
                if fmt in blobs:
                    try:
                        exports[fmt] = stage_figure_artifact(owner=owner, session_id=session_id,
                            filename=f"{_safe_stem(stem)}.{fmt}", content=blobs[fmt],
                            mime=MIME_BY_FORMAT[fmt], request_id=request_id)
                    except Exception as exc:
                        raise ProjectError("publish_failed", "图件文件无法暂存，可重试。", 503) from exc
            raise_if_cancelled("artifact:before_commit")
            yield exports, (batch, session_id, request_id, list(exports.values()))
        finally:
            if exports:
                # Never discard a committed batch after transient promotion failure.
                try:
                    if not pending_publication(batch, owner):
                        discard_staged_figures(owner, session_id, list(exports.values()))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("figure stage cleanup deferred", exc_info=True)


def complete_render_publication(figure_id, owner, *, required_batch=None):
    try:
        finish_publications(figure_id, owner, required_batch=required_batch)
    except Exception as exc:
        raise ProjectError("publication_retryable", "图件已保存，文件登记暂未完成，可重试打开。", 503) from exc


def _safe_stem(stem: str) -> str:
    text = str(stem or "figure").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)
    cleaned = cleaned.strip("._") or "figure"
    return cleaned[:80]


# ── Project operations ────────────────────────────────────────────────────

def list_for_owner(owner: int, *, limit: int = 100,
                   offset: int = 0) -> list[dict[str, Any]]:
    """The account's projects, as the workspace needs them."""
    projects = list_projects(owner, limit=limit, offset=offset)
    result = []
    for project in projects:
        try:
            complete_render_publication(project["figure_id"], owner)
        except ProjectError as exc:
            # Do not return staged download URLs while publication is retryable.
            project = {**project, "exports_json": {}, "status": "publication_retryable",
                       "last_error": exc.message}
        result.append(_public(project))
    return result


def get_for_owner(figure_id: str, owner: int) -> dict[str, Any]:
    """One project owned by this account.

    A project belonging to somebody else is *not found*, not found-and-refused:
    the caller must not be able to tell the two apart.
    """
    project = get_project(str(figure_id), int(owner))
    if not project:
        raise ProjectError("not_found", "图件不存在或无权访问。", status=404)
    complete_render_publication(str(figure_id), int(owner))
    return project


def _public(project: dict[str, Any]) -> dict[str, Any]:
    """The project as the workspace sees it."""
    manifest = _normalise_manifest(project.get("manifest_json"))
    exports = project.get("exports_json") or {}
    if not isinstance(exports, dict):
        exports = {}
    return {
        "figure_id": project.get("figure_id", ""),
        "title": project.get("title", ""),
        "source_kind": project.get("source_kind", "spec"),
        "status": project.get("status", "ready"),
        "last_error": project.get("last_error", ""),
        "created_at": project.get("created_at", 0),
        "updated_at": project.get("updated_at", 0),
        "render_version": project.get("render_version", 0),
        "objects": len(manifest.get("objects", [])),
        "exports": exports,
        "has_script": bool((project.get("source_json") or {}).get("script")),
        "editable": _can_edit(project),
    }


def _is_script(project: dict[str, Any]) -> bool:
    source = project.get("source_json") or {}
    return project.get("source_kind") == "script" or "script" in source


def _can_edit(project: dict[str, Any]) -> bool:
    if not _is_script(project):
        return project.get("source_kind", "spec") in {"spec", "svg"}
    source = project.get("source_json") or {}
    try:
        _require_replay_evidence(_frozen_render_state(source)["receipt"])
    except ProjectError:
        return False
    from src.config import settings
    backend = str(getattr(settings, "FIGURES_SANDBOX_BACKEND", "") or "").lower()
    return (
        (backend in {"docker", "wsl-docker", "apptainer"} and bool(getattr(settings, "FIGURES_SANDBOX_IMAGE", "")))
        or (backend == "local" and bool(getattr(settings, "FIGURES_SANDBOX_ALLOW_LOCAL", False))))


def edit_context(project: dict[str, Any]) -> dict[str, Any]:
    """Owned recovery references and precise refusal, without replaying any code."""
    source = project.get("source_json") or {}
    manifest = _normalise_manifest(project.get("manifest_json"))
    editable = _can_edit(project)
    error = None
    if not editable and _is_script(project):
        try:
            _require_replay_evidence(_frozen_render_state(source)["receipt"])
        except ProjectError as exc:
            error = {"code": exc.code, "message": exc.message}
        if error is None:
            error = {"code": "sandbox_unavailable", "message": "当前未配置可用的图件样式重绘沙箱。"}
    elif not editable:
        error = {"code": "source_not_editable", "message": "当前图件来源不支持样式重绘。"}
    bindings = {}
    for row in source.get("input_snapshots", []):
        if row.get("dataset_ref"):
            bindings[row["variable"]] = {"dataset_ref": row["dataset_ref"]}
        elif row.get("source_file_id"):
            bindings[row["variable"]] = {"file_id": row["source_file_id"]}
    frozen = source.get("frozen_render") or {}
    return {
        "editable": editable, "script_ref": source.get("script_ref"),
        "run_id": source.get("run_id"), "input_bindings": bindings,
        "dataset_refs": (manifest.get("data_provenance") or {}).get("dataset_refs", []),
        "frozen_data_refs": [{key: row[key] for key in ("variable", "dataset_ref", "sha256") if key in row}
                             for row in frozen.get("input_snapshots", [])] if isinstance(frozen, dict) else [],
        "edit_error": error, "edit_limitation": error["message"] if error else "",
        "recovery": ("读取script_ref原始脚本及run_id运行回执，复用实际dataset_refs或input_bindings；"
                     "缺少冻结渲染时，用现有工具保存完整中间表并创建独立render_script的新运行，"
                     "实际执行并检查返回的editable后再提交样式编辑。保存脚本不等于执行或修改图件。"
                     if error else ""),
    }


def detail_for_owner(figure_id: str, owner: int, *, upgrade_manifest: bool = False) -> dict[str, Any]:
    """Read stored exports; optionally discover legacy objects without saving."""
    project = get_for_owner(figure_id, owner)
    manifest = _normalise_manifest(project.get("manifest_json"))
    public = _public(project)
    public["exports"] = {
        **public["exports"], **list_revision_exports(figure_id, owner, public["render_version"]),
    }
    from src.figures.manifest import MANIFEST_VERSION
    svg_detail = {}
    if public["source_kind"] == "svg":
        from src.figures.svg_projects import editable_svg_targets
        source = project.get("source_json") or {}
        targets = editable_svg_targets(source["svg"], plot=bool(source.get("nature_plot"))) if "svg" in source else []
        svg_detail = {"svg_targets": targets, "editable": bool(targets and "svg" in public["exports"])}
    legacy_edit = (public["source_kind"] != "svg" and public["editable"] and "svg" in public["exports"]
                   and manifest.get("version", 1) < MANIFEST_VERSION)
    if upgrade_manifest and legacy_edit:
        try:
            _, _, manifest, _ = _render_edits(project, owner, [], formats=("svg",))
            public["manifest_upgraded"] = True
        except ProjectError as exc:
            public.update(editable=False, edit_error={"code": exc.code, "message": exc.message})
    return {
        **edit_context(project),
        **public,
        **svg_detail,
        "generatable_formats": ["png", "pdf"] if public["editable"] and public["source_kind"] != "svg" else [],
        "manifest": manifest,
        # A disposable legacy-inventory upgrade has not been accepted by the
        # project CAS; do not attach bindings for the stored manifest to it.
        "document": (project.get("document_json") or None) if not public.get("manifest_upgraded") else None,
        "overrides": project.get("overrides_json") or [],
        "data_refs": project.get("data_refs_json") or [],
        "edit_requires_preparation": bool(legacy_edit and not upgrade_manifest),
        "preview_url": (f"/api/v2/figures/projects/{figure_id}/preview.svg" if "svg" in public["exports"]
                        else f"/api/v2/figures/projects/{figure_id}/preview.png" if "png" in public["exports"]
                        else ""),
    }


def _assert_current(project: dict[str, Any], owner: int) -> None:
    current = get_project(str(project["figure_id"]), int(owner))
    if current is None or any(current.get(key) != project.get(key) for key in (
            "render_version", "source_json", "data_refs_json", "overrides_json")):
        raise ProjectError("revision_conflict", "渲染期间图件已更新或删除，请重新加载后合并修改。", status=409)


def _verify_sources(project: dict[str, Any], owner: int) -> None:
    from src.session import files as session_files
    import hashlib
    source = project.get("source_json") or {}
    if _is_script(project):
        frozen = _frozen_render_state(source)
        _require_replay_evidence(frozen["receipt"])
        snapshots = frozen["input_snapshots"]
        pins = {ref.get("file_id"): ref.get("sha256") for ref in project.get("data_refs_json") or []
                if isinstance(ref, dict) and ref.get("file_id")}
        if any(not isinstance(row.get("file_id"), str) or pins.get(row["file_id"]) != row["sha256"]
               for row in snapshots):
            raise ProjectError("frozen_data_unpinned", "冻结表缺少本图件的固定数据引用。", status=409)
        try:
            script_replay_inputs({"input_snapshots": snapshots}, owner)
        except ProjectError as exc:
            codes = {"data_unavailable": "frozen_data_unavailable", "source_changed": "frozen_data_changed"}
            raise ProjectError(codes.get(exc.code, exc.code), exc.message, status=409) from exc
        return
    from src.figures.declarative_snapshot import load_snapshot, SnapshotError
    try:
        load_snapshot(source)
    except SnapshotError as exc:
        raise ProjectError("source_unverified", "创建时的解析结果快照无法验证。") from exc
    if source.get("analysis_result_ref"):
        try:
            from src.research.analysis_results import load_analysis_result, load_dataset_snapshot
            analysis = load_analysis_result(owner, source["analysis_result_ref"])
            if analysis.to_dict()["dataset_ref"] != source.get("dataset_ref"):
                raise ValueError("analysis dataset changed")
            load_dataset_snapshot(owner, source["dataset_ref"])
        except Exception as exc:
            raise ProjectError("analysis_unavailable", "保存的分析结果或数据版本无法验证，拒绝重放。") from exc
    refs = project.get("data_refs_json") or []
    pinned = {str(ref.get("file_id")): ref for ref in refs if isinstance(ref, dict) and ref.get("file_id")}
    spec = (project.get("source_json") or {}).get("spec") or {}
    if isinstance(spec, dict):
        specs = list(spec.get("panels") or []) if spec.get("panels") else [spec]
        for item, name in ((item, name) for item in specs for name in ("x", "y", "group", "weight", "series_key")):
            column = item.get(name)
            if not isinstance(column, dict):
                continue
            if column.get("kind") == "attachment":
                raise ProjectError("source_unverified", "旧图件仍引用临时附件，无法重建原始输入；请重新出图。")
            if column.get("kind") == "account_file" and str(column.get("file_id")) not in pinned:
                raise ProjectError("source_unverified", "图件数据引用缺少保存时的校验信息，请重新出图。")
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("file_id"):
            continue
        if not ref.get("sha256"):
            raise ProjectError("source_unverified", "图件缺少原始数据校验信息，请重新出图。")
        # References belong to the persisted, owner-bound project; script
        # inputs are private fixed snapshots, not caller-supplied file IDs.
        entry = session_files.get_account_file(str(ref["file_id"]), owner, allow_internal=True)
        if not entry:
            raise ProjectError("data_unavailable", "图件引用的原始文件已不可用。")
        try:
            with open(entry["path"], "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as exc:
            raise ProjectError("data_unavailable", "图件引用的原始文件无法读取。") from exc
        if digest != ref["sha256"]:
            raise ProjectError("source_changed", "图件原始文件的内容已改变，请新建数据版本后重新出图。")


def _render_edits(project: dict[str, Any], owner: int, raw_overrides: Any,
                  *, formats: tuple[str, ...] = DEFAULT_FORMATS, render_mode: str = "edit",
                  require_current: bool = True
                  ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any], dict[str, bytes]]:
    """The one preview/save path; all outputs remain in memory until commit."""
    from src.figures.trusted_render import render_resolved, TrustedRenderError

    if require_current:
        _assert_current(project, owner)
    _verify_sources(project, owner)
    source = project.get("source_json") or {}
    if _is_script(project):
        if not isinstance(raw_overrides, list) or any(not isinstance(item, dict) for item in raw_overrides):
            raise ProjectError("invalid_overrides", "修改必须是对象列表。")
        # Validate every submitted entry in the worker before merging; a bad
        # duplicate must not disappear behind a later valid value.
        blobs, new_manifest, merged = _render_script(
            source, raw_overrides, owner_user_id=owner,
            existing=project.get("overrides_json") or [], formats=formats, render_mode=render_mode)
        changed = [item["gid"] for item in merged]
    else:
        resolved, info = _render_source(source, owner_user_id=owner)
        try:
            result = render_resolved(resolved, overrides=raw_overrides,
                                     existing=project.get("overrides_json") or [],
                                     formats=formats, dpi=_figure_dpi(), render_mode=render_mode)
        except TrustedRenderError as exc:
            raise ProjectError(exc.code, exc.message) from exc
        merged, changed, new_manifest, blobs = result.overrides, result.changed, result.manifest, result.blobs
        from src.figures.declarative_snapshot import confirm_render, SnapshotError
        try:
            confirm_render(source, new_manifest)
        except SnapshotError as exc:
            raise ProjectError("worker_protocol_invalid", "工作进程未确认保存的解析结果。") from exc
        new_manifest["data_provenance"] = info
    _validate_render_receipt(new_manifest, blobs, render_mode)
    _preserve_initial_receipt(project, new_manifest, render_mode, bool(merged))
    try:
        manifest_size = len(json.dumps(new_manifest, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ProjectError("manifest_invalid", "图件对象清单包含无效数据。") from exc
    if (sum(len(blob) for blob in blobs.values()) > MAX_RENDER_BYTES
            or len(blobs.get("svg", b"")) > MAX_PREVIEW_BYTES
            or manifest_size > MAX_MANIFEST_BYTES):
        raise ProjectError("render_too_large", "图件预览或对象清单超过体积上限。")
    _verify_sources(project, owner)
    if require_current:
        _assert_current(project, owner)
    return merged, changed, new_manifest, blobs


def _validate_render_receipt(manifest: dict, blobs: dict[str, bytes], mode: str) -> None:
    import hashlib
    receipt = manifest.get("render_receipt") or {}
    checks = receipt.get("checks") or {}
    if receipt.get("version") != 2 or receipt.get("mode") != mode:
        raise ProjectError("worker_protocol_invalid", "绘图工作器不支持当前编辑协议，请更新绘图镜像。")
    for fmt, blob in blobs.items():
        check = checks.get(fmt) or {}
        signature = {"svg": b"<?xml", "png": b"\x89PNG\r\n\x1a\n", "pdf": b"%PDF-",
                     "tiff": (b"II", b"MM")}.get(fmt)
        if not isinstance(blob, bytes) or signature is None or not blob.startswith(signature):
            raise ProjectError("worker_protocol_invalid", "图件输出格式无效。")
        if (check.get("format") != "pass" or check.get("size_bytes") != len(blob)
                or check.get("sha256") != hashlib.sha256(blob).hexdigest()):
            raise ProjectError("worker_protocol_invalid", "图件输出与工作器完整性回执不一致。")

    if mode != "preview":
        from src.figures.nature_receipts import accept_current_render_qa
        try:
            accept_current_render_qa(manifest, blobs, mode)
        except ValueError as exc:
            raise ProjectError("worker_protocol_invalid", str(exc)) from exc


def _preserve_initial_receipt(project: dict, manifest: dict, mode: str, edited: bool) -> None:
    from copy import deepcopy
    previous = project.get("manifest_json") or {}
    initial = previous.get("initial_nature_skill_receipt", previous.get("nature_skill_receipt"))
    # Never validate the old PDF hash against new bytes or claim edited exports
    # inherited the initial aesthetic verdict. Legacy absence remains unknown.
    manifest.pop("nature_skill_receipt", None)
    if initial is not None:
        manifest["initial_nature_skill_receipt"] = deepcopy(initial)
    original_qa = previous.get("initial_qa_receipt", previous.get("qa_receipt"))
    if original_qa is not None:
        manifest["initial_qa_receipt"] = deepcopy(original_qa)
    if "initial_current_qa_receipt" in previous:
        manifest["initial_current_qa_receipt"] = deepcopy(previous["initial_current_qa_receipt"])
    elif (previous.get("current_qa_receipt") or {}).get("mode") == "initial":
        manifest["initial_current_qa_receipt"] = deepcopy(previous["current_qa_receipt"])
    if mode != "preview" and manifest.get("qa_receipt"):
        manifest["manual_edit_qa_receipt"] = manifest.pop("qa_receipt")
    manifest["edit_provenance"] = {
        "version": 1, "mode": mode, "manual_edits": edited,
        "initial_nature_audit": "recorded" if initial is not None else "unknown_legacy",
        "current_nature_audit": "not_repeated_manual_edit",
        "initial_render_version": (previous.get("edit_provenance") or {}).get(
            "initial_render_version", project.get("render_version", 0)),
    }


def _load_revision(figure_id: str, owner: int, expected_revision: int | None) -> dict[str, Any]:
    project = get_for_owner(figure_id, owner)
    if expected_revision is not None and int(expected_revision) != int(project.get("render_version") or 0):
        raise ProjectError("revision_conflict", "图件已在其他页面更新，请重新加载后合并修改。", status=409)
    return project


def generate_export(figure_id: str, owner: int, fmt: str, *, expected_revision: int) -> dict:
    """Return a valid export or regenerate only the exact current frozen revision."""
    from src.figures.nature_receipts import current_qa_summary
    from src.figures.workbench.artifacts import read_entry, valid_export_bytes

    if fmt not in {"png", "pdf"}:
        raise ProjectError("format_unsupported", "仅支持生成 PNG 或 PDF。")
    project = get_for_owner(figure_id, owner)
    restored = get_revision_export(figure_id, owner, expected_revision, fmt)
    current = project["render_version"] == expected_revision
    if restored:
        try:
            read_entry(restored["export"], owner, fmt)
        except ProjectError:
            pass
        else:
            return restored
    if not current:
        raise ProjectError("revision_conflict", "图件版本已更新，请重新打开。", status=409)
    existing = (project.get("exports_json") or {}).get(fmt)
    try:
        read_entry(existing, owner, fmt)
    except ProjectError:
        pass
    else:
        return {"figure_id": figure_id, "render_version": expected_revision, "format": fmt, "export": existing}
    if project.get("source_kind") == "svg" or not _can_edit(project):
        raise ProjectError("render_unavailable", "当前图件没有可重放的冻结工程。", status=409)
    session_id = project.get("session_id")
    if not session_id:
        raise ProjectError("no_session", "当前图件没有已授权的登记会话。", status=409)
    _, _, manifest, blobs = _render_edits(project, owner, [], formats=(fmt,))
    _assert_current(project, owner)
    if fmt not in blobs or not valid_export_bytes(blobs[fmt], fmt):
        raise ProjectError("artifact_unavailable", "新生成的图件格式或大小无法验证。", status=409)
    summary = current_qa_summary(manifest)
    checks = [check for check in summary.get("checks", []) if (check.get("artifact") or {}).get("format") == fmt]
    with stage_rendered(owner, session_id, project.get("title"), {fmt: blobs[fmt]},
                        request_id=f"figure-export:{figure_id}:{expected_revision}:{fmt}") as (exports, publication):
        payload = {"figure_id": figure_id, "render_version": expected_revision, "format": fmt,
                   "export": exports[fmt], "checks": checks, "qa_receipt_id": summary.get("receipt_id")}
        committed = record_revision_export(figure_id, owner, expected_revision, fmt, payload,
                                           previous=restored, publication=publication)
        if committed is None:
            raise ProjectError("revision_conflict", "图件版本已更新，请重新打开。", status=409)
        complete_render_publication(figure_id, owner, required_batch=publication[0])
        if (committed.get("render_version") != expected_revision or committed.get("format") != fmt):
            raise ProjectError("artifact_unavailable", "该版本的文件无法验证，可刷新后重试。", status=409)
        read_entry(committed["export"], owner, fmt)
        return committed


def _resolve_edit_targets(project: dict[str, Any], raw_overrides: Any,
                          expected_revision: int | None) -> tuple[Any, dict[str, tuple[str, str]]]:
    """Translate document identities for this exact persisted render."""
    if not isinstance(raw_overrides, list):
        return raw_overrides, {}
    from src.figures.document import DocumentTargetError, resolve_document_object

    entries, semantic_targets = [], {}
    for item in raw_overrides:
        if not isinstance(item, dict) or "object_id" not in item:
            entries.append(item)
            continue
        if type(expected_revision) is not int:
            raise ProjectError("revision_required", "对象编辑需要当前图件版本。", status=409)
        target = item["object_id"]
        if "gid" in item or not isinstance(target, str) or not re.fullmatch(r"obj_[0-9a-f]{32}", target):
            raise ProjectError("invalid_object_target", "请选择一个有效的图件对象。")
        document = project.get("document_json") or {}
        try:
            binding = resolve_document_object(document, target, expected_revision=expected_revision)
        except DocumentTargetError as exc:
            raise ProjectError("object_target_unavailable", "图件对象已不可用，请重新加载后再编辑。", status=409) from exc
        if binding["identity_scope"] != "semantic":
            raise ProjectError("object_target_unavailable", "此图件对象没有稳定的编辑标识。", status=409)
        gid = binding["gid"]
        semantic_targets[gid] = (binding["semantic_key"], binding["role"])
        entries.append({**{key: value for key, value in item.items() if key != "object_id"}, "gid": gid})
    return entries, semantic_targets


def _confirm_edit_targets(manifest: dict[str, Any], targets: dict[str, tuple[str, str]]) -> None:
    if not targets:
        return
    objects = [item for item in manifest.get("objects", []) if isinstance(item, dict)]
    gids = Counter(item["gid"] for item in objects if isinstance(item.get("gid"), str))
    semantic_keys = Counter(item["semantic_key"] for item in objects if isinstance(item.get("semantic_key"), str))
    identities = {item["gid"]: (item.get("semantic_key"), item.get("role")) for item in objects
                  if isinstance(item.get("gid"), str) and item.get("identity_scope") == "semantic"}
    if manifest.get("semantic_identity_version") != 1 or any(
        identities.get(gid) != target or gids.get(gid) != 1 or semantic_keys.get(target[0]) != 1
        for gid, target in targets.items()
    ):
        raise ProjectError("object_target_changed", "重绘时图件对象已变化，无法安全应用这批编辑。", status=409)


def preview_overrides(figure_id: str, owner: int, raw_overrides: Any,
                      *, expected_revision: int | None = None, draft_revision: int = 0,
                      confirm_targets=None) -> dict[str, Any]:
    """Render a disposable draft; authorization and integrity precede every hit."""
    from src.figures.preview_pipeline import render_preview
    project = _load_revision(figure_id, owner, expected_revision)
    entries, targets = _resolve_edit_targets(project, raw_overrides, expected_revision)

    def validate():
        _assert_current(project, owner)
        _verify_sources(project, owner)
    def render():
        merged, changed, manifest, blobs = _render_edits(project, owner, entries,
                                                   formats=("svg",), render_mode="preview")
        _confirm_edit_targets(manifest, targets)
        return {"figure_id": figure_id, "manifest": manifest,
                "svg": blobs["svg"].decode("utf-8"),
                "render_version": int(project.get("render_version") or 0), "overrides": merged, "changed": changed}
    result = render_preview(project, owner, raw_overrides, validate=validate,
                            render=render, is_script=_is_script(project))
    if confirm_targets is not None:
        confirm_targets(result["manifest"], result["overrides"], result.get("changed", []))
    return {**{key: value for key, value in result.items() if key != "changed"}, "draft_revision": draft_revision}


def save_overrides(figure_id: str, owner: int, raw_overrides: Any,
                   *, session_id: str = "", expected_revision: int | None = None,
                   confirm_targets=None) -> dict[str, Any]:
    """Validate the user's edits, apply them, re-render and publish.

    The whole batch is rejected if any entry is invalid. A partial
    application would leave the figure in a state the user did not ask for and
    did not see.
    """
    project = _load_revision(figure_id, owner, expected_revision)
    entries, targets = _resolve_edit_targets(project, raw_overrides, expected_revision)
    from src.figures.preview_pipeline import invalidate
    invalidate(owner, figure_id)
    revision = int(project.get("render_version") or 0)
    previous_exports = project.get("exports_json") or {}
    formats = tuple(fmt for fmt in FIGURE_FORMATS if fmt in DEFAULT_FORMATS or fmt in previous_exports)
    merged, changed, new_manifest, blobs = _render_edits(
        project, owner, entries, formats=formats)
    _confirm_edit_targets(new_manifest, targets)
    if confirm_targets is not None:
        confirm_targets(new_manifest, merged, changed)

    stem = project.get("title") or "figure"
    with stage_rendered(owner, session_id or project.get("session_id", ""), stem, blobs) as (published, publication):
        committed_project = update_project_committed(
            figure_id, owner, expected_revision=revision, publication=publication,
            overrides_json=merged, manifest_json=new_manifest, exports_json=published,
            render_version=revision + 1, status="ready", last_error="")
        if committed_project is None:
            raise ProjectError("revision_conflict", "保存期间图件已更新或删除，当前版本未被覆盖。", status=409)
        complete_render_publication(figure_id, owner)
    committed_revision = int(committed_project.get("render_version") or 0)
    document = committed_project.get("document_json") or None
    # Return only the document written by this commit. Never fall back to a
    # concurrent revision's document when this one is missing or mismatched.
    if (not isinstance(document, dict)
            or document.get("revision") != committed_revision):
        document = None
    return {
        "figure_id": figure_id,
        "overrides": merged,
        "changed": changed,
        "manifest": new_manifest,
        "exports": published,
        "render_version": committed_revision,
        "document": document,
    }


def delete_for_owner(figure_id: str, owner: int) -> bool:
    """Delete a project owned by this account."""
    from src.figures.preview_pipeline import invalidate
    if get_project(str(figure_id), int(owner)) is None:
        raise ProjectError("not_found", "图件不存在或无权访问。", status=404)
    invalidate(owner, figure_id)
    return delete_project(str(figure_id), int(owner))


def import_from_library(owner: int, session_id: str, file_id: str,
                         *, title: str = "", mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Create a project from a file already in the account's library.

    The file must belong to this account — the library's own lookup enforces
    that, and a file belonging to somebody else is simply not found.
    """
    from src.session import files as session_files

    entry = session_files.get_account_file(str(file_id), int(owner))
    if not entry:
        raise ProjectError("not_found", "资料库中没有该文件，或该文件不属于当前账号。",
                           status=404)
    filename = str(entry.get("filename") or "")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in {"csv", "tsv", "xlsx", "xlsm", "xls", "xlsb"}:
        raise ProjectError(
            "unsupported_file",
            "只能从表格文件（CSV/TSV/XLSX）导入图件；"
            "绘图脚本请通过聊天里的绘图工具生成。")

    from src.figures.spec import parse_spec
    from src.figures.trusted_render import render_resolved, TrustedRenderError
    from src.figures.resolve import resolve_spec
    from src.figures.spec import SpecError
    from src.figures.declarative_snapshot import prepare_source, confirm_render, SnapshotError
    from src.research.tools import _load_frame_from_bytes

    try:
        import hashlib
        input_bytes = Path(entry["path"]).read_bytes()
        input_digest = hashlib.sha256(input_bytes).hexdigest()
        frame, _meta = _load_frame_from_bytes(input_bytes, filename)
    except Exception as exc:  # noqa: BLE001
        raise ProjectError("data_unavailable", f"文件无法解析：{exc}") from exc

    columns = [str(name) for name in list(frame.columns)]
    if not columns:
        raise ProjectError("empty_table", "这张表没有可用的列。")
    numeric = _numeric_columns(frame, columns)
    if not numeric:
        raise ProjectError("no_numeric_column", "这张表里没有可用的数值列。")

    from src.figures.resolve import select_table_figure
    if _meta.get("truncated"):
        raise ProjectError("data_truncated", "表格读取范围不完整，请选择明确的数据范围后再制图。")
    try:
        selected = (_mapped_import_spec(frame, str(file_id), mapping)
                    if mapping is not None else select_table_figure(frame, file_id=str(file_id)))
        selected["title"] = title or filename.rsplit(".", 1)[0]
        spec = parse_spec(selected)
    except SpecError as exc:
        raise ProjectError(exc.code, exc.message) from exc
    try:
        from src.figures.resolve import table_resolution_scope
        with table_resolution_scope({(int(owner), str(file_id)): frame}):
            resolved, info = resolve_spec(spec, owner_user_id=owner)
        source = prepare_source(spec, resolved, info)
        result = render_resolved(resolved, formats=DEFAULT_FORMATS, dpi=int(_figure_dpi()))
        blobs, manifest = result.blobs, result.manifest
        confirm_render(source, manifest)
    except (SpecError, TrustedRenderError, SnapshotError) as exc:
        raise ProjectError("render_failed",
                           str(getattr(exc, "message", exc))) from exc

    _verify_sources({"source_json": source,
                     "data_refs_json": [{**ref, "sha256": input_digest} for ref in info.get("refs", [])]}, owner)
    stem = title or filename.rsplit(".", 1)[0]
    manifest["data_provenance"] = info
    with stage_rendered(owner, session_id, stem, blobs) as (published, publication):
        project = create_project(owner_user_id=owner, title=stem, source_kind="spec", source=source,
            session_id=session_id, data_refs=info.get("refs", []), initial_manifest=manifest,
            initial_exports=published, publication=publication)
        complete_render_publication(project["figure_id"], owner)

    return {
        "figure_id": project["figure_id"],
        "title": stem,
        "columns": columns,
        "numeric_columns": numeric,
        "manifest": manifest,
        "exports": published,
    }


def _mapped_import_spec(frame: Any, file_id: str, mapping: dict[str, str]) -> dict:
    """Map explicit user choices only; multiple numeric columns never imply Y."""
    from src.figures.spec import SpecError
    allowed = {"chart", "x", "y", "group", "x_unit", "y_unit"}
    if (not isinstance(mapping, dict) or set(mapping) - allowed
            or any(not isinstance(value, str) or len(value) > 200 for value in mapping.values())):
        raise SpecError("figure_selection_required", "请明确图型、数据列与单位。")
    chart = mapping.get("chart", "")
    if chart not in {"histogram", "boxplot", "scatter", "line", "bar"}:
        raise SpecError("figure_selection_required", "请选择支持的图型；热图请使用脚本路径。")
    paired = chart in {"scatter", "line", "bar"}
    x, y, group = (mapping.get(name, "") for name in ("x", "y", "group"))
    columns = [str(name) for name in frame.columns]
    if len(set(columns)) != len(columns):
        raise SpecError("figure_selection_required", "存在重复列名，请先给每列唯一名称。")
    if y not in columns or (paired and x not in columns) or (group and group not in columns):
        raise SpecError("figure_selection_required", "所选列不存在，或尚未明确选择 X/Y。")
    if y not in _numeric_columns(frame, columns):
        raise SpecError("figure_selection_required", "Y 指标需要可解析的数值列。")
    if group and chart == "histogram":
        raise SpecError("figure_selection_required", "分组分布请明确选择箱线图；直方图不隐式合并分组。")
    if group and (group == y or (paired and group == x)):
        raise SpecError("figure_selection_required", "分组列不能同时作为 X/Y 指标。")
    def ref(name):
        return {"kind": "account_file", "file_id": file_id, "column": name}
    def label(name, unit):
        return f"{name} ({unit.strip()})" if unit.strip() else name
    result = {"chart": chart, "y": ref(y), "error": "none", "legend": bool(group),
              "x_label": label(x, mapping.get("x_unit", "")) if paired else
                         label(y, mapping.get("y_unit", "")) if chart == "histogram" else group,
              "y_label": "Count" if chart == "histogram" else label(y, mapping.get("y_unit", ""))}
    if paired:
        result["x"] = ref(x)
    if group:
        result["group"] = ref(group)
    return result


def _numeric_columns(frame: Any, columns: list[str]) -> list[str]:
    """The columns whose values can be read as numbers.

    The dtype is not enough: the service's CSV reader keeps every column as
    ``object`` so that mixed columns survive the round trip. So a column
    counts as numeric when its values actually convert — which is what the
    renderer will do with them.
    """
    out: list[str] = []
    for name in columns:
        series = frame[name]
        values = [value for value in list(series)[:200] if value is not None
                  and str(value).strip() != ""]
        if not values:
            continue
        convertible = 0
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number == number and number not in (float("inf"), float("-inf")):
                convertible += 1
        if convertible and convertible >= len(values) * 0.8:
            out.append(name)
    return out


def _figure_dpi() -> int:
    from src.config import settings
    try:
        return int(getattr(settings, "FIGURES_DPI", 300) or 300)
    except (TypeError, ValueError):
        return 300


def data_files(owner: int) -> list[dict[str, Any]]:
    """The account's tables, for the workspace's "import from library" list."""
    from src.session import files as session_files

    entries = session_files.list_account_files(int(owner))
    out: list[dict[str, Any]] = []
    for entry in entries:
        filename = str(entry.get("filename") or "")
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in {"csv", "tsv", "xlsx", "xlsm", "xls", "xlsb"}:
            continue
        out.append({
            "file_id": entry.get("file_id", ""),
            "filename": filename,
            "size_bytes": entry.get("size_bytes", 0),
            "created_at": entry.get("created_at", 0),
            "download_url": entry.get("download_url", ""),
        })
    return out


def schema_for_file(owner: int, file_id: str) -> dict[str, Any]:
    """The columns of one account table, read only."""
    from src.session import files as session_files

    entry = session_files.get_account_file(str(file_id), int(owner))
    if not entry:
        raise ProjectError("not_found", "文件不存在或不属于当前账号。", status=404)
    filename = str(entry.get("filename") or "")
    from src.research.tools import _load_frame_from_bytes
    try:
        frame, meta = _load_frame_from_bytes(
            Path(entry["path"]).read_bytes(), filename)
    except Exception as exc:  # noqa: BLE001
        raise ProjectError("data_unavailable", f"文件无法解析：{exc}") from exc
    session_files.get_account_file(str(file_id), int(owner), touch=True)
    columns = []
    numeric = set(_numeric_columns(frame, [str(name) for name in list(frame.columns)]))
    for name in list(frame.columns):
        series = frame[name]
        columns.append({
            "name": str(name),
            "dtype": str(series.dtype),
            "numeric": str(name) in numeric,
            "non_null": int(series.notna().sum()),
        })
    return {
        "file_id": str(file_id),
        "filename": filename,
        "rows": int(frame.shape[0]),
        "columns": columns,
        "sheet": meta.get("sheet", ""),
        "truncated": bool(meta.get("truncated")),
    }


__all__ = [
    "DEFAULT_FORMATS", "FIGURE_FORMATS", "MIME_BY_FORMAT", "ProjectError",
    "data_files", "delete_for_owner", "detail_for_owner", "get_for_owner",
    "import_from_library", "list_for_owner", "stage_rendered",
    "preview_overrides", "save_overrides", "schema_for_file",
]
