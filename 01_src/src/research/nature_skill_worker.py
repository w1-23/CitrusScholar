"""Deprecated compatibility adapter for the pinned Nature fixed templates.

What changed (audit F-04, Phase C)
----------------------------------
``run_nature_plot`` / ``collect_nature_plot`` used to own a second execution
chain: their own workspace binding, their own Code Worker submission, their own
90 s ``ExecutionLimits``, their own publication and their own workbench
initialization. The audit kept the ``nature-figure`` Skill and the five
templates, and retired that chain.

This module is the compatibility window. It no longer schedules anything:

* ``schedule_plot`` validates the request exactly as before (authorized public
  CSV, pinned release, fixed template, R gating) and then **forwards** the typed
  template to :mod:`src.figures.template_execution`, which runs the one unified
  figure tool. The execution id it returns is the unified run id — there is no
  second submission record and no second execution id.
* ``collect_plot`` reads that same unified run receipt; it publishes nothing.

What is kept on purpose
-----------------------
``_source``, ``_meta``, ``_bound_request`` and ``LIMITS`` remain because
historical receipts still have to be readable and verifiable after the entry
points are removed. They are readers now, not a submission path.

R stays explicit: the pinned R worker supports only a narrow marginal
capability and is not a general backend. Until R passes the shared identity,
budget, deadline, network policy, receipt and workbench contracts, it is
reported as unavailable rather than silently swapped for Python.
"""
from __future__ import annotations

from pathlib import Path

from src.agent.code_tools import _deadline, _service, authorize_fusion_code  # noqa: F401
from src.code_execution.protocol import ExecutionLimits, ExecutionRequest
from src.session import files

from . import nature_skill_r
from .nature_skill_contract import (CSV, MAX_CSV_BYTES, PRODUCER, SCOPE, SCRIPT, SKILL_ID,
                                   RunArgs, argv, output_names, template_qa, template_request)

FORWARDED_TOOL = "run_research_figure_script"
FORWARD_REASON = "nature_plot_forwarded_to_unified_figure_execution"
FORWARD_UNAVAILABLE = "nature_plot_forward_to_unified_execution_unavailable"
R_NOT_UNIFIED = "nature_plot_r_backend_not_unified"

# The single Worker budget policy (F-05) replaces the old 90 s constant.
from citrus_figures.timeouts import resolve_timeout  # noqa: E402  (policy import is deliberate)

LIMITS = ExecutionLimits(
    timeout_sec=resolve_timeout(kind="template"),
    memory_mb=2048, pids=64, log_bytes=64 << 10,
    scratch_bytes=96 << 20, output_bytes=64 << 20, output_files=4)

# Receipts created before F-05 recorded 90 s. Historical receipts stay readable
# and verifiable; they are never re-created.
HISTORICAL_TIMEOUTS = frozenset({90, LIMITS.timeout_sec})


class NatureSubmissionUncertain(RuntimeError):
    """A submit may have committed, or its existing receipt could not be checked."""


# ── historical receipt verification (readers only) ────────────────────────

def _meta(pin, spec):
    metadata = {"code_scope": SCOPE, "release_sha256": pin.release_sha256,
                "upstream_commit": pin.commit, "script_sha256": pin.sha256,
                "spec": spec.model_dump()}
    if spec.backend == "r":
        metadata["r_image_sha256"] = nature_skill_r.R_IMAGE_SHA256
    return metadata


def _execution_template(release, pin, spec):
    return (nature_skill_r.SCRIPT, nature_skill_r.sealed_adapter(Path(release), pin)) \
        if spec.backend == "r" else (SCRIPT, pin)


def _source(identity, ref):
    row = files.get_task_input_file(identity.owner_user_id, identity.run_id, ref.file_id, ref.sha256)
    if (not row or row.get("visibility") != "public" or row.get("origin") not in files.ORIGINS
            or row.get("sha256") != ref.sha256):
        raise PermissionError("nature_plot_input_scope_denied")
    if (Path(row["filename"]).suffix.lower() != ".csv"
            or type(row.get("size_bytes")) is not int or not 0 < row["size_bytes"] <= MAX_CSV_BYTES):
        raise ValueError("nature_plot_requires_nonempty_utf8_csv_within_worker_limit")
    return {"file_id": ref.file_id, "sha256": ref.sha256, "size_bytes": row["size_bytes"]}


def _limits_accepted(limits) -> bool:
    """Accept the policy budget and the historical 90 s receipt it replaced."""
    return (isinstance(limits, ExecutionLimits)
            and limits.memory_mb == LIMITS.memory_mb and limits.pids == LIMITS.pids
            and limits.log_bytes == LIMITS.log_bytes
            and limits.scratch_bytes == LIMITS.scratch_bytes
            and limits.output_bytes == LIMITS.output_bytes
            and limits.output_files == LIMITS.output_files
            and limits.timeout_sec in HISTORICAL_TIMEOUTS)


