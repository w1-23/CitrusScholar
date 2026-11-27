"""Transport, authorization and persistence tests with no network or live Host."""
import hashlib
import io
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from integrations.citrus_host import make_router
from wb.api import create_app
from wb.auth import issue
from wb.library import Library

KEY = b"h" * 32
SECRET = b"s" * 32
BASE = "/api/v1"
SOURCE = b"import matplotlib.pyplot as plt\nplt.plot([1, 2])\n"


def sample_png():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), "orange").save(stream, format="PNG")
    return stream.getvalue()


def fake_render(language, _source, settings):
    text = settings["overrides"].get("title", {}).get("text", "Initial")
    fmt = settings["format"]
    objects = [] if language == "static" else [{"id": "title", "panel_id": "panel", "kind": "title",
                                                "label": "Title", "properties": {"text": text}, "editable": ["text"]}]
    return {"renderer": {"id": language, "version": "1"}, "editability": "semantic" if objects else "layout_only",
            "panels": [{"id": "panel", "label": "Panel"}], "objects": objects, "limitations": []}, {
                "png": sample_png(), "svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>', "pdf": b"%PDF-1.4",
            }[fmt]


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    return {"db_path": tmp_path / "state.sqlite3", "run": fake_render, "secret": SECRET,
            "host_key": KEY, "standalone_token": "local-test"}


def assert_ok(response, status=200):
    assert response.status_code == status, response.text[:500]
    return response


def launch(client, user="51", project="account"):
    ticket = issue(KEY, "citrus-workbench-launch", "citrus-workbench", {
        "jti": uuid4().hex, "user_ref": user, "project_ref": project, "return_to": "/conversation/123",
    })
    assert_ok(client.post(BASE + "/launch", json={"ticket": ticket}))
    return ticket


def import_asset(client, *, filename="chart.py", body=SOURCE, ref=None, kind="script", language="unknown"):
    if ref is None:
        ref = assert_ok(client.post(BASE + "/assets", headers={"X-Filename": filename}, content=body), 201).json()["asset_ref"]
    return assert_ok(client.post(BASE + "/figures/import", json={
        "source": {"kind": kind, "language": language, "entrypoint_ref": ref, "assets": []},
    }), 201).json()["figure_id"]


def test_revision_restore_and_restart(app_settings):
    path = "/figures/"
    with TestClient(create_app(**app_settings)) as client:
        assert client.get(BASE + "/figures").status_code == 401
        assert_ok(client.post(BASE + "/standalone", json={"token": "local-test"}))
        ref = import_asset(client)
        url = BASE + path + ref
        assert assert_ok(client.get(url)).json()["source"]["source_hash"] == hashlib.sha256(SOURCE).hexdigest()
        patch = {"expected_revision": 1, "overrides": {"title": {"text": "New"}}}
        edited = assert_ok(client.patch(url + "/overrides", json=patch)).json()
        assert edited["objects"][0]["properties"]["text"] == "New" and edited["revision"] == 2
        assert client.patch(url + "/overrides", json=patch).status_code == 409
        assert client.patch(url + "/overrides", json=patch, headers={"origin": "https://foreign.invalid"}).status_code == 403
        response = assert_ok(client.post(url + "/export", json={"format": "png", "width_mm": 90}))
        assert response.headers["x-figure-revision"] == "2"
        assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
        restored = assert_ok(client.post(url + "/restore", json={"expected_revision": 2, "revision": 1})).json()
        assert restored["revision"] == 3 and restored["objects"][0]["properties"]["text"] == "Initial"
        assert [item["revision"] for item in assert_ok(client.get(url + "/history")).json()["versions"]] == [3, 2, 1]
    with TestClient(create_app(**app_settings)) as new_client:
        assert_ok(new_client.post(BASE + "/standalone", json={"token": "local-test"}))
        assert assert_ok(new_client.get(url)).json()["revision"] == 3


