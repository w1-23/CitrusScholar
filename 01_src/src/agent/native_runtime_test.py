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


def test_installed_skill_selection_uses_active_release_entries(tmp_path, monkeypatch):
    import hashlib
    import json
    import pytest

    from src.agent.installed_skills import list_installed, model_directory
    from src.agent.skill_adapter import scenario_catalog, validate_scenario_selection

    active = tmp_path / "first-release"
    entry = active / "skills" / "additional-method" / "SKILL.md"
    entry.parent.mkdir(parents=True)
    raw = b"---\nname: additional-method\ndescription: One task-specific method.\n---\n\nMethod."
    entry.write_bytes(raw)
    sha = "2" * 40
    (active / "release.json").write_text(json.dumps({"additional-method": sha}), encoding="utf-8")
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(active))
    assert model_directory(active) == entry.parent.parent
    assert [item["id"] for item in scenario_catalog()] == ["additional-method"]
    selected = {"id": "additional-method", "version": sha, "sha256": hashlib.sha256(raw).hexdigest()}
    assert validate_scenario_selection([selected]) == [selected]

    empty = tmp_path / "empty-release"
    (empty / "skills").mkdir(parents=True)
    (empty / "release.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(empty))
    assert scenario_catalog() == []
    with pytest.raises(ValueError, match="selected_skill_identity_mismatch"):
        validate_scenario_selection([selected])
    assert validate_scenario_selection([selected], catalog=list_installed(active)) == [selected]

    entry.write_text("---\nname: different-method\ndescription: Wrong path.\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_skill_frontmatter"):
        list_installed(active)


def test_workspace_uses_installed_skill_inventory_and_live_policy(tmp_path, monkeypatch):
    import hashlib
    import json
    import pytest
    from src.agent.workspace_projection import read, resources
    from src.core import capability_policy

    skill_id = "data-audit"
    version = "a" * 40
    prefix = f"skills/{skill_id}/"
    entry = prefix + "SKILL.md"
    usage = prefix + "references/usage.md"
    files = {entry: b"---\nname: data-audit\ndescription: Active audit method\n---\n",
             usage: b"Read the selected region.\n"}
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (tmp_path / prefix / "unlisted.md").write_text("unlisted", encoding="utf-8")
    (tmp_path / "release.json").write_text(json.dumps({skill_id: version}), encoding="utf-8")
    (tmp_path / "inventory.json").write_text(json.dumps(
        {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}), encoding="utf-8")
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(tmp_path))
    inventory = dict(resources("/skills"))
    assert set(inventory) == {"/" + name for name in files}
    descriptor = inventory["/" + usage]
    assert descriptor["skill_version"] == version
    assert read("/" + usage, descriptor)[0] == files[usage].decode()
    (tmp_path / usage).write_bytes(b"Do not read changed bytes.")
    with pytest.raises(ValueError, match="workspace_skill_integrity_mismatch"):
        read("/" + usage, descriptor)
    monkeypatch.setattr(capability_policy, "allows", lambda name: name != f"skill:{skill_id}")
    assert not list(resources("/skills"))
    with pytest.raises(PermissionError, match="skill_disabled"):
        read("/" + usage, descriptor)


async def _pinned_skill_and_read_file_survive_thread_reopen(tmp_path):
    skill_root = tmp_path / "skills"
    demo = skill_root / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\nRead-only demo",
                                 encoding="utf-8")
    histories = []
    admissions = []
    events = []

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

    async def admit(history, identity):
        admissions.append(identity)

    async def capture_event(value):
        events.append(value)

    request = {}
    runtime = HarnessRuntime(model, unexpected_dispatch, admit, accept, RunLimits(), [],
                             on_event=capture_event,
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
                native_options={**options, "summary_model": options["summary_model"] if index else None},
                pinned_skills=["demo"] if not index else [])
        assert result.status == "complete"
        recovered = await load_recovery_state(root, identity, provider)
        assert recovered["agent_resume"] and recovered["agent_recovery_answered"]
        assert recovered["job_id"] == identity.run_id and recovered["query"] == text

    with pytest.raises(TaskConflict, match="recovery_identity_mismatch"):
        await load_recovery_state(root, ExecutionIdentity(1, "thread", "turn-0", "job-0"), provider)
    with pytest.raises(TaskConflict, match="recovery_checkpoint_missing"):
        await load_recovery_state(root, ExecutionIdentity(2, "thread", "turn-1", "job-1"), provider)
    assert [event.kind for event in events if event.tool_call_ids == ("read-1",)] == [
        "model_finished", "batch_started", "batch_finished",
    ]
    assert [identity.run_id for identity in admissions] == ["job-0", "job-0", "job-0", "job-1"]
    assert len(histories) == 3
    assert [message.content for message in histories[-1] if message.type == "human"
            and message.additional_kwargs.get("context_origin") == "user"] == [
                "first question", "legacy question", "second question"]
    assert sum(message.content == "legacy answer" for message in histories[-1]) == 1
    assert all(message.content not in ("duplicate persisted history", "previous saved answer")
               for message in histories[-1])


