"""LLM 客户端进程级复用 (v8.4).

此前每请求新建 2~N 个 ChatOpenAI 实例（supervisor/子Agent/压缩/hint 各建），
每个实例独立 httpx 连接池，初始化与连接开销重复。现按
(model, api_key, base_url, temperature, timeout, max_tokens, thinking_off) 缓存复用；
bind_tools 仍由调用方按需包装（轻量本地操作，不属于客户端创建开销）。

注意: 前端设置面板切换模型后，get_deepseek_model() 返回新值（RESOLVED_MAIN_MODEL），
自然产生新缓存键，旧实例保留至进程退出（成本可忽略）。
"""
from __future__ import annotations

import json
import logging
import threading

from langchain_openai import ChatOpenAI

from src.config import settings
from src.core.provider_protocol import thinking_rejected, uses_deepseek_protocol

logger = logging.getLogger(__name__)


_cache: dict = {}
_lock = threading.Lock()


def is_thinking_rejected(exc: Exception) -> bool:
    """Only an explicitly unsupported thinking option permits parameter fallback.

    Protocol-state failures (notably missing reasoning_content), invalid tool
    messages, unavailable models and ordinary HTTP errors must reach the caller.
    """
    return thinking_rejected(getattr(exc, "status_code", None), getattr(exc, "body", None), str(exc))


class _ThinkingOffWrapper:
    """v8.17.19: thinking_off 客户端级 fail-soft 包装。

    primary = 带关闭字段（extra_body）的实例；fallback = 无字段实例。
    网关拒绝关闭字段（400/422）时自动用 fallback 重试一次并记日志——
    保证该调用点不因参数被拒而挂（仅多一次快速失败请求）；真机日志据此
    判断字段是否被接受（连续出现"去参回退重试"警告 = 字段无效，需换
    config model.reasoning_off_body 或模型档）。

    只转发实际使用的接口（ainvoke / astream / bind_tools / model_kwargs 只读）。
    """

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    @property
    def model_kwargs(self) -> dict:
        return self.primary.model_kwargs

    @property
    def extra_body(self) -> dict | None:
        return self.primary.extra_body

    def bind_tools(self, tools, **kw):
        return _ThinkingOffWrapper(
            self.primary.bind_tools(tools, **kw),
            self.fallback.bind_tools(tools, **kw))

    def _begin_fallback(self, messages, error):
        from src.core import task_events
        from src.core.request_context import _observation
        scope = _observation.get()
        if scope is not None:
            scope.update(redirected=True, transport_error=type(error).__name__)
        task_events.finish_attempt(task_events._active_model_attempt.get(), error=type(error).__name__,
            observation=task_events._attempt_response(scope, None, type(error).__name__) if scope else None)

    async def ainvoke(self, messages, **kw):
        try:
            return await self.primary.ainvoke(messages, **kw)
        except Exception as e:
            if is_thinking_rejected(e):
                logger.warning(
                    "[LLMPool] unsupported thinking option; retrying without it (%s)", type(e).__name__)
                from src.core.task_events import run_model_attempt
                self._begin_fallback(messages, e)
                return await run_model_attempt(self.fallback, messages,
                    lambda: self.fallback.ainvoke(messages, **kw),
                    purpose="thinking_parameter_fallback", output_tokens=kw.get("max_tokens"))
            raise

    async def astream(self, messages, **kw):
        yielded = False
        try:
            async for c in self.primary.astream(messages, **kw):
                yielded = True
                yield c
        except Exception as e:
            # A new physical response must never extend a partial tool proposal.
            if not yielded and is_thinking_rejected(e):
                logger.warning(
                    "[LLMPool] unsupported thinking option; retrying without it (%s)", type(e).__name__)
                from src.core.task_events import run_model_attempt
                self._begin_fallback(messages, e)
                async def collect():
                    stream = self.fallback.astream(messages, **kw)
                    aggregate = None
                    try:
                        async for chunk in stream:
                            aggregate = chunk if aggregate is None else aggregate + chunk
                    finally:
                        await stream.aclose()
                    return aggregate
                aggregate = await run_model_attempt(self.fallback, messages, collect,
                    purpose="thinking_parameter_fallback", output_tokens=kw.get("max_tokens"), streaming=True)
                if aggregate is not None:
                    yield aggregate
            else:
                raise


