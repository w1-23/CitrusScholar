import asyncio
import pytest
from langchain_core.messages import AIMessage, HumanMessage, add_notification_metadata

from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
from src.agent.harness import HarnessRuntime
from src.agent.framework_test_support import ScriptedModel
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
    from src.agent.installed_skills import scenario_catalog, validate_scenario_selection

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
    from hashlib import sha256
    import json

    skill_root = tmp_path / "skills"
    demo = skill_root / "demo"
    demo.mkdir(parents=True)
    entry = demo / "SKILL.md"
    entry.write_text("---\nname: demo\ndescription: Demo\n---\nRead-only demo", encoding="utf-8")
    (tmp_path / "inventory.json").write_text(json.dumps({"skills/demo/SKILL.md": sha256(entry.read_bytes()).hexdigest()}))
    histories = []
    admissions = []
    events = []

    async def model(history, identity, *, native_schemas):
        histories.append(history)
        # One native file surface plus the metered delegation and sandbox tools.
        assert {entry["function"]["name"] for entry in native_schemas} == {
            "ls", "glob", "grep", "read_file", "write_file", "edit_file", "delete",
            "execute", "write_todos", "task"}
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
    runtime = HarnessRuntime(ScriptedModel(script=model), unexpected_dispatch, admit, accept, RunLimits(), [],
                             on_event=capture_event,
                             export_host=lambda: {"original_request_state": dict(request)})
    provider = ("https://example.test", "example-model")
    options = {"skill_root": skill_root, "system_prompt": "fixture prompt"}
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
        async with open_framework_checkpoint(root, identity, provider) as saver:
            result = await runtime.run(identity, history, saver=saver,
                checkpoint_config=framework_checkpoint_scope(identity, provider),
                agent_options=options,
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
        from src.agent.deep_agent_runtime import EXECUTOR_ID

        skill_root = tmp_path / "skills"
        skill_root.mkdir()
        (tmp_path / "inventory.json").write_text("{}")
        options = {"skill_root": skill_root, "system_prompt": "fixture prompt"}
        calls = []

        async def model(history, identity, *, native_schemas):
            calls.append(identity.run_id)
            if identity.run_id == "cancelled":
                raise asyncio.CancelledError()
            return AIMessage(content="new answer")

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(ScriptedModel(script=model), None, lambda *args: None, accept, RunLimits(), [],
                                 check_terminal_orphan=lambda _: asyncio.sleep(0, result=True))
        provider = ("https://example.test", "fixture")
        for index, (job_id, status) in enumerate((("cancelled", "cancelled"), ("next", "complete"))):
            identity = ExecutionIdentity(1, "thread", f"turn-{index}", job_id)
            human = HumanMessage(job_id, additional_kwargs=identity.message_metadata("user"))
            config = framework_checkpoint_scope(identity, provider)
            async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
                result = await runtime.run(identity, [human], saver=saver,
                                           checkpoint_config=config, agent_options=options)
                saved = await saver.aget_tuple({"configurable": config})
                assert saved.checkpoint["channel_values"]["executor_id"] == EXECUTOR_ID
            assert result.status == status
        assert calls == ["cancelled", "next"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failing_phase", ["first_turn", "later_turn", "before_checkpoint"])
def test_framework_diagnostic_reports_only_the_failing_run_checkpoint(tmp_path, failing_phase):
    async def scenario():
        async def model(_, identity, **kwargs):
            assert identity.run_id == "previous"
            return AIMessage("previous answer")

        def admit(_, identity):
            if identity.run_id == "current":
                raise AttributeError("fixture pre-model failure")

        async def event(record):
            if failing_phase == "before_checkpoint" and record.identity.run_id == "current":
                raise AttributeError("fixture before checkpoint")

        async def accept(*_):
            return FinalCheck()

        runtime = HarnessRuntime(ScriptedModel(script=model), None, admit, accept, RunLimits(), [], on_event=event)
        previous = ExecutionIdentity(1, "diagnostic-session", "turn-1", "previous")
        current = ExecutionIdentity(1, "diagnostic-session", "turn-2", "current")
        provider = ("https://example.test", "fixture")
        scope = framework_checkpoint_scope(current, provider)
        config = {"configurable": scope}
        async with open_framework_checkpoint(tmp_path, current, provider) as saver:
            if failing_phase != "first_turn":
                result = await runtime.run(previous, [HumanMessage("previous question")], saver=saver,
                                           checkpoint_config=scope)
                assert result.status == "complete"
            with pytest.raises(AttributeError, match="^fixture"):
                await runtime.run(current, [HumanMessage("current question")], saver=saver, checkpoint_config=scope)
            saved = await saver.aget_tuple(config)
            if failing_phase == "before_checkpoint":
                assert saved.checkpoint["channel_values"]["active_run_id"] == previous.run_id
                assert runtime.diagnostic_checkpoint == ""
            else:
                assert saved.checkpoint["channel_values"]["active_run_id"] == current.run_id
                assert runtime.diagnostic_checkpoint == saved.checkpoint["id"]

    asyncio.run(scenario())


def test_framework_notification_keeps_the_real_user_turn():
    identity = ExecutionIdentity(1, "thread", "turn", "job")
    user = HumanMessage("question", additional_kwargs=identity.message_metadata("user"))
    notification = add_notification_metadata(HumanMessage("skill method"), source="pinned_skill")
    assert current_tool_loop_start([user, AIMessage("observation"), notification]) == 1


def test_native_experiment_scope_discards_prior_context_and_summary(tmp_path, monkeypatch):
    from langchain_core.messages.utils import count_tokens_approximately

    from src.agent.recovery import load_recovery_state
    from src.core import jobs
    from src.core.capability_policy import CapabilityPolicy

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        skill_root = tmp_path / "skills"
        skill_root.mkdir()
        (tmp_path / "inventory.json").write_text("{}")
        provider = ("https://example.test", "example-model")
        root = tmp_path / "state"
        histories = []
        request = {}

        async def model(history, identity, **kwargs):
            histories.append(str([message.content for message in history]))
            return AIMessage(content="fixture answer")

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(ScriptedModel(script=model), None, lambda *args: None, accept, RunLimits(), [],
                                 export_host=lambda: {"original_request_state": dict(request)})
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
            async with open_framework_checkpoint(root, identity, provider, **scope) as saver:
                result = await runtime.run(identity, [*(local_history if index == 0 else []), human], saver=saver,
                    checkpoint_config=framework_checkpoint_scope(identity, provider, **scope),
                    agent_options={"skill_root": skill_root, "system_prompt": "fixture prompt",
                                    "capability_policy": policy})
            assert result.status == "complete"
            recovered = await load_recovery_state(root, identity, provider)
            assert recovered["capability_snapshot"] == policy.snapshot()
            assert recovered["job_id"] == job.job_id
        assert "unique-local-only" in histories[0]
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
    assert framework_checkpoint_scope(identity, provider) != framework_checkpoint_scope(other, provider)
    first = _NativeEncryptedSerializer(b"s" * 32, _native_binding(identity, provider))
    second = _NativeEncryptedSerializer(b"s" * 32, _native_binding(other, provider))
    kind, ciphertext = first.dumps_typed(HumanMessage("private attachment reference"))
    assert b"private attachment reference" not in ciphertext
    with pytest.raises(CheckpointProtectionError, match="checkpoint_authentication_failed"):
        second.loads_typed((kind, ciphertext))
    with pytest.raises(CheckpointProtectionError, match="checkpoint_authentication_failed"):
        first.loads_typed((kind, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])))


