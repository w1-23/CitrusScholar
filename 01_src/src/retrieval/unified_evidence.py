"""Bind opaque Corpus evidence to one authorized Host session and turn."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

from src.corpus_integration.evidence_client import IDENTITY, SearchUnavailable, read_exact

HOST_REF = re.compile(r"ce1:[a-f0-9]{64}")


def _identity(raw):
    return "ce1:" + hashlib.sha256(raw).hexdigest()


def bind(row, snapshot, root, owner, session, turn):
    """Persist only exact identity and a prefix digest, not the excerpt itself."""
    if row.get("readback") != "exact":
        raise ValueError("corpus_exact_ref_required")
    binding = {key: row[key] for key in (*IDENTITY, "evidence_ref", "title")}
    excerpt = row["excerpt"][:240]
    binding.update(snapshot=snapshot, owner=owner, session=session, turn=turn,
                   prefix_chars=len(excerpt), prefix_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest())
    if row.get("source_hash"):
        binding["source_hash"] = row["source_hash"]
    raw = json.dumps(binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ref = _identity(raw)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / ref[4:])
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return ref, turn


def read(ref, path, owner, session, turn, *, generation, offset, max_chars, user=None):
    expired = {"status": "evidence_expired", "ref": ref, "text": "", "next_offset": None}
    try:
        with path.open("rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768 or _identity(raw) != ref:
            return expired
        binding = json.loads(raw)
        if (binding.get("generation_ref") != generation or binding.get("owner") != owner
                or binding.get("session") != session or binding.get("turn") != turn):
            return expired
        text, total, next_offset = read_exact(binding, offset, max_chars, user=user)
        if offset == 0:
            prefix_chars = binding["prefix_chars"]
            if (len(text) >= prefix_chars and hashlib.sha256(text[:prefix_chars].encode("utf-8")).hexdigest()
                    != binding["prefix_hash"]):
                return expired
            if next_offset is None and hashlib.sha256(text.encode("utf-8")).hexdigest() != binding["passage_hash"]:
                return expired
    except (OSError, SearchUnavailable, UnicodeError):
        return expired
    locator = {"corpus_id": binding["source_id"], "paper_id": binding["document_ref"],
               "chunk_id": binding["passage_ref"], "corpus_family_id": binding["source_id"],
               "index_generation": generation, "input_sha256": binding["passage_hash"],
               "source_hash": binding.get("source_hash")}
    document_id = json.dumps([binding[key] for key in ("source_id", "document_ref", "version_ref", "generation_ref")],
                             ensure_ascii=True, separators=(",", ":")).encode()
    document_key = "corpus-document:" + hashlib.sha256(document_id).hexdigest()
    return {"status": "ok", "ref": ref, "text": text, "next_offset": next_offset, "total_chars": total,
            "range": {"start": offset, "end": offset + len(text)},
            "source": {"source_ref": ref, "document_key": document_key, "title": binding["title"], "locator": locator}}
