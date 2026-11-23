"""Invocation-private evidence of a refused workspace or Code mutation."""
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import hashlib
import json

_current = contextvars.ContextVar("workspace_preflight_reconciliation", default=None)
_CODE_REASONS = {
    "code_write": {"workspace_revision_conflict"},
    "code_bind_input": {"workspace_revision_conflict", "code_input_source_denied"},
    "code_execute": {"code_worker_unavailable", "environment_revision_not_found",
                     "execution_base_environment_revision_must_be_1", "environment_selection_scope_mismatch"},
    "code_environment_install": {"code_worker_unavailable", "workspace_revision_conflict",
                                 "environment_lock_not_bound"},
    "code_environment_publish": {"environment_publication_conflict", "environment_execution_not_found",
                                 "environment_successful_installer_required", "environment_base_is_immutable"},
}
_WORKSPACE_MUTATIONS = frozenset({"workspace_write", "workspace_edit"})
ZERO_WRITE_TOOL_NAMES = _WORKSPACE_MUTATIONS | frozenset(_CODE_REASONS)


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _PreflightEvidence:
    owner: int
    session: str
    run_id: str
    tool_name: str
    call_id: str
    preflight_digest: str = ""

    def matches(self, message):
        if (not self.preflight_digest or message.name != self.tool_name
                or message.tool_call_id != self.call_id):
            return False
        try:
            payload = json.loads(message.content)
            return (_digest(payload) == self.preflight_digest
                    and {key: value for key, value in (message.artifact or {}).items()
                         if key != "op_receipt"} == payload)
        except (ValueError, TypeError, AttributeError):
            return False


@contextmanager
def capture_zero_write(owner, session, run_id, tool_name, call_id):
    evidence = _PreflightEvidence(owner, session, run_id, tool_name, call_id)
    token = _current.set(evidence)
    try:
        yield evidence
    finally:
        _current.reset(token)


def _observe(owner, session, run_id, tool_name, call_id, payload, allowed):
    from src.core import tracing
    evidence = _current.get()
    outcome = payload.get("_tool_outcome", {})
    reason = outcome.get("reason_code")
    if (evidence is not None and (owner, session, run_id, tool_name, call_id)
            == (evidence.owner, evidence.session, evidence.run_id, evidence.tool_name, evidence.call_id)
            and (tracing.get_user_id(), tracing.get_session_id(), tracing.get_job_id()) == (owner, session, run_id)
            and reason in allowed and outcome == {"status": "not_run", "reason_code": reason, "artifacts": []}):
        evidence.preflight_digest = _digest(payload)


def observe_write_conflict(storage, payload):
    """Only after WorkspaceConflict has unwound and staged-file cleanup succeeded."""
    evidence = _current.get()
    if evidence is not None and evidence.tool_name in _WORKSPACE_MUTATIONS and payload.get("write_applied") is False:
        _observe(storage.owner, storage.session, evidence.run_id, evidence.tool_name, evidence.call_id,
                 payload, {"workspace_version_conflict"})


def observe_code_rejection(identity, call_id, operation, payload):
    tool_name = f"code_{operation}"
    _observe(identity.owner_user_id, identity.session_id, identity.run_id, tool_name, call_id,
             payload, _CODE_REASONS.get(tool_name, ()))
