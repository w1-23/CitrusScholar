import asyncio
from langchain_core.messages import AIMessage, HumanMessage, add_notification_metadata

from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
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
        options = {"skill_root": skill_root, "system_prompt": "fixture prompt"}
        calls = []

        async def model(history, identity, *, native_schemas):
            calls.append(identity.run_id)
            if identity.run_id == "cancelled":
                raise asyncio.CancelledError()
            return AIMessage(content="new answer")

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(model, None, lambda *args: None, accept, RunLimits(), [],
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


def test_retired_skills_are_only_readable_by_historical_execution(tmp_path, monkeypatch):
    import hashlib
    import json
    import pytest

    from src.agent import research_skill_catalog
    from src.agent.skill_adapter import SkillSession, scenario_catalog, validate_scenario_selection
    from src.agent.workspace_projection import resources
    from src.research import workspace_tools

    legacy = dict(zip(('id', 'version', 'sha256'),
                      ('candidate-search', *research_skill_catalog.PINNED['candidate-search'])))
    identity = ExecutionIdentity(1, 'skill-test-session', 'skill-test-turn', 'skill-test-run')
    monkeypatch.delenv('CITRUS_SKILL_RELEASE', raising=False)
    assert scenario_catalog() == []
    assert list(resources('/skills')) == []
    assert not (research_skill_catalog.ROOT / 'skills').exists()
    assert not (research_skill_catalog.ROOT / '01_src/src/prompts/skills').exists()
    with pytest.raises(ValueError, match='selected_skill_identity_mismatch'):
        SkillSession(identity, set).page(legacy['id'], legacy['version'], legacy['sha256'])
    archive = SkillSession(identity, set, allow_archived=True)
    assert archive.page(legacy['id'], legacy['version'], legacy['sha256'])['page']['text']

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
    assert [row['id'] for row in call(operation='skills')[1]['skills']] == [new_id]
    assert call(operation='read_skill', skill_id='candidate-search')[1]['error'] == 'skill_resource_unavailable'
    assert call(operation='read_skill', skill_id=new_id)[1]['entrypoint']['text'] == content.decode()
    assert call(operation='read_skill_resource', skill_id=new_id, resource='unlisted.md')[1]['error'] == 'skill_resource_unavailable'


def test_framework_seed_uses_parallel_toolnode_without_charging_model_steps(tmp_path):
    async def scenario():
        from langchain_core.messages import ToolMessage
        from src.core.provider_protocol import _DeepSeekMessage, _ProtocolState
        from src.agent.recovery import _checkpoint_reader

        i = ExecutionIdentity(1, "s", "t", "j")
        provider = ("https://api.deepseek.com/v1", "deepseek-fixture")
        admitted = asyncio.Event()
        entered, model_calls = set(), []
        names = {"local_hybrid_search", "deepseek_search"}
        proposal = [{"name": name, "id": name, "args": {}} for name in sorted(names)]

        async def seed(history):
            return proposal, "Read only."

        async def execute(call, _):
            entered.add(call["name"])
            if entered == names:
                admitted.set()
            await asyncio.wait_for(admitted.wait(), timeout=2)
            return ToolMessage("real observation", name=call["name"], tool_call_id=call["id"]), {}

        async def model(history, _, **kwargs):
            model_calls.append(history)
            assert {m.tool_call_id for m in history if isinstance(m, ToolMessage)} == names
            assert any(m.content == "Read only." for m in history)
            assert any(isinstance(m, _DeepSeekMessage) and m.provider_protocol.reasoning == ""
                       for m in history)
            return AIMessage("answered")

        async def accept(*args):
            return FinalCheck()

        schemas = [{"function": {"name": name, "description": name, "parameters": {"type": "object"}}}
                   for name in names]
        runtime = HarnessRuntime(model, execute, lambda *_: None, accept, RunLimits(1), schemas,
            seed_plan=seed, seed_message=lambda proposal, metadata: _DeepSeekMessage(
                "", tool_calls=proposal, additional_kwargs=metadata,
                provider_protocol=_ProtocolState(provider, "")))
        async with open_framework_checkpoint(tmp_path, i, provider) as saver:
            config = framework_checkpoint_scope(i, provider)
            result = await runtime.run(i, [HumanMessage("request", additional_kwargs=i.message_metadata("user"))],
                                       saver=saver, checkpoint_config=config)
            snapshot = await _checkpoint_reader(saver).aget_state({"configurable": config})
            stored = next(m for m in snapshot.values["messages"] if isinstance(m, _DeepSeekMessage))
            assert stored.provider_protocol.reasoning == ""
        assert result.status == "complete" and result.model_steps == len(model_calls) == 1
        assert entered == names

    asyncio.run(scenario())


def test_framework_interrupt_parks_after_pair_and_reopens_without_redispatch(tmp_path, monkeypatch):
    from langchain_core.messages import ToolMessage
    from src.agent.recovery import load_recovery_state
    from src.core import jobs, task_events
    from dataclasses import asdict
    import pytest

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def scenario():
        provider = ("https://example.test", "fixture")
        job = jobs.claim_job("session", "turn", owner_user_id=1, client_request_id="client", payload={})
        i = ExecutionIdentity(1, "session", "turn", job.job_id)
        original = {"user_id": 1, "session_id": "session", "request_id": "turn", "job_id": job.job_id,
                    "query": "request", "client_request_id": "client", "idempotency_key": "key"}
        wait = "a" * 32
        calls, acknowledgments = [], []
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
            return ("receipt ready",)

        async def acknowledge(_, waits, checkpoint):
            assert waits == (wait,)
            acknowledgments.append(checkpoint)

        async def accept(*args):
            return FinalCheck()

        schemas = [{"function": {"name": proposal["name"], "description": "Fixture",
                                 "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(model, execute, lambda *_: None, accept, RunLimits(), schemas,
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
        async with open_framework_checkpoint(tmp_path, i, provider) as saver:
            result = await runtime.run(i, [], saver=saver, checkpoint_config=config, resume=True)
        assert result.status == "complete" and result.model_steps == 2
        assert calls == ["tool-1"] and len(acknowledgments) == 1
        assert acknowledgments[0] != checkpoint

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
        runtime = HarnessRuntime(model, operation, lambda *_: None, not_final, RunLimits(), schemas,
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


def test_only_authenticated_answered_question_child_can_start_new_turn():
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from src.agent.runtime import AdmissionStopped
    import pytest

    async def run():
        parent = ExecutionIdentity(1, "s", "t1", "parent")
        child = ExecutionIdentity(1, "s", "t2", "child")
        calls = []
        question = {"name": "request_user_input", "id": "ask", "args": {}}

        async def model(history, identity, **kwargs):
            if identity == parent:
                return AIMessage("", tool_calls=[question])
            assert any(m.content == "my answer" for m in history)
            return AIMessage("final")

        async def execute(call, _):
            calls.append(call["id"])
            return ToolMessage("question recorded", name=call["name"], tool_call_id=call["id"]), {"pending_question": True}

        async def accept(*args):
            return FinalCheck()

        schemas = [{"function": {"name": question["name"], "description": "Fixture question",
                                 "parameters": {"type": "object"}}}]
        runtime = HarnessRuntime(model, execute, lambda *_: None, accept, RunLimits(), schemas)
        saver = InMemorySaver()
        config = {"thread_id": "same-session"}
        assert (await runtime.run(parent, [HumanMessage("request")], saver=saver,
                                  checkpoint_config=config)).status == "waiting"
        with pytest.raises(AdmissionStopped, match="native_thread_requires_reconciliation"):
            await runtime.run(child, [HumanMessage("arbitrary user input")], saver=saver, checkpoint_config=config)
        runtime.authorized_question_parent = parent.run_id
        result = await runtime.run(child, [HumanMessage("my answer")], saver=saver, checkpoint_config=config)
        assert result.status == "complete" and calls == ["ask"]

    asyncio.run(run())
