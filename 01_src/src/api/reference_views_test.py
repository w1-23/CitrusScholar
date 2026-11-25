"""Scope tests for reference views; no network, model, or original user data."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from fastapi import HTTPException
import pytest

from src.api import main
from src.core import jobs, task_events, turn_sources
from src.session import files


def test_full_citation_matches_only_a_delivered_exact_turn_bubble(monkeypatch):
    allowed = "cite:" + "a" * 64
    candidate = "cite:" + "b" * 64
    payload = {"display": "bubble_only", "bubbles": [{"kind": "local", "record_key": allowed, "title": "A"}],
               "uncited": [{"kind": "local", "record_key": candidate, "title": "B"}]}
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE task_events(job_id TEXT,event TEXT,seq INT,data TEXT)")
    conn.execute("INSERT INTO task_events VALUES (?,?,?,?)", ("job-a", "citations", 1, json.dumps(payload)))
    identity = {"user_id": 2}
    seen = []
    monkeypatch.setattr(main, "current_user", lambda request: identity)

    def owned(sid, user):
        if sid != "owned" or user["user_id"] != 2:
            raise HTTPException(404, "not found")

    def owned_jobs(sid, owner, keys):
        assert (sid, owner) == ("owned", 2)
        return {"request": {"job_id": "job-a", "client_request_id": "ledger"}} if keys == ["request"] else {}

    @contextmanager
    def tx():
        yield conn

    def read(item, user, **kwargs):
        seen.append((item["record_key"], user["user_id"], kwargs))
        return {"ok": True, "markdown": "exact-authorized document"}

    monkeypatch.setattr(main, "require_owned_session", owned)
    monkeypatch.setattr(jobs, "jobs_for_turns", owned_jobs)
    monkeypatch.setattr(task_events, "transaction", tx)
    monkeypatch.setattr(turn_sources, "read_cited_source", read)

    async def check():
        response = await main.session_citation_source("owned", object(), "request", allowed, length=50)
        assert response.status_code == 200 and json.loads(response.body)["ok"]
        assert response.headers["cache-control"] == "private, no-store"
        assert seen == [(allowed, 2, {"session_id": "owned", "request_id": "ledger", "offset": None, "length": 50})]
        assert (await main.session_citation_source("owned", object(), "request", candidate)).status_code == 404
        assert (await main.session_citation_source("owned", object(), "other-turn", allowed)).status_code == 404
        with pytest.raises(HTTPException) as ex:
            await main.session_citation_source("owned", object(), "request", allowed, offset=-1)
        assert ex.value.status_code == 400
        identity["user_id"] = 3
        with pytest.raises(HTTPException) as ex:
            await main.session_citation_source("owned", object(), "request", allowed)
        assert ex.value.status_code == 404
        assert len(seen) == 1

    try:
        asyncio.run(check())
    finally:
        conn.close()


def test_table_sample_reads_bounded_bytes_without_touching_retention(tmp_path):
    file = tmp_path / "data.csv"
    file.write_text("name,value,note,third,ignored\nA,1,emoji 🍊,ok,ignored\nB,2,ready,yes,ignored\nD,3,hidden,no,ignored\n")
    info = {"filename": file.name, "path": str(file), "kind": "table", "size_bytes": file.stat().st_size}
    with patch.object(main, "current_user", return_value={"user_id": 7}), patch.object(files, "get_account_file", return_value=info) as get:
        response = asyncio.run(main.account_table_sample("abc", object()))
        assert json.loads(response.body)["rows"] == [["name", "value", "note", "third"],
            ["A", "1", "emoji 🍊", "ok"], ["B", "2", "ready", "yes"]]
        assert response.headers["cache-control"] == "private, no-store"
        get.assert_called_once_with("abc", 7, touch=False)
    assert file.read_text().startswith("name,")
    with patch.object(main, "current_user", return_value={"user_id": 8}), patch.object(files, "get_account_file", return_value=None) as get:
        with pytest.raises(HTTPException) as ex:
            asyncio.run(main.account_table_sample("abc", object()))
        assert ex.value.status_code == 404
        get.assert_called_once_with("abc", 8, touch=False)


def test_historical_admin_original_returns_verified_bytes_not_a_path():
    from src.core import corpus_review

    info = {"filename": "paper.md", "mime": "text/markdown", "data": b"# Historical text"}
    with patch.object(main, "require_admin") as gate, patch.object(corpus_review, "raw_file", return_value=info) as reader:
        response = asyncio.run(main.admin_corpus_raw(1, object()))
        assert response.body == info["data"]
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"] == 'inline; filename="paper.md"'
        assert response.headers["x-content-type-options"] == "nosniff"
        assert asyncio.run(main.admin_corpus_raw(1, object(), True)).headers["content-disposition"].startswith("attachment")
        assert gate.call_count == reader.call_count == 2
    with patch.object(main, "require_admin", side_effect=HTTPException(403)), patch.object(corpus_review, "raw_file") as reader:
        with pytest.raises(HTTPException) as ex:
            asyncio.run(main.admin_corpus_raw(1, object()))
        assert ex.value.status_code == 403
        reader.assert_not_called()
