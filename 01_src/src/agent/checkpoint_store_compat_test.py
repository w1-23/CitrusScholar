"""Public LangGraph APIs across the encrypted checkpoint version boundary."""
from __future__ import annotations

import asyncio
from collections import defaultdict
import sqlite3
from typing import Annotated, TypedDict

import aiosqlite
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, message_to_dict
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, Overwrite, Send, interrupt
import pytest

from src.agent.checkpoint_store import (
    CheckpointProtectionError, ProtectedCheckpointSerializer, _AesGcm,
    _framework_binding, _prepare_database, framework_checkpoint_path,
    framework_checkpoint_scope, open_framework_checkpoint,
)
from src.agent.batch_journal import BatchObservationNotStored
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import AdmissionStopped
from src.core.provider_protocol import _DeepSeekMessage, _DeepSeekMessageChunk, _ProtocolState

_SCOPE = ("https://api.deepseek.com/v1", "deepseek-fixture")
_IDENTITY = ExecutionIdentity(12, "storage-compat", "turn-1", "job-1")
_REASONING = "protected non-ASCII reasoning 证据"
_LEGACY_TAG = "__citrus_checkpoint_v1__"
_LEGACY_TYPE = "citrus-checkpoint-v1+aesgcm-v1"
_NATIVE_TYPE = "citrus-checkpoint-v2+aesgcm-v1"


def _append(left, writes):
    return [*left, *(message for batch in writes for message in batch)]


def _state_graph():
    class State(TypedDict):
        messages: Annotated[list, DeltaChannel(_append, snapshot_frequency=2)]
        stage: str

    graph = StateGraph(State)
    graph.add_node("private_model", lambda _: {"messages": [_private(_DeepSeekMessage)]})
    graph.add_node("mark", lambda _: {"stage": "ready"})
    graph.add_node("prepare_pause", lambda _: {"stage": "waiting"})
    graph.add_node("pause", lambda _: {"messages": [AIMessage(interrupt("wait"))]})
    graph.add_edge(START, "private_model")
    graph.add_edge("private_model", "mark")
    graph.add_edge("mark", "prepare_pause")
    graph.add_edge("prepare_pause", "pause")
    graph.add_edge("pause", END)
    return graph


def _private(cls):
    return cls("assistant", provider_protocol=_ProtocolState(_SCOPE, _REASONING))


def _public_clone(message):
    cls = AIMessageChunk if type(message) is _DeepSeekMessageChunk else AIMessage
    return cls(**message.model_dump())


def test_official_checkpoint_codec_keeps_private_state_and_native_wrappers_scoped():
    serializer = ProtectedCheckpointSerializer(b"s" * 32, _IDENTITY, _SCOPE)
    private = _private(_DeepSeekMessage)
    chunk = _private(_DeepSeekMessageChunk)
    value = {"message": private, "chunk": chunk, "pending": [Send("node", private),
        Interrupt({"message": chunk}, id="interrupt"), Overwrite([private])]}
    kind, data = serializer.dumps_typed(value)
    assert kind == _NATIVE_TYPE
    assert _REASONING.encode() not in data
    assert serializer.loads_typed((kind, data)) == value
    for message in (private, chunk):
        assert _REASONING not in str(message.model_dump())
        assert _REASONING not in message.model_dump_json()
        assert _REASONING not in str(message_to_dict(message))
        assert _REASONING not in str(message.to_json())
        assert _public_clone(message).content == message.content
    other = ProtectedCheckpointSerializer(b"s" * 32, _IDENTITY, (_SCOPE[0], "other-model"))
    with pytest.raises(CheckpointProtectionError, match="checkpoint_authentication_failed"):
        other.loads_typed((kind, data))
    with pytest.raises(CheckpointProtectionError, match="private_protocol_scope_mismatch"):
        other.dumps_typed(value)
    for label in ("pickle", "json", "msgpack", "pickle+aesgcm-v1"):
        with pytest.raises(CheckpointProtectionError, match="encrypted_checkpoint_required"):
            serializer.loads_typed((label, data))


class _LegacyFixtureCodec:
    """Emit the deployed Citrus v1 wire format from official graph checkpoints."""

    def __init__(self, binding):
        self.binding = binding
        self.delegate = JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[], allowed_msgpack_modules=[])

    def _value(self, value):
        if type(value) is dict:
            return {key: self._value(child) for key, child in value.items()}
        if type(value) is list:
            return [self._value(child) for child in value]
        if type(value) is tuple:
            return {_LEGACY_TAG: "tuple", "items": [self._value(child) for child in value]}
        if type(value) in (AIMessage, HumanMessage):
            return {_LEGACY_TAG: "message", "class": type(value).__name__, "public": self._value(value.model_dump())}
        if type(value) is _DeepSeekMessage:
            state = value.provider_protocol
            return {_LEGACY_TAG: "deepseek", "public": self._value(value.model_dump()),
                    "private": {"scope": list(state.scope), "reasoning": state.reasoning}}
        if type(value) is Interrupt:
            return Interrupt(self._value(value.value), id=value.id, response_schema=value.response_schema)
        if isinstance(value, tuple):
            # The graph, not the test, created the SDK snapshot. Read the
            # accumulated state through the documented channel interface.
            seed = DeltaChannel(_append).from_checkpoint(value).get()
            return {_LEGACY_TAG: "delta_snapshot", "value": self._value(seed)}
        return value

    def dumps_typed(self, value):
        typ, data = self.delegate.dumps_typed({"version": 1, "binding": self.binding, "value": self._value(value)})
        assert typ == "msgpack"
        return "citrus-checkpoint-v1", data


