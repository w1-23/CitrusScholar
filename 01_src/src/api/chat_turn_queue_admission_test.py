"""A2 turn-queue admission tests: disposable SQLite + private attachment bytes.

No live service, tunnel, network, user database, account lease or real project
state is touched. Every test uses its own temporary sessions DB, account-file
DB and private file root. Run with the isolated test-runtime runner described in
the task hand-off.
"""
import base64
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api import turn_envelope
from src.core import chat_turn_queue as turn_queue
from src.core import jobs
from src.session import files as session_files

OWNER = 7
SESSION = "turn-session-1"
LIBRARY_SESSION = "library-session"
CLIENT = "client-1"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_files, "DB_PATH", tmp_path / "session_files.sqlite3")
    monkeypatch.setattr(session_files, "FILES_ROOT", tmp_path / "session_files")
    turn_queue.ensure_schema()
    session_files.init_store()


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _request(*, query="hello world", cid=CLIENT, session=SESSION,
             attachments=None, file_refs=None, skills=None,
             visualization=False, compression=True):
    return {
        "session_id": session,
        "query": query,
        "client_request_id": cid,
        "light_mode": None,
        "web_search_enabled": None,
        "visualization_enabled": visualization,
        "context_compression": compression,
        "selected_skills": skills or [],
        "attachments": attachments or [],
        "file_refs": file_refs or [],
    }


def _attachment(name="notes.txt", content=b"hello attachment", mime="text/plain"):
    return {
        "name": name,
        "type": mime,
        "data": f"data:{mime};base64,"
                + base64.b64encode(content).decode("ascii"),
    }


def _admit(request, *, limits=None, enforce_rate=False):
    return turn_envelope.admit_turn(
        request=request, owner_user_id=OWNER, session_id=request["session_id"],
        account_epoch=3, request_id="server-request-1", limits=limits,
        enforce_rate=enforce_rate)


def _library_file(content=b"library bytes", name="ref.pdf",
                  mime="application/pdf", session=LIBRARY_SESSION,
                  cid="library-request"):
    info = session_files.register_file(
        session_id=session, owner_user_id=OWNER, filename=name,
        content=content, mime=mime, origin=session_files.ORIGIN_UPLOADED,
        client_request_id=cid)
    return str(info["file_id"])


