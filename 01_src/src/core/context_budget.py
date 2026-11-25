"""Read-only history size estimates for the conversation context UI."""
from dataclasses import dataclass

from src.core.request_context import estimate_request


@dataclass(frozen=True)
class ContextBudgetConfig:
    max_tokens: int = 64000
    soft_threshold: float = 0.75
    hard_threshold: float = 1.0


class ContextBudget:
    def __init__(self, config: ContextBudgetConfig | None = None) -> None:
        self.config = config or ContextBudgetConfig()

    def estimate_tokens(self, messages: list) -> int:
        return estimate_request(messages)["estimated_input_tokens"]
