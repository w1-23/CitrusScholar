"""Browser-facing original/preview contract for both authenticated URLs."""
import asyncio
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from src.api import main
from src.session import files, manager


SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">\r\n'
       b'<rect width="12" height="12" fill="orange"/>\r\n</svg>\r\n')
HTML = b'<!doctype html><title>Fixture report</title><p>Preserved report</p>'
OWNER = {"x-test-owner": "31"}
OTHER = {"x-test-owner": "32"}


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    monkeypatch.setattr(manager, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(manager.SessionManager, "_instance", None)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    sessions = manager.SessionManager()
    monkeypatch.setattr(main, "session_manager", sessions)
    monkeypatch.setattr(manager, "session_manager", sessions)

    def user(request: Request):
        owner = request.headers.get("x-test-owner")
        if not owner:
            raise HTTPException(401, "login required")
        return {"user_id": int(owner), "role": "user"}

    monkeypatch.setattr(main, "current_user", user)
    first = asyncio.run(sessions.get_or_create_session("file-preview-first", owner_user_id=31))
    second = asyncio.run(sessions.get_or_create_session("file-preview-second", owner_user_id=31))
    foreign = asyncio.run(sessions.get_or_create_session("file-preview-other", owner_user_id=32))
    app = FastAPI()
    app.add_api_route("/", main.serve_frontend)
    app.add_api_route("/api/v2/files", main.list_account_file_library)
    app.add_api_route("/api/v2/session/{session_id}/citation-source", main.session_citation_source)
    app.add_api_route("/api/v2/session/{session_id}/turn-artifacts", main.session_turn_artifacts)
    app.add_api_route("/api/v2/files/{file_id}", main.delete_account_library_file, methods=["DELETE"])
    app.add_api_route("/api/v2/session/{session_id}/files/{file_id}", main.delete_session_file, methods=["DELETE"])
    app.add_api_route("/api/v2/session/{session_id}/files/upload", main.upload_account_library_file,
                      methods=["POST"], status_code=201)
    for suffix, account, session in (
        ("preview", main.preview_account_file, main.preview_session_file),
        ("download", main.download_account_file, main.download_session_file),
    ):
        app.add_api_route(f"/api/v2/files/{{file_id}}/{suffix}", account)
        app.add_api_route(f"/api/v2/session/{{session_id}}/files/{{file_id}}/{suffix}", session)
    with TestClient(app) as client:
        yield client, first, second, foreign


def test_chat_page_allows_only_local_and_browser_created_images(library):
    client, *_ = library
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "img-src 'self' blob: data:"


def test_library_svg_selection_returns_isolated_previews_and_exact_download(library):
    client, session_id, wrong_session, other_session = library
    filename = "图件 & specimen.SVG"
    uploaded = client.post(f"/api/v2/session/{session_id}/files/upload", params={"filename": filename},
                           headers={**OWNER, "content-type": "application/octet-stream"}, content=SVG)
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["file_id"]
    record, = client.get("/api/v2/files", headers=OWNER).json()["uploaded"]
    assert record["file_id"] == file_id and record["kind"] == "svg"
    assert record["mime_type"] == "image/svg+xml"
    account = f"/api/v2/files/{file_id}"
    legacy = f"/api/v2/session/{session_id}/files/{file_id}"
    for base in (account, legacy):
        preview = client.get(base + "/preview", headers=OWNER)
        assert preview.status_code == 200
        assert preview.headers["Content-Type"] == "image/svg+xml"
        assert preview.headers["Content-Disposition"].startswith("inline;")
        policy = preview.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy and "sandbox" in policy
        assert "frame-ancestors 'none'" in policy
        assert "allow-scripts" not in policy and "allow-same-origin" not in policy
        assert preview.content == SVG
        download = client.get(base + "/download", headers=OWNER)
        assert download.status_code == 200
        assert download.headers["Content-Disposition"].startswith("attachment;")
        assert filename in unquote(download.headers["Content-Disposition"])
        assert download.content == SVG
        for response in (preview, download):
            assert response.headers["Cache-Control"] == "private, no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        for action in ("preview", "download"):
            assert client.get(f"{base}/{action}").status_code == 401
            assert client.get(f"{base}/{action}", headers=OTHER).status_code in {403, 404}
            assert client.get(f"/api/v2/session/{wrong_session}/files/{file_id}/{action}",
                              headers=OWNER).status_code == 404
            assert client.get(f"/api/v2/session/{other_session}/files/{file_id}/{action}",
                              headers=OTHER).status_code == 404
    thumb = client.get(account + "/preview?library_tile=1", headers=OWNER)
    assert thumb.status_code == 200
    assert thumb.content == SVG
    assert thumb.headers["Content-Security-Policy"] == preview.headers["Content-Security-Policy"]


@pytest.mark.parametrize("filename,source", [("plot.py", b"print('figure')\n"), ("plot.R", b"plot(1:4)\n")])
def test_plot_source_upload_is_download_only_and_account_owned(library, filename, source):
    client, session, sibling, foreign = library
    endpoint = f"/api/v2/session/{session}/files/upload"
    params = {"filename": filename}
    assert client.post(endpoint, params=params, content=source).status_code == 401
    assert client.post(endpoint, params=params, headers=OTHER, content=source).status_code in {403, 404}
    for invalid in (b"", b"\0binary", b"\xff", b"  \n"):
        assert client.post(endpoint, params=params, headers=OWNER, content=invalid).status_code == 400
    uploaded = client.post(endpoint, params=params, headers={**OWNER, "Content-Type": "text/html"}, content=source)
    assert uploaded.status_code == 201, uploaded.text
    metadata, = client.get("/api/v2/files", headers=OWNER).json()["uploaded"]
    ref = uploaded.json()["file_id"]
    assert metadata["file_id"] == ref and metadata["mime_type"] == "text/plain; charset=utf-8"
    assert source.decode().strip() not in str(metadata)
    for base in (f"/api/v2/files/{ref}", f"/api/v2/session/{session}/files/{ref}"):
        assert client.get(base + "/preview", headers=OWNER).status_code == 404
        saved = client.get(base + "/download", headers=OWNER)
        assert saved.content == source and saved.headers["Content-Disposition"].startswith("attachment;")
        assert saved.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get(base + "/download", headers=OTHER).status_code in {403, 404}
    assert client.get(f"/api/v2/session/{sibling}/files/{ref}/download", headers=OWNER).status_code == 404
    assert client.get(f"/api/v2/session/{foreign}/files/{ref}/download", headers=OTHER).status_code == 404


def test_plot_source_upload_checks_workbench_size_and_not_chat_ingress(library, monkeypatch):
    import base64
    from src.api import turn_envelope, workbench_bridge_protocol

    client, session, *_ = library
    monkeypatch.setattr(workbench_bridge_protocol, "ASSET_LIMIT", 3)
    response = client.post(f"/api/v2/session/{session}/files/upload", params={"filename": "plot.py"},
                           headers=OWNER, content=b"plot()")
    assert response.status_code == 413
    with pytest.raises(HTTPException) as error:
        turn_envelope._decode_upload({"name": "plot.py", "data": base64.b64encode(b"plot()").decode()},
                                     remaining=1024)
    assert error.value.status_code == 400


def test_byte_mismatch_rejects_all_browser_file_read_routes(library):
    client, session_id, *_ = library
    row = files.register_file(session_id=session_id, owner_user_id=31, filename="bound.txt",
                              content=b"checked", mime="text/plain", origin=files.ORIGIN_UPLOADED)
    file_id = row["file_id"]
    info = files.get_account_file(file_id, 31)
    info["path"].write_bytes(b"changed")  # Same length: the SHA, not stat, rejects it.
    for prefix in (f"/api/v2/files/{file_id}", f"/api/v2/session/{session_id}/files/{file_id}"):
        for action in ("preview", "download"):
            assert client.get(f"{prefix}/{action}", headers=OWNER).status_code == 404


def test_legacy_svg_rows_and_html_report_sandbox_stay_compatible(library):
    client, session_id, *_ = library
    for filename, content, mime, frame in (("historic.svg", SVG, "image/svg+xml", "'none'"),
                                            ("historic.html", HTML, "text/html", "'self'")):
        row = files.register_file(session_id=session_id, owner_user_id=31, filename=filename,
                                  content=content, mime="application/octet-stream", origin=files.ORIGIN_UPLOADED)
        paths = (row["preview_url"], f"/api/v2/session/{session_id}/files/{row['file_id']}/preview")
        for path in paths:
            preview = client.get(path, headers=OWNER)
            assert preview.status_code == 200, preview.text
            assert preview.content == content
            assert preview.headers["Content-Type"].split(";")[0] == mime
            assert f"frame-ancestors {frame}" in preview.headers["Content-Security-Policy"]


def test_library_limits_and_pinned_delete_are_shared_with_legacy(library, monkeypatch):
    from src.api.turn_envelope import MAX_FILES, MAX_LIBRARY_REF_BYTES
    from src.figures import store

    monkeypatch.setattr(store, "migrate_resource_references", lambda: None)
    client, session_id, *_ = library
    refs = client.get("/api/v2/files", headers=OWNER).json()["reference_limits"]
    assert refs == {"max_turn_files": MAX_FILES, "max_reference_bytes": MAX_LIBRARY_REF_BYTES}
    row = files.register_file(session_id=session_id, owner_user_id=31, filename="pinned.svg", content=SVG,
                              mime="image/svg+xml", origin=files.ORIGIN_UPLOADED)
    file_id = row["file_id"]
    files.replace_resource_references(31, "turn_input", "fixture-turn", [file_id])
    for endpoint in (f"/api/v2/files/{file_id}", f"/api/v2/session/{session_id}/files/{file_id}"):
        response = client.delete(endpoint, headers=OWNER)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "file_pinned"
        assert client.get(row["download_url"], headers=OWNER).content == SVG
        other = client.delete(endpoint, headers=OTHER)
        assert other.status_code in {403, 404}
    files.release_resource_references(31, "turn_input", "fixture-turn")
    assert client.delete(f"/api/v2/files/{file_id}", headers=OWNER).status_code == 200
    assert client.get(row["download_url"], headers=OWNER).status_code == 404


def test_only_validated_descendant_producer_can_read_its_own_answer_and_artifacts(library, tmp_path, monkeypatch):
    from src.core import jobs, task_events, turn_sources, user_questions

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.db")
    client, sid, other_session, _ = library
    owner = int(OWNER["x-test-owner"])

    def claim(session, scope):
        return jobs.claim_job(session, "server-" + scope, owner_user_id=owner,
                              client_request_id=scope, payload={"query": "fixture"}).job_id

    def continue_from(parent, reply):
        jobs.update_job(parent, status="running")
        q = user_questions.create_question(parent, owner_user_id=owner, session_id=sid,
            prompt="Choose the offline fixture", options=[], context={"query": "Synthetic root question"})
        jobs.update_job(parent, status="awaiting_input")
        child = claim(sid, reply)
        with task_events.transaction() as conn:
            answer = {"text": reply, "cancelled": False, "selected_option_ids": []}
            conn.execute("UPDATE task_questions SET status='answered',answer=?,answer_hash=?,child_job_id=? WHERE id=?",
                         (task_events.canonical(answer), task_events.input_hash(answer), child, q["id"]))
            conn.execute("UPDATE task_jobs SET parent_job_id=?,question_id=? WHERE job_id=?", (parent, q["id"], child))
        return child

    root = claim(sid, "shown-root")
    child = continue_from(root, "choice-one")
    latest = continue_from(child, "choice-two")
    other = claim(sid, "unrelated-root")
    other_sid_job = claim(other_session, "other-session")
    child_request = jobs.get_job(latest)["client_request_id"]
    child_ref, not_cited = "cite:" + "a" * 64, "cite:" + "b" * 64
    task_events.append_event(latest, "citations", {"display": "bubble_only", "bubbles": [
        {"kind": "local", "record_key": child_ref, "title": "Child-only offline evidence"}]})
    seen = []

    def read(source, user, *, session_id, request_id, offset, length):
        seen.append((source["record_key"], user["user_id"], session_id, request_id))
        return {"ok": True, "markdown": "Child citation source"} if request_id == child_request else None

    monkeypatch.setattr(turn_sources, "read_cited_source", read)
    artifacts_dir = tmp_path / "artifacts" / sid
    artifacts_dir.mkdir(parents=True)
    monkeypatch.setattr(files, "_artifact_session_dir", lambda session: artifacts_dir if session == sid else tmp_path)

    def product(filename, scope):
        path = artifacts_dir / filename
        path.write_bytes(SVG)
        return files.register_file(session_id=sid, owner_user_id=owner, filename=filename, storage_path=str(path),
            mime="image/svg+xml", origin=files.ORIGIN_GENERATED, client_request_id=scope)

    produced = product("child.svg", child_request)
    product("different.svg", "unrelated-root")
    citation = f"/api/v2/session/{sid}/citation-source"
    artifacts = f"/api/v2/session/{sid}/turn-artifacts"
    params = {"request_id": "shown-root", "producer_job_id": latest}
    response = client.get(citation, params={**params, "ref_id": child_ref}, headers=OWNER)
    assert response.status_code == 200, response.text
    assert response.json()["markdown"] == "Child citation source"
    actual = client.get(artifacts, params=params, headers=OWNER)
    assert actual.status_code == 200, actual.text
    assert actual.json()["producer_request_id"] == child_request
    assert [item["file_id"] for item in actual.json()["files"]] == [produced["file_id"]]
    assert client.get(citation, params={"request_id": "shown-root", "ref_id": child_ref}, headers=OWNER).status_code == 404
    assert client.get(citation, params={**params, "ref_id": not_cited}, headers=OWNER).status_code == 404
    for rejected in ({**params, "producer_job_id": other}, {**params, "producer_job_id": other_sid_job},
                     {**params, "request_id": "unrelated-root"}):
        assert client.get(citation, params={**rejected, "ref_id": child_ref}, headers=OWNER).status_code == 404
        assert client.get(artifacts, params=rejected, headers=OWNER).status_code == 404
    for url, query in ((citation, {**params, "ref_id": child_ref}), (artifacts, params)):
        assert client.get(url, params=query, headers=OTHER).status_code in {403, 404}
    # Matching job.parent_job_id by itself never authorizes a read.
    with task_events.transaction() as conn:
        conn.execute("UPDATE task_questions SET child_job_id='' WHERE job_id=?", (child,))
    assert client.get(citation, params={**params, "ref_id": child_ref}, headers=OWNER).status_code == 404
    assert client.get(artifacts, params=params, headers=OWNER).status_code == 404
    assert seen == [(child_ref, owner, sid, child_request)]
