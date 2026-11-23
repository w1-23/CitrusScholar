"""Reference roles survive persistence and refresh, without fake citations."""
import asyncio
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src import config
from src.api import main
from src.core import jobs
from src.core.turn_sources import references_data
from src.session import manager


MAIN = [
    {"doi": "10.1000/cited", "title": "Cited paper", "snippet": "cited body",
     "score": 1.25, "source": "rag"},
    {"doi": "10.1000/candidate", "title": "Candidate paper",
     "snippet": "candidate body", "source": "rag"},
]
WEB = [
    {"url": "https://www.nature.com/articles/abc", "title": "Nature item"},
    {"url": "https://example.com/unknown", "title": "Unknown item"},
]


def test_roles_are_decided_by_actual_markers():
    refs = references_data("本地结论[1]，网络[W1]；死号[9][W3]", MAIN, WEB)
    assert [x["ref_id"] for x in refs["cited"]] == ["1"]
    assert refs["cited"][0]["type"] == "main"
    assert refs["cited"][0]["role"] == "cited"
    assert [x["ref_id"] for x in refs["uncited"]] == ["2"]
    assert refs["uncited"][0]["type"] == "candidate"
    assert refs["uncited"][0]["role"] == "candidate"
    assert refs["uncited"][0]["cited"] is False

    first, second = refs["web_separate"]
    assert first["role"] == "cited" and first["cited"] is True
    assert first["tier"] == "T1"
    assert first["origin_label"] == "网络来源"
    assert second["role"] == "candidate" and second["cited"] is False
    assert second["tier"] == "T3"
    assert second["tier_label"].startswith("网络来源")
    assert "9" in refs["dropped"] and "W3" in refs["dropped"]


def test_dedupes_local_doi_and_web_url():
    refs = references_data("", MAIN + [dict(MAIN[0])],
                           WEB + [dict(WEB[0])])
    assert len(refs["uncited"]) == 2
    assert len(refs["web_separate"]) == 2


@pytest.fixture
def citation_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manager, "STATE_DIR", state)
    monkeypatch.setattr(manager, "DB_PATH", state / "sessions.db")
    monkeypatch.setattr(manager.SessionManager, "_instance", None)
    monkeypatch.setattr(jobs, "_DB_PATH", state / "sessions.db")
    sessions = manager.SessionManager()
    monkeypatch.setattr(main, "session_manager", sessions)
    monkeypatch.setattr(manager, "session_manager", sessions)
    monkeypatch.setattr(main, "current_user", lambda _request: {"user_id": 5})
    monkeypatch.setattr(main, "require_owned_session", lambda *a, **k: None)
    sid = asyncio.run(sessions.get_or_create_session("cite-session", owner_user_id=5))
    app = FastAPI()
    app.add_api_route("/sessions/{session_id}/citations", main.session_citations)
    return {"sessions": sessions, "sid": sid,
            "client": TestClient(app), "state": state}


def test_refresh_restores_roles_tiers_and_candidates(citation_env):
    refs = references_data("本地[1]，网络[W1]。", MAIN, WEB)
    evidence = [{"doi": "10.1000/cited", "title": "Cited paper", "source": "rag"},
                {"doi": "10.1000/candidate", "title": "Candidate paper", "source": "rag"}]
    asyncio.run(citation_env["sessions"].save_evidence(
        citation_env["sid"], "q", evidence, "", refs["cited"], references=refs))
    data = citation_env["client"].get(
        f"/sessions/{citation_env['sid']}/citations").json()
    groups = data["groups"]
    local = groups["rag"]
    by_title = {item["title"]: item for item in local}
    assert by_title["Cited paper"]["type"] == "main"
    assert by_title["Cited paper"]["role"] == "cited"
    assert by_title["Candidate paper"]["type"] == "candidate"
    assert by_title["Candidate paper"]["role"] == "candidate"

    t1 = groups["web_t1"]
    t3 = groups["web_t3"]
    assert [item["role"] for item in t1] == ["cited"]
    assert [item["role"] for item in t3] == ["candidate"]
    assert t1[0]["tier"] == "T1" and t1[0]["url"].startswith("https://www.nature.com/")
    assert t3[0]["tier"] == "T3"


def test_old_rows_without_references_json_still_read(citation_env):
    with sqlite3.connect(str(citation_env["state"] / "sessions.db")) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_evidence)")}
        assert "references_json" in cols
        old = [{"ref_id": "1", "type": "main", "source": "rag",
                "doi": "10.1000/old", "title": "Old row", "score": 1.0}]
        conn.execute(
            "INSERT INTO session_evidence (session_id, turn_seq, query, evidence_json, "
            "report_text, cited_refs_json, references_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (citation_env["sid"], 99, "old", "[]", "", json.dumps(old), "",
             "2026-01-01T00:00:00"))
        conn.commit()
    data = citation_env["client"].get(
        f"/sessions/{citation_env['sid']}/citations").json()
    assert any(item["title"] == "Old row" for item in data["groups"]["rag"])
