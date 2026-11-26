import asyncio
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage

from src.agent.identity import ExecutionIdentity
from src.agent.project_memory import AuthorizedProjectRules, ScopedProjectMemory


def test_official_memory_discards_stale_state_before_provider_send():
    identity = ExecutionIdentity(7, "session-a", "turn-a", "run-a")
    content = b"Approved templates for project Alpha."
    rules = AuthorizedProjectRules(7, "session-a", "project/alpha", 42,
                                   sha256(content).hexdigest(), content)
    scoped = ScopedProjectMemory(identity, rules)
    model = object()
    system = SystemMessage("System policy")
    runtime = SimpleNamespace(context=SimpleNamespace(identity=identity))
    stale_state = {"active_run_id": "run-a", "memory_contents": {"/team/beta/AGENTS.md": "private B"}}
    request = ModelRequest(model=model, system_message=system, state=stale_state, runtime=runtime, messages=[])
    assert scoped.modify_request(request) is request
    loaded = asyncio.run(scoped.abefore_agent(stale_state, runtime, {}))
    assert loaded == {"memory_contents": {"/project/alpha/v42/AGENTS.md": content.decode()}}
    assert scoped.before_agent(stale_state, runtime, {}) == loaded
    pinned = ModelRequest(model=model, system_message=system, state={**stale_state, **loaded},
                          runtime=runtime, messages=[])
    final = scoped.modify_request(pinned).system_message
    assert "Approved templates" in final.text and "System policy" in final.text
    assert "private B" not in final.text and "edit_file" not in final.text
    for other in (replace(identity, owner_user_id=8), replace(identity, session_id="session-b"),
                  replace(identity, run_id="run-b")):
        wrong = SimpleNamespace(context=SimpleNamespace(identity=other))
        assert scoped.before_agent(pinned.state, wrong, {}) == {"memory_contents": {}}
        wrong_request = ModelRequest(model=model, system_message=system, state=pinned.state,
                                     runtime=wrong, messages=[])
        assert scoped.modify_request(wrong_request) is wrong_request
    for changes in ({"owner_user_id": 8}, {"session_id": "session-b"}, {"namespace": "../AGENTS.md"},
                    {"namespace": None}, {"version": 0}, {"content": b"changed rules"}):
        with pytest.raises(ValueError, match="binding"):
            ScopedProjectMemory(identity, replace(rules, **changes))



def test_framework_checkpoint_does_not_keep_an_old_project(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
    from src.agent.deep_agent_runtime import create_framework_agent
    from src.agent.framework_test_support import ScriptedModel
    from src.agent.harness import HarnessState, _RunContext

    one = ExecutionIdentity(7, "session-a", "turn-a", "run-a")
    two = replace(one, user_turn_id="turn-b", run_id="run-b")
    provider = ("https://test.invalid/v1", "fixture")
    cfg = {"configurable": framework_checkpoint_scope(one, provider)}
    sent = []

    async def model(messages, identity, **_):
        sent.append("\n".join(msg.text for msg in messages if isinstance(msg, SystemMessage)))
        return AIMessage("done", additional_kwargs=identity.message_metadata("assistant"))

    async def ask(identity, name, content):
        rules = AuthorizedProjectRules(7, "session-a", "project/" + name, 1, sha256(content).hexdigest(), content)
        async with open_framework_checkpoint(tmp_path, identity, provider) as saver:
            graph = create_framework_agent(ScriptedModel(script=model), [], [ScopedProjectMemory(identity, rules)],
                                           saver, HarnessState, context_schema=_RunContext, task_subagent=False)
            await graph.ainvoke({"active_run_id": identity.run_id, "messages": [HumanMessage("check")]},
                                cfg, context=_RunContext(identity), durability="sync")
            return (await graph.aget_state(cfg)).values["memory_contents"]

    async def scenario():
        assert set(await ask(one, "alpha", b"Project Alpha rules only")) == {"/project/alpha/v1/AGENTS.md"}
        assert set(await ask(two, "beta", b"Project Beta rules only")) == {"/project/beta/v1/AGENTS.md"}
        assert "Project Alpha rules only" in sent[0] and "Project Beta rules only" not in sent[0]
        assert "Project Beta rules only" in sent[1] and "Project Alpha rules only" not in sent[1]

    asyncio.run(scenario())