def test_retired_skills_do_not_leak_into_current_method_interfaces(tmp_path, monkeypatch):
    import hashlib
    import json
    import pytest

    from src.agent import research_skill_catalog
    from src.agent.installed_skills import scenario_catalog, validate_scenario_selection
    from src.agent.workspace_projection import read, resources
    from src.agent.installed_skills import (TurnSkills, bind_turn_skills, release_pin, task_release)
    from src.agent.deep_agent_runtime import ScopedSkillBackend
    from src.core.capability_policy import CapabilityPolicy, bind_policy
    from src.research import workspace_tools

    legacy = dict(zip(('id', 'version', 'sha256'),
                      ('candidate-search', *research_skill_catalog.PINNED['candidate-search'])))
    monkeypatch.delenv('CITRUS_SKILL_RELEASE', raising=False)
    assert scenario_catalog() == []
    assert list(resources('/skills')) == []
    assert not (research_skill_catalog.ROOT / 'skills').exists()
    assert not (research_skill_catalog.ROOT / '01_src/src/prompts/skills').exists()

    new_id = 'research-question-answering'
    entry = f'skills/{new_id}/SKILL.md'
    content = f'---\nname: {new_id}\ndescription: Current method.\n---\n'.encode()
    path = tmp_path / entry
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    (path.parent / 'unlisted.md').write_text('legacy stray file')
    (tmp_path / 'release.json').write_text(json.dumps({new_id: '1' * 40}))
    (tmp_path / 'inventory.json').write_text(json.dumps({entry: hashlib.sha256(content).hexdigest()}))
    monkeypatch.setenv('CITRUS_SKILL_RELEASE', str(tmp_path))
    assert [row['id'] for row in scenario_catalog()] == [new_id]
    with pytest.raises(ValueError, match='selected_skill_identity_mismatch'):
        validate_scenario_selection([legacy])
    monkeypatch.setattr(workspace_tools, '_identity', lambda: (1, 'skill-test-session'))
    call = workspace_tools.research_workspace.func
    assert [row["id"] for row in call(operation="skills")[1]["skills"]] == [new_id]
    assert call(operation="read_skill", skill_id=new_id)[1]["entrypoint"]["text"] == content.decode()
    assert list(resources("/skills/" + new_id))
    policy = CapabilityPolicy()
    sdk = ScopedSkillBackend(tmp_path / "skills", policy)
    assert sdk.read("/" + entry.removeprefix("skills/")).error is None
    assert sdk.read("/" + new_id + "/unlisted.md").error
    selected = validate_scenario_selection([{key: scenario_catalog()[0][key]
        for key in ("id", "version", "sha256")}])
    with pytest.raises(ValueError, match="selected_skill_identity_mismatch"):
        validate_scenario_selection([dict(selected[0], sha256="0" * 64)])
    pin = release_pin(tmp_path)
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(tmp_path / "unrelated"))
    assert task_release(dict(pin), resume=True) == tmp_path
    assert task_release({}, resume=False, continuation_pin=pin) == tmp_path
    view = TurnSkills.from_selection(tmp_path, selected)
    with bind_turn_skills(view):
        assert [row["id"] for row in call(operation="skills")[1]["skills"]] == [new_id]
        assert call(operation="read_skill", skill_id=new_id)[1]["entrypoint"]["text"] == content.decode()
        assert call(operation="read_skill_resource", skill_id=new_id, resource="unlisted.md")[1]["error"] == "skill_resource_unavailable"
        key, descriptor = next(resources("/" + entry))
        assert read(key, descriptor)[0] == content.decode()
        sdk = ScopedSkillBackend(tmp_path / "skills", policy, view)
        assert sdk.read("/" + entry.removeprefix("skills/")).error is None
    with bind_turn_skills(TurnSkills(tmp_path)):
        assert read(key, descriptor)[0] == content.decode()
        assert list(resources("/skills/" + new_id))
    revoked = CapabilityPolicy(revoked=("skill:" + new_id,))
    with bind_policy(revoked), bind_turn_skills(view):
        assert not list(resources("/skills/" + new_id))
        assert call(operation="skills")[1]["skills"] == []
        assert ScopedSkillBackend(tmp_path / "skills", revoked, view).read("/" + entry.removeprefix("skills/")).error