def _bound_request(service, identity, row, pin, *, release=None):
    """Verify a retained historical request against this account and pin."""
    request = ExecutionRequest.parse(row["request"])
    if request.identity != identity or not request.task_id.startswith("nature_"):
        raise PermissionError("nature_plot_execution_scope_denied")
    current = service.workspace.open(request.workspace_id)
    metadata = current["metadata"]
    if metadata.get("code_scope") != SCOPE:
        raise PermissionError("nature_plot_execution_scope_denied")
    spec = RunArgs.model_validate(metadata.get("spec"))
    script, pin = _execution_template(release, pin, spec)
    stem, paths = output_names(spec.plot.template, request.task_id)
    if (metadata != _meta(pin, spec)
            or current["task_id"] != request.task_id
            or request.operation_id != request.task_id or request.idempotency_key != request.task_id
            or request.script.path != script or request.script.sha256 != pin.sha256
            or request.script.size_bytes != len(pin.content) or request.script.version != 1
            or len(request.inputs) != 1 or request.inputs[0].path != CSV
            or request.inputs[0].version != 1 or request.inputs[0].file_id != spec.input.file_id
            or request.inputs[0].sha256 != spec.input.sha256
            or not 0 < request.inputs[0].size_bytes <= MAX_CSV_BYTES
            or request.arguments != argv(spec, stem) or request.outputs != paths
            or request.environment_id != "base" or request.environment_revision != 1
            or request.network_policy != "offline" or not _limits_accepted(request.limits)):
        raise PermissionError("nature_plot_execution_contract_mismatch")
    return request, spec, stem


# ── compatibility entry points: forward, never schedule ───────────────────

def schedule_plot(identity, *, release, pin, spec):
    """Forward a deprecated fixed-template request to the one figure chain.

    Returns the unified execution id. The R backend is refused explicitly: it
    is not part of the unified worker yet, and it is never replaced by Python.
    """
    authorize_fusion_code(identity, release)
    if spec.backend == "r":
        nature_skill_r.require_template(spec)
        if not nature_skill_r.available():
            raise LookupError("nature_plot_r_runtime_unavailable")
        raise LookupError(R_NOT_UNIFIED)
    source = _source(identity, spec.input)
    template_id, parameters = template_request(spec)
    from src.figures.template_execution import execute_template, forwarded_receipt

    _content, artifact = execute_template(
        template_id=template_id, parameters=parameters, file_id=source["file_id"],
        title=spec.title or "", formats=("svg", "pdf"))
    execution_id, status = forwarded_receipt(artifact)
    if not execution_id:
        reason = str(((artifact or {}).get("_tool_outcome") or {}).get("reason_code") or "")
        raise LookupError(reason or FORWARD_UNAVAILABLE)
    return {"execution_id": execution_id, "status": status, "forwarded": True,
            "forwarded_to": FORWARDED_TOOL, "artifact": artifact}


def collect_plot(identity, *, release, pin, execution_id, receipts):
    """Read one unified execution receipt; publish nothing of its own."""
    from src.research import script_assets

    authorize_fusion_code(identity, release)
    result = {"execution_id": execution_id, "forwarded_to": FORWARDED_TOOL,
              "deprecated": True, "completion_scope": "forwarded_collection"}
    try:
        row = script_assets.get_run(identity.owner_user_id, identity.session_id, execution_id)
    except Exception:
        row = None
    if not isinstance(row, dict):
        return result, "not_run", "nature_plot_collection_unavailable"
    payload = row.get("result") if isinstance(row.get("result"), dict) else {}
    result["worker_status"] = row.get("status")
    result["template"] = str((payload.get("template") or {}).get("template_id") or "")
    if row.get("status") != "succeeded":
        if row.get("status") in {"failed", "partial"}:
            return result, "failed", "nature_plot_worker_failed"
        return result, "not_run", "nature_plot_worker_not_settled"
    exports = _verified_exports(identity, payload)
    if not exports:
        return result, "not_run", "nature_plot_collection_unavailable"
    result.update(original_exports=exports,
                  verified_original_links=[item["artifact_url"] for item in exports.values()],
                  figures=[{"figure_id": figure.get("figure_id"),
                            "workspace_url": figure.get("workspace_url"),
                            "project_registered": bool(figure.get("project_registered"))}
                           for figure in (payload.get("figures") or []) if isinstance(figure, dict)],
                  provenance={"forwarded_to": FORWARDED_TOOL,
                              "unified_run_id": execution_id,
                              "template": result["template"]})
    receipts[:] = [{"url": item["artifact_url"]} for item in exports.values()]
    return result, "complete", "nature_plot_originals_verified"


def _verified_exports(identity, payload):
    """Only links the artifact registry authenticates as current-account bytes."""
    from src.research.tools import validate_artifact_links

    verified = {}
    for figure in payload.get("figures") or []:
        if not isinstance(figure, dict):
            continue
        for item in figure.get("exports") or []:
            url = str(item.get("artifact_url") or "")
            if not url:
                continue
            checked = validate_artifact_links([url], session_id=identity.session_id,
                                              owner_user_id=identity.owner_user_id)
            if not checked or not checked[0].get("valid"):
                continue
            entry = dict(item)
            entry["artifact_url"] = url
            verified[str(item.get("filename") or f"{item.get('format') or 'figure'}")] = entry
    return verified


_TERMINAL_RECEIPT_STATES = frozenset({"failed", "partial", "succeeded"})

__all__ = ["FORWARDED_TOOL", "FORWARD_REASON", "FORWARD_UNAVAILABLE", "HISTORICAL_TIMEOUTS",
           "LIMITS", "NatureSubmissionUncertain", "R_NOT_UNIFIED", "collect_plot",
           "schedule_plot"]
