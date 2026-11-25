"""Business boundary contracts shared by the framework and the host."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage


@dataclass(frozen=True)
class BatchObservation:
    """Executor-owned observations, persisted before returning to the loop."""
    messages: tuple[ToolMessage, ...]
    pending_question: bool = False
    unknown_operations: tuple[str, ...] = ()
    # Host-supplied method/update observations, never interpreted as a user turn.
    context_notes: tuple[str, ...] = ()
    # Host-issued durable wait IDs, never unfinished native tool calls. Every
    # issuing call already has its paired observation before suspension.
    execution_waits: tuple[str, ...] = ()

    def __post_init__(self):
        validate_execution_waits(self.execution_waits)


def validate_execution_waits(values):
    """A bounded, inert checkpoint reference list; authorization stays in host."""
    import re
    if (type(values) is not tuple or len(values) > 256
            or any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{32}", value) for value in values)
            or len(set(values)) != len(values)):
        raise ValueError("invalid_execution_wait_references")
    return values


@dataclass(frozen=True)
class FinalCheck:
    """Fact checking, not a list of mandatory tool names or business steps."""
    accepted: bool = True
    observations: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.accepted and not self.observations:
            raise ValueError("rejected_final_requires_observation")


@dataclass(frozen=True)
class RunLimits:
    max_model_steps: int = 64
    deadline: float = float("inf")  # monotonic seconds, independent of provider usage

    def __post_init__(self):
        if type(self.max_model_steps) is not int or self.max_model_steps < 1:
            raise ValueError("invalid_model_step_limit")
        if self.deadline != self.deadline:
            raise ValueError("invalid_deadline")


@dataclass(frozen=True)
class RunResult:
    status: Literal["complete", "incomplete", "waiting", "suspended", "cancelled"]
    reason: str
    messages: tuple[BaseMessage, ...]
    model_steps: int
    answer: AIMessage | None = None
    unknown_operations: tuple[str, ...] = ()
    # These are provider IDs needing reconciliation, NOT invented operation IDs.
    pending_tool_calls: tuple[str, ...] = ()
    execution_waits: tuple[str, ...] = ()


class AdmissionStopped(Exception):
    """A metered context/resource boundary stopped admission before dispatch."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _pending_calls(messages: Sequence[BaseMessage]) -> tuple[str, ...]:
    """Only inspect the protocol tail; an older display record is not recovery state."""
    results = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            results.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            return tuple(c["id"] for c in message.tool_calls
                         if c.get("id") and c["id"] not in results)
        elif not isinstance(message, SystemMessage):
            break
    return ()
