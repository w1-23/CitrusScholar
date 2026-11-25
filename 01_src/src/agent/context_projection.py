"""Read-only compatibility for previously issued context archive receipts."""
from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import BaseMessage, messages_to_dict

from .checkpoint_store import (WORKING_CONTEXT_ARCHIVE_NAMESPACE, checkpoint_scope,
                               open_legacy_checkpoint_reader)
from .identity import ExecutionIdentity
from .runtime import AdmissionStopped

_ARCHIVE_NS = WORKING_CONTEXT_ARCHIVE_NAMESPACE


@dataclass(frozen=True)
class ArchiveReference:
    thread_id: str
    checkpoint_id: str
    message_count: int

    def as_dict(self) -> dict:
        return {"kind": "encrypted_context_archive", "thread_id": self.thread_id,
                "checkpoint_ns": _ARCHIVE_NS, "checkpoint_id": self.checkpoint_id,
                "message_count": self.message_count}


class EncryptedContextArchiveReader:
    """Read old, encrypted context archives without writing or regenerating them."""

    def __init__(self, root, identity: ExecutionIdentity, provider_scope: tuple[str, str]):
        self.root, self.identity, self.provider_scope = root, identity, provider_scope
        self.config = {"configurable": {
            **checkpoint_scope(identity, provider_scope), "checkpoint_ns": _ARCHIVE_NS}}

    async def read(self, reference: ArchiveReference) -> tuple[BaseMessage, ...]:
        """Internal raw readback, including private fields; never publish directly."""
        if (reference.thread_id != self.config["configurable"]["thread_id"]
                or not reference.checkpoint_id or reference.message_count < 0):
            raise AdmissionStopped("context_archive_scope_mismatch")
        config = {"configurable": {**self.config["configurable"],
                                    "checkpoint_id": reference.checkpoint_id}}
        async with open_legacy_checkpoint_reader(self.root, self.identity, self.provider_scope) as saver:
            saved = await saver.aget_tuple(config)
        if saved is None:
            raise AdmissionStopped("context_archive_missing")
        messages = saved.checkpoint["channel_values"]["messages"]
        if len(messages) != reference.message_count:
            raise AdmissionStopped("context_archive_count_mismatch")
        return tuple(messages)

    async def read_evidence(self, reference: ArchiveReference, *, start: int = 0,
                            stop: int | None = None) -> list[dict]:
        """Authorized evidence adapter: full public message fields, no private state.

        Indexes address the original raw list, not the summary. The calling host
        must bind this archive to its authenticated execution identity, never one
        obtained from a model's tool arguments.
        """
        messages = await self.read(reference)
        end = len(messages) if stop is None else stop
        if not 0 <= start <= end <= len(messages):
            raise AdmissionStopped("context_archive_invalid_range")
        public = messages_to_dict(messages[start:end])
        for item in public:
            item["data"].get("additional_kwargs", {}).pop("host_preflight_reconciled", None)
        return public
