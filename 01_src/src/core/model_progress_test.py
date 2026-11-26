"""Native first-packet evidence and transport admission facts."""
import asyncio
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from src.agent import model
from src.core import progress_bus
from src.core.request_context import _observation


def test_official_chunks_measure_first_packet_without_emitting_provider_drafts():
    scope = {"purpose": "main_loop", "attempt_id": "test-attempt"}
    saved = _observation.set(scope)
    try:
        with patch.object(model, "time") as clock, patch.object(progress_bus, "emit_progress") as emit:
            clock.monotonic.side_effect = [10, 11]
            evidence = model._StreamEvidence()
            assert "provider_first_packet_ms" not in scope
            evidence.on_llm_new_token("", chunk=ChatGenerationChunk(message=AIMessageChunk(
                content="", additional_kwargs={"reasoning_content": "private fixture"})))
            evidence.on_llm_new_token("draft fixture", chunk=ChatGenerationChunk(message=AIMessageChunk(
                content="draft fixture", response_metadata={"finish_reason": "stop"})))
            response = evidence.check(AIMessage(content="draft fixture", additional_kwargs={
                "reasoning_content": "private fixture"}), allow_tool_stop=False, output_limit=None)
        assert scope["provider_first_packet_ms"] == 1000
        assert response.content == "draft fixture"
        assert "reasoning_content" not in response.additional_kwargs
        emit.assert_not_called()
    finally:
        _observation.reset(saved)


@pytest.mark.parametrize("extra, budget_limit, stop", [
    (300, 3000, ""), (3000, 3000, "context_budget_exhausted"),
    (300, 95, "model_budget_exhausted:tokens"),
])
def test_http_request_observer_revalidates_the_actual_body(tmp_path, monkeypatch, extra, budget_limit, stop):
    import json
    import time
    import httpx
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from src.agent.runtime import AdmissionStopped
    from src.config import settings
    from src.core import jobs, request_context, task_events, tracing

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(settings, "CONTEXT_ACTIVE_MAX_INPUT_TOKENS", 512)
    claim = jobs.claim_job("session", "request", owner_user_id=7, payload={})
    task_events.init_budget(claim.job_id, time.time() + 120, max_calls=2, max_tokens=budget_limit)
    sent = not stop
    saved_job = tracing.get_job_id()
    tracing.set_job_id(claim.job_id)
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "model-fixture", "model": "fixture", "created": 1,
            "object": "chat.completion", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "completed"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 1, "total_tokens": 91}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
                event_hooks={"request": [request_context.observe_async_provider_request]}) as client:
            model = ChatOpenAI(model="fixture", base_url="https://test.invalid/v1", api_key="fixture",
                               http_async_client=client, max_tokens=64, max_retries=0)
            history = [HumanMessage(content="original question")]
            async def invoke():
                return await model.ainvoke(history, extra_body={"messages": [
                    {"role": "user", "content": "s" * extra}]})
            run = task_events.run_model_attempt(model, history, invoke, purpose="main_loop")
            if sent:
                assert (await run).content == "completed"
            else:
                with pytest.raises(AdmissionStopped, match=stop):
                    await run
    try:
        asyncio.run(exercise())
    finally:
        tracing.set_job_id(saved_job)
    assert len(received) == int(sent)
    with task_events.transaction() as conn:
        attempt = conn.execute("SELECT * FROM task_model_attempts WHERE job_id=?", (claim.job_id,)).fetchone()
        budget = conn.execute("SELECT * FROM task_budgets WHERE job_id=?", (claim.job_id,)).fetchone()
        totals = task_events._usage_totals(conn, claim.job_id)
    assert (budget["calls"], budget["tokens"]) == ((1, 91) if sent else (0, 0))
    assert attempt["status"] == ("complete" if sent else "not_sent")
    assert totals["unknown_attempts"] == 0
    if sent:
        assert attempt["reserved_tokens"] == request_context.estimate_request(received[0])["estimated_total_tokens"]
        assert json.loads(attempt["observation"])["estimated_input_tokens"] == 87
    else:
        assert json.loads(attempt["usage"]) == {}


def test_counted_provider_call_rejects_unexpected_endpoint_before_transport():
    import httpx
    from src.agent.runtime import AdmissionStopped
    from src.core import request_context

    scope = {"purpose": "main_loop"}
    saved = request_context._observation.set(scope)
    request = httpx.Request("POST", "https://test.invalid/v1/responses", json={"input": "fixture"})
    try:
        with pytest.raises(AdmissionStopped, match="unexpected_provider_request_endpoint"):
            request_context.observe_provider_request(request)
        assert scope["pre_transport_denial"] == "unexpected_provider_request_endpoint"
        assert "provider_request_admitted" not in scope
    finally:
        request_context._observation.reset(saved)


def test_duplicate_provider_request_does_not_refund_admitted_attempt():
    import httpx
    from src.agent.runtime import AdmissionStopped
    from src.core import request_context

    scope = {"purpose": "main_loop", "provider_request_admitted": True}
    saved = request_context._observation.set(scope)
    try:
        request = httpx.Request("POST", "https://test.invalid/v1/chat/completions", json={"messages": []})
        with pytest.raises(AdmissionStopped, match="duplicate_provider_request"):
            request_context.observe_provider_request(request)
        assert "pre_transport_denial" not in scope
    finally:
        request_context._observation.reset(saved)