def test_handoff_is_one_use_and_scope_isolation(app_settings):
    app = create_app(**app_settings)
    with TestClient(app) as owner, TestClient(app) as other:
        ticket = launch(owner)
        assert owner.post(BASE + "/launch", json={"ticket": ticket}).status_code == 409
        assert assert_ok(owner.get(BASE + "/session")).json()["return_to"] == "/conversation/123"
        fid = import_asset(owner)
        url = f"{BASE}/figures/{fid}"
        launch(other, "52")
        assert other.get(url).status_code == 404
        launch(other, "51", "session:s1")
        assert other.get(url + "/history").status_code == 404
        assert other.get(url + "/preview").status_code == 404
        assert other.get(url + "/objects").status_code == 404
        assert other.post(url + "/export", json={"format": "png"}).status_code == 404


def test_bridge_launch_list_preview_and_import(app_settings, tmp_path):
    asset = tmp_path / "candidate.png"
    asset.write_bytes(sample_png())
    sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    valid, other_account, document, other_session = (value * 32 for value in "abcd")
    rows = {
        valid: (51, "s1", "candidate.png", "image", asset),
        other_account: (52, "s1", "other.png", "image", asset),
        document: (51, "s1", "article.pdf", "pdf", asset),
        other_session: (51, "s2", "different-session.png", "image", asset),
    }

    def identity(request: Request):
        if request.headers.get("X-Test-Login") != "owner":
            raise HTTPException(401)
        return {"user_id": 51}

    def owned_session(sid, user):
        if user["user_id"] != 51 or sid not in {"s1", "s2"}:
            raise HTTPException(404)

    def get_file(ref, owner, *, touch=False):
        hit = rows.get(ref)
        if hit and hit[0] == owner:
            _, sid, name, kind, path = hit
            return {"file_id": ref, "session_id": sid, "filename": name, "kind": kind, "path": path,
                    "sha256": sha, "origin": "generated"}
        return None

    def list_files(owner, *, session_id):
        return [get_file(ref, owner) for ref, r in rows.items() if r[0] == owner and
                (not session_id or r[1] == session_id)]

    host = FastAPI()
    host.include_router(make_router(key=KEY, current_user=identity, require_owned_session=owned_session,
                                    get_account_file=get_file, list_account_files=list_files,
                                    return_to=lambda sid: f"/conversation/{sid}"))
    with TestClient(host, base_url="http://127.0.0.1") as host_client:
        resources = Library("http://127.0.0.1", KEY, client=host_client)
        with TestClient(create_app(**app_settings, library=resources)) as wb_client:
            opening = "/api/internal/workbench/open"
            headers = {"X-Test-Login": "owner"}
            request = {"session_ref": "s1", "source_assets": [valid]}
            assert host_client.post(opening, json=request).status_code == 401
            for denied in (other_account, document, other_session):
                assert host_client.post(opening, json=dict(request, source_assets=[denied]), headers=headers).status_code == 404
            ticket = assert_ok(host_client.post(opening, json=request, headers=headers)).json()["launch_ticket"]
            assert_ok(wb_client.post(BASE + "/launch", json={"ticket": ticket}))
            listed = assert_ok(wb_client.get(BASE + "/library?format=png")).json()["assets"]
            assert [item["asset_ref"] for item in listed] == ["library:" + valid]
            assert listed[0]["source"] == "generated" and listed[0]["version"] == sha
            assert wb_client.get(BASE + f"/library/{other_session}/preview").status_code == 404
            assert assert_ok(wb_client.get(listed[0]["preview_url"])).content == asset.read_bytes()
            ref = import_asset(wb_client, ref=listed[0]["asset_ref"], kind="rendered_asset")
            imported = assert_ok(wb_client.get(f"{BASE}/figures/{ref}")).json()
            assert imported["editability"] == "layout_only"
            assert imported["source"]["library_asset_ref"] == listed[0]["asset_ref"]
            assert imported["source"]["library_version"] == sha
            assert str(asset) not in str(imported)
