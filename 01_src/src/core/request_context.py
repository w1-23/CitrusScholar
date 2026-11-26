"""One request estimate for admission, reservations and display.

Counts the converted request once. Estimates are never provider usage. Image costs
are conservative configurable placeholders, not a claim about any vendor tokenizer.
"""
from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass

_observation = contextvars.ContextVar("model_request_observation", default=None)
_epoch = contextvars.ContextVar("model_context_epoch", default=None)
_logical_call = contextvars.ContextVar("model_logical_call", default="")
_main_model_budget = contextvars.ContextVar("main_model_budget", default=None)


@dataclass(frozen=True)
class MainModelBudget:
    max_output_tokens: int
    model_window: int | None
    output_limit_source: str
    active_input_cap: int
    application_window: int
    safety_margin: int

    def input_limit(self, output_tokens=None) -> int:
        window = min(self.application_window, self.model_window or self.application_window)
        reserve = self.max_output_tokens if output_tokens is None else output_tokens
        return max(0, min(self.active_input_cap, window - reserve - self.safety_margin))


def current_main_model_budget() -> MainModelBudget | None:
    return _main_model_budget.get()


def resolve_main_model_budget(transport, settings) -> MainModelBudget:
    """Resolve the selected provider/model's caps once for a main turn."""
    models = settings.MAIN_MODEL_BUDGETS.get(transport.provider_id, {})
    capability = models.get(transport.model, {})
    provider_output = capability.get("max_output_tokens")
    model_window = capability.get("context_window_tokens")
    if type(provider_output) is not int or provider_output < 1:
        provider_output = None
    if type(model_window) is not int or model_window < 1:
        model_window = None
    application_window = int(transport.configured_context_window or settings.RESOLVED_CONTEXT_BUDGET_MAX_TOKENS)
    safety_margin = int(settings.CONTEXT_SAFETY_MARGIN_TOKENS)
    configured = provider_output or int(settings.MAX_TOKENS)
    ceiling = int(settings.AGENT_MAX_OUTPUT_TOKENS)
    context_output = min(application_window, model_window or application_window) - safety_margin - 1
    output = max(1, min(configured, ceiling, context_output))
    source = ("context_window" if context_output < min(configured, ceiling)
              else "agent_ceiling" if ceiling < configured
              else "provider_model_configuration" if provider_output else "legacy_configuration")
    return MainModelBudget(output, model_window, source, int(settings.CONTEXT_ACTIVE_MAX_INPUT_TOKENS),
                           application_window, safety_margin)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def text_tokens(text):
    # Shared character heuristic; no tool arguments are counted a second time.
    text = str(text or "")
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return max(1, int(cjk * 1.2 + (len(text) - cjk) / 4))


def message_dict(message):
    if isinstance(message, dict):
        return message
    try:
        from langchain_core.messages import convert_to_openai_messages
        text_format = "block" if isinstance(getattr(message, "content", None), list) else "string"
        return convert_to_openai_messages(message, text_format=text_format, pass_through_unknown_blocks=True)
    except (TypeError, AttributeError, ValueError):
        data = {"role": str(getattr(message, "type", "unknown")),
                "content": getattr(message, "content", "")}
        calls = getattr(message, "tool_calls", None)
        if isinstance(calls, list) and calls:
            data["tool_calls"] = calls
        return data


def estimate_request(messages, tools=None, output_tokens=0, model_window=None):
    """Accept converted payload or messages; always include schemas and images."""
    payload = messages if isinstance(messages, dict) else {
        "messages": [message_dict(m) for m in messages], "tools": tools or []}
    parts = {"system": 0, "messages": 0, "tool_arguments": 0,
             "tool_schemas": 0, "images": 0, "protocol": 0}
    image_count = 0
    for msg in payload.get("messages", []):
        content = msg.get("content", "")
        text = ""
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    text += str(block)
                elif block.get("type") in {"image_url", "input_image", "image"}:
                    image_count += 1
                    # Unknown resolution/model: reserve high-detail allowance.
                    parts["images"] += 4096
                else:
                    text += _json(block)
        else:
            text = str(content or "")
        key = "system" if msg.get("role") in {"system", "developer"} else "messages"
        parts[key] += text_tokens(text)
        calls = msg.get("tool_calls") or msg.get("function_call")
        if calls:
            parts["tool_arguments"] += text_tokens(_json(calls))
        if msg.get("reasoning_content"):
            parts["messages"] += text_tokens(msg["reasoning_content"])
        parts["protocol"] += 12 + text_tokens(msg.get("name", "")) + text_tokens(msg.get("tool_call_id", ""))
    definitions = payload.get("tools") or payload.get("functions") or []
    if definitions:
        parts["tool_schemas"] = text_tokens(_json(definitions))
    for option in ("response_format", "tool_choice"):
        if payload.get(option):
            parts["protocol"] += text_tokens(_json(payload[option]))
    output = max(0, int(output_tokens or payload.get("max_completion_tokens") or payload.get("max_tokens") or 0))
    total = sum(parts.values())
    return {"estimated_input_tokens": total, "output_tokens": output,
            "estimated_total_tokens": total + output, "components": parts,
            "estimate_scope": "full_request_estimate", "estimate_method": "character_heuristic",
            "image_count": image_count, "image_estimate_status": "conservative_unknown_model" if image_count else "none",
            "tool_message_count": sum(msg.get("role") in {"tool", "function"} for msg in payload.get("messages", [])),
            "model_window": model_window}