def test_framework_waits_for_model_and_uses_only_the_selected_source():
    async def scenario():
        from langchain_core.messages import ToolMessage
        from langgraph.checkpoint.memory import InMemorySaver

        identity = ExecutionIdentity(1, "session-fixture", "turn-fixture", "run-fixture")
        names = {"local_hybrid_search", "deepseek_search"}
        sequence = []

        async def execute(call, _):
            sequence.append(call["name"])
            return ToolMessage("observed evidence", name=call["name"], tool_call_id=call["id"]), {}

        async def model(history, _, **kwargs):
            sequence.append("model")
            if not any(isinstance(message, ToolMessage) for message in history):
                return AIMessage("", tool_calls=[{"name": "deepseek_search", "id": "web-first", "args": {}}])
            return AIMessage("answered")

        async def accept(*_):
            return FinalCheck()

        schemas = [{"function": {"name": name, "description": name, "parameters": {"type": "object"}}}
                   for name in names]
        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(2), schemas)
        result = await runtime.run(identity, [HumanMessage("rwp基因如何调控胚性")], saver=InMemorySaver())
        assert result.status == "complete" and result.model_steps == 2
        assert sequence == ["model", "deepseek_search", "model"]

    asyncio.run(scenario())


def test_legacy_synthetic_tool_call_must_already_be_paired():
    from langchain_core.messages import ToolMessage
    from src.agent.harness import _unsettled_legacy_seed

    call = {"name": "local_hybrid_search", "id": "legacy", "args": {}}
    synthetic = AIMessage("", tool_calls=[call], additional_kwargs={"citrus_retrieval_seed": True})
    paired = ToolMessage("stored observation", tool_call_id="legacy")
    assert _unsettled_legacy_seed({"seed_proposal": [call]})
    assert _unsettled_legacy_seed({"messages": [synthetic]})
    assert not _unsettled_legacy_seed({"messages": [synthetic, paired], "seed_proposal": [call]})
    assert not _unsettled_legacy_seed({"messages": [AIMessage("", tool_calls=[call])]})


