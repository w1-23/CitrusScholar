"""Durable outbox for a published capability batch, on the existing jobs DB.

This is not an operation ledger: HostTools owns effects, leases and full result
storage. Only the server identity, native-call digest and bounded published
observations live here. An intent is never a license to replay uncertain work.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from typing import Awaitable, Callable

from langchain_core.messages import AIMessage, ToolMessage

from src.core import task_events
from .identity import ExecutionIdentity
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


def _calls(response):
    if not isinstance(response, AIMessage):
        raise TypeError("batch_journal_requires_ai_message")
    calls = response.tool_calls
    ids = [call.get("id") for call in calls]
    if (not calls or response.invalid_tool_calls
            or any(not isinstance(cid, str) or not cid for cid in ids)
            or len(set(ids)) != len(ids)
            or any(not isinstance(c.get("name"), str) or not c["name"]
                   or not isinstance(c.get("args"), dict) for c in calls)):
        raise ValueError("invalid_batch_call_ids")
    # Keep operation identities independent of observation normalization.
    return json.loads(task_events.canonical(calls))


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


def _reject_nonfinite(_):
    raise ValueError("batch_non_finite_number")


def _decode(encoded, calls):
    try:
        if len(encoded.encode("utf-8")) > MAX_BATCH_BYTES:
            raise ValueError("oversized_batch_checkpoint")
        data = json.loads(encoded, parse_constant=_reject_nonfinite)
        fields = {"version", "messages", "pending_question", "unknown_operations", "context_notes"}
        version = data.get("version") if isinstance(data, dict) else None
        if (type(version) is not int or version not in (1, 2)
                or set(data) != (fields | {"execution_waits"} if version == 2 else fields)
                or (version == 2 and not isinstance(data["execution_waits"], list))
                or not isinstance(data["messages"], list)
                or any(not isinstance(m, dict) or set(m) != _MESSAGE_FIELDS
                       for m in data["messages"])
                or not isinstance(data["unknown_operations"], list)
                or not isinstance(data["context_notes"], list)):
            raise ValueError("invalid_batch_checkpoint")
        # ToolMessage is the only constructor allowed. JSON dictionaries that
        # resemble a serialized object stay inert dictionaries.
        batch = BatchObservation(
            tuple(ToolMessage(**message) for message in data["messages"]),
            pending_question=data["pending_question"],
            unknown_operations=tuple(data["unknown_operations"]),
            context_notes=tuple(data["context_notes"]),
            execution_waits=tuple(data.get("execution_waits", ())))
        _validate(batch, calls)
        return batch
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _report_failure("read", "batch_observation_invalid", len(calls))
        raise RecoveryRequiresReconciliation() from None


class BatchJournal:
    """Wrap one existing executor; completed batches can be read after restart.

    ``await BatchJournal(identity).execute(response, capability_executor)``
    accepts the executor's async ``(response, identity) -> BatchObservation``
    contract. The job must still be running/detached, including for cache reads.
    There is deliberately no takeover, delete, retry or effect-recovery API.
    """

    def __init__(self, identity: ExecutionIdentity):
        if not isinstance(identity, ExecutionIdentity):
            raise TypeError("batch_journal_requires_execution_identity")
        self.identity = identity

    def _authorize(self, conn):
        identity = self.identity
        job = conn.execute("SELECT owner_user_id,session_id,request_id,status "
                           "FROM task_jobs WHERE job_id=?", (identity.run_id,)).fetchone()
        if (job is None or job["owner_user_id"] != identity.owner_user_id
                or job["session_id"] != identity.session_id
                or job["request_id"] != identity.user_turn_id
                or job["status"] not in {"running", "detached"}):
            raise task_events.TaskConflict("agent_execution_identity_conflict")

    def _read_intent(self, conn, calls):
        observations = []
        for call in calls:
            row = conn.execute(
                "SELECT * FROM task_operations WHERE job_id=? AND owner_user_id=? AND tool_call_id=? "
                "AND logical_key <> '' AND host_observation <> ''",
                (self.identity.run_id, self.identity.owner_user_id, call["id"])).fetchone()
            if not row or row["tool_name"] != call["name"] or row["status"] in {"running", "interrupted", "unknown"}:
                raise RecoveryRequiresReconciliation()
            try:
                expected = task_events.input_hash({"tool": call["name"], "args": call["args"],
                                                   "refs": json.loads(row["input_refs"])})
                item = _decode(row["host_observation"], [call])
                metadata = item.messages[0].additional_kwargs
                publication = metadata["host_publication"]
                raw = json.loads(row["result"])["content"]
                reference = metadata["result_reference"]
                if (expected != row["input_hash"] or metadata["operation_id"] != row["operation_id"]
                        or publication["logical_key"] != row["logical_key"]
                        or publication["attempt"] != row["dispatch_attempt"] or item.unknown_operations
                        or reference["full_sha256"] != hashlib.sha256(raw.encode("utf-8")).hexdigest()):
                    raise RecoveryRequiresReconciliation()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise RecoveryRequiresReconciliation() from None
            observations.append(item)
        return BatchObservation(tuple(item.messages[0] for item in observations),
            pending_question=any(item.pending_question for item in observations),
            execution_waits=tuple(dict.fromkeys(wait for item in observations for wait in item.execution_waits)))

    async def execute(self, response: AIMessage,
                      execute: Callable[[AIMessage, ExecutionIdentity], Awaitable[BatchObservation]],
                      *, validate_recovered: Callable[[ToolMessage], object] | None = None,
                      ) -> BatchObservation:
        calls = _calls(response)
        identity = self.identity
        digest = hashlib.sha256(_json({
            "owner_user_id": identity.owner_user_id, "session_id": identity.session_id,
            "request_id": identity.user_turn_id, "calls": calls,
        }).encode("utf-8")).hexdigest()
        recovered = None
        with task_events.transaction() as conn:
            self._authorize(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_batch_journal (
                run_id TEXT NOT NULL, batch_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('intent','complete')),
                observation TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, completed_at REAL,
                PRIMARY KEY(run_id,batch_digest)
            )""")
            claimed = conn.execute(
                "INSERT INTO agent_batch_journal(run_id,batch_digest,status,created_at) "
                "VALUES (?,?,'intent',?) ON CONFLICT(run_id,batch_digest) DO NOTHING",
                (identity.run_id, digest, time.time())).rowcount
            if not claimed:
                row = conn.execute("SELECT status,observation FROM agent_batch_journal "
                                   "WHERE run_id=? AND batch_digest=?",
                                   (identity.run_id, digest)).fetchone()
                if row["status"] == "complete":
                    return _decode(row["observation"], calls)
                if validate_recovered is None:
                    raise RecoveryRequiresReconciliation()
                recovered = self._read_intent(conn, calls)
        if recovered is None:
            batch = await execute(response, identity)
        else:
            for message in recovered.messages:
                try:
                    validate_recovered(message)
                except (ValueError, RuntimeError, sqlite3.Error):
                    raise RecoveryRequiresReconciliation() from None
            batch = recovered
        try:
            encoded = _encode(batch, calls)
            canonical_batch = _decode(encoded, calls)
        except _EncodingError as exc:
            reason = exc.args[0]
            _report_failure("encode", reason, len(calls))
            raise BatchObservationNotStored(effects_uncertain=(
                reason == "batch_observation_contract_invalid" or bool(batch.unknown_operations))) from None
        try:
            with task_events.transaction() as conn:
                self._authorize(conn)
                changed = conn.execute(
                    "UPDATE agent_batch_journal SET status='complete',observation=?,completed_at=? "
                    "WHERE run_id=? AND batch_digest=? AND status='intent'",
                    (encoded, time.time(), identity.run_id, digest)).rowcount
                if changed != 1:
                    raise RecoveryRequiresReconciliation()
        except sqlite3.Error:
            _report_failure("commit", "batch_observation_commit_unconfirmed", len(calls))
            raise BatchObservationNotStored(effects_uncertain=bool(batch.unknown_operations)) from None
        return canonical_batch
