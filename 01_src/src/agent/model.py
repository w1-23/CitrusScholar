"""Admission and response checks around the official agent's model handler."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import math
import time

from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.tool import invalid_tool_call
from langchain_core.utils.function_calling import convert_to_openai_function, convert_to_openai_tool

from src.core.request_context import MainModelBudget, _main_model_budget

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


class _StreamEvidence(BaseCallbackHandler):
    """Observe SDK chunks without streaming or aggregating the answer ourselves."""

    run_inline = True

    def __init__(self):
        self.started = time.monotonic()
        self.first_packet = False
        self.terminal = None
        self.terminal_conflict = False
        self.cancelled = False
        self.tool_chunks = []

    def on_llm_error(self, error, **_):
        self.cancelled = isinstance(error, asyncio.CancelledError)

    def on_llm_new_token(self, _token, *, chunk=None, **_):
        message = getattr(chunk, "message", None)
        if not isinstance(message, AIMessageChunk):
            return
        if not self.first_packet:
            from src.core.public_delivery import duration_ms
            from src.core.request_context import _observation

            self.first_packet = True
            elapsed = duration_ms((time.monotonic() - self.started) * 1000)
            observation = _observation.get()
            if observation is not None:
                observation["provider_first_packet_ms"] = elapsed
        fragments = message.tool_call_chunks
        if self.terminal is not None and (message.content or fragments):
            self.terminal_conflict = True
        reason = message.response_metadata.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or (self.terminal is not None and reason != self.terminal):
                self.terminal_conflict = True
            else:
                self.terminal = reason
        if fragments:
            self.tool_chunks.append(AIMessageChunk(content="", tool_call_chunks=fragments))

    def check(self, response, *, allow_tool_stop, output_limit):
        metadata = dict(response.response_metadata)
        for key in ("citrus_stream_terminal_unconfirmed", "citrus_output_limit",
                    "citrus_output_limit_exhausted", "citrus_output_limit_status"):
            metadata.pop(key, None)
        if self.terminal_conflict or self.terminal is None:
            metadata.pop("finish_reason", None)
        else:
            metadata["finish_reason"] = self.terminal
        if output_limit is not None:
            metadata["citrus_output_limit"] = output_limit
        # Some compatible gateways use "length" for several conditions.
        # Attribute an output cap only when terminal metadata and usage agree.
        if metadata.get("finish_reason") in _LENGTH_TERMINALS:
            from src.core.cache_metrics import extract_usage

            usage = extract_usage(response) or {}
            used = usage.get("output_tokens")
            exhausted = output_limit is not None and type(used) is int and used >= output_limit
            metadata["citrus_output_limit_status"] = "exhausted" if exhausted else "unconfirmed"
            if exhausted:
                metadata["citrus_output_limit_exhausted"] = True
        update = {"response_metadata": metadata, "additional_kwargs": {
            key: value for key, value in response.additional_kwargs.items() if key != "reasoning_content"}}
        if self.tool_chunks:
            # The framework merges and parses calls. Its tolerant parser can
            # repair incomplete JSON, so independently attest the raw JSON.
            first, *rest = self.tool_chunks
            merged = first + rest if rest else first
            remaining = list(response.tool_calls)
            calls, invalid = [], []
            for fragment in merged.tool_call_chunks:
                name, raw, call_id = fragment.get("name"), fragment.get("args"), fragment.get("id")
                try:
                    args = json.loads(raw, object_pairs_hook=_unique_arguments, parse_constant=_reject_json_constant,
                                      parse_float=_finite_tool_float)
                    if not name or not isinstance(args, dict):
                        raise ValueError("invalid_tool_argument_object")
                    index = next(i for i, call in enumerate(remaining)
                                 if (call["name"], call.get("id"), call["args"]) == (name, call_id, args))
                    calls.append(remaining.pop(index))
                except (TypeError, ValueError, RecursionError, StopIteration):
                    invalid.append(invalid_tool_call(name=name, args=raw, id=call_id, error="invalid_tool_arguments"))
            if remaining and not invalid:
                invalid.append(invalid_tool_call(name=None, args=None, id=None, error="unverified_tool_arguments"))
            update.update(tool_calls=calls, invalid_tool_calls=invalid)
        elif response.tool_calls:
            metadata.pop("finish_reason", None)  # No raw evidence for a proposed tool batch.
        if update.get("invalid_tool_calls"):
            # Pydantic validation normalizes invalid-call members to the same
            # native shape that LangGraph reconstructs from its checkpoint.
            response = type(response).model_validate({**response.__dict__, **update})
        else:
            response = response.model_copy(update=update)
        if not stream_terminal_confirmed(response, allow_tool_stop=allow_tool_stop):
            response.response_metadata["citrus_stream_terminal_unconfirmed"] = True
        return response


@dataclass
class ModelDriver:
    catalog: CapabilityCatalog
    max_output_tokens: int | None = None
    allow_tool_stop: bool = False
    budget: MainModelBudget | None = None

    def _schemas_for(self, names):
        # Provider strict mode requires every object field. Clarification refs
        # are optional on the wire and still validated by the host at dispatch.
        return [{"type": "function", "function": convert_to_openai_function(schema["function"], strict=True)}
                if (schema["function"]["name"] != "request_user_input"
                    and self.catalog.execution_mode(schema["function"]["name"]) == "interaction")
                else schema for schema in self.catalog.schemas(names)]

    async def __call__(self, request, handler, identity: ExecutionIdentity, *, allowed, host_tools,
                       historical_readback=None, edit_history=True):
        token = _main_model_budget.set(self.budget)
        try:
            return await self._invoke(request, handler, identity, allowed=allowed, host_tools=host_tools,
                                      historical_readback=historical_readback, edit_history=edit_history)
        finally:
            _main_model_budget.reset(token)

    async def _invoke(self, request, handler, identity: ExecutionIdentity, *, allowed, host_tools,
                      historical_readback, edit_history):
        from src.core.provider_transport import async_deepseek_attempt
        from src.core.task_events import TaskConflict, run_model_attempt
        from .deep_agent_runtime import NATIVE_READ_TOOLS
        from .plan_tools import NATIVE_PLAN_TOOL

        if request.response_format is not None:
            raise RuntimeError("unexpected_structured_response_format")
        native_schemas, available = [], set()
        for tool in request.tools:
            name = tool["function"]["name"] if isinstance(tool, dict) else tool.name
            if name in host_tools:
                available.add(name)
            elif name in NATIVE_READ_TOOLS or name == NATIVE_PLAN_TOOL:
                native_schemas.append(convert_to_openai_tool(tool))
        allowed = set(allowed) & available
        model = request.model
        if self.budget is not None:
            limit = self.budget.max_output_tokens
        else:
            limit = int(getattr(model, "max_tokens", 0) or 4096)
            if self.max_output_tokens is not None:
                limit = min(limit, self.max_output_tokens)
        options = {**request.model_settings, "stream": True, "max_tokens": limit}
        options.pop("max_completion_tokens", None)
        if request.tool_choice is not None:
            raise RuntimeError("unexpected_main_model_tool_choice")
        schemas = [*self._schemas_for(allowed), *native_schemas]
        # The official handler owns tool binding. The estimate-only binding
        # carries the same options and is never invoked.
        estimate_model = model.bind(**(options | ({"tools": schemas} if schemas else {})))
        view, offered = list(request.messages), ()
        system = [request.system_message] if request.system_message else []
        if historical_readback is not None and "read_tool_result" in available:
            from .deep_agent_runtime import prepare_context_for_send

            read_schemas = [*self._schemas_for(allowed | {"read_tool_result"}), *native_schemas]
            if any(schema["function"]["name"] == "read_tool_result" for schema in read_schemas):
                candidate = model.bind(**(options | {"tools": read_schemas}))
                edited, offered = await prepare_context_for_send(model, candidate, [*system, *view], identity,
                    historical_readback.historical_result_reference, edit=edit_history)
                if offered:
                    view, estimate_model, schemas = edited[len(system):], candidate, read_schemas

        model_response = None

        async def invoke():
            nonlocal model_response
            if offered:
                historical_readback.historical_result_offers.update(offered)
            from src.core.request_context import _observation
            scope = _observation.get()
            evidence = _StreamEvidence()
            observed_model = model.model_copy(update={"cache": False, "callbacks": AsyncCallbackManager.configure(
                inheritable_callbacks=model.callbacks, local_callbacks=[evidence])})
            try:
                async with async_deepseek_attempt(observed_model, [*system, *view], options) as private:
                    model_response = await handler(request.override(model=observed_model, messages=view, tools=schemas,
                                                                     model_settings=options))
            except AttributeError as error:
                # Core 1.8.0 can mask an internally cancelled provider call
                # while creating its end callback. Trust the prior error event.
                if evidence.cancelled:
                    raise asyncio.CancelledError() from error
                raise
            if len(model_response.result) != 1 or not isinstance(model_response.result[0], AIMessage):
                raise TypeError("model_must_return_ai_message")
            if private is not None and private.terminal_conflict:
                evidence.terminal_conflict = True
            checked = evidence.check(model_response.result[0], allow_tool_stop=self.allow_tool_stop,
                                     output_limit=scope.get("actual_output_tokens") if scope else None)
            if private is not None and stream_terminal_confirmed(checked, allow_tool_stop=self.allow_tool_stop):
                return private.protect(checked)
            return checked

        try:
            response = await run_model_attempt(estimate_model, [*system, *view], invoke,
                purpose="main_loop", usage_session_id=identity.session_id, streaming=True)
        except TaskConflict as error:
            reason = str(error)
            if (reason == "context_budget_exhausted:tool_observations" or
                    reason == "context_budget_exhausted" and historical_readback is not None):
                raise AdmissionStopped(reason) from None
            raise
        return replace(model_response, result=[response])
