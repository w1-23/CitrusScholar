"""Bounded evidence codec and per-operation journal guard.

ToolNode owns scheduling. A stored operation receipt is evidence only. It never
licenses another dispatch or automatic execution recovery.
"""
from __future__ import annotations

import json
import logging
import math

from langchain_core.messages import ToolMessage

from src.core import task_events
from .runtime import BatchObservation, validate_execution_waits

# A checkpoint must stay bounded. Reject, rather than silently trim a published
# observation; the host's full-result references remain the source of truth.
logger = logging.getLogger(__name__)
MAX_BATCH_BYTES = 1024 * 1024
_MESSAGE_FIELDS = frozenset({"content", "tool_call_id", "name", "id", "status",
                             "artifact", "additional_kwargs", "response_metadata"})


class RecoveryRequiresReconciliation(task_events.TaskConflict):
    """Execution may have happened. Reconcile in the host; never retry here."""

    def __init__(self):
        self.reason = "recovery_requires_reconciliation"
        super().__init__(self.reason)


class BatchObservationNotStored(task_events.TaskConflict):
    """Effects are not retried when their model observation cannot be saved."""

    def __init__(self, *, effects_uncertain):
        self.reason = "batch_observation_not_stored"
        self.effects_uncertain = effects_uncertain
        super().__init__(self.reason)


class _EncodingError(ValueError):
    pass


def _report_failure(stage, reason, count):
    # reason is supplied by local encoders, never by tool payloads or sqlite.
    logger.warning("batch_journal_failed stage=%s reason=%s call_count=%s",
                   stage, reason, count)


def _json_safe(value):
    """Return a JSON-native copy, converting only numeric and time scalars.

    Tool observations routinely carry numpy scalars (``numpy.float64`` is not a
    ``float``) or an array; the strict checker below rejects those and would
    fail a whole turn after its side effects had already run. Only these
    closed families are converted; every other object still fails ``check``, so
    no provider message, custom class or host path can reach the journal.
    """
    if type(value) is float and not math.isfinite(value):
        return None
    if type(value) in (list, tuple):
        return [_json_safe(item) for item in value]
    if type(value) is dict:
        return {key: _json_safe(item) for key, item in value.items()}
    module = (type(value).__module__ or "").split(".")[0]
    name = type(value).__name__
    if module == "numpy":
        if hasattr(value, "tolist"):
            converted = value.tolist()
            if type(converted) is not type(value):
                return _json_safe(converted)
    if module == "pandas" and name in ("NAType", "NaTType"):
        return None
    if module == "pandas" and name in ("Timestamp", "Timedelta"):
        return value.isoformat()
    if module == "datetime" and name in ("datetime", "date", "time"):
        return value.isoformat()
    return value


def observation_json(value):
    # No default encoder, model dumping, pickle, or import-based revival. Reject
    # objects (including private provider messages) even inside tool artifacts.
    def check(item):
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _EncodingError("batch_non_finite_number")
            return
        # Product operation receipts contain immutable artifact sequences.
        # JSON preserves their ordered values as arrays; never revive classes.
        if type(item) in (list, tuple):
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        logger.warning("batch_journal_non_json_value kind=%s",
                       type(item).__name__[:40])
        raise TypeError("batch_journal_requires_json_values")

    value = _json_safe(value)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


_json = observation_json


def persistent_message(message):
    fields = {key: getattr(message, key) for key in _MESSAGE_FIELDS}
    encoded = observation_json(fields)
    if len(encoded.encode("utf-8")) > MAX_BATCH_BYTES:
        raise _EncodingError("batch_observation_exceeds_checkpoint_limit")
    return ToolMessage(**json.loads(encoded))


def retain_observation(identity, execution, message, call):
    if not execution.operation_id or not execution.logical_key or execution.uncertain:
        return
    try:
        task_events.retain_operation_observation(identity, execution.operation_id, message.tool_call_id,
            execution.logical_key, execution.attempt,
            _encode(BatchObservation((message,), pending_question=execution.pending_question,
                    execution_waits=execution.execution_waits), [call]))
    except task_events.TaskConflict:
        _report_failure("retain", "operation_observation_unconfirmed", 1)


def _validate(batch, calls):
    if not isinstance(batch, BatchObservation):
        raise TypeError("executor_must_return_batch_observation")
    validate_execution_waits(batch.execution_waits)
    if type(batch.pending_question) is not bool:
        raise TypeError("batch_pending_question_requires_bool")
    for values in (batch.context_notes, batch.unknown_operations):
        if not isinstance(values, (tuple, list)) or any(type(v) is not str for v in values):
            raise TypeError("batch_metadata_requires_strings")
    if any(not isinstance(m, ToolMessage) for m in batch.messages):
        raise TypeError("executor_observation_must_be_tool_message")
    names = {call["id"]: call["name"] for call in calls}
    ids = [m.tool_call_id for m in batch.messages]
    if ids != list(names):
        raise ValueError("executor_observation_pairing_mismatch")
    if any(m.name and m.name != names[m.tool_call_id] for m in batch.messages):
        raise ValueError("executor_observation_name_mismatch")


def _encode(batch, calls):
    try:
        _validate(batch, calls)
    except (ValueError, TypeError) as exc:
        raise _EncodingError("batch_observation_contract_invalid") from exc
    data = {"version": 2 if batch.execution_waits else 1, "messages": [
        {key: getattr(message, key) for key in _MESSAGE_FIELDS}
        for message in batch.messages],
        "pending_question": batch.pending_question,
        "unknown_operations": list(batch.unknown_operations),
        "context_notes": list(batch.context_notes)}
    if batch.execution_waits:
        data["execution_waits"] = list(batch.execution_waits)
    try:
        encoded = _json(data)
        encoded_size = len(encoded.encode("utf-8"))
    except _EncodingError:
        raise
    except UnicodeError as exc:
        raise _EncodingError("batch_observation_invalid_text") from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise _EncodingError("batch_observation_not_json") from exc
    if encoded_size > MAX_BATCH_BYTES:
        raise _EncodingError("batch_observation_exceeds_checkpoint_limit")
    return encoded


def require_unclaimed_call(identity, call):
    """Fail closed if this exact provider call already reached the operation journal."""
    from .runtime import AdmissionStopped

    with task_events.transaction() as conn:
        row = conn.execute("SELECT 1 FROM task_operations WHERE job_id=? AND owner_user_id=? "
                           "AND tool_call_id=? LIMIT 1", (identity.run_id, identity.owner_user_id,
                           call["id"])).fetchone()
    if row is not None:
        raise AdmissionStopped("recovery_requires_reconciliation")