def model_request(model, messages, output_tokens=None):
    primary = getattr(model, "primary", model)
    base = getattr(primary, "bound", primary)
    kwargs = getattr(primary, "kwargs", {})
    kwargs = dict(kwargs) if isinstance(kwargs, dict) else {}
    output = output_tokens if output_tokens is not None else kwargs.get("max_tokens")
    if output is None:
        value = getattr(base, "max_tokens", None)
        output = value if isinstance(value, (int, float)) else 512
    output = max(1, int(output))
    # A public conversion is used for deterministic preflight. The HTTPX
    # hook checks the actual, fully serialized body before transport.
    rows = [message_dict(message) for message in messages]
    from langchain_openai import ChatOpenAI
    from src.core.provider_protocol import attach_current_tool_reasoning, current_tool_reasoning, model_deepseek_scope
    if isinstance(base, ChatOpenAI):
        # These Chat Completions fields do not enter the provider request.
        rows = [row.copy() for row in rows]
        for row in rows:
            if row.get("role") == "tool":
                row.pop("name", None)
            elif row.get("role") == "assistant" and (row.get("tool_calls") or row.get("function_call")):
                if not row.get("content"):
                    row["content"] = None
    payload = {**kwargs, "messages": rows, "max_tokens": output,
               "model": str(getattr(base, "model_name", ""))}
    replay = current_tool_reasoning(messages, model_deepseek_scope(model, kwargs))
    attach_current_tool_reasoning(rows, replay)
    budget = current_main_model_budget()
    return payload, estimate_request(payload, output_tokens=output,
                                     model_window=budget.model_window if budget is not None else None)


@dataclass(frozen=True)
class ContextEpoch:
    """Persisted timing and diagnostic labels, not a message or context view."""
    number: int = 0
    reason: str = "initial"
    history_load_status: str = "unknown"
    history_load_reason: str = ""


def observe_provider_request(request):
    """Public HTTPX request hook, after SDK serialization and before transport."""
    scope = _observation.get()
    if scope is None or request.method != "POST":
        return
    from src.core.task_events import TaskConflict, adjust_model_reservation, model_input_limit
    from src.core.turn_context import capture_request
    from src.agent.runtime import AdmissionStopped

    def blocked(reason):
        if not scope.get("provider_request_admitted"):
            scope["pre_transport_denial"] = reason
        if scope["purpose"] == "main_loop":
            raise AdmissionStopped(reason)
        raise TaskConflict(reason)

    if not request.url.path.endswith("/chat/completions"):
        blocked("unexpected_provider_request_endpoint")
    if scope.get("provider_request_admitted"):
        blocked("duplicate_provider_request")
    try:
        payload = json.loads(request.content)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise ValueError("invalid chat request body")
        prepared = estimate_request(payload, model_window=scope["estimate"].get("model_window"))
    except (TypeError, ValueError):
        blocked("model_request_body_unavailable")
    if (scope["purpose"] == "main_loop"
            and prepared["output_tokens"] != scope["estimate"]["output_tokens"]):
        blocked("model_output_budget_mismatch")
    if prepared["estimated_input_tokens"] > model_input_limit(prepared["output_tokens"]):
        blocked("context_budget_exhausted")
    if prepared["estimated_total_tokens"] > scope["estimate"]["estimated_total_tokens"]:
        try:
            adjust_model_reservation(scope.get("reservation_id"), prepared["estimated_total_tokens"])
        except TaskConflict as exc:
            blocked(str(exc))
    scope["snapshot"] = capture_request(payload, model=str(payload.get("model", "")),
        purpose=scope["purpose"], attempt_id=scope["attempt_id"],
        epoch=scope.get("epoch", 0), logical_call_id=scope.get("logical_call_id", ""),
        change_reason=scope.get("change_reason", ""))
    changed = prepared != scope.get("estimate")
    scope["estimate"] = prepared
    scope["model"] = str(payload.get("model", scope.get("model", "")))
    if changed and scope.get("publish_context"):
        scope["publish_context"]()
    scope["actual_output_tokens"] = prepared["output_tokens"] or None
    scope["provider_request_admitted"] = True


async def observe_async_provider_request(request):
    # The OpenAI SDK sends an already-built JSON body; no request is made here.
    observe_provider_request(request)
