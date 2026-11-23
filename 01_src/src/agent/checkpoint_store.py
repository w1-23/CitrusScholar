"""Protected, execution-bound LangGraph checkpoints (never public history).

The caller supplies an admitted identity, provider scope and runtime state root.
Reopening requires the SAME run identity; cross-run recovery/admission belongs to
execution policy. Only synthetic tests import this module without application config.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, replace
from datetime import date
from functools import lru_cache
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePath
import re
import stat
import tempfile
from typing import Any, AsyncIterator

import aiosqlite
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from langchain_core.messages import (
    AIMessage, AIMessageChunk, BaseMessage, ChatMessage, ChatMessageChunk,
    FunctionMessage, FunctionMessageChunk, HumanMessage, HumanMessageChunk,
    RemoveMessage, SystemMessage, SystemMessageChunk, ToolMessage, ToolMessageChunk,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
# DeepAgentState uses this private LangGraph snapshot type; pin the SDK version.
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Interrupt, Send

from src.agent.identity import ExecutionIdentity
from src.core.provider_protocol import _DeepSeekMessage, _DeepSeekMessageChunk, _ProtocolState

import logging

logger = logging.getLogger(__name__)

_TAG = "__citrus_checkpoint_v1__"
_FORMAT = "citrus-checkpoint-v1"
_CIPHER = "aesgcm-v1"
WORKING_CONTEXT_ARCHIVE_NAMESPACE = "working-context-archive-v1"
_MESSAGES = (
    AIMessage, AIMessageChunk, ChatMessage, ChatMessageChunk,
    FunctionMessage, FunctionMessageChunk, HumanMessage, HumanMessageChunk,
    RemoveMessage, SystemMessage, SystemMessageChunk, ToolMessage, ToolMessageChunk,
)


class CheckpointProtectionError(ValueError):
    """A checkpoint cannot safely be written or resumed; contains no payload data."""

    def __init__(self, reason: str, *, structure: str = "unknown", value_kind: str = "unknown"):
        super().__init__(reason)
        self.structure = structure
        self.value_kind = value_kind


# Diagnostic fields are fixed independently of exception text, checkpoint keys,
# graph topology, provider identifiers and user-controlled data.
_CHECKPOINT_REASONS = {
    "checkpoint_identity_required": "checkpoint_binding_mismatch",
    "checkpoint_binding_mismatch": "checkpoint_binding_mismatch",
    "checkpoint_provider_scope_required": "checkpoint_provider_scope_mismatch",
    "private_protocol_scope_mismatch": "checkpoint_provider_scope_mismatch",
    "checkpoint_provider_scope_mismatch": "checkpoint_provider_scope_mismatch",
    "checkpoint_authentication_failed": "checkpoint_authentication_failed",
    "private_protocol_state_missing": "private_protocol_state_missing",
    "invalid_legacy_tool_call": "invalid_legacy_tool_call",
    "unsupported_checkpoint_value": "unsupported_checkpoint_value",
    "unsupported_checkpoint_key": "unsupported_checkpoint_value",
    "nested_message_in_public_fields": "unsupported_public_message_field",
    "private_state_in_public_fields": "unsupported_public_message_field",
    "unsupported_public_message_field": "unsupported_public_message_field",
    "unsafe_checkpoint_directory": "checkpoint_storage_rejected",
    "unsafe_checkpoint_key": "checkpoint_key_rejected",
    "unsafe_checkpoint_key_permissions": "checkpoint_key_rejected",
    "invalid_checkpoint_key": "checkpoint_key_rejected",
    "checkpoint_key_missing": "checkpoint_key_rejected",
    "unsafe_checkpoint_database": "checkpoint_storage_rejected",
    "unsafe_checkpoint_database_permissions": "checkpoint_storage_rejected",
}
_CHECKPOINT_STRUCTURES = frozenset({
    "envelope", "envelope.binding", "envelope.value", "value.tag",
    "value.mapping_key", "message.public", "message.public.tool_calls", "message.private", "ciphertext",
})

_CHECKPOINT_VALUE_KINDS = frozenset({"path", "date", "message", "other"})


def _value_kind(value: Any) -> str:
    if isinstance(value, PurePath):
        return "path"
    if isinstance(value, date):
        return "date"
    if isinstance(value, BaseMessage):
        return "message"
    return "other"


def _admission_stopped_type():
    """Closed allowlist: the only application exception a graph node raises
    as ordinary control flow, and the one LangGraph stores in pending writes
    so a resumed task can re-raise it. Imported lazily to avoid a cycle."""
    from .runtime import AdmissionStopped
    return AdmissionStopped


def _batch_observation_type():
    """Closed allowlist: the journal refuses to store an observation once its
    effects may have run, and LangGraph keeps that refusal in pending writes
    so a resumed task re-raises it instead of replaying the work."""
    from .batch_journal import BatchObservationNotStored
    return BatchObservationNotStored
def _admission_reason(value: Any) -> str:
    reason = getattr(value, "reason", None)
    if type(reason) is not str or not reason or len(reason) > 200 or any(ord(c) < 32 for c in reason):
        return ""
    return reason


@lru_cache(maxsize=1)
def _dependency_versions() -> dict[str, str]:
    return {name.replace("-", "_") + "_version": version(name) for name in (
        "langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core",
    )}


def checkpoint_diagnostic(error: CheckpointProtectionError) -> dict:
    reason = error.args[0] if error.args and type(error.args[0]) is str else None
    return {
        "reason": _CHECKPOINT_REASONS.get(reason, "invalid_checkpoint_payload"),
        "structure": error.structure if error.structure in _CHECKPOINT_STRUCTURES else "unknown",
        "value_kind": error.value_kind if error.value_kind in _CHECKPOINT_VALUE_KINDS else "unknown",
        "schema_version": 1,
    }


@contextmanager
def _checkpoint_operation(stage: str, namespace: str = ""):
    try:
        yield
    except CheckpointProtectionError as error:
        try:
            from src.core.diag import diag
            kind = {"": "execution", WORKING_CONTEXT_ARCHIVE_NAMESPACE: "working_context"}.get(
                namespace, "graph_subspace")
            image = os.environ.get("CITRUSQA_APP_IMAGE_SHA256", "")
            diag("checkpoint_protection_failed",
                 stage=stage, namespace=kind, **checkpoint_diagnostic(error),
                 key_slot="checkpoint_storage_v1",
                 app_image_sha256=image if re.fullmatch(r"[a-f0-9]{64}", image) else None,
                 **_dependency_versions())
        except Exception:
            pass  # Diagnostics must never replace the protection exception.
        raise


def _binding(identity: ExecutionIdentity, provider_scope: tuple[str, str]) -> dict:
    if not isinstance(identity, ExecutionIdentity):
        raise CheckpointProtectionError("checkpoint_identity_required")
    if (type(provider_scope) is not tuple or len(provider_scope) != 2
            or any(not isinstance(v, str) or not v or v != v.strip()
                   or any(ord(c) < 32 for c in v) for v in provider_scope)):
        raise CheckpointProtectionError("checkpoint_provider_scope_required")
    return {**asdict(identity), "provider": provider_scope[0], "model": provider_scope[1]}


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def checkpoint_scope(identity: ExecutionIdentity, provider_scope: tuple[str, str]) -> dict:
    """Stable LangGraph configurable mapping; no user-controlled filesystem names."""
    digest = hashlib.sha256(_canonical(_binding(identity, provider_scope))).hexdigest()
    return {"thread_id": digest, "checkpoint_ns": ""}


def checkpoint_path(root: Path, identity: ExecutionIdentity,
                    provider_scope: tuple[str, str]) -> Path:
    return Path(root) / "checkpoints" / checkpoint_scope(identity, provider_scope)["thread_id"] / "checkpoint.sqlite3"


class _PrivateMessages(SerializerProtocol):
    """Transform private messages explicitly; delegate ordinary types to JsonPlus.

    No application class is in the revival allowlist. Only the built-in safe
    JsonPlus types are enabled, pickle/legacy JSON decoding are never accepted.
    Tuple tags preserve tuples (JsonPlus otherwise projects them into lists).
    """

    def __init__(self, binding: dict):
        self.binding = binding
        self.scope = (binding["provider"], binding["model"])
        self.delegate = JsonPlusSerializer(
            pickle_fallback=False, allowed_json_modules=[], allowed_msgpack_modules=[])

    def _state(self, value: Any) -> _ProtocolState:
        if (type(value) is not dict or set(value) != {"scope", "reasoning"}
                or not isinstance(value["reasoning"], str)):
            raise CheckpointProtectionError("private_protocol_state_missing", structure="message.private")
        if value["scope"] != list(self.scope):
            raise CheckpointProtectionError("private_protocol_scope_mismatch", structure="message.private")
        return _ProtocolState(self.scope, value["reasoning"])

    @staticmethod
    def _restore_tool_calls(public: dict) -> dict:
        calls = public.get("tool_calls")
        if calls is None:
            return public
        native = {"id", "name", "args", "type"}
        legacy = {"_host_logical_key", "_attempt"}
        if type(calls) is not list:
            raise CheckpointProtectionError("invalid_legacy_tool_call", structure="message.public.tool_calls")
        restored = []
        for call in calls:
            if type(call) is not dict or set(call) - native - legacy:
                raise CheckpointProtectionError("invalid_legacy_tool_call", structure="message.public.tool_calls")
            if "_host_logical_key" in call:
                key = call["_host_logical_key"]
                if (type(key) is not str or len(key) != 37 or not key.startswith("host:")
                        or any(char not in "0123456789abcdef" for char in key[5:])):
                    raise CheckpointProtectionError("invalid_legacy_tool_call", structure="message.public.tool_calls")
            if "_attempt" in call and ("_host_logical_key" not in call or type(call["_attempt"]) is not int
                                       or not 1 <= call["_attempt"] <= 2**31 - 1):
                raise CheckpointProtectionError("invalid_legacy_tool_call", structure="message.public.tool_calls")
            if set(call) & legacy and (type(call.get("id")) is not str or not call["id"]
                                      or type(call.get("name")) is not str or not call["name"]
                                      or type(call.get("args")) is not dict):
                raise CheckpointProtectionError("invalid_legacy_tool_call", structure="message.public.tool_calls")
            restored.append({key: value for key, value in call.items() if key in native})
        return {**public, "tool_calls": restored}

    def _walk(self, value: Any, *, reading: bool = False) -> Any:
        if type(value) is _DeltaSnapshot:
            if reading:
                raise CheckpointProtectionError("unexpected_delta_snapshot_revival")
            # Recurse before serialization so private protocol fields survive
            # inside snapshots without enabling JsonPlus class revival.
            return {_TAG: "delta_snapshot", "value": self._walk(value.value)}
        if type(value) in (_DeepSeekMessage, _DeepSeekMessageChunk):
            if reading:
                raise CheckpointProtectionError("unexpected_private_message_revival")
            state = value.provider_protocol
            private = None if state is None else {"scope": list(state.scope), "reasoning": state.reasoning}
            self._state(private)
            for name, field in value.__dict__.items():
                if name != "provider_protocol":
                    self._check_public_field(field)
            return {_TAG: "deepseek_chunk" if type(value) is _DeepSeekMessageChunk else "deepseek",
                    "public": self._walk(value.model_dump()), "private": private}
        if type(value) is dict:
            if _TAG in value:
                if not reading:
                    raise CheckpointProtectionError("reserved_checkpoint_tag")
                kind = value[_TAG]
                if kind == "delta_snapshot" and set(value) == {_TAG, "value"}:
                    return _DeltaSnapshot(self._walk(value["value"], reading=True))
                if kind == "bytearray" and set(value) == {_TAG, "value"} and type(value["value"]) is bytes:
                    return bytearray(value["value"])
                if kind in ("tuple", "set", "frozenset") and set(value) == {_TAG, "items"} and type(value["items"]) is list:
                    cls = {"tuple": tuple, "set": set, "frozenset": frozenset}[kind]
                    return cls(self._walk(v, reading=True) for v in value["items"])
                if kind == "message" and set(value) == {_TAG, "class", "public"}:
                    classes = {cls.__name__: cls for cls in _MESSAGES}
                    cls = classes.get(value["class"])
                    public = self._walk(value["public"], reading=True)
                    if cls is None or type(public) is not dict:
                        raise CheckpointProtectionError("invalid_public_message")
                    self._check_public_field(public)
                    if issubclass(cls, AIMessage):
                        public = self._restore_tool_calls(public)
                    return cls(**public)
                if (kind == "batch_observation_not_stored"
                        and set(value) == {_TAG, "effects_uncertain"}):
                    uncertain = value["effects_uncertain"]
                    if type(uncertain) is not bool:
                        raise CheckpointProtectionError("invalid_checkpoint_payload",
                                                        structure="value.tag")
                    return _batch_observation_type()(effects_uncertain=uncertain)
                if kind == "admission_stopped" and set(value) == {_TAG, "reason"}:
                    reason = value["reason"]
                    if type(reason) is not str or not reason or len(reason) > 200:
                        raise CheckpointProtectionError("invalid_checkpoint_payload",
                                                        structure="value.tag")
                    return _admission_stopped_type()(reason)
                if kind in ("deepseek", "deepseek_chunk") and set(value) == {_TAG, "public", "private"}:
                    state = self._state(value["private"])
                    public = self._walk(value["public"], reading=True)
                    if type(public) is not dict or "provider_protocol" in public:
                        raise CheckpointProtectionError("invalid_private_message_public_fields")
                    cls = _DeepSeekMessage if kind == "deepseek" else _DeepSeekMessageChunk
                    self._check_public_field(public)
                    public = self._restore_tool_calls(public)
                    try:
                        return cls(**public, provider_protocol=state)
                    except Exception:
                        raise CheckpointProtectionError("invalid_private_message") from None
                raise CheckpointProtectionError("invalid_checkpoint_tag", structure="value.tag")
            if any(type(k) not in (str, int, float, bool, bytes, type(None)) for k in value):
                raise CheckpointProtectionError("unsupported_checkpoint_key", structure="value.mapping_key")
            return {k: self._walk(v, reading=reading) for k, v in value.items()}
        if type(value) is tuple:
            if reading:
                return tuple(self._walk(v, reading=True) for v in value)
            return {_TAG: "tuple", "items": [self._walk(v) for v in value]}
        if type(value) in (set, frozenset) and not reading:
            return {_TAG: type(value).__name__, "items": [self._walk(v) for v in value]}
        if type(value) in (list, set, frozenset):
            return type(value)(self._walk(v, reading=reading) for v in value)
        if type(value) is Send:
            return Send(value.node, self._walk(value.arg, reading=reading), timeout=value.timeout)
        if type(value) is Interrupt:
            return replace(value, value=self._walk(value.value, reading=reading))
        if type(value) in _MESSAGES:
            # Public message fields may contain data, but never embedded private
            # message instances whose excluded fields model_dump would discard.
            for field in value.__dict__.values():
                self._check_public_field(field)
            if reading:
                return value  # Compatibility with earlier public-message checkpoints.
            return {_TAG: "message", "class": type(value).__name__,
                    "public": self._walk(value.model_dump())}
        if type(value) is bytearray and not reading:
            return {_TAG: "bytearray", "value": bytes(value)}
        if value is None or type(value) in (str, bool, int, float, bytes, bytearray):
            return value
        if type(value) is _batch_observation_type():
            uncertain = getattr(value, "effects_uncertain", None)
            return {_TAG: "batch_observation_not_stored",
                    "effects_uncertain": uncertain if type(uncertain) is bool else True}
        if type(value) is _admission_stopped_type():
            reason = _admission_reason(value)
            if reason:
                return {_TAG: "admission_stopped", "reason": reason}
        logger.warning("checkpoint value unsupported: %s", type(value).__name__)
        raise CheckpointProtectionError("unsupported_checkpoint_value", structure="envelope.value",
                                        value_kind=_value_kind(value))

    def _check_public_field(self, value: Any) -> None:
        if isinstance(value, BaseMessage):
            raise CheckpointProtectionError("nested_message_in_public_fields", structure="message.public",
                                            value_kind="message")
        if type(value) is dict:
            if _TAG in value or "provider_protocol" in value:
                raise CheckpointProtectionError("private_state_in_public_fields", structure="message.public")
            for child in value.values():
                self._check_public_field(child)
        elif type(value) in (list, tuple, set, frozenset):
            for child in value:
                self._check_public_field(child)
        elif value is not None and type(value) not in (str, bool, int, float, bytes, bytearray):
            raise CheckpointProtectionError("unsupported_public_message_field", structure="message.public",
                                            value_kind=_value_kind(value))

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        envelope = {"version": 1, "binding": self.binding, "value": self._walk(obj)}
        typ, payload = self.delegate.dumps_typed(envelope)
        if typ != "msgpack":
            raise CheckpointProtectionError("unsupported_checkpoint_format")
        return _FORMAT, payload

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if data[0] != _FORMAT:
            raise CheckpointProtectionError("unsupported_checkpoint_format")
        try:
            envelope = self.delegate.loads_typed(("msgpack", data[1]))
            if (type(envelope) is not dict or set(envelope) != {"version", "binding", "value"}
                    or type(envelope["version"]) is not int or envelope["version"] != 1
                    or type(envelope["binding"]) is not dict
                    or set(envelope["binding"]) != set(self.binding)):
                raise CheckpointProtectionError("invalid_checkpoint_payload", structure="envelope")
            saved_binding = envelope["binding"]
            if (type(saved_binding["owner_user_id"]) is not int
                    or any(saved_binding[k] != v for k, v in self.binding.items()
                           if k not in {"provider", "model"})):
                raise CheckpointProtectionError("checkpoint_binding_mismatch", structure="envelope.binding")
            if (saved_binding["provider"], saved_binding["model"]) != self.scope:
                raise CheckpointProtectionError("checkpoint_provider_scope_mismatch", structure="envelope.binding")
            return self._walk(envelope["value"], reading=True)
        except CheckpointProtectionError:
            raise
        except Exception:
            raise CheckpointProtectionError("invalid_checkpoint_payload", structure="envelope.value") from None


class _AesGcm:
    def __init__(self, key: bytes, binding: dict):
        self.aes = AESGCM(key)
        self.aad = _FORMAT.encode() + b"\0" + _canonical(binding)

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        return _CIPHER, nonce + self.aes.encrypt(nonce, plaintext, self.aad)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != _CIPHER or len(ciphertext) < 28:
            raise CheckpointProtectionError("invalid_checkpoint_ciphertext", structure="ciphertext")
        try:
            return self.aes.decrypt(ciphertext[:12], ciphertext[12:], self.aad)
        except InvalidTag:
            raise CheckpointProtectionError("checkpoint_authentication_failed", structure="ciphertext") from None


class ProtectedCheckpointSerializer(EncryptedSerializer):
    """Encrypted-only public serializer, also used for every pending write."""

    def __init__(self, key: bytes, identity: ExecutionIdentity, provider_scope: tuple[str, str]):
        binding = _binding(identity, provider_scope)
        super().__init__(_AesGcm(key, binding), _PrivateMessages(binding))

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        # Upstream EncryptedSerializer accepts plaintext. This boundary does not.
        if data[0] != f"{_FORMAT}+{_CIPHER}":
            raise CheckpointProtectionError("encrypted_checkpoint_required", structure="ciphertext")
        return super().loads_typed(data)


def _directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode) or path.is_symlink():
        raise CheckpointProtectionError("unsafe_checkpoint_directory")
    if os.name == "posix":
        path.chmod(0o700)


def _read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise CheckpointProtectionError("unsafe_checkpoint_key")
    with os.fdopen(os.open(path, flags), "rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or (os.name == "posix" and mode & 0o077):
            raise CheckpointProtectionError("unsafe_checkpoint_key_permissions")
        key = stream.read(33)
    if len(key) != 32:
        raise CheckpointProtectionError("invalid_checkpoint_key")
    return key


def _load_key(directory: Path) -> bytes:
    path = directory / "storage.key"
    try:
        return _read_key(path)
    except FileNotFoundError:
        if any(directory.glob("*/checkpoint.sqlite3")):
            # Another creator may have published the key and opened its database
            # between our read and glob. Never replace a genuinely missing key.
            try:
                return _read_key(path)
            except FileNotFoundError:
                raise CheckpointProtectionError("checkpoint_key_missing") from None
    # Publish a fully written key with an atomic EXCLUSIVE hard link. O_EXCL on
    # the final file alone would expose an empty/partial key to another process.
    # NTFS and Linux support this; unsupported filesystems fail closed.
    fd, name = tempfile.mkstemp(prefix=".key-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(AESGCM.generate_key(bit_length=256))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            if os.name == "posix":
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return _read_key(path)


class _ProtectedSqliteSaver(AsyncSqliteSaver):
    async def aget_tuple(self, config):
        with _checkpoint_operation("read", config["configurable"].get("checkpoint_ns", "")):
            return await super().aget_tuple(config)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        with _checkpoint_operation("write_pending", config["configurable"].get("checkpoint_ns", "")):
            await super().aput_writes(config, writes, task_id, task_path)

    async def aput(self, config, checkpoint, metadata, new_versions):
        # SQLite's metadata column bypasses serde. Persist only fixed control
        # fields, NEVER arbitrary metadata/config strings, messages or reasoning.
        safe_metadata = {}
        if type(metadata.get("source")) is str and metadata["source"] in {"input", "loop", "update", "fork"}:
            safe_metadata["source"] = metadata["source"]
        if type(metadata.get("step")) is int:
            safe_metadata["step"] = metadata["step"]
        safe_metadata["parents"] = {}
        safe_config = {"configurable": {k: v for k, v in config["configurable"].items()
                                        if k in {"thread_id", "checkpoint_ns", "checkpoint_id"}}}
        with _checkpoint_operation("write", safe_config["configurable"].get("checkpoint_ns", "")):
            return await super().aput(safe_config, checkpoint, safe_metadata, new_versions)


def _prepare(root: Path, identity: ExecutionIdentity, provider_scope: tuple[str, str]):
    return _prepare_database(checkpoint_path(root, identity, provider_scope))


def _prepare_database(path: Path):
    directory = path.parent.parent
    _directory(directory)
    key = _load_key(directory)
    _directory(path.parent)
    # Precreate with a restrictive mode so SQLite doesn't use umask defaults.
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # Never open/close a second raw descriptor on an active SQLite file:
        # POSIX close can release that process's advisory SQLite locks.
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise CheckpointProtectionError("unsafe_checkpoint_database")
        if os.name == "posix" and mode & 0o077:
            raise CheckpointProtectionError("unsafe_checkpoint_database_permissions")
    else:
        os.close(fd)
    return path, key


@asynccontextmanager
async def open_checkpoint(root: Path, identity: ExecutionIdentity,
                          provider_scope: tuple[str, str]) -> AsyncIterator[AsyncSqliteSaver]:
    """Open protected storage under caller-owned state root; closes on failure.

    Use config={"configurable": checkpoint_scope(identity, provider_scope)}.
    Root must be a trusted, local service state directory (Windows ACLs remain
    the deployment owner's responsibility). Metadata is limited to source/step;
    arbitrary metadata and parent-namespace indexes are intentionally omitted.
    """
    with _checkpoint_operation("prepare"):
        _binding(identity, provider_scope)  # Validate before creating any state.
        path, key = await asyncio.to_thread(_prepare, Path(root), identity, provider_scope)
    async with aiosqlite.connect(str(path)) as connection:
        yield _ProtectedSqliteSaver(
            connection, serde=ProtectedCheckpointSerializer(key, identity, provider_scope))


_NATIVE_NAMESPACE = "native-session-v1"


def _native_binding(identity: ExecutionIdentity, provider_scope: tuple[str, str], context_scope: str = "") -> dict:
    binding = _binding(identity, provider_scope)
    del binding["run_id"], binding["user_turn_id"]
    if context_scope:
        if not re.fullmatch(r"[a-f0-9]{64}", context_scope):
            raise CheckpointProtectionError("invalid_checkpoint_context_scope")
        binding["context_scope"] = context_scope
    return {**binding, "namespace": _NATIVE_NAMESPACE}


def native_checkpoint_scope(identity: ExecutionIdentity, provider_scope: tuple[str, str], *,
                            context_scope: str = "") -> dict:
    digest = hashlib.sha256(_canonical(_native_binding(identity, provider_scope, context_scope))).hexdigest()
    return {"thread_id": digest, "checkpoint_ns": ""}


def native_checkpoint_path(root: Path, identity: ExecutionIdentity,
                           provider_scope: tuple[str, str], *, context_scope: str = "") -> Path:
    thread_id = native_checkpoint_scope(identity, provider_scope, context_scope=context_scope)["thread_id"]
    return Path(root) / _NATIVE_NAMESPACE / thread_id / "checkpoint.sqlite3"


class _NativeEncryptedSerializer(EncryptedSerializer):
    def __init__(self, key: bytes, binding: dict):
        super().__init__(_AesGcm(key, binding), JsonPlusSerializer(
            pickle_fallback=False, allowed_json_modules=[], allowed_msgpack_modules=[]))

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        tag, _, cipher = data[0].partition("+")
        if cipher != _CIPHER or tag not in {"null", "bytes", "bytearray", "msgpack"}:
            raise CheckpointProtectionError("encrypted_checkpoint_required", structure="ciphertext")
        try:
            return super().loads_typed(data)
        except CheckpointProtectionError:
            raise
        except Exception:
            raise CheckpointProtectionError("invalid_checkpoint_payload", structure="envelope.value") from None


@asynccontextmanager
async def open_native_checkpoint(root: Path, identity: ExecutionIdentity,
                                 provider_scope: tuple[str, str], *,
                                 context_scope: str = "") -> AsyncIterator[AsyncSqliteSaver]:
    binding = _native_binding(identity, provider_scope, context_scope)
    with _checkpoint_operation("prepare"):
        path, key = await asyncio.to_thread(_prepare_database,
            native_checkpoint_path(root, identity, provider_scope, context_scope=context_scope))
    async with aiosqlite.connect(str(path)) as connection:
        yield _ProtectedSqliteSaver(connection, serde=_NativeEncryptedSerializer(key, binding))
