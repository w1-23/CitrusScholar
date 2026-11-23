import asyncio
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, add_notification_metadata

from src.agent.checkpoint_store import native_checkpoint_scope, open_native_checkpoint
from src.agent.harness import HarnessRuntime
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import FinalCheck, RunLimits
from src.core.provider_protocol import current_tool_loop_start


def test_pinned_skill_and_read_file_survive_thread_reopen(tmp_path):
    asyncio.run(_pinned_skill_and_read_file_survive_thread_reopen(tmp_path))


async def _pinned_skill_and_read_file_survive_thread_reopen(tmp_path):
    skill_root = tmp_path / "skills"
    demo = skill_root / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\nRead-only demo",
                                 encoding="utf-8")
    histories = []

    async def model(history, identity, *, native_schemas):
        histories.append(history)
        assert {entry["function"]["name"] for entry in native_schemas} == {"read_file", "ls", "glob", "grep"}
        if len(histories) == 1:
            assert any("Read-only demo" in str(message.content) for message in history)
            return AIMessage(content="", tool_calls=[{"id": "read-1", "name": "read_file",
                "args": {"file_path": "/skills/demo/SKILL.md"}}])
        if len(histories) == 2:
            assert any(message.type == "tool" and "Read-only demo" in str(message.content)
                       for message in history)
        return AIMessage(content="answer")

    async def accept(*args):
        return FinalCheck()

    async def unexpected_dispatch(*args):
        raise AssertionError("native read reached business dispatch")

    import pytest
    from src.agent.recovery import load_recovery_state
    from src.core.task_events import TaskConflict

    request = {}
    runtime = HarnessRuntime(model, unexpected_dispatch, lambda *args: None, accept, RunLimits(), [],
                             export_host=lambda: {"original_request_state": dict(request)})
    provider = ("https://example.test", "example-model")
    options = {"skill_root": skill_root, "system_prompt": "fixture prompt",
               "summary_model": FakeListChatModel(responses=["summary"])}
    root = tmp_path / "state"
    for index, text in enumerate(("first question", "second question")):
        identity = ExecutionIdentity(1, "thread", f"turn-{index}", f"job-{index}")
        request.update(query=text, user_id=identity.owner_user_id, session_id=identity.session_id,
                       request_id=identity.user_turn_id, job_id=identity.run_id,
                       client_request_id=f"client-{index}", idempotency_key=f"key-{index}")
        human = HumanMessage(text, additional_kwargs=identity.message_metadata("user"))
        history = [human]
        if index:
            previous = ExecutionIdentity(1, "thread", "turn-0", "job-0")
            legacy = ExecutionIdentity(1, "thread", "legacy-turn", "legacy-job")
            history[:0] = [HumanMessage("duplicate persisted history"),
                           AIMessage("previous saved answer", additional_kwargs=previous.message_metadata("assistant")),
                           HumanMessage("legacy question", additional_kwargs=legacy.message_metadata("user")),
                           AIMessage("legacy answer", additional_kwargs=legacy.message_metadata("assistant"))]
        async with open_native_checkpoint(root, identity, provider) as saver:
            result = await runtime.run(identity, history, saver=saver,
                checkpoint_config=native_checkpoint_scope(identity, provider),
                native_options=options, pinned_skills=["demo"] if not index else [])
        assert result.status == "complete"
        recovered = await load_recovery_state(root, identity, provider)
        assert recovered["agent_resume"] and recovered["agent_recovery_answered"]
        assert recovered["job_id"] == identity.run_id and recovered["query"] == text

    with pytest.raises(TaskConflict, match="recovery_identity_mismatch"):
        await load_recovery_state(root, ExecutionIdentity(1, "thread", "turn-0", "job-0"), provider)
    with pytest.raises(TaskConflict, match="recovery_checkpoint_missing"):
        await load_recovery_state(root, ExecutionIdentity(2, "thread", "turn-1", "job-1"), provider)
    assert len(histories) == 3
    assert [message.content for message in histories[-1] if message.type == "human"
            and message.additional_kwargs.get("context_origin") == "user"] == [
                "first question", "legacy question", "second question"]
    assert sum(message.content == "legacy answer" for message in histories[-1]) == 1
    assert all(message.content not in ("duplicate persisted history", "previous saved answer")
               for message in histories[-1])


def test_framework_notification_keeps_the_real_user_turn():
    identity = ExecutionIdentity(1, "thread", "turn", "job")
    user = HumanMessage("question", additional_kwargs=identity.message_metadata("user"))
    notification = add_notification_metadata(HumanMessage("skill method"), source="pinned_skill")
    assert current_tool_loop_start([user, AIMessage("observation"), notification]) == 1


def test_native_summary_is_reused_across_encrypted_connections(tmp_path):
    asyncio.run(_native_summary_is_reused_across_encrypted_connections(tmp_path))


async def _native_summary_is_reused_across_encrypted_connections(tmp_path):
    from langchain_core.messages.utils import count_tokens_approximately

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    histories = []

    async def model(history, identity, *, native_schemas):
        histories.append(history)
        return AIMessage(content="answer")

    async def accept(*args):
        return FinalCheck()

    runtime = HarnessRuntime(model, None, lambda *args: None, accept, RunLimits(), [])
    provider = ("https://example.test", "example-model")
    summary = FakeListChatModel(responses=["Existing evidence is documented", "unexpected repeat"])
    options = {"skill_root": skill_root, "system_prompt": "fixture prompt", "summary_model": summary}
    old = [message for index in range(42) for message in (
        HumanMessage(f"question {index} " + "context-value " * 180),
        AIMessage(f"evidence {index} " + "result-value " * 180))]
    assert count_tokens_approximately(old) > 48000
    for index in range(2):
        identity = ExecutionIdentity(1, "thread", f"turn-{index}", f"job-{index}")
        human = HumanMessage(f"question now {index}", additional_kwargs=identity.message_metadata("user"))
        async with open_native_checkpoint(tmp_path / "state", identity, provider) as saver:
            result = await runtime.run(identity, [*old, human], saver=saver,
                checkpoint_config=native_checkpoint_scope(identity, provider), native_options=options)
        assert result.status == "complete"
    assert summary.i == 1
    assert all(any("Existing evidence is documented" in str(message.content) for message in history)
               for history in histories)
    assert all(count_tokens_approximately(history) < 36000 for history in histories)


def test_native_encryption_rejects_cross_account_and_tampering():
    from src.agent.checkpoint_store import (CheckpointProtectionError, _NativeEncryptedSerializer,
                                            _native_binding)
    import pytest

    provider = ("https://example.test", "example-model")
    identity = ExecutionIdentity(1, "thread", "turn", "job")
    other = ExecutionIdentity(2, "thread", "turn", "job")
    assert native_checkpoint_scope(identity, provider) != native_checkpoint_scope(other, provider)
    first = _NativeEncryptedSerializer(b"s" * 32, _native_binding(identity, provider))
    second = _NativeEncryptedSerializer(b"s" * 32, _native_binding(other, provider))
    kind, ciphertext = first.dumps_typed(HumanMessage("private attachment reference"))
    assert b"private attachment reference" not in ciphertext
    with pytest.raises(CheckpointProtectionError, match="checkpoint_authentication_failed"):
        second.loads_typed((kind, ciphertext))
    with pytest.raises(CheckpointProtectionError, match="checkpoint_authentication_failed"):
        first.loads_typed((kind, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])))