def test_native_thread_accepts_new_turn_after_cancelling_model(tmp_path):
    async def scenario():
        from src.agent.deep_agent_runtime import NATIVE_EXECUTOR_ID

        skill_root = tmp_path / "skills"
        skill_root.mkdir()
        options = {"skill_root": skill_root, "system_prompt": "fixture prompt", "summary_model": None}
        calls = []

        async def model(history, identity, *, native_schemas):
            calls.append(identity.run_id)
            if identity.run_id == "cancelled":
                raise asyncio.CancelledError()
            return AIMessage(content="new answer")

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(model, None, lambda *args: None, accept, RunLimits(), [])
        provider = ("https://example.test", "fixture")
        for index, (job_id, status) in enumerate((("cancelled", "cancelled"), ("next", "complete"))):
            identity = ExecutionIdentity(1, "thread", f"turn-{index}", job_id)
            human = HumanMessage(job_id, additional_kwargs=identity.message_metadata("user"))
            config = native_checkpoint_scope(identity, provider)
            async with open_native_checkpoint(tmp_path, identity, provider) as saver:
                result = await runtime.run(identity, [human], saver=saver,
                                           checkpoint_config=config, native_options=options)
                saved = await saver.aget_tuple({"configurable": config})
                assert saved.checkpoint["channel_values"]["executor_id"] == NATIVE_EXECUTOR_ID
            assert result.status == status
        assert calls == ["cancelled", "next"]

    asyncio.run(scenario())


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


def test_native_experiment_scope_discards_prior_context_and_summary(tmp_path, monkeypatch):
    from langchain_core.messages.utils import count_tokens_approximately

    from src.agent.recovery import load_recovery_state
    from src.core import jobs
    from src.core.capability_policy import CapabilityPolicy

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        skill_root = tmp_path / "skills"
        skill_root.mkdir()
        provider = ("https://example.test", "example-model")
        root = tmp_path / "state"
        histories = []
        request = {}

        async def model(history, identity, **kwargs):
            histories.append(str([message.content for message in history]))
            return AIMessage(content="fixture answer")

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(model, None, lambda *args: None, accept, RunLimits(), [],
                                 export_host=lambda: {"original_request_state": dict(request)})
        summary = FakeListChatModel(responses=["unique-local-summary", "unexpected second summary"])
        local_history = [message for index in range(42) for message in (
            HumanMessage(f"question {index} " + "context-value " * 180),
            AIMessage(f"unique-local-only {index} " + "result-value " * 180))]
        assert count_tokens_approximately(local_history) > 48000
        experiment = CapabilityPolicy(local_rag=False, experiment_group="without-rag")
        for index, policy in enumerate((CapabilityPolicy(), experiment, experiment)):
            turn_id = f"turn-{index}"
            job = jobs.claim_job("one-session", turn_id, owner_user_id=1, client_request_id=turn_id, payload={})
            identity = ExecutionIdentity(1, "one-session", turn_id, job.job_id)
            if policy.history_key:
                assert jobs.bind_native_context_scope(identity, policy.history_key)
                assert not jobs.bind_native_context_scope(identity, "f" * 64)
            question = f"question now {index}"
            request.update(request_id=turn_id, capability_snapshot=policy.snapshot(), query=question,
                           session_id=identity.session_id, job_id=identity.run_id, user_id=1,
                           client_request_id=turn_id, idempotency_key=turn_id)
            human = HumanMessage(question, additional_kwargs=identity.message_metadata("user"))
            scope = {"context_scope": policy.history_key}
            async with open_native_checkpoint(root, identity, provider, **scope) as saver:
                result = await runtime.run(identity, [*(local_history if index == 0 else []), human], saver=saver,
                    checkpoint_config=native_checkpoint_scope(identity, provider, **scope),
                    native_options={"skill_root": skill_root, "system_prompt": "fixture prompt",
                                    "summary_model": summary, "capability_policy": policy})
            assert result.status == "complete"
            recovered = await load_recovery_state(root, identity, provider)
            assert recovered["capability_snapshot"] == policy.snapshot()
            assert recovered["job_id"] == job.job_id
        assert "unique-local-summary" in histories[0]
        assert summary.i == 1
        assert all("unique-local" not in history for history in histories[1:])
        assert "question now 1" in histories[-1] and "question now 2" in histories[-1]

    asyncio.run(scenario())


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