def _new_client(model, api_key, base_url, temperature, timeout, max_tokens,
                extra_body: dict | None, default_headers: dict | None = None):
    from openai import DefaultAsyncHttpxClient, DefaultHttpxClient
    from src.core.request_context import observe_async_provider_request, observe_provider_request
    if uses_deepseek_protocol(model, base_url):
        import httpx
        from src.core.provider_transport import AsyncDeepSeekTransport, DeepSeekTransport

        sync_client = httpx.Client(transport=DeepSeekTransport(base_url), follow_redirects=False)
        async_client = httpx.AsyncClient(transport=AsyncDeepSeekTransport(base_url), follow_redirects=False)
    else:
        sync_client = DefaultHttpxClient(follow_redirects=False,
            event_hooks={"request": [observe_provider_request]})
        async_client = DefaultAsyncHttpxClient(follow_redirects=False,
            event_hooks={"request": [observe_async_provider_request]})
    kw = {"http_client": sync_client, "http_async_client": async_client, "use_responses_api": False}
    if extra_body:
        # v8.17.19: 关闭字段必须经 extra_body 原样透传网关——langchain-openai
        # 把 model_kwargs 里的 extra_body 提取为显式构造参数（UserWarning 实证：
        # "should be specified explicitly"），故直接传构造参数；SDK 将其并入
        # 请求 body 由网关裁决。v8.17.18 曾放 model_kwargs 顶层 thinking，
        # langchain 展开为 create() 关键字参数 → TypeError（retrieve-agent 全挂根因）。
        kw["extra_body"] = dict(extra_body)
    if default_headers:
        kw["default_headers"] = dict(default_headers)
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=0,  # Retry attempts are owned and charged by the coordinator.
        **kw,
    )


def get_llm(
    model: str,
    api_key: str,
    base_url: str,
    *,
    temperature: float = 0.0,
    timeout: float = 300,
    max_tokens: int | None = None,
    thinking_off: bool = False,
    provider_headers: dict[str, str] | None = None,
):
    """进程级客户端复用；v8.17.19: thinking_off 恢复有效逻辑。

    thinking_off=True 时经 extra_body 下发关闭字段（形态见
    settings.MODEL_REASONING_OFF_BODY，默认官方推荐 {"thinking":
    {"type": "disabled"}}），返回 _ThinkingOffWrapper（fail-soft 自动去参重试）；
    thinking_off=False/不传时不发送任何思维链字段（默认保持思维链开启——
    supervisor 融合判断路径即此形态）。
    """
    extra_body = None
    if thinking_off:
        cfg = getattr(settings, "MODEL_REASONING_OFF_BODY", None) or {}
        if isinstance(cfg, dict) and cfg:
            extra_body = dict(cfg)
    if provider_headers is None:
        # Legacy callers without a snapshot must not pass gateway headers to
        # an unrelated origin, even during a concurrent provider switch.
        from src.config import model_origin
        current = settings.model_transport()
        provider_headers = current.headers if model_origin(base_url) == model_origin(current.base_url) else {}
    header_key = tuple(sorted(provider_headers.items()))
    body_key = json.dumps(extra_body, sort_keys=True, separators=(",", ":")) if extra_body else None
    key = (model, api_key, base_url, temperature, timeout, max_tokens,
           body_key, header_key)
    with _lock:
        inst = _cache.get(key)
        if inst is None:
            inst = _new_client(model, api_key, base_url, temperature, timeout,
                               max_tokens, extra_body, provider_headers)
            _cache[key] = inst
        if extra_body:
            fkey = (model, api_key, base_url, temperature, timeout, max_tokens,
                    None, header_key)
            fallback = _cache.get(fkey)
            if fallback is None:
                fallback = _new_client(model, api_key, base_url, temperature,
                                       timeout, max_tokens, None, provider_headers)
                _cache[fkey] = fallback
            return _ThinkingOffWrapper(inst, fallback)
        return inst
