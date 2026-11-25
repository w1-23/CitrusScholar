"""Business observation for one framework-owned capability invocation."""
from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import ToolMessage


@dataclass(frozen=True)
class ToolExecution:
    message: ToolMessage
    operation_id: str = ""
    uncertain: bool = False
    pending_question: bool = False
    execution_waits: tuple[str, ...] = ()
    logical_key: str = ""
    attempt: int = 1

    def __post_init__(self):
        from .runtime import validate_execution_waits
        validate_execution_waits(self.execution_waits)
        if not isinstance(self.message, ToolMessage):
            raise TypeError("operation_requires_tool_message")
        if self.uncertain and not self.operation_id:
            raise ValueError("unknown_operation_requires_durable_id")