def _file_rows():
    with closing(sqlite3.connect(session_files.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM session_files")]


def _queue_rows():
    with closing(sqlite3.connect(jobs._DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM chat_turn_queue")]


def _resource_rows():
    with closing(sqlite3.connect(session_files.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM session_file_resource_refs")]


def _stored_payload(client=CLIENT):
    with closing(sqlite3.connect(jobs._DB_PATH)) as conn:
        row = conn.execute(
            "SELECT bounded_payload FROM chat_turn_queue "
            "WHERE owner_user_id=? AND client_request_id=?",
            (OWNER, client)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Admission, pins and the 202 view.
# ---------------------------------------------------------------------------

def test_admission_registers_upload_pins_and_returns_bounded_202_view():
    turn = _admit(_request(attachments=[_attachment()]))

    assert turn["created"] is True
    assert turn["state"] == "queued"
    assert turn["queue_seq"] == 1
    assert turn["job_id"]
    rows = _file_rows()
    assert len(rows) == 1
    file_id = rows[0]["file_id"]
    assert _resource_rows()  # pinned before the queue row is visible
    assert session_files.file_dependencies(file_id, OWNER)

    view = turn_envelope.present_turn(turn, include_content=True)
    assert view["status_url"] == f"/jobs/{turn['job_id']}"
    assert view["events_url"] == f"/jobs/{turn['job_id']}/events"
    assert view["query"] == "hello world"
    assert view["attachments"][0]["file_id"] == file_id
    assert view["attachments"][0]["download_url"].endswith("/download")
    # Owner identity, stored payload and host paths never reach the client.
    rendered = repr(view)
    assert "owner_user_id" not in view
    assert "bounded_payload" not in view
    assert "payload_digest" not in view
    assert "storage_path" not in rendered
    assert "data:" not in rendered
    assert str(session_files.FILES_ROOT) not in rendered

    # The durable payload holds ordered opaque refs + SHA, never bytes/paths.
    payload = _stored_payload()
    assert "data:" not in payload
    assert "storage_path" not in payload
    assert str(session_files.FILES_ROOT) not in payload


def test_idempotent_replay_does_not_reregister_or_advance_sequence():
    first = _admit(_request(attachments=[_attachment()]))
    second = _admit(_request(attachments=[_attachment()]))

    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    assert second["queue_seq"] == first["queue_seq"] == 1
    assert len(_queue_rows()) == 1
    assert len(_file_rows()) == 1


def test_same_id_with_changed_upload_name_or_refs_conflicts():
    _admit(_request(attachments=[_attachment(name="notes.txt")]))

    with pytest.raises(turn_queue.TurnPayloadConflict):
        _admit(_request(attachments=[_attachment(name="renamed.txt")]))
    # The rejected replay must not have registered a second copy.
    assert len(_file_rows()) == 1

    reference = _library_file()
    with pytest.raises(turn_queue.TurnPayloadConflict):
        _admit(_request(attachments=[_attachment(name="notes.txt")],
                        file_refs=[reference]))


def test_library_reference_is_pinned_without_duplication():
    reference = _library_file()
    before = len(_file_rows())

    turn = _admit(_request(file_refs=[reference]))

    assert len(_file_rows()) == before
    inputs = turn["bounded_payload"]["inputs"]
    assert [item["file_id"] for item in inputs] == [reference]
    assert inputs[0]["source"] == "library"
    assert session_files.file_dependencies(reference, OWNER)
    view = turn_envelope.present_turn(turn, include_content=True)
    assert view["attachments"][0]["file_id"] == reference


def test_identical_upload_bytes_keep_each_selected_name_and_reference():
    turn = _admit(_request(attachments=[_attachment("alpha.txt"), _attachment("beta.txt")]))
    payload = turn["bounded_payload"]["inputs"]
    ids = [item["file_id"] for item in payload]
    assert len(set(ids)) == 2
    assert len({item["sha256"] for item in payload}) == 1
    assert [item["name"] for item in payload] == ["alpha.txt", "beta.txt"]
    assert {row["file_id"] for row in _resource_rows()} == set(ids)
    request, metadata = turn_envelope.load_dispatch_input(turn)
    assert request["file_refs"] == ids
    assert [item["filename"] for item in metadata] == ["alpha.txt", "beta.txt"]


def test_upload_registration_only_reuses_exact_identity_in_source_session():
    first = _library_file(session=SESSION)
    assert _library_file(session=SESSION) == first
    assert _library_file() != first
    # Names that sanitize to the same disk basename are distinct inputs too.
    one = _library_file(name="one?.txt")
    two = _library_file(name="one:.txt")
    assert one != two
    assert session_files.get_account_file(one, OWNER)["filename"] == "one?.txt"
    other = session_files.get_account_file(two, OWNER)
    assert other["filename"] == "one:.txt"
    assert "upload_identity" not in other


def test_replay_returns_stored_receipt_after_original_was_deleted(monkeypatch):
    reference = _library_file()
    other = _library_file(name="other.pdf")
    request = _request(file_refs=[reference, other, reference])
    first = _admit(request)
    turn_queue.cancel_queued(owner_user_id=OWNER, session_id=SESSION, client_request_id=CLIENT)
    turn_envelope.release_turn_inputs(OWNER, SESSION, CLIENT)
    assert session_files.delete_account_file(reference, OWNER)
    before = _queue_rows()

    def unexpected_library_lookup(*args, **kwargs):
        pytest.fail("replay consulted the current library")

    with monkeypatch.context() as replay:
        replay.setattr(session_files, "get_account_file", unexpected_library_lookup)
        second = _admit(request)
        assert second["created"] is False and second["state"] == "terminal"
        assert second["job_id"] == first["job_id"]
        assert second["bounded_payload"] == first["bounded_payload"]
        for changed in ([other, reference], ["f" * 32]):
            with pytest.raises(turn_queue.TurnPayloadConflict):
                _admit(_request(file_refs=changed))
    assert _queue_rows() == before
    assert _resource_rows() == []
    with pytest.raises(HTTPException) as missing:
        _admit(_request(cid="new-authorization", file_refs=[reference]))
    assert missing.value.status_code == 404


def test_pending_replay_does_not_repin_but_dispatch_verifies_bytes(monkeypatch):
    reference = _library_file()
    request = _request(file_refs=[reference])
    first = _admit(request)
    info = session_files.get_account_file(reference, OWNER)
    Path(info["path"]).unlink()
    before = _resource_rows()
    with monkeypatch.context() as replay:
        replay.setattr(session_files, "replace_resource_references", lambda *a: pytest.fail("replay wrote pins"))
        replay.setattr(session_files, "get_account_file", lambda *a: pytest.fail("replay read library"))
        replay.setattr(session_files, "link_turn_files", lambda *a: pytest.fail("replay wrote turn links"))
        assert _admit(request)["job_id"] == first["job_id"]
    assert _resource_rows() == before
    with pytest.raises(turn_queue.TurnReconcileRequired):
        turn_envelope.load_dispatch_input(first)


def test_rejected_admission_cleans_staged_upload_and_pin():
    limits = turn_queue.QueueLimits(max_pending_per_session=0)

    with pytest.raises(turn_queue.TurnQueueFull):
        _admit(_request(attachments=[_attachment()]), limits=limits)

    assert _queue_rows() == []
    assert _file_rows() == []
    assert _resource_rows() == []


def test_release_keeps_accepted_inputs_but_removes_pin():
    turn = _admit(_request(attachments=[_attachment()]))
    file_id = turn["bounded_payload"]["inputs"][0]["file_id"]
    assert session_files.file_dependencies(file_id, OWNER)

    # A still-active turn keeps its protection pin.
    assert turn_envelope.release_turn_inputs(OWNER, SESSION, CLIENT) == 0
    assert session_files.file_dependencies(file_id, OWNER)

    turn_queue.cancel_queued(owner_user_id=OWNER, session_id=SESSION,
                             client_request_id=CLIENT)
    removed = turn_envelope.release_turn_inputs(OWNER, SESSION, CLIENT)

    assert removed == 1
    assert session_files.file_dependencies(file_id, OWNER) == []
    # A previously accepted input is not deleted by release/cancel cleanup.
    assert session_files.get_account_file(file_id, OWNER) is not None
    assert [row["file_id"] for row in _file_rows()] == [file_id]


def test_release_removes_orphan_staging_when_no_turn_was_accepted():
    file_id = _library_file(session=SESSION, cid="orphan-request",
                            name="orphan.txt", mime="text/plain")
    resource_id = turn_envelope.turn_input_resource_id(SESSION, "orphan-request")
    session_files.replace_resource_references(
        OWNER, turn_envelope.RESOURCE_KIND, resource_id, [file_id])
    assert _resource_rows()

    turn_envelope.release_turn_inputs(OWNER, SESSION, "orphan-request")

    assert session_files.get_account_file(file_id, OWNER) is None
    assert _resource_rows() == []


# ---------------------------------------------------------------------------
# Dispatch rehydration.
# ---------------------------------------------------------------------------

def test_load_dispatch_input_reconstitutes_request_and_verifies_frozen_hash():
    turn = _admit(_request(attachments=[_attachment(content=b"payload-123")]))
    claimed = turn_queue.claim_head(owner_user_id=OWNER, session_id=SESSION)
    assert "bounded_payload" not in claimed  # force the scoped re-read

    request_dict, metadata = turn_envelope.load_dispatch_input(claimed)

    assert request_dict["attachments"] == []
    assert request_dict["query"] == "hello world"
    assert request_dict["client_request_id"] == CLIENT
    assert request_dict["file_refs"] == [metadata[0]["file_id"]]
    assert metadata[0]["sha256"] == hashlib.sha256(b"payload-123").hexdigest()
    assert "path" not in metadata[0]
    assert "storage_path" not in metadata[0]

    info = session_files.get_account_file(metadata[0]["file_id"], OWNER)
    Path(info["path"]).write_bytes(b"tampered")
    with pytest.raises(turn_queue.TurnReconcileRequired):
        turn_envelope.load_dispatch_input(claimed)


def test_present_turn_rereads_payload_for_list_view():
    _admit(_request(attachments=[_attachment()]))
    rows = turn_queue.list_turns(owner_user_id=OWNER, session_id=SESSION)
    assert rows and "bounded_payload" not in rows[0]

    view = turn_envelope.present_turn(rows[0], include_content=True)

    assert view["query"] == "hello world"
    assert view["attachments"]
    assert "owner_user_id" not in view
    assert "bounded_payload" not in view


# ---------------------------------------------------------------------------
# HTTP validation and error mapping.
# ---------------------------------------------------------------------------

def test_bad_http_inputs_raise_http_exceptions_not_raw_valueerrors():
    with pytest.raises(HTTPException) as bad_id:
        _admit(_request(cid="bad id!"))
    assert bad_id.value.status_code == 400

    for blank in ("   ", "\t", "\n\t  "):
        with pytest.raises(HTTPException) as blank_query:
            _admit(_request(query=blank))
        assert blank_query.value.status_code == 400
        assert blank_query.value.detail == "问题内容为空或过长。"

    with pytest.raises(HTTPException) as bad_suffix:
        _admit(_request(attachments=[_attachment(name="run.exe")]))
    assert bad_suffix.value.status_code == 400

    too_many = [_attachment(name=f"n{index}.txt") for index in range(9)]
    with pytest.raises(HTTPException) as too_many_files:
        _admit(_request(attachments=too_many))
    assert too_many_files.value.status_code == 413

    with pytest.raises(HTTPException) as missing_ref:
        _admit(_request(file_refs=["f" * 32]))
    assert missing_ref.value.status_code == 404


def test_to_http_exception_maps_queue_facts():
    assert turn_envelope.to_http_exception(
        turn_queue.TurnQueueFull("full")).status_code == 429
    assert turn_envelope.to_http_exception(
        turn_queue.TurnPayloadConflict("conflict")).status_code == 409
    assert turn_envelope.to_http_exception(
        turn_queue.TurnNotFound("missing")).status_code == 404


def test_browse_opt_in_is_persisted_and_changes_request_identity():
    base = _request()
    original = _admit(base)
    assert not turn_envelope.load_dispatch_input(original)[0]["browse_requested"]
    with pytest.raises(turn_queue.TurnPayloadConflict):
        _admit({**base, "browse_requested": True})
    requested = _admit({**_request(cid="browse-turn"), "browse_requested": True})
    assert turn_envelope.load_dispatch_input(requested)[0]["browse_requested"] is True


def test_skill_release_is_pinned_at_admission_without_leaking_into_public_view(tmp_path, monkeypatch):
    import json

    from src.agent.installed_skills import RQA_ID, list_installed, release_pin

    release = tmp_path / "immutable-skill-release"
    entry = release / "skills" / RQA_ID / "SKILL.md"
    entry.parent.mkdir(parents=True)
    entry.write_text(f"---\nname: {RQA_ID}\ndescription: Research method.\n---\n")
    inventory = {str(entry.relative_to(release)): hashlib.sha256(entry.read_bytes()).hexdigest()}
    (release / "inventory.json").write_text(json.dumps(inventory))
    (release / "release.json").write_text(json.dumps({RQA_ID: "e" * 40}))
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(release))
    selected = {k: list_installed(release)[0][k] for k in ("id", "version", "sha256")}
    request = _request(skills=[selected])
    turn = _admit(request)
    pin = release_pin(release)
    assert all(turn["bounded_payload"][key] == value for key, value in pin.items())
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(release / "unrelated"))
    assert _admit(request)["created"] is False  # receipt, not a new admission
    data, _ = turn_envelope.load_dispatch_input(turn)
    assert data["selected_skills"] == [selected]
    assert all(data[key] == value for key, value in pin.items())
    assert str(release) not in json.dumps(turn_envelope.present_turn(turn))
    fresh = _admit(_request(cid="client-fresh"))
    assert fresh["bounded_payload"]["selected_skills"] == []
    assert not any(key in fresh["bounded_payload"] for key in pin)


class _CrashBetweenStores(BaseException):
    pass


def test_combined_limit_rejects_before_any_queue_or_upload_write(monkeypatch):
    limit = turn_envelope.MAX_ATTACHMENT_BYTES
    library = _library_file(content=b"a" * (limit - 2))
    with pytest.raises(HTTPException) as error:
        _admit(_request(file_refs=[library], attachments=[_attachment(content=b"xyz")]))
    assert error.value.status_code == 413
    assert not _queue_rows() and not _resource_rows()
    assert [row["file_id"] for row in _file_rows()] == [library]
    assert not turn_envelope.staging.pending()

    at_limit = _admit(_request(file_refs=[library], attachments=[_attachment(content=b"xy")]))
    assert at_limit["attachment_bytes"] == limit
    assert len(_resource_rows()) == 2


def test_reconcile_hard_interruption_between_pin_and_queue_receipt(monkeypatch):
    library = _library_file()
    enqueue = turn_queue.enqueue_turn
    def crash(**kwargs):
        assert _resource_rows() and len(_file_rows()) == 2
        raise _CrashBetweenStores
    with monkeypatch.context() as broken:
        broken.setattr(turn_queue, "enqueue_turn", crash)
        with pytest.raises(_CrashBetweenStores):
            _admit(_request(file_refs=[library], attachments=[_attachment()]))
    assert turn_queue.enqueue_turn is enqueue
    assert not _queue_rows() and turn_envelope.staging.pending()
    turn_envelope.reconcile_staged_turn_inputs()
    assert not _resource_rows() and not turn_envelope.staging.pending()
    assert [row["file_id"] for row in _file_rows()] == [library]
    assert session_files.list_turn_files(SESSION, OWNER, [CLIENT]) == {}


def test_reconcile_preserves_committed_receipt_and_pin_after_interruption(monkeypatch):
    enqueue = turn_queue.enqueue_turn
    def crash(**kwargs):
        enqueue(**kwargs)
        raise _CrashBetweenStores
    body = _request(attachments=[_attachment()])
    with monkeypatch.context() as broken:
        broken.setattr(turn_queue, "enqueue_turn", crash)
        with pytest.raises(_CrashBetweenStores):
            _admit(body)
    pinned = _resource_rows()
    assert len(pinned) == 1 and turn_envelope.staging.pending()
    assert not _admit(body)["created"]
    turn_envelope.reconcile_staged_turn_inputs()
    assert not turn_envelope.staging.pending()
    assert _resource_rows() == pinned
    assert len(session_files.list_turn_files(SESSION, OWNER, [CLIENT])[CLIENT]) == 1


def test_reconcile_removes_recorded_unindexed_upload_after_interruption(monkeypatch):
    write_bytes = Path.write_bytes
    written = []
    def crash(path, content):
        result = write_bytes(path, content)
        if path.is_relative_to(session_files.FILES_ROOT):
            written.append(path)
            raise _CrashBetweenStores
        return result
    with monkeypatch.context() as broken:
        broken.setattr(Path, "write_bytes", crash)
        with pytest.raises(_CrashBetweenStores):
            _admit(_request(attachments=[_attachment()]))
    assert len(written) == 1 and written[0].is_file() and not _file_rows()
    turn_envelope.reconcile_staged_turn_inputs()
    assert not written[0].exists() and not turn_envelope.staging.pending()


def test_reconcile_after_actual_admission_process_death():
    import json
    import subprocess
    import sys

    script = """
import json, os, sys
from pathlib import Path
from src.api import turn_envelope
from src.core import chat_turn_queue as queue, jobs
from src.session import files
jobs._DB_PATH, files.DB_PATH, files.FILES_ROOT = map(Path, sys.argv[1:4])
body = json.loads(sys.argv[4])
queue.enqueue_turn = lambda **kwargs: os._exit(73)
turn_envelope.admit_turn(request=body, owner_user_id=int(sys.argv[5]), session_id=body['session_id'],
                         account_epoch=3, request_id='killed-admission', enforce_rate=False)
"""
    args = [str(path) for path in (jobs._DB_PATH, session_files.DB_PATH, session_files.FILES_ROOT)]
    run = subprocess.run([sys.executable, "-c", script, *args,
                          json.dumps(_request(attachments=[_attachment()])), str(OWNER)], timeout=25)
    assert run.returncode == 73
    assert not _queue_rows() and _resource_rows() and turn_envelope.staging.pending()
    turn_envelope.reconcile_staged_turn_inputs()
    assert not _file_rows() and not _resource_rows() and not turn_envelope.staging.pending()
