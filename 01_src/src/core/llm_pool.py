"""Reuse the official model clients and their HTTP connection pools per configuration."""
from __future__ import annotations

import threading

from langchain_openai import ChatOpenAI

from src.config import settings
from src.core.provider_protocol import uses_deepseek_protocol

_cache: dict = {}
_lock = threading.Lock()


def _new_client(model, api_key, base_url, temperature, timeout, max_tokens,
                default_headers: dict | None = None):
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
    if default_headers:
        kw["default_headers"] = dict(default_headers)
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=0,  # One metered attempt admits exactly one provider request.
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
    provider_headers: dict[str, str] | None = None,
):
    """Use the existing provider thinking behavior without a second client or retry."""
    if provider_headers is None:
        # Legacy callers without a snapshot must not pass gateway headers to
        # an unrelated origin, even during a concurrent provider switch.
        from src.config import model_origin
        current = settings.model_transport()
        provider_headers = current.headers if model_origin(base_url) == model_origin(current.base_url) else {}
    header_key = tuple(sorted(provider_headers.items()))
    key = (model, api_key, base_url, temperature, timeout, max_tokens, header_key)
    with _lock:
        inst = _cache.get(key)
        if inst is None:
            inst = _new_client(model, api_key, base_url, temperature, timeout,
                               max_tokens, provider_headers)
            _cache[key] = inst
        return inst