def _db_checkpoint_rows(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT checkpoint_id,type,checkpoint,metadata FROM checkpoints ORDER BY checkpoint_id")}


def test_v1_snapshot_seed_reopens_read_only_and_resumes_without_rewriting(tmp_path):
    async def scenario():
        config = {"configurable": framework_checkpoint_scope(_IDENTITY, _SCOPE)}
        builder = _state_graph()
        fixture = InMemorySaver(serde=ProtectedCheckpointSerializer(b"x" * 32, _IDENTITY, _SCOPE))
        original = builder.compile(checkpointer=fixture)
        assert (await original.ainvoke({"messages": [HumanMessage("input")]}, config, durability="sync"))["__interrupt__"]
        expected = await original.aget_state(config)
        assert expected.values["messages"][-1].provider_protocol.reasoning == _REASONING
        snapshots = [snap async for snap in fixture.alist(config)]
        assert any("messages" in snap.checkpoint["channel_values"] for snap in snapshots[1:])
        database, key = _prepare_database(framework_checkpoint_path(tmp_path, _IDENTITY, _SCOPE))
        binding = _framework_binding(_IDENTITY, _SCOPE)
        async with aiosqlite.connect(str(database)) as connection:
            legacy = AsyncSqliteSaver(connection, serde=EncryptedSerializer(_AesGcm(key, binding), _LegacyFixtureCodec(binding)))
            for snap in reversed(snapshots):
                await legacy.aput(snap.parent_config or config, snap.checkpoint, snap.metadata, {})
                task_writes = defaultdict(list)
                for task_id, channel, value in snap.pending_writes:
                    task_writes[task_id].append((channel, value))
                for task_id, writes in task_writes.items():
                    await legacy.aput_writes(snap.config, writes, task_id)
        originals = _db_checkpoint_rows(database)
        assert all(row[0] == _LEGACY_TYPE for row in originals.values())
        before = {file: file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()}
        async with open_framework_checkpoint(tmp_path, _IDENTITY, _SCOPE, read_only=True) as readonly:
            graph = builder.compile(checkpointer=readonly)
            recovered = await graph.aget_state(config)
            assert recovered.values == expected.values
            assert recovered.interrupts == expected.interrupts
            history = await readonly.aget_delta_channel_history(config=recovered.config, channels=["messages"])
            assert history["messages"]["seed"] == expected.values["messages"]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await readonly.aput(config, snapshots[0].checkpoint, {}, {})
        assert before == {file: file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()}
        async with open_framework_checkpoint(tmp_path, _IDENTITY, _SCOPE) as writer:
            resumed = await builder.compile(checkpointer=writer).ainvoke(Command(resume="finished"), config, durability="sync")
            assert resumed["messages"][:-1] == expected.values["messages"]
            assert resumed["messages"][-1].content == "finished"
        after = _db_checkpoint_rows(database)
        assert {key: after[key] for key in originals} == originals
        assert any(row[0] == _NATIVE_TYPE for row in after.values())

    asyncio.run(scenario())


def test_legacy_delta_outside_proven_channel_fails_closed():
    binding = _framework_binding(_IDENTITY, _SCOPE)
    key = b"k" * 32
    delegate = JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=[], allowed_msgpack_modules=[])
    _, payload = delegate.dumps_typed({"version": 1, "binding": binding,
        "value": {_LEGACY_TAG: "delta_snapshot", "value": ["not a framework seed"]}})
    _, data = _AesGcm(key, binding).encrypt(payload)
    protected = ProtectedCheckpointSerializer(key, _IDENTITY, _SCOPE, context_binding=binding)
    with pytest.raises(CheckpointProtectionError, match="legacy_delta_requires_reconciliation"):
        protected.loads_typed((_LEGACY_TYPE, data))


def test_framework_error_storage_never_persists_exception_payload(tmp_path):
    async def scenario():
        async with open_framework_checkpoint(tmp_path, _IDENTITY, _SCOPE) as saver:
            label, data = saver.serde.dumps_typed(RuntimeError(_REASONING))
            assert _REASONING.encode() not in data
            restored = saver.serde.loads_typed((label, data))
            assert type(restored) is AdmissionStopped
            assert restored.reason == "checkpoint_node_error"
            preserved = saver.serde.loads_typed(saver.serde.dumps_typed(BatchObservationNotStored(effects_uncertain=False)))
            assert type(preserved) is BatchObservationNotStored
            assert preserved.effects_uncertain is False
            config = {"configurable": {**framework_checkpoint_scope(_IDENTITY, _SCOPE), "injected": _REASONING}}
            await saver.aput(config, empty_checkpoint(),
                {"source": "input", "step": 1, "parents": {"injected": _REASONING}, "injected": _REASONING}, {})
        checkpoint, = _db_checkpoint_rows(framework_checkpoint_path(tmp_path, _IDENTITY, _SCOPE)).values()
        assert _REASONING.encode() not in checkpoint[1]
        assert checkpoint[2] == b'{"source": "input", "step": 1, "parents": {}}'
    asyncio.run(scenario())
