"""Stream user-facing answer text and aggregate a complete model response.

替换用户可见回答生成点的 ainvoke（expert/light 的 supervisor 主循环与
统一收尾 _force_final_answer）：
  - astream 逐 chunk 聚合：AIMessageChunk 合并 → 完整 AIMessage
    （tool_calls / usage_metadata 完整，cache_metrics 计量路径不变）
  - on_text: optional internal observation of tentative content. It does not
    authorize or publish a visible answer.
  - Private provider protocol state survives live aggregation in the adapter's
    excluded field. It is never streamed or persisted in conversation history.
  - Failures propagate to the caller for retry. Draft bytes are not published here.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


async def stream_llm_response(
    llm,
    messages: list,
    on_text: Optional[Callable[[str], None]] = None,
    on_reasoning: Optional[Callable[[str], None]] = None,
    *,
    verify_terminal: bool = False,
    on_provider_first_packet: Optional[Callable[[int], None]] = None,
):
    """流式调用 LLM 并聚合完整响应，返回 AIMessage（与 ainvoke 同构）。

    Args:
        llm: 支持 astream 的 langchain chat model（CitrusChatOpenAI）
        messages: langchain 消息列表
        on_text: tentative content observer, not a public-delivery callback
        on_reasoning: deprecated compatibility argument; never invoked
        verify_terminal: track finish reasons on physical chunks before merging
        on_provider_first_packet: elapsed ms to the first observed stream packet,
            even if it contains only role, usage, tool or private reasoning metadata
    """
    from src.core.public_delivery import duration_ms
    from src.core.request_context import _observation
    full = None
    first_packet_seen = False
    terminal = None
    terminal_conflict = False
    started = time.monotonic()
    async for chunk in llm.astream(messages):
        if not first_packet_seen:
            first_packet_seen = True
            elapsed = duration_ms((time.monotonic() - started) * 1000)
            observation = _observation.get()
            if observation is not None:
                observation["provider_first_packet_ms"] = elapsed
            if on_provider_first_packet is not None:
                try:
                    on_provider_first_packet(elapsed)
                except Exception:
                    pass
        if verify_terminal:
            reason = chunk.response_metadata.get("finish_reason")
            private = getattr(chunk, "provider_protocol", None)
            if terminal is not None and (chunk.content or getattr(chunk, "tool_call_chunks", ())
                                         or getattr(private, "reasoning", "")):
                terminal_conflict = True
            if reason is not None:
                if not isinstance(reason, str) or (terminal is not None and reason != terminal):
                    terminal_conflict = True
                else:
                    terminal = reason
        if full is None:
            full = chunk
        else:
            full = full + chunk

        # content 增量（str 形态；多模态 list 形态罕见，跳过展示）
        c = chunk.content
        if isinstance(c, str) and c and on_text is not None:
            try:
                on_text(c)
            except Exception:
                pass

    if full is None:
        raise RuntimeError("stream_llm_response: empty stream")
    # Unscoped vendor metadata is not protocol state. The scoped adapter keeps
    # its required state separately, excluded from public message serialization.
    try:
        full.additional_kwargs.pop("reasoning_content", None)
    except Exception:
        pass
    if verify_terminal:
        full.response_metadata.pop("citrus_stream_terminal_unconfirmed", None)
        if terminal_conflict or terminal is None:
            full.response_metadata.pop("finish_reason", None)
            full.response_metadata["citrus_stream_terminal_unconfirmed"] = True
        else:
            full.response_metadata["finish_reason"] = terminal
    return full
