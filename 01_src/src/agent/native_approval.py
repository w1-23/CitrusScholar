"""Bridge native per-call HITL to owner-scoped approval records.

This module does not persist decisions or invoke the graph. The host must read
its current authenticated checkpoint, hold the run fence through resume, and
supply a decision from a scoped durable DB lookup, never from a client payload.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command, StateSnapshot

from src.core.task_events import input_hash

from .identity import ExecutionIdentity


class InvalidNativeApproval(ValueError):
    """The approval cannot be safely bound or applied."""


def _identifier(value: object) -> bool:
    return (type(value) is str and 0 < len(value) <= 256 and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _tool_names(names: tuple[str, ...]) -> tuple[str, ...]:
    if (type(names) is not tuple or not names or not all(_identifier(name) for name in names)
            or len(set(names)) != len(names)):
        raise InvalidNativeApproval('invalid_approval_tool_policy')
    return names


def native_approval_middleware(tool_names: tuple[str, ...]) -> HumanInTheLoopMiddleware:
    """Configure only server-selected tools. This policy must be pinned on replay."""
    return HumanInTheLoopMiddleware(interrupt_mode='per_call', interrupt_on={
        name: {'allowed_decisions': ['approve', 'reject'], 'description': 'Approval required'}
        for name in _tool_names(tool_names)
    })


def _is_json(value: object, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_json(item, depth + 1) for key, item in value.items())
    return False


def _args_hash(args: object) -> str:
    if type(args) is not dict or not _is_json(args):
        raise InvalidNativeApproval('invalid_approval_arguments')
    try:
        return input_hash(args)
    except (ValueError, UnicodeError) as exc:
        raise InvalidNativeApproval('invalid_approval_arguments') from exc


@dataclass(frozen=True)
class ApprovalBinding:
    identity: ExecutionIdentity
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    interrupt_id: str
    tool_call_id: str
    tool_name: str
    args_hash: str

    @property
    def approval_id(self) -> str:
        return input_hash({'kind': 'citrus_native_approval_v1', **asdict(self)})[:32]

    def request_event(self) -> dict[str, str]:
        return {'approval_id': self.approval_id, 'job_id': self.identity.run_id,
                'tool_name': self.tool_name, 'status': 'pending'}


@dataclass(frozen=True)
class StoredApproval:
    """Immutable association loaded together with its current durable decision."""

    approval_id: str
    binding: ApprovalBinding
    status: str


def _checkpoint(snapshot: StateSnapshot, identity: ExecutionIdentity,
                scope: Mapping[str, str]) -> tuple[str, str, str, list]:
    config = snapshot.config.get('configurable', {})
    thread_id = config.get('thread_id')
    namespace = config.get('checkpoint_ns', '')
    checkpoint_id = config.get('checkpoint_id')
    state = snapshot.values
    messages = state.get('messages') if isinstance(state, dict) else None
    if (not _identifier(thread_id) or not _identifier(checkpoint_id)
            or (namespace != '' and not _identifier(namespace))
            or thread_id != scope.get('thread_id') or namespace != scope.get('checkpoint_ns', '')
            or not isinstance(messages, list) or not messages
            or state.get('active_run_id') != identity.run_id):
        raise InvalidNativeApproval('invalid_approval_checkpoint')
    return thread_id, namespace, checkpoint_id, messages


def pending_approvals(snapshot: StateSnapshot, identity: ExecutionIdentity, *,
                      scope: Mapping[str, str], tool_names: tuple[str, ...]) -> tuple[ApprovalBinding, ...]:
    """Validate all pending approval interrupts against the active trusted proposal.

    Nothing from the interrupt description or argument values leaves this helper.
    Return bindings in proposal order so a one-card UI can reveal them serially.
    """
    tools = _tool_names(tool_names)
    thread_id, namespace, checkpoint_id, messages = _checkpoint(snapshot, identity, scope)
    index = next((i for i in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[i], AIMessage)), -1)
    if index < 0:
        raise InvalidNativeApproval('approval_proposal_missing')
    proposal = messages[index]
    if (proposal.invalid_tool_calls or not proposal.tool_calls
            or any(type(proposal.additional_kwargs.get(key)) is not type(value)
                   or proposal.additional_kwargs.get(key) != value
                   for key, value in identity.message_metadata('assistant').items())):
        raise InvalidNativeApproval('untrusted_approval_proposal')
    calls = {call.get('id'): call for call in proposal.tool_calls if _identifier(call.get('id'))}
    if len(calls) != len(proposal.tool_calls):
        raise InvalidNativeApproval('invalid_approval_proposal_ids')
    paired = {msg.tool_call_id for msg in messages[index + 1:] if isinstance(msg, ToolMessage)}
    seen = set()
    bindings = {}
    for interrupt in snapshot.interrupts:
        value = interrupt.value
        if (not _identifier(interrupt.id) or interrupt.id in seen or type(value) is not dict
                or set(value) != {'type', 'tool_call_id', 'name', 'args', 'description'}
                or value['type'] != 'tool_approval'):
            raise InvalidNativeApproval('invalid_native_approval_interrupt')
        cid, name = value['tool_call_id'], value['name']
        if (not _identifier(cid) or cid in bindings or cid in paired or cid not in calls
                or not _identifier(name) or name not in tools or calls[cid].get('name') != name):
            raise InvalidNativeApproval('approval_proposal_call_mismatch')
        digest = _args_hash(value['args'])
        if digest != _args_hash(calls[cid].get('args')):
            raise InvalidNativeApproval('approval_argument_hash_mismatch')
        seen.add(interrupt.id)
        bindings[cid] = ApprovalBinding(identity, thread_id, namespace, checkpoint_id,
                                        interrupt.id, cid, name, digest)
    return tuple(bindings[cid] for cid in calls if cid in bindings)


def approval_resume(snapshot: StateSnapshot, identity: ExecutionIdentity, *,
                    scope: Mapping[str, str], tool_names: tuple[str, ...], records: tuple[StoredApproval, ...]) -> Command:
    """Build the server-only resume from exactly one decided record per interrupt.

    Never resume a partial batch. Re-read the latest checkpoint under the run
    fence: once consumed, stale bindings cannot authorize the next head.
    """
    expected = pending_approvals(snapshot, identity, scope=scope, tool_names=tool_names)
    if not expected or len(records) != len(expected):
        raise InvalidNativeApproval('approval_batch_not_ready')
    rows = {record.approval_id: record for record in records}
    if len(rows) != len(records):
        raise InvalidNativeApproval('duplicate_approval_record')
    resume = {}
    for binding in expected:
        row = rows.get(binding.approval_id)
        if row is None or row.binding != binding:
            raise InvalidNativeApproval('approval_binding_mismatch')
        if row.status == 'granted':
            decision = 'approve'
        elif row.status in ('rejected', 'expired', 'cancelled'):
            decision = 'reject'
        else:
            raise InvalidNativeApproval('approval_decision_not_final')
        resume[binding.interrupt_id] = {'type': decision}
    return Command(resume=resume)
