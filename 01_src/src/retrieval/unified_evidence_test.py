from __future__ import annotations

import hashlib

import pytest

from src.retrieval import unified_evidence as refs


def test_scope_hash_and_reauthorization(tmp_path, monkeypatch):
    text = "旧库中的可验证芽变记录。"
    row = {"source_id": "cqa_literature", "generation_ref": "g1", "document_ref": "doc", "version_ref": "v1",
           "passage_ref": "p1", "title": "Source", "excerpt": text, "evidence_ref": "ev1_secret",
           "readback": "exact", "passage_hash": hashlib.sha256(text.encode()).hexdigest()}
    ref, turn = refs.bind(row, "snapshot-v1", tmp_path, 19, "owned", "turn")
    path = tmp_path / ref[4:]
    assert refs.HOST_REF.fullmatch(ref) and turn == "turn"
    assert text not in path.read_text()
    assert (path.stat().st_mode & 0o077) == 0
    calls = []

    def verify(binding, offset, max_chars, *, user):
        calls.append((binding["evidence_ref"], offset, max_chars, user))
        return text, len(text), None

    monkeypatch.setattr(refs, "read_exact", verify)
    kwargs = {"generation": "g1", "offset": 0, "max_chars": len(text), "user": {"user_id": 19}}
    assert refs.read(ref, path, 20, "owned", "turn", **kwargs)["status"] == "evidence_expired"
    assert refs.read(ref, path, 19, "other", "turn", **kwargs)["status"] == "evidence_expired"
    assert not calls
    assert refs.read(ref, path, 19, "owned", "turn", **kwargs)["text"] == text
    assert calls == [("ev1_secret", 0, len(text), {"user_id": 19})]
    monkeypatch.setattr(refs, "read_exact", lambda *a, **k: ("伪" + text[1:], len(text), None))
    assert refs.read(ref, path, 19, "owned", "turn", **kwargs)["status"] == "evidence_expired"


def test_excerpt_only_never_binds(tmp_path):
    with pytest.raises(ValueError, match="corpus_exact_ref_required"):
        refs.bind({"readback": "excerpt_only"}, "snapshot-v1", tmp_path, 19, "owned", "turn")