@pytest.mark.parametrize("resume_mode", ["normal", "expired", "ack_crash"])
def test_framework_interrupt_parks_after_pair_and_reopens_without_redispatch(tmp_path, monkeypatch, resume_mode):
    from langchain_core.messages import ToolMessage
    from src.agent.recovery import load_recovery_state
    from src.core import jobs, task_events
    from dataclasses import asdict
    import time

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        provider = ("https://example.test", "fixture")
        job = jobs.claim_job("session", "turn", owner_user_id=1, client_request_id="client", payload={})
        i = ExecutionIdentity(1, "session", "turn", job.job_id)
        original = {"user_id": 1, "session_id": "session", "request_id": "turn", "job_id": job.job_id,
                    "query": "request", "client_request_id": "client", "idempotency_key": "key"}
        wait = "a" * 32
        calls, acknowledgments, resolutions = [], [], []
        proposal = {"id": "tool-1", "name": "fixture_code_execution", "args": {}}

        async def model(history, _, **kwargs):
            if not calls:
                return AIMessage("", tool_calls=[proposal])
            assert acknowledgments and any(m.content == "receipt ready" for m in history)
            return AIMessage("accepted")

        async def execute(call, _):
            calls.append(call["id"])
            return ToolMessage("enqueued", name=call["name"], tool_call_id=call["id"]), {"execution_waits": [wait]}

        async def resolve(_, waits):
            assert waits == (wait,)
            resolutions.append(wait)
            return ("receipt ready",)

        async def acknowledge(_, waits, checkpoint):
            assert waits == (wait,)
            acknowledgments.append(checkpoint)
            if resume_mode == "ack_crash" and len(acknowledgments) == 1:
                # The remote acknowledgment already committed when the Web died.
                raise RuntimeError("process ended after acknowledgment")

        async def accept(*args):
            return FinalCheck()

        schemas = [{"function": {"name": proposal["name"], "description": "Fixture",
                                 "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(), schemas,
            export_host=lambda: {"original_request_state": original, "identity": asdict(i)},
            resolve_execution_waits=resolve, acknowledge_execution_waits=acknowledge)
        config = framework_checkpoint_scope(i, provider)
        async with open_framework_checkpoint(tmp_path, i, provider) as saver:
            result = await runtime.run(i, [HumanMessage("request")], saver=saver, checkpoint_config=config)
            checkpoint = (await saver.aget_tuple({"configurable": config})).config["configurable"]["checkpoint_id"]
            assert result.status == "suspended" and result.execution_waits == (wait,)
            assert result.pending_tool_calls == ()
        with pytest.raises(task_events.TaskConflict, match="recovery_execution_wait_invalid"):
            await load_recovery_state(tmp_path, i, provider, execution_wait={"checkpoint_ref": "wrong", "wait_ids": [wait]})
        recovered = await load_recovery_state(tmp_path, i, provider,
                                            execution_wait={"checkpoint_ref": checkpoint, "wait_ids": [wait]})
        assert recovered["agent_resume"]
        discovered = await load_recovery_state(tmp_path, i, provider, discover_execution_wait=True)
        assert discovered["agent_recovery_execution_wait"] == {"checkpoint_ref": checkpoint, "wait_ids": [wait]}
        if resume_mode == "expired":
            runtime.limits = RunLimits(deadline=time.monotonic() - 1)
        if resume_mode == "ack_crash":
            async with open_framework_checkpoint(tmp_path, i, provider) as saver:
                deferred = await runtime.run(i, [], saver=saver, checkpoint_config=config, resume=True)
                assert deferred.status == "suspended" and deferred.execution_waits == (wait,)
            recovered = await load_recovery_state(tmp_path, i, provider,
                execution_wait={"checkpoint_ref": checkpoint, "wait_ids": [wait]})
            assert recovered["agent_recovery_wake_committed"] is True
        async with open_framework_checkpoint(tmp_path, i, provider) as saver:
            result = await runtime.run(i, [], saver=saver, checkpoint_config=config, resume=True)
        if resume_mode == "expired":
            assert (result.status, result.reason, result.model_steps) == ("incomplete", "deadline", 1)
        else:
            assert result.status == "complete" and result.model_steps == 2
        assert calls == ["tool-1"] and resolutions == [wait]
        assert len(acknowledgments) == (2 if resume_mode == "ack_crash" else 1)
        assert len(set(acknowledgments)) == 1 and acknowledgments[0] != checkpoint

    asyncio.run(scenario())


def test_unknown_operation_never_executes_sibling_or_next_turn():
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from src.agent.runtime import AdmissionStopped
    import pytest

    async def scenario():
        i = ExecutionIdentity(1, "s", "t", "j")
        called = []
        async def model(*args, **kwargs):
            return AIMessage("", tool_calls=[{"id": f"call{n}", "name": "write_fixture", "args": {}}
                                             for n in range(3)])

        async def operation(call, _):
            called.append(call["id"])
            return ToolMessage("unknown", name=call["name"], tool_call_id=call["id"]), {
                "uncertain": True, "operation_id": "effect"}

        async def not_final(*args):
            raise AssertionError("unreconciled operation must not be finalized")

        schemas = [{"function": {"name": "write_fixture", "description": "Fixture",
                                 "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(ScriptedModel(script=model), operation, lambda *_: None, not_final, RunLimits(), schemas,
                                 tool_mode=lambda _: "sequential")
        saver = InMemorySaver()
        config = {"thread_id": "same-session"}
        result = await runtime.run(i, [HumanMessage("request")], saver=saver, checkpoint_config=config)
        assert (result.status, result.reason) == ("incomplete", "operation_outcome_unknown")
        assert result.unknown_operations == ("effect",) and result.pending_tool_calls == ()
        assert len(called) == 1
        with pytest.raises(AdmissionStopped, match="native_thread_requires_reconciliation"):
            await runtime.run(ExecutionIdentity(1, "s", "next", "next-job"), [HumanMessage("another")],
                              saver=saver, checkpoint_config=config)
        assert len(called) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("crash_ack", (False, True))
def test_question_resume_continues_original_interrupt_without_replaying_tools(tmp_path, monkeypatch, crash_ack):
    from dataclasses import asdict
    from langchain_core.messages import ToolMessage
    from src.agent.recovery import QuestionWakeDeferred, load_recovery_state
    from src.core import jobs, progress_bus, task_events, tracing
    from src.agent.runtime import AdmissionStopped
    from src.core.task_events import TaskConflict

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def run():
        job = jobs.claim_job("session-fixture", "turn-fixture", owner_user_id=1,
                             client_request_id="client", payload={})
        identity = ExecutionIdentity(1, "session-fixture", "turn-fixture", job.job_id)
        provider = ("https://example.test", "fixture")
        original = {"user_id": 1, "session_id": identity.session_id,
                    "request_id": identity.user_turn_id, "job_id": identity.run_id,
                    "query": "request", "client_request_id": "client", "idempotency_key": "key"}
        for name, value in (("user_id", identity.owner_user_id), ("session_id", identity.session_id),
                            ("request_id", identity.user_turn_id), ("job_id", identity.run_id)):
            monkeypatch.setattr(tracing, f"get_{name}", lambda value=value: value)
        queue = task_events.DurableEventQueue(identity.run_id)
        monkeypatch.setattr(progress_bus, "get_progress_queue", lambda: queue)
        next_turn = ExecutionIdentity(1, "session-fixture", "other-turn", "other-run")
        calls, model_calls, bindings, acks = [], [], [], []
        question = {"name": "request_user_input", "id": "ask", "args": {}}

        async def model(history, active, **kwargs):
            assert active == identity
            model_calls.append(active)
            if len(model_calls) == 1:
                return AIMessage("", tool_calls=[question])
            assert sum(isinstance(message, HumanMessage) and message.content == "request" for message in history) == 1
            assert sum(isinstance(message, HumanMessage) and "fixture answer" in str(message.content)
                       for message in history) == 1
            if len(model_calls) == 2:
                return AIMessage("Unpublished model draft after clarification", tool_calls=[{
                    "name": "write_todos", "id": "resumed-progress", "args": {"todos": [
                        {"content": "公开步骤：核对附件范围", "status": "in_progress"}]}}])
            assert [n["data"]["text"] for n in task_events.public_working_notes(identity.run_id)] == ["核对附件范围"]
            return AIMessage("final")

        async def execute(call, _):
            calls.append(call["id"])
            return ToolMessage("question recorded", name=call["name"], tool_call_id=call["id"]), {
                "pending_question": {"question_id": "first-question", "version": 1}}

        async def final(*args):
            return FinalCheck()

        async def bind(value, interrupt_id, checkpoint):
            bindings.append((value, interrupt_id, checkpoint))

        async def ack(value, checkpoint):
            acks.append((value, checkpoint))
            if crash_ack and len(acks) == 1:
                raise QuestionWakeDeferred("simulated loss before ledger ACK")

        schemas = [{"function": {"name": question["name"], "description": "Fixture question",
                                 "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, final, RunLimits(), schemas,
                                 bind_user_input=bind, acknowledge_user_input=ack,
                                 export_host=lambda: {"identity": asdict(identity), "original_request_state": original})
        config = framework_checkpoint_scope(identity, provider)
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            parked = await runtime.run(identity, [HumanMessage("request")], saver=saver, checkpoint_config=config)
            assert parked.status == "waiting"
            with pytest.raises(AdmissionStopped, match="native_thread_requires_reconciliation"):
                await runtime.run(next_turn, [HumanMessage("arbitrary user input")], saver=saver, checkpoint_config=config)
        payload, interrupt_id, ref = bindings[0]
        assert payload == {"kind": "user_input", "question_id": "first-question", "version": 1}
        answer = {**payload, "interrupt_id": interrupt_id, "selected_option_ids": [], "selected_labels": [],
                  "text": "fixture answer"}
        runtime.user_input = {"value": {**answer, "interrupt_id": "wrong"}, "checkpoint_ref": ref}
        with pytest.raises(TaskConflict, match="question_resume_checkpoint_conflict"):
            await load_recovery_state(tmp_path, identity, provider, question_resume=runtime.user_input)
        runtime.user_input = {"value": answer, "checkpoint_ref": ref}
        resumed = await load_recovery_state(tmp_path, identity, provider, question_resume=runtime.user_input)
        assert resumed["agent_question_resume"] == runtime.user_input and resumed["messages"] == []
        if crash_ack:
            async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
                with pytest.raises(QuestionWakeDeferred):
                    await runtime.run(identity, [], saver=saver, checkpoint_config=config, resume=True)
            assert len(model_calls) == 1
            resumed = await load_recovery_state(tmp_path, identity, provider, question_resume=runtime.user_input)
            assert resumed["agent_question_resume"] == runtime.user_input
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            result = await runtime.run(identity, [], saver=saver, checkpoint_config=config, resume=True)
        assert result.status == "complete" and calls == ["ask"]
        assert result.messages[-1].content == "final" and len(model_calls) == 3
        assert len(set(checkpoint for _, checkpoint in acks)) == 1
        assert "Unpublished model draft" not in repr(task_events.replay(identity.run_id))
        assert len(acks) == (2 if crash_ack else 1)

    asyncio.run(run())


@pytest.mark.parametrize("tool_name,lose_receipt", [("fixture_write", False), ("draw_svg", False), ("fixture_write", True)])
def test_framework_recovers_retained_receipt_without_running_tool_twice(tmp_path, monkeypatch, tool_name, lose_receipt):
    import pytest
    from dataclasses import asdict
    from types import SimpleNamespace
    from langchain_core.messages import ToolMessage
    from src.agent.batch_journal import retain_observation
    from src.agent.recovery import load_recovery_state
    from src.core import figures, jobs, task_events

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        provider = ("https://example.test", "fixture")
        job = jobs.claim_job("session", "turn", owner_user_id=1, client_request_id="client", payload={})
        identity = ExecutionIdentity(1, "session", "turn", job.job_id)
        original = {"user_id": 1, "session_id": "session", "request_id": "turn", "job_id": job.job_id,
                    "query": "request", "client_request_id": "client", "idempotency_key": "key"}
        proposal = {"id": "opaque-call", "name": tool_name, "args": {}}
        figures.begin_request()
        writes, restored = [], []

        async def model(history, _, **kwargs):
            if not writes:
                return AIMessage("", tool_calls=[proposal])
            messages = [m for m in history if isinstance(m, ToolMessage)]
            assert [m.content for m in messages] == ["durable receipt"]
            assert all("host_figure_recovery" not in m.additional_kwargs for m in messages)
            if tool_name == "draw_svg":
                assert "<svg>original</svg>" in figures.expand("[[FIGURE:1]]")
            return AIMessage("accepted")

        async def execute(call, _):
            writes.append(call["id"])
            operation, lease = task_events.start_operation(identity.run_id, 1, call["name"], {},
                tool_call_id=call["id"], logical_key="host:fixture")
            observed = ToolMessage("durable receipt", name=call["name"], tool_call_id=call["id"],
                additional_kwargs={"operation_id": operation, "host_publication": {"tool_call_id": call["id"]}},
                artifact={"_tool_outcome": {"status": "complete", "reason_code": "fixture_completed"}})
            task_events.finish_operation(operation, lease, observed)
            if tool_name == "draw_svg":
                figures.register("<svg>original</svg>")
                observed.additional_kwargs["host_figure_recovery"] = figures.export_recovery(identity)
            retain_observation(identity, SimpleNamespace(operation_id=operation, logical_key="host:fixture",
                uncertain=False, attempt=1, pending_question=False, execution_waits=()), observed, call)
            raise RuntimeError("process ended after effect committed")

        async def accept(*_):
            return FinalCheck()

        schemas = [{"function": {"name": proposal["name"], "description": "Fixture", "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(), schemas,
            export_host=lambda: {"identity": asdict(identity), "original_request_state": original},
            restore_host=lambda _: None,
            restore_tool_observation=lambda message, call: restored.append((message.tool_call_id, call["id"])))
        config = framework_checkpoint_scope(identity, provider)
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            with pytest.raises(RuntimeError, match="process ended after effect committed"):
                await runtime.run(identity, [HumanMessage("request")], saver=saver, checkpoint_config=config)
        if lose_receipt:
            with task_events.transaction() as conn:
                conn.execute("UPDATE task_operations SET host_observation='' WHERE job_id=?", (identity.run_id,))
            with pytest.raises(task_events.TaskConflict, match="recovery_requires_reconciliation"):
                await load_recovery_state(tmp_path, identity, provider)
            assert writes == [proposal["id"]] and restored == []
            return
        await load_recovery_state(tmp_path, identity, provider)
        figures.begin_request()
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            result = await runtime.run(identity, [], saver=saver, checkpoint_config=config, resume=True)
        assert result.status == "complete"
        assert writes == [proposal["id"]] and restored == [(proposal["id"], proposal["id"])]

    asyncio.run(scenario())


def test_quarantined_head_cannot_replay_pending_tool_into_new_turn():
    import pytest
    from langgraph.checkpoint.memory import InMemorySaver

    async def scenario():
        old = ExecutionIdentity(1, "session", "turn1", "run1")
        new = ExecutionIdentity(1, "session", "turn2", "run2")
        proposal = {"id": "pending", "name": "fixture_write", "args": {}}
        writes = []

        async def model(history, identity, **kwargs):
            if identity == old:
                return AIMessage("", tool_calls=[proposal])
            assert not any(isinstance(m, AIMessage) and m.tool_calls for m in history)
            assert any(isinstance(m, HumanMessage) and m.content == "new request" for m in history)
            return AIMessage("accepted")

        async def execute(call, _):
            writes.append(call["id"])
            raise RuntimeError("effect is not known")

        async def accept(*_):
            return FinalCheck()

        async def quarantined(values):
            assert values.get("active_run_id") == old.run_id
            return "quarantined"

        schemas = [{"function": {"name": proposal["name"], "description": "Fixture", "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(), schemas,
                                 check_terminal_orphan=quarantined)
        saver = InMemorySaver()
        config = {"thread_id": "session"}
        with pytest.raises(RuntimeError, match="effect is not known"):
            await runtime.run(old, [HumanMessage("old request")], saver=saver, checkpoint_config=config)
        result = await runtime.run(new, [HumanMessage("new request")], saver=saver, checkpoint_config=config)
        assert result.status == "complete" and writes == ["pending"]

    asyncio.run(scenario())


def test_protected_reviewed_answer_is_delivered_without_model_when_deadline_elapsed(tmp_path, monkeypatch):
    from dataclasses import asdict
    import time
    from src.agent.recovery import load_recovery_state
    from src.core import jobs, task_events

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        provider = ("https://example.test", "fixture")
        job = jobs.claim_job("session", "turn", owner_user_id=1, client_request_id="client", payload={})
        identity = ExecutionIdentity(1, "session", "turn", job.job_id)
        original = {"user_id": 1, "session_id": "session", "request_id": "turn", "job_id": job.job_id,
                    "query": "original question", "client_request_id": "client", "idempotency_key": "key"}
        model_calls = []

        async def model(*_args, **_kwargs):
            model_calls.append(True)
            return AIMessage("already accepted")

        async def accept(*_):
            return FinalCheck()

        async def execute(*_):
            pytest.fail("A reviewed-answer recovery cannot invoke tools")

        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(), (),
            export_host=lambda: {"identity": asdict(identity), "original_request_state": original})
        config = framework_checkpoint_scope(identity, provider)
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            assert (await runtime.run(identity, [HumanMessage("original question")], saver=saver,
                                      checkpoint_config=config)).status == "complete"
        with task_events.transaction() as conn:
            assert task_events.current_terminal_event(conn, identity.run_id) is None
        assert (await load_recovery_state(tmp_path, identity, provider))["agent_recovery_answered"] is True
        runtime.limits = RunLimits(deadline=time.monotonic()-1)
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            result = await runtime.run(identity, [], saver=saver, checkpoint_config=config, resume=True)
        assert result.status == "complete" and result.answer.content == "already accepted"
        assert model_calls == [True]

    asyncio.run(scenario())


def test_rqa_method_projection_retains_tool_pairing_and_nonmethod_evidence(tmp_path):
    import json
    from langchain_core.messages import ToolMessage

    from src.agent.installed_skills import RQA_ID, TurnSkills, project_rqa_history
    from src.research.result_tools import _shared_result_accessible

    parent = ExecutionIdentity(3, "scope-session", "parent-turn", "parent-run")
    current = ExecutionIdentity(3, "scope-session", "natural-turn", "natural-run")
    selected = TurnSkills.from_selection(tmp_path, [{"id": RQA_ID, "version": "a" * 40, "sha256": "f" * 64}])
    call_data = [("sdk", "read_file", {"file_path": f"/skills/{RQA_ID}/references/host-capabilities.md"}),
                 ("file", "workspace_read", {"path": f"/skills/{RQA_ID}/SKILL.md"}),
                 ("workspace", "research_workspace", {"operation": "read_skill_resource", "skill_id": RQA_ID}),
                 ("evidence", "deepseek_search", {"query": "fixture public question"}),
                 ("readback", "read_tool_result", {"tool_call_id": "receipt-workspace"})]
    request = AIMessage("", tool_calls=[{"name": name, "id": cid, "args": args} for cid, name, args in call_data])
    observations = [ToolMessage("prior method" if cid != "readback" else json.dumps({
        "tool_name": "research_workspace", "tool_result_ref": {"tool_call_id": "receipt-workspace"},
        "result_text": "prior method page"}), tool_call_id=cid, name=name,
        artifact={"result": "prior method bytes"}, additional_kwargs={**parent.message_metadata("tool"),
        "result_reference": {"tool_call_id": "receipt-" + cid}}) for cid, name, _ in call_data]
    original = tuple([request, *observations])
    outgoing = project_rqa_history(original, current, TurnSkills(tmp_path))
    assert outgoing[0] is request
    assert [item.tool_call_id for item in outgoing[1:]] == [item.tool_call_id for item in observations]
    assert outgoing[4] is observations[3]  # real public evidence remains as-is
    assert all(item.artifact is None and not item.additional_kwargs for item in (*outgoing[1:4], outgoing[5]))
    assert "prior method" in original[1].content and original[1].artifact
    assert project_rqa_history(original, current, selected, parent_run_id=parent.run_id) == original
    assert project_rqa_history(original, current, selected)[1].content == outgoing[1].content
    row = {"tool_name": "research_workspace", "result_text": json.dumps({"text": "legacy method page"}),
           "args_json": json.dumps({"operation": "read_skill_resource", "skill_id": RQA_ID})}
    pointer = {"tool_name": "read_tool_result"}
    outer = json.dumps({"tool_result_ref": {"tool_call_id": "receipt-workspace"}})
    fetch = lambda *_a, **_kw: row
    assert _shared_result_accessible(pointer, outer, fetch)
    assert not _shared_result_accessible(pointer, outer, fetch, allow_rqa=False)


def test_sdk_discovers_unselected_method_without_carrying_prior_method_bytes(tmp_path):
    import hashlib
    import json
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from src.agent.installed_skills import RQA_ID, TurnSkills, project_rqa_history
    from src.core.capability_policy import CapabilityPolicy

    entry = tmp_path / "skills" / RQA_ID / "SKILL.md"
    entry.parent.mkdir(parents=True)
    raw = f"---\nname: {RQA_ID}\ndescription: Exact method specimen.\n---\nScope specimen 9651.\n"
    entry.write_text(raw)
    version = "a" * 40
    sha = hashlib.sha256(raw.encode()).hexdigest()
    (tmp_path / "release.json").write_text(json.dumps({RQA_ID: version}))
    (tmp_path / "inventory.json").write_text(json.dumps({str(entry.relative_to(tmp_path)): sha}))
    selected = TurnSkills.from_selection(tmp_path, [{"id": RQA_ID, "version": version, "sha256": sha}])
    unselected = TurnSkills(tmp_path)

    async def scenario():
        parent = ExecutionIdentity(1, "thread", "first", "selected")
        current = ExecutionIdentity(1, "thread", "second", "unselected")
        saver = InMemorySaver()
        histories = []

        async def model(history, identity, **_):
            view = selected if identity == parent else unselected
            outgoing = project_rqa_history(history, identity, view)
            histories.append(outgoing)
            if identity == parent and len(histories) == 1:
                assert any("Exact method specimen" in str(message.content) for message in outgoing)
                return AIMessage("", tool_calls=[{"name": "read_file", "id": "rqa-native", "args": {
                    "file_path": f"/skills/{RQA_ID}/SKILL.md"}}])
            observations = [message for message in outgoing if isinstance(message, ToolMessage)]
            if identity == parent:
                assert any("Scope specimen 9651." in str(message.content) for message in observations)
            else:
                leaked = [(type(message).__name__, getattr(message, "name", ""), message.content,
                           message.additional_kwargs) for message in outgoing if "Scope specimen 9651." in str(message.content)]
                assert not leaked
                assert any("Exact method specimen" in str(message.content) for message in outgoing)
                assert all(message.additional_kwargs.get("skill", {}).get("name") != RQA_ID
                           for message in outgoing if isinstance(message, HumanMessage))
                assert any(message.tool_call_id == "rqa-native" for message in observations)
            return AIMessage("ordinary answer")

        async def execute(*_):
            raise AssertionError("Native Skill File call reached business dispatch")

        async def accept(*_):
            return FinalCheck()

        runtime = HarnessRuntime(ScriptedModel(script=model), execute, lambda *_: None, accept, RunLimits(3), [])
        config = {"thread_id": "same-thread"}
        for identity, view in ((parent, selected), (current, unselected)):
            result = await runtime.run(identity, [HumanMessage(identity.user_turn_id,
                additional_kwargs=identity.message_metadata("user"))], saver=saver, checkpoint_config=config,
                agent_options={"skill_root": tmp_path / "skills", "capability_policy": CapabilityPolicy(),
                               "turn_skills": view, "system_prompt": "Fixture"},
                pinned_skills=[RQA_ID] if identity == parent else [])
            assert result.status == "complete"
        assert len(histories) == 3

    asyncio.run(scenario())
