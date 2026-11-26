"""Explicit, metered ``general-purpose`` subagent over the same Workspace view.

The SDK's native ``task`` tool delegates to a compiled subagent graph. Every
model call inside that graph must still charge the same job budget, so the
subagent's model is a thin proxy: it forwards to the real chat model and wraps
the call in the existing ``run_model_attempt`` ledger. The subagent receives only
the native file tools over the parent's backend — never host publishing,
capability or fusion tools — so it cannot deliver artifacts, publish corpora or
widen its own permissions.
"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

GENERAL_PURPOSE_NAME = "general-purpose"
GENERAL_PURPOSE_DESCRIPTION = (
    "Delegate a self-contained, multi-step file/report task. Returns a short "
    "summary plus the workspace paths it used or created.")
GENERAL_PURPOSE_PROMPT = (
    "You are a focused sub-agent working inside an already-authorized workspace. "
    "Use the native file tools only. Read before editing, keep changes inside "
    "/workspace or /outputs, never touch /input, /skills or /01_src, and never "
    "publish or deliver files: the main agent owns publication. "
    "Finish with a concise summary of what you did, the exact paths and versions "
    "you created or changed, and anything you could not complete.")


class MeteredSubagentModel(BaseChatModel):
    """Forward to the real model while charging the shared job budget."""

    inner: Any = Field(exclude=True)
    identity: Any = Field(exclude=True)
    budget: Any = Field(default=None, exclude=True)
    tools: list = Field(default_factory=list)
    bound_kwargs: dict = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "citrus-metered-subagent"

    @property
    def _identifying_params(self) -> dict:
        return {"inner": str(getattr(self.inner, "model_name", "")), "tool_count": len(self.tools)}

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": list(tools),
                                       "bound_kwargs": {**self.bound_kwargs, **kwargs}})

    def _generate(self, *args, **kwargs):
        raise AssertionError("asynchronous subagent model required")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from src.core.request_context import _main_model_budget
        from src.core.task_events import run_model_attempt
        from src.core.tracing import get_job_id

        if get_job_id() != self.identity.run_id:
            raise RuntimeError("subagent_budget_scope_mismatch")
        bound = self.inner
        if self.tools:
            binder = getattr(bound, "bind_tools", None)
            if binder is None:
                raise RuntimeError("subagent_model_cannot_bind_tools")
            bound = binder(self.tools, **self.bound_kwargs)

        cap = min(self.budget.max_output_tokens, 2048) if self.budget is not None else None
        if cap is not None:
            bound = bound.bind(max_tokens=cap, stream=False)

        async def invoke():
            reply = await bound.ainvoke(list(messages), stop=stop)
            if (not isinstance(reply, AIMessage) or reply.invalid_tool_calls
                    or reply.response_metadata.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"}):
                raise ValueError("incomplete_subagent_response")
            return reply

        token = _main_model_budget.set(self.budget) if self.budget is not None else None
        try:
            reply = await run_model_attempt(bound, list(messages), invoke, purpose="subagent_loop",
                output_tokens=cap, usage_session_id=self.identity.session_id, streaming=False)
        finally:
            if token is not None:
                _main_model_budget.reset(token)
        return ChatResult(generations=[ChatGeneration(message=reply)])


def delegated_observation(result, call_id):
    updates = result.update
    if (result.graph is not None or result.goto or result.resume is not None
            or not isinstance(updates, dict) or set(updates) != {"messages"}
            or not isinstance(updates["messages"], list) or len(updates["messages"]) != 1):
        raise ValueError("unexpected_native_task_update")
    message = updates["messages"][0]
    if not isinstance(message, ToolMessage) or message.tool_call_id != call_id:
        raise ValueError("native_task_observation_mismatch")
    return message.model_copy(update={"artifact": None})


def general_purpose_subagent(*, model, identity, backend, permissions, file_tools, budget=None):
    """One restricted synchronous subagent sharing the job budget and workspace."""
    from deepagents.middleware.filesystem import FilesystemMiddleware

    # The SDK otherwise gives the subagent its own default filesystem
    # middleware over a state backend. Passing ours keeps a single file view.
    # No shell in a subagent: it works on files and reports back. Command
    # execution stays with the parent, which owns dispatch and receipts.
    allowed = tuple(name for name in file_tools if name != "execute")
    filesystem = FilesystemMiddleware(backend=backend, permissions=permissions,
                                      tools=list(allowed),
                                      human_message_token_limit_before_evict=None,
                                      tool_token_limit_before_evict=20000)
    return {
        "name": GENERAL_PURPOSE_NAME,
        "description": GENERAL_PURPOSE_DESCRIPTION,
        "system_prompt": GENERAL_PURPOSE_PROMPT,
        "model": MeteredSubagentModel(inner=model, identity=identity, budget=budget, cache=False),
        # No host capability tools: a subagent cannot publish or escalate.
        "tools": [],
        "middleware": [filesystem],
    }
