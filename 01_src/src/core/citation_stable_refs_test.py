from src.core import turn_source_ledger
from src.core.turn_sources import references_data


MAIN = [
    {"doi": "10.1000/a", "title": "Alpha", "year": "2020",
     "citation_ref": "L1", "candidate_id": "c01"},
    {"doi": "10.1000/b", "title": "Beta", "year": "2021",
     "citation_ref": "L2", "candidate_id": "c02"},
]

WEB = [
    {"url": "https://example.org/one", "title": "One", "citation_ref": "W1"},
    {"url": "https://example.org/one", "title": "One duplicate", "citation_ref": "W2"},
    {"url": "https://example.org/two", "title": "Two", "citation_ref": "W3"},
]


def test_display_ids_are_not_renumbered():
    refs = references_data("本地 [L2]，网络 [W3]。", MAIN, WEB)
    assert [item["ref_id"] for item in refs["cited"]] == ["L2"]
    assert [item["ref_id"] for item in refs["web_separate"]] == ["W1", "W3"]
    assert [item["ref_id"] for item in refs["web_separate"] if item["cited"]] == ["W3"]
    assert [item["ref_id"] for item in refs["uncited"]] == ["L1"]


def test_candidate_alias_and_legacy_numeric_marker():
    by_candidate = references_data("[c02]", MAIN, [])
    assert [item["ref_id"] for item in by_candidate["cited"]] == ["L2"]
    legacy = references_data("[1]", MAIN, [])
    assert [item["ref_id"] for item in legacy["cited"]] == ["L1"]


def test_duplicate_url_marker_resolves_to_first_entry():
    refs = references_data("[W2]", MAIN, WEB)
    assert [item["ref_id"] for item in refs["web_separate"] if item["cited"]] == ["W1"]
    assert refs["dropped"] == []


def test_invalid_and_historical_markers_are_dropped():
    refs = references_data("[W9] [L9] [H1] [42]", MAIN, WEB)
    assert refs["cited"] == []
    assert refs["dropped"] == ["W9", "L9", "H1", "42"]
    assert refs["historical"] == []


def test_allocator_is_turn_global_and_kind_scoped(tmp_path, monkeypatch):
    from src.session import manager

    monkeypatch.setattr(manager, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(turn_source_ledger, "_schema_ready", False)
    assert turn_source_ledger.allocate_citation_refs("s1", "r1", "web", 2) == ["W1", "W2"]
    assert turn_source_ledger.allocate_citation_refs("s1", "r1", "web", 1) == ["W3"]
    assert turn_source_ledger.allocate_citation_refs("s1", "r1", "local", 2) == ["L1", "L2"]
    assert turn_source_ledger.allocate_citation_refs("s1", "r2", "web", 1) == ["W1"]
