"""Private, invocation-scoped evidence for a failed script with no publication.

Only production adapters can record this evidence. Nothing in tool arguments,
worker JSON, or a model-visible outcome grants reconciliation authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import hashlib
import json


_current = contextvars.ContextVar("script_failure_reconciliation", default=None)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _FailureEvidence:
    owner: int
    session: str
    source: dict | None = None
    result_digest: str = ""
    preflight_digest: str = ""
    undeclared_table: str = ""
    repair_projection: dict | None = None
    repair_digest: str = ""

    def output_contract_repair(self, message, *, reconciled):
        """Return host-only comparison keys for a bound, acknowledged invocation."""
        if not self.repair_projection or not self.repair_digest:
            return None
        if "missing" in self.repair_projection and not reconciled:
            return None
        try:
            payload = json.loads(message.content)
            receipt = (message.artifact or {}).get("op_receipt") or {}
            if (_digest(payload) == self.repair_digest
                    and receipt.get("run_id") == payload["run_id"]
                    and receipt.get("repair_identity") == payload["repair_identity"]
                    and receipt.get("status") == payload["_tool_outcome"]["status"]
                    and receipt.get("reason_code") == payload["_tool_outcome"]["reason_code"]):
                return self.repair_projection
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
        return None

    def matches(self, message) -> bool:
        """Recheck private invocation evidence before relaxing unknown."""
        if self.preflight_digest:
            try:
                payload = json.loads(message.content)
                return (payload.get("_tool_outcome") == {
                            "status": "not_run", "reason_code": "script_execution_rejected", "artifacts": []}
                        and _digest(payload) == self.preflight_digest
                        and {key: value for key, value in (message.artifact or {}).items()
                             if key != "op_receipt"} == payload)
            except (ValueError, TypeError, AttributeError):
                return False
        if not self.source or not self.result_digest:
            return False
        try:
            from src.research import script_assets
            from src.figures.projects import script_replay_inputs
            payload = json.loads(message.content)
            if (_digest(payload) != self.result_digest
                    or payload.get("_tool_outcome", {}).get("status") != "failed"
                    or (message.artifact or {}).get("_tool_outcome") != payload["_tool_outcome"]):
                return False
            run = script_assets.get_run(self.owner, self.session, self.source["run_id"])
            if (run["status"] != "failed" or not run["finished_at"]
                    or run["script_ref"] != self.source["script_ref"]
                    or _digest(run["result"]) != self.result_digest):
                return False
            # get_run verifies the owned source version; verify frozen input
            # bytes too, rather than allowing a repair against missing evidence.
            script_replay_inputs(payload["replay"], self.owner)
            return True
        except Exception:
            return False  # Lost/compacted/corrupt receipts remain unknown writes.


@contextmanager
def capture_script_failure(owner: int, session: str):
    evidence = _FailureEvidence(owner, session)
    token = _current.set(evidence)
    try:
        yield evidence
    finally:
        _current.reset(token)


def observe_preflight_rejection(payload):
    """Seal a pure validation rejection before any script/run/input writes.

    Only the first-party adapter calls this branch. Public payload fields do
    not grant authority, and evidence never survives the invocation context.
    """
    from src.core.tracing import get_user_id, get_session_id
    evidence = _current.get()
    if (evidence is not None and evidence.source is None and not evidence.result_digest
            and (get_user_id(), get_session_id()) == (evidence.owner, evidence.session)
            and payload.get("_tool_outcome") == {
                "status": "not_run", "reason_code": "script_execution_rejected", "artifacts": []}):
        evidence.preflight_digest = _digest(payload)


def observe_failed_worker(result, source: dict, *, validated_output_failure=False, explicit_outputs=False):
    """Called only at adapter branches that have made no publication attempt.

    A successful worker with a validated v2 failure receipt can also be observed;
    the publisher must have rejected all outputs without attempting persistence.
    """
    from src.figures.sandbox import (SandboxResult, EXIT_SCRIPT_ERROR, EXIT_NO_FIGURE,
                                    EXIT_MULTIPLE_FIGURES, EXIT_FORBIDDEN, EXIT_OK)
    evidence = _current.get()
    if (evidence is None or not isinstance(result, SandboxResult)
            or result.process_exited is not True or result.timed_out
            or result.backend not in {"docker", "wsl-docker", "apptainer"}):
        return
    known_failure = (not result.ok and not result.outputs
                     and result.exit_code in {EXIT_SCRIPT_ERROR, EXIT_NO_FIGURE,
                                              EXIT_MULTIPLE_FIGURES, EXIT_FORBIDDEN})
    if not known_failure and not (validated_output_failure and result.ok and result.exit_code == EXIT_OK):
        return
    if not source.get("run_id") or not source.get("script_ref") or "input_snapshots" not in source:
        return
    evidence.source = json.loads(json.dumps({key: source[key] for key in
        ("run_id", "script_ref", "input_snapshots")}))
    diagnostic = result.diagnostic
    if (explicit_outputs and known_failure and result.exit_code == EXIT_SCRIPT_ERROR
            and diagnostic.get("stage") == "analysis"
            and diagnostic.get("error_code") == "undeclared_table_output"):
        evidence.undeclared_table = diagnostic.get("output_name") or ""


def finalize_failed_worker(owner, session, run, payload, warning):
    """Seal only an acknowledged durable failed run, before returning its receipt."""
    evidence = _current.get()
    if (evidence is None or evidence.source is None or warning is not None
            or (owner, session) != (evidence.owner, evidence.session)
            or run["run_id"] != evidence.source["run_id"]
            or payload.get("run_id") != evidence.source["run_id"]
            or payload.get("script_ref") != evidence.source["script_ref"]
            or payload.get("_tool_outcome", {}).get("status") != "failed"
            or payload.get("replay", {}).get("input_snapshots") != evidence.source["input_snapshots"]):
        return
    evidence.result_digest = _digest(payload)


def finalize_output_contract(owner, session, run, payload, warning, *, snapshots, environment):
    """Compare exact contracts modulo renderer source and one unused new table."""
    from src.core.tracing import get_client_request_id

    evidence = _current.get()
    request = get_client_request_id()
    if (evidence is None or not request or warning is not None or not isinstance(snapshots, list)
            or (owner, session) != (evidence.owner, evidence.session)
            or not payload.get("repair_identity") or not run.get("logical_task_id")):
        return
    outcome = payload.get("_tool_outcome") or {}
    contract = run.get("output_contract") or {}
    figures = contract.get("figures") or []
    if not figures:
        return
    # Preserve the renderer/analysis boundary while permitting renderer code edits.
    contract = {**contract, "figures": [
        {**figure, **({"render_script": True} if "render_script" in figure else {})}
        for figure in figures]}
    facts = [owner, session, request, run["logical_task_id"].split(":", 1)[0],
             run["inputs"], run["parameters"],
             environment, [{key: item.get(key) for key in
                 ("variable", "sha256", "source_file_id", "dataset_ref")} for item in snapshots]]
    tables = contract["tables"]
    if (outcome.get("status") == "failed" and outcome.get("reason_code") == "SCRIPT_ERROR"
            and not outcome.get("artifacts") and evidence.result_digest and evidence.undeclared_table):
        name = evidence.undeclared_table
        if any(item["name"].casefold() == name.casefold() for item in tables + figures):
            return
        projection = {"missing": {name: _digest([facts, contract])}}
    elif (outcome.get("status") == "complete" and outcome.get("reason_code") == "figure_published"
            and payload.get("execution_stage") == "worker_receipt_verified"
            and outcome.get("artifacts")):
        # No original slot, figure parent or intermediate table may depend on the
        # declaration removed for comparison. The new slot is auxiliary only.
        used = {parent.casefold() for item in tables + figures for parent in item.get("parents", [])}
        added = {table["name"]: _digest([facts, {**contract, "tables": [
            item for item in tables if item is not table]}])
            for table in tables if table["name"].casefold() not in used}
        if not added:
            return
        projection = {"added": added}
    else:
        return
    evidence.repair_projection = projection
    evidence.repair_digest = _digest(payload)
