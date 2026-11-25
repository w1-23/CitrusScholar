"""Account-scoped keyset paging of the navigation list."""
import asyncio
import sqlite3

import pytest

from src.api import main as api
from src.api.chat_turn_queue_http_test import runtime  # noqa: F401


def test_session_keyset_reaches_the_101st_row_without_leaking_private_or_deleted_rows(runtime):
    async def populate():
        for i in range(101):
            await api.session_manager.get_or_create_session(f"paging-{i:03}", owner_user_id=runtime.owner)
        await api.session_manager.get_or_create_session("paging-other", owner_user_id=runtime.other_owner)
        await api.session_manager.get_or_create_session("paging-deleted", owner_user_id=runtime.owner)

    asyncio.run(populate())
    assert api.session_manager.soft_delete_session("paging-deleted")
    # Same timestamp for all rows tests the required deterministic ID tie break.
    with sqlite3.connect(api.session_manager.db_path) as connection:
        connection.execute("UPDATE sessions SET updated_at='2026-01-01T00:00:00' WHERE session_id LIKE 'paging-%'")
    client = runtime.client
    before = None
    ids = []
    while True:
        response = client.get("/api/v2/sessions", params={"limit":20, **({"before":before} if before else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == len(body["sessions"]) <= 20
        ids.extend(item["session_id"] for item in body["sessions"])
        before = body["next_before"]
        if not before:
            break
    assert len(ids) == len(set(ids)) == 102
    assert [sid for sid in ids if sid.startswith("paging-")] == [f"paging-{i:03}" for i in reversed(range(101))]


@pytest.mark.parametrize("params", [{"limit":0}, {"limit":101}, {"before":"bad cursor ?"}, {"before":"W10"}])
def test_session_paging_rejects_bad_inputs(runtime, params):
    assert runtime.client.get("/api/v2/sessions", params=params).status_code == 422


def test_session_storage_failure_is_not_reported_as_an_empty_success(runtime, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fixture database unavailable")

    monkeypatch.setattr(api.session_manager, "list_session_page", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        runtime.client.get("/api/v2/sessions")
