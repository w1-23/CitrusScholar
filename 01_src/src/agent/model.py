"""Connect the general loop to existing provider transport and attempt metering."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, message_chunk_to_message
from langchain_core.messages.tool import invalid_tool_call, tool_call

from .capabilities import CapabilityCatalog
from .identity import ExecutionIdentity
from .runtime import AdmissionStopped

_TEXT_TERMINALS = frozenset({"stop", "end_turn", "stop_sequence"})
_TOOL_TERMINALS = frozenset({"tool_calls", "tool_use", "function_call"})
_LENGTH_TERMINALS = frozenset({"length", "max_tokens", "max_output_tokens"})
NON_SUCCESS_TERMINALS = frozenset({"content_filter", "error", "unknown", "aborted",
                                  "refusal", "pause_turn", "incomplete"})


def stream_terminal_confirmed(response: AIMessage, *, allow_tool_stop: bool = False) -> bool:
    metadata = response.response_metadata
    if metadata.get("citrus_stream_terminal_unconfirmed"):
        return False
    reason = metadata.get("finish_reason")
    if not isinstance(reason, str):
        return False
    if reason in _LENGTH_TERMINALS or reason in NON_SUCCESS_TERMINALS:
        return True
    if response.tool_calls or response.invalid_tool_calls:
        return reason in _TOOL_TERMINALS or (allow_tool_stop and reason in _TEXT_TERMINALS)
    return reason in _TEXT_TERMINALS


def _unique_arguments(pairs):
    args = dict(pairs)
    if len(args) != len(pairs):
        raise ValueError("duplicate_tool_argument")
    return args


def _reject_json_constant(_value):
    raise ValueError("nonfinite_tool_argument")


def _finite_tool_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_tool_argument")
    return number


def _complete_stream_message(chunk):
    response = message_chunk_to_message(chunk)
    fragments = getattr(chunk, "tool_call_chunks", ())
    if not fragments:
        return response
    calls, invalid = [], []
    for fragment in fragments:
        name, raw, call_id = fragment.get("name"), fragment.get("args"), fragment.get("id")
        try:
            args = json.loads(raw, object_pairs_hook=_unique_arguments, parse_constant=_reject_json_constant,
                              parse_float=_finite_tool_float)
            if not name or not isinstance(args, dict):
                raise ValueError("invalid_tool_argument_object")
        except (TypeError, ValueError, RecursionError):
            invalid.append(invalid_tool_call(name=name, args=raw, id=call_id, error="invalid_tool_arguments"))
        else:
            calls.append(tool_call(name=name, args=args, id=call_id))
    return response.model_copy(update={"tool_calls": calls, "invalid_tool_calls": invalid})


@dataclass
class ModelDriver:
    model: object
    catalog: CapabilityCatalog
    allowed: Callable[[ExecutionIdentity], set[str]]
    on_text: Callable[[str], None] | None = None
    max_output_tokens: int | None = None
    output_tokens: int | None = None
    allow_tool_stop: bool = False

    def schemas_for(self, identity: ExecutionIdentity) -> list[dict]:
        # Resolving implementations is the executor's job. Tool discovery cannot
        # import an unavailable scientific plugin before the model can act.
        schemas = self.catalog.schemas(self.allowed(identity))
        from langchain_core.utils.function_calling import convert_to_openai_function
        # convert_to_openai_tool returns already-wrapped function tools unchanged,
        # ignoring strict. Apply the conversion to the function definition itself.
        return [{"type": "function", "function": convert_to_openai_function(schema["function"], strict=True)}
                   if self.catalog.execution_mode(schema["function"]["name"]) == "interaction"
                   else schema for schema in schemas]

    async def __call__(self, messages: tuple[BaseMessage, ...],
                       identity: ExecutionIdentity, *, native_schemas=(), on_delta=None) -> AIMessage:
        schemas = [*self.schemas_for(identity), *native_schemas]
        model = self.model
        limit = int(self.output_tokens or getattr(model, "max_tokens", 0) or 4096)
        if self.max_output_tokens is not None:
            limit = min(limit, self.max_output_tokens)
            model = model.model_copy(update={"max_tokens": limit})
        bound = model.bind_tools(schemas) if schemas else model
        from src.core.stream_llm import stream_llm_response
        from src.core.task_events import TaskConflict, run_model_attempt

        async def invoke():
            chunk = await stream_llm_response(bound, list(messages), on_text=on_delta, verify_terminal=True)
            return _complete_stream_message(chunk)

        try:
            response = await run_model_attempt(
                bound, list(messages), invoke,
                purpose="main_loop", usage_session_id=identity.session_id, streaming=True)
        except TaskConflict as error:
            if str(error) == "context_budget_exhausted:tool_observations":
                raise AdmissionStopped(str(error)) from None
            raise
        confirmed = stream_terminal_confirmed(response, allow_tool_stop=self.allow_tool_stop)
        if not confirmed:
            response.response_metadata["citrus_stream_terminal_unconfirmed"] = True
        elif (self.on_text is not None and response.response_metadata.get("finish_reason") in _TEXT_TERMINALS
              and not response.tool_calls and not response.invalid_tool_calls and isinstance(response.content, str)
              and response.content):
            try:
                self.on_text(response.content)
            except Exception:
                pass
        if self.max_output_tokens is not None:
            truncated = confirmed and response.response_metadata.get("finish_reason") in _LENGTH_TERMINALS
            # The next request remains inside the same ledger and context admission.
            # Persist this allowance with host state so a resume cannot reset it.
            self.output_tokens = min(limit * 2, self.max_output_tokens) if truncated else limit
            response.response_metadata.update(citrus_output_limit=limit,
                citrus_output_limit_exhausted=limit >= self.max_output_tokens)
        return response
