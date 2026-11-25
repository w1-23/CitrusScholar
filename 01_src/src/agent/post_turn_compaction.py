"""Run stock LangChain summarization after a terminal turn, off the live graph.

The live checkpoint keeps its canonical messages. A replace-only working_context
channel records the SDK's compacted view and the last raw message it covers.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
import logging
from typing import Literal

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import StateSnapshot
from pydantic import Field

CompactionOutcome = Literal["compacted", "busy", "stale", "ineligible", "failed"]
_logger = logging.getLogger(__name__)
_summary_stops = {"stop", "end_turn", "stop_sequence"}


def select_working_messages(
    messages: Sequence[BaseMessage], working_context: object,
) -> list[BaseMessage]:
    """Read the established working branch plus new tape, or fall back to tape."""
    if isinstance(working_context, Mapping):
        anchor = working_context.get("through_message_id")
        compacted = working_context.get("messages")
        if (isinstance(anchor, str) and anchor and isinstance(compacted, list) and compacted
                and all(isinstance(message, BaseMessage) for message in compacted)
                and compacted[-1].id == anchor):
            matches = [index for index, message in enumerate(messages) if message.id == anchor]
            if len(matches) == 1:
                return [*compacted, *messages[matches[0] + 1:]]
    return list(messages)


class _SummaryRejected(Exception):
    pass


class _GuardedSummaryModel(BaseChatModel):
    """Validate the provider response before the SDK can turn it into summary text."""

    transport: BaseChatModel = Field(exclude=True, repr=False)
    accepted: bool = Field(default=False, exclude=True)

    @property
    def _llm_type(self):
        return "citrus-post-turn-summary"

    def with_retry(self, **kwargs):
        return super().with_retry(**{**kwargs, "stop_after_attempt": 1})

    def _generate(self, *args, **kwargs):
        raise _SummaryRejected("async_summary_required")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            reply = await self.transport.ainvoke(messages, stop=stop)
        except Exception:
            # Do not expose the provider's error text or allow an SDK overflow retry.
            raise _SummaryRejected("summary_provider_failed") from None
        if (not isinstance(reply, AIMessage) or not isinstance(reply.content, str)
                or not reply.content.strip() or reply.tool_calls or reply.invalid_tool_calls
                or reply.response_metadata.get("citrus_stream_terminal_unconfirmed")
                or reply.response_metadata.get("finish_reason") not in _summary_stops):
            raise _SummaryRejected("summary_response_invalid")
        self.accepted = True
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _checkpoint_id(snapshot: StateSnapshot) -> str | None:
    checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
    return checkpoint_id if isinstance(checkpoint_id, str) and checkpoint_id else None


def _eligible(snapshot: StateSnapshot, expected_run_id: str) -> bool:
    values = snapshot.values
    return (bool(_checkpoint_id(snapshot)) and not snapshot.next and not snapshot.tasks
            and not snapshot.interrupts and values.get("active_run_id") == expected_run_id
            and values.get("stop_status") == "complete"
            and values.get("batch_status") not in {"unknown", "waiting", "execution"}
            and not any(values.get(key) for key in
                        ("unknown_operations", "execution_waits", "execution_wake"))
            and not (values.get("host_state") or {}).get("pending_question"))


async def _compact_view(
    messages: list[BaseMessage], summary_model: BaseChatModel, trigger: int, keep: int,
) -> list[BaseMessage] | None:
    guarded = _GuardedSummaryModel(transport=summary_model, profile=summary_model.profile)
    middleware = SummarizationMiddleware(
        guarded, trigger=("tokens", trigger), keep=("tokens", keep), trim_tokens_to_summarize=None,
    )
    work = StateGraph(MessagesState)

    async def summarize(state: MessagesState, runtime: Runtime):
        return await middleware.abefore_model(state, runtime)

    work.add_node("summarize", summarize)
    work.add_edge(START, "summarize")
    work.add_edge("summarize", END)
    candidate = (await work.compile().ainvoke({"messages": deepcopy(messages)}))["messages"]
    # Stock middleware can sometimes produce fallback text without invoking its model.
    return candidate if guarded.accepted and candidate != messages else None


async def compact_post_turn(
    graph: CompiledStateGraph,
    config: RunnableConfig,
    *,
    summary_model: BaseChatModel,
    trigger_tokens: int,
    keep_tokens: int,
    session_lock: asyncio.Lock,
    is_busy: Callable[[], Awaitable[bool]],
    expected_run_id: str,
    as_node: str = "maintenance",
    execution_guard: Callable[[], AbstractContextManager] = nullcontext,
    check_committed: Callable[[StateSnapshot], Awaitable[bool]] | None = None,
) -> CompactionOutcome:
    """Compare and update a completed head only while both existing locks are held.

    A storage-only graph must supply ``check_committed`` to check pending
    writes of the actual live graph, which the storage-only graph cannot see.
    """
    # An explicit checkpoint ID would keep both reads on a stale ancestor.
    head_config: RunnableConfig = {**config, "configurable": {
        key: value for key, value in config.get("configurable", {}).items() if key != "checkpoint_id"
    }}
    if not expected_run_id or not 0 < keep_tokens < trigger_tokens:
        return "ineligible"
    try:
        if session_lock.locked():
            return "busy"
        async with session_lock:
            if await is_busy():
                return "busy"
            with execution_guard():
                snapshot = await graph.aget_state(head_config)
                if not _eligible(snapshot, expected_run_id) or (
                    check_committed is not None and not await check_committed(snapshot)
                ):
                    return "ineligible"
        tape = snapshot.values.get("messages")
        if (not isinstance(tape, list) or not tape or not isinstance(tape[-1].id, str)
                or not tape[-1].id):
            return "ineligible"
        anchor = tape[-1].id
        if sum(message.id == anchor for message in tape) != 1:
            return "ineligible"
        messages = select_working_messages(tape, snapshot.values.get("working_context"))
        candidate = await _compact_view(messages, summary_model, trigger_tokens, keep_tokens)
        if candidate is None:
            return "ineligible"
        if not candidate or candidate[-1].id != anchor:
            return "failed"

        if session_lock.locked():
            return "busy"
        async with session_lock:
            if await is_busy():
                return "busy"
            with execution_guard():
                latest = await graph.aget_state(head_config)
                if (_checkpoint_id(latest) != _checkpoint_id(snapshot)
                        or not _eligible(latest, expected_run_id)
                        or check_committed is not None and not await check_committed(latest)):
                    return "stale"
                await graph.aupdate_state(latest.config, {"working_context": {
                    "messages": candidate, "through_message_id": anchor,
                }}, as_node=as_node)
        return "compacted"
    except _SummaryRejected:
        return "failed"
    except RuntimeError as exc:
        if exc.args == ("agent_run_already_active",):
            return "busy"
        _logger.warning("post_turn_compaction_failed kind=%s", type(exc).__name__)
        return "failed"
    except Exception as exc:
        _logger.warning("post_turn_compaction_failed kind=%s", type(exc).__name__)
        return "failed"
