from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.api import workbench_bridge as bridge
from src.api import workbench_bridge_protocol as wire
from src.session import files


OWNER, OTHER = 51, 52
SESSION, SIBLING, FOREIGN = "test-session", "another-session", "foreign-session"
LAUNCH, LIST = "/api/internal/workbench/open", wire.ASSETS_PATH
ORIGIN = "/workbench"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def assertion(key, path=LIST, **overrides):
    claims = {"iss": "workbench-resource", "aud": "citrus-resource-api",
              "exp": int(time.time()) + 25, "user_ref": str(OWNER), "scope": "figures:read",
              "project_ref": f"session:{SESSION}", "request_path": path} | overrides
    encoded = _b64(json.dumps(claims).encode())
    tag = _b64(hmac.new(key, f"workbench-resource.{encoded}".encode(), hashlib.sha256).digest())
    return {"Authorization": f"Bearer {encoded}.{tag}"}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.sqlite3")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    key = secrets.token_hex(32).encode()
    app = FastAPI()
    owners = {SESSION: OWNER, SIBLING: OWNER, FOREIGN: OTHER}
    users = {uid: {"user_id": uid, "active": True, "approval_status": "approved"} for uid in (OWNER, OTHER)}

    def current_user(request: Request):
        if request.cookies.get("citrus_auth") != "test-browser-session":
            raise HTTPException(401)
        return users[OWNER]

    def require_owned_session(session, actor):
        if owners.get(session) != actor["user_id"]:
            raise HTTPException(404)

    def put(session, owner, name, data=b"fixture", **extra):
        return files.register_file(session_id=session, owner_user_id=owner, filename=name,
                                   content=data, **extra)["file_id"]

    app.include_router(bridge.bridge_router(bridge.BridgeConfig(key, "citrus-workbench",
        ORIGIN + "/workbench/", ORIGIN + "/api/v1/launch"), current_user=current_user,
        require_owned_session=require_owned_session, account_user=users.get,
        list_account_files=files.list_account_files, get_account_file=files.get_account_file))
    return TestClient(app), key, put, users, owners


def test_default_disabled_without_even_loading_secret(monkeypatch):
    monkeypatch.delenv("CITRUS_WORKBENCH_BRIDGE_ENABLED", raising=False)
    monkeypatch.setenv("WORKBENCH_HOST_KEY_FILE", "/unavailable")
    app = FastAPI()
    bridge.register_workbench_bridge(app, current_user=None, require_owned_session=None)
    client = TestClient(app)
    assert client.post(LAUNCH, json={}).status_code == 404
    assert client.get(LIST, params={"type": "figure"}).status_code == 404


@pytest.mark.parametrize("destination", [
    "https://external.example/workbench/", "//external.example/workbench/", "/\\external.example/",
    "/workbench/?ticket=1", "/workbench/%2e%2e/path", "/workbench/../path",
])
def test_configuration_rejects_noncanonical_destinations(monkeypatch, destination):
    monkeypatch.setenv("CITRUS_WORKBENCH_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("CITRUS_WORKBENCH_PUBLIC_URL", destination)
    monkeypatch.setenv("CITRUS_WORKBENCH_LAUNCH_ENDPOINT", ORIGIN + "/api/v1/launch")
    with pytest.raises(ValueError, match="same-origin"):
        bridge.BridgeConfig.from_environment()


def test_enabled_requires_key_file_and_uses_only_server_destinations(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key = secrets.token_hex(32).encode()
    key_file.write_bytes(key + b"\n")
    monkeypatch.setenv("CITRUS_WORKBENCH_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("CITRUS_WORKBENCH_PUBLIC_URL", ORIGIN + "/workbench/")
    monkeypatch.setenv("CITRUS_WORKBENCH_LAUNCH_ENDPOINT", ORIGIN + "/api/v1/launch")
    monkeypatch.delenv("WORKBENCH_HOST_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="WORKBENCH_HOST_KEY_FILE"):
        bridge.BridgeConfig.from_environment()
    monkeypatch.setenv("WORKBENCH_HOST_KEY_FILE", str(key_file))
    config = bridge.BridgeConfig.from_environment()
    assert config.key == key and key.decode() not in repr(config)


def test_launch_signed_claims_and_host_authorization(host):
    client, key, put, _, _ = host
    own = put(SESSION, OWNER, "figure.png")
    sibling = put(SIBLING, OWNER, "sibling.svg")
    foreign = put(FOREIGN, OTHER, "foreign.png")
    internal = put(SESSION, OWNER, "receipt.png", visibility="internal")
    assert client.post(LAUNCH, json={}).status_code == 401
    client.cookies.set("citrus_auth", "test-browser-session")
    for ref in (sibling, foreign, internal):
        assert client.post(LAUNCH, json={"session_ref": SESSION, "source_assets": [ref]}).status_code == 404
    assert client.post(LAUNCH, json={"session_ref": FOREIGN}).status_code == 404
    for injected in ("project_ref", "user_ref", "return_to", "launch_endpoint", "workbench_url"):
        assert client.post(LAUNCH, json={injected: "fake"}).status_code == 422
    response = client.post(LAUNCH, json={"session_ref": SESSION, "source_assets": [own, own]})
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert result["launch_endpoint"] == ORIGIN + "/api/v1/launch"
    assert result["workbench_url"] == ORIGIN + "/workbench/"
    raw, tag = result["launch_ticket"].split(".")
    assert hmac.compare_digest(_unb64(tag), hmac.new(key, f"citrus-workbench-launch.{raw}".encode(), "sha256").digest())
    claims = json.loads(_unb64(raw))
    assert claims["aud"] == "citrus-workbench" and claims["iss"] == "citrus-workbench-launch"
    assert claims["user_ref"] == str(OWNER) and claims["project_ref"] == f"session:{SESSION}"
    assert claims["return_to"] == f"/?figure_session={SESSION}" and claims["source_assets"] == [own]
    assert int(time.time()) < claims["exp"] <= int(time.time()) + 60
    assert wire.resource_principal(key, "Bearer " + result["launch_ticket"], LIST) is None
    again = client.post(LAUNCH, json={}).json()["launch_ticket"]
    account = json.loads(_unb64(again.split(".")[0]))
    assert account["project_ref"] == "account" and account["jti"] != claims["jti"]


def test_inventory_and_asset_roles_are_scope_bound_and_private(host, monkeypatch):
    client, key, put, _, _ = host
    own = put(SESSION, OWNER, "figure.png", b"figure-bytes")
    other = put(FOREIGN, OTHER, "foreign.png")
    sibling = put(SIBLING, OWNER, "sibling.svg")
    put(SESSION, OWNER, "article.pdf")
    script = put(SESSION, OWNER, "script.py", mime="text/html")
    put(SESSION, OWNER, "hidden.svg", visibility="internal")
    query = {"type": "figure"}
    assert client.get(LIST, params=query).status_code == 401
    inventory = client.get(LIST, params=query, headers=assertion(key))
    assert inventory.json() == []
    inventory = client.get(LIST, params={**query, "include_rendered": "true"}, headers=assertion(key))
    assert [r["asset_ref"] for r in inventory.json()] == [own]
    assert inventory.headers["cache-control"] == "private, no-store"
    assert set(inventory.json()[0]) == {"asset_ref", "name", "format", "version", "source", "editability"}
    account = client.get(LIST, params={**query, "format": "svg", "limit": 1},
                         headers=assertion(key, project_ref="account"))
    assert [r["asset_ref"] for r in account.json()] == [sibling]
    p = f"{LIST}/{own}/preview"
    assert client.get(p, headers=assertion(key)).status_code == 401
    assert client.get(p, headers=assertion(key, p.replace("preview", "source"))).status_code == 401
    served = client.get(p, headers=assertion(key, p))
    assert served.content == b"figure-bytes" and served.headers["content-type"] == "image/png"
    assert served.headers["X-Asset-Version"] == served.headers["X-Asset-SHA256"] == hashlib.sha256(served.content).hexdigest()
    assert served.headers["X-Asset-Filename"] == "figure.png" and "sandbox" in served.headers["content-security-policy"]
    for ref in (sibling, other, script):
        path = f"{LIST}/{ref}/source"
        assert client.get(path, headers=assertion(key, path)).status_code == 404
    info = files.get_account_file(own, OWNER)
    info["path"].write_bytes(b"altered-data")  # same byte count, different digest
    assert client.get(p, headers=assertion(key, p)).status_code == 409
    monkeypatch.setattr(wire, "ASSET_LIMIT", 4)
    assert client.get(p, headers=assertion(key, p)).status_code == 409


@pytest.mark.parametrize("claims", [
    {"iss": "citrus-workbench-launch"}, {"aud": "not-citrus"}, {"scope": "figures:write"},
    {"exp": 0}, {"exp": 9999999999}, {"exp": True}, {"user_ref": "051"},
    {"user_ref": str(2**63)}, {"project_ref": "session:../foreign-session"},
    {"project_ref": "global"}, {"request_path": LIST + "/different"},
])
def test_rejects_bad_signed_resource_claims(host, claims):
    client, key, _, _, _ = host
    headers = assertion(key, **claims)
    assert client.get(LIST, params={"type": "figure"}, headers=headers).status_code == 401


def test_rechecks_live_account_and_session_ownership_on_each_read(host):
    client, key, _, users, owners = host
    params = {"type": "figure"}
    headers = assertion(key)
    assert client.get(LIST, params=params, headers=headers).status_code == 200
    owners[SESSION] = OTHER
    assert client.get(LIST, params=params, headers=headers).status_code == 404
    users[OWNER]["active"] = False
    assert client.get(LIST, params=params, headers=assertion(key, project_ref="account")).status_code == 401
    assert client.get(LIST, params=params, headers=assertion(secrets.token_bytes(32))).status_code == 401


def test_candidate_and_dependency_catalogs_have_independent_read_roles(host):
    client, key, put, _, _ = host
    source = b"from matplotlib import pyplot as plt\nplt.plot([4, 5, 6])\n"
    py = put(SESSION, OWNER, "owner_plot.py", source, mime="text/plain; charset=utf-8")
    r = put(SESSION, OWNER, "owner_plot.R", b"plot(1:3)", mime="application/octet-stream")
    csv = put(SESSION, OWNER, "observations.csv", b"count\n1\n")
    hidden_script = put(SESSION, OWNER, "private_receipt.py", source, visibility="internal")
    hidden_json = put(SESSION, OWNER, "private_receipt.json", b"{}", visibility="internal")
    bad_script = put(SESSION, OWNER, "invalid.py", b"\x00", mime="text/plain")
    bad_csv = put(SESSION, OWNER, "outdated.csv", b"count\n1\n")
    sibling = put(SIBLING, OWNER, "sibling.csv")
    foreign = put(FOREIGN, OTHER, "foreign.csv")
    png = put(SESSION, OWNER, "figure.png")
    # Register_file sanitizes unsafe names. Seed an old unsanitized index entry
    # to exercise the callback's independent metadata boundary.
    raw = put(SESSION, OWNER, "legacy_plot.py", source)
    with files._connect() as connection:
        connection.execute("UPDATE session_files SET filename=? WHERE file_id=?", ("invalid:filename.py", raw))
    put(SESSION, OWNER, "CON.csv")

    def listing(resource, **params):
        result = client.get(LIST, params={"type": resource, **params}, headers=assertion(key))
        assert result.status_code == 200
        return result.json()

    def read(ref, role):
        path = f"{LIST}/{ref}/{role}"
        return client.get(path, headers=assertion(key, path))

    assert {row["asset_ref"] for row in listing("figure")} == {py, r, bad_script}
    (candidate,) = [
        row for row in listing("figure", format="py") if row["asset_ref"] == py]
    assert candidate == {"asset_ref": py, "name": "owner_plot.py", "format": "py",
                         "source": "uploaded", "editability": "partial",
                         "version": hashlib.sha256(source).hexdigest()}
    assert {row["asset_ref"] for row in listing("figure", include_rendered="true")} == {py, r, bad_script, png}
    assert [row["asset_ref"] for row in listing("figure", format="png")] == [png]
    assert [row["asset_ref"] for row in listing("figure", rendered_only="true", limit=1)] == [png]
    assert {row["asset_ref"] for row in listing("dependency")} == {csv, bad_csv}
    assert listing("dependency", format="py") == []
    assert read(py, "source").content == source
    assert read(r, "source").content == b"plot(1:3)"
    assert read(bad_script, "source").status_code == 409
    assert read(py, "preview").status_code == 404
    assert read(py, "dependency").status_code == 404
    assert read(csv, "source").status_code == 404
    assert read(csv, "preview").status_code == 404
    dependency = read(csv, "dependency")
    assert dependency.headers["content-type"].startswith("text/csv")
    assert dependency.headers["X-Asset-SHA256"] == dependency.headers["X-Asset-Version"] == hashlib.sha256(dependency.content).hexdigest()
    assert dependency.content == b"count\n1\n"
    for ref, role in ((hidden_script, "source"), (hidden_json, "dependency"),
                      (sibling, "dependency"), (foreign, "dependency")):
        assert read(ref, role).status_code == 404
    files.get_account_file(bad_csv, OWNER)["path"].write_bytes(b"count\n2\n")
    assert read(bad_csv, "dependency").status_code == 409
    client.cookies.set("citrus_auth", "test-browser-session")
    assert client.post(LAUNCH, json={"session_ref": SESSION, "source_assets": [csv]}).status_code == 404


@pytest.mark.skipif(not (os.getenv("WORKBENCH_HOST_SMOKE") and os.getenv("WORKBENCH_IMAGE")),
                    reason="Requires independent Workbench Python path and an isolated renderer SIF")
@pytest.mark.parametrize("language,extension", [("python", "py"), ("r", "R")])
def test_registered_host_script_and_selected_csv_render_in_real_workbench(host, tmp_path, monkeypatch,
                                                                       language, extension):
    from wb.api import create_app
    from wb.library import Library

    host_client, key, put, _, _ = host
    source = b"""import csv
from matplotlib import pyplot as plt
with open('measurements.csv', newline='') as stream:
    records = list(csv.DictReader(stream))
fig, ax = plt.subplots()
ax.plot([float(row['dose']) for row in records], [float(row['signal']) for row in records])
ax.set(title='Explicit dependency plot', xlabel='Dose', ylabel='Signal')
"""
    if language == "r":
        source = b"""library(ggplot2)
records <- read.csv('measurements.csv')
figure <- ggplot(records, aes(dose, signal)) + geom_line() + geom_point() +
  labs(title = 'Explicit dependency plot', x = 'Dose', y = 'Signal') + theme_minimal()
"""
    csv = b"dose,signal\n0,1\n2,5\n4,3\n"
    script_ref = put(SESSION, OWNER, "library_plot." + extension, source, mime="text/plain")
    data_ref = put(SESSION, OWNER, "measurements.csv", csv)
    put(SESSION, OWNER, "not_selected.json", b'{"fixture":true}')
    foreign = put(FOREIGN, OTHER, "measurements.csv", csv)
    static = put(SESSION, OWNER, "reference.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    host_client.cookies.set("citrus_auth", "test-browser-session")
    opening = host_client.post(LAUNCH, json={"session_ref": SESSION, "source_assets": [script_ref]})
    assert opening.status_code == 200, opening.text
    resources = Library("http://127.0.0.1", key, client=host_client)
    wb = create_app(db_path=tmp_path / "independent-workbench.sqlite3", library=resources,
                    host_key=key, secret=b"w" * 32)
    with TestClient(wb) as browser:
        root = "/api/v1"
        launched = browser.post(root + "/launch", json={"ticket": opening.json()["launch_ticket"]})
        assert launched.status_code == 200, launched.text
        library = browser.get(root + "/library", params={"type": "script"}).json()["assets"]
        key_script, key_data = "library:" + script_ref, "library:" + data_ref
        assert [row["asset_ref"] for row in library] == [key_script]
        listed = browser.get(root + "/library", params={"type": "dependency"}).json()["assets"]
        assert {row["format"] for row in listed} == {"csv", "json"}
        assert [row["asset_ref"] for row in listed if row["format"] == "csv"] == [key_data]
        assert browser.get(f"{root}/library/{static}/preview").content.startswith(b"<svg")
        for ref, expected in (("library:" + foreign, 404), (key_data, 422)):
            payload = {"source": {"kind": "script", "language": language, "entrypoint_ref": key_script,
                                  "assets": [ref], "hashes": {ref: "0" * 64}},
                       "options": {"parse_mode": "editable"}}
            rejected = browser.post(root + "/figures/import", json=payload)
            assert rejected.status_code == expected, rejected.text
        imported = browser.post(root + "/figures/import", json={
            "source": {"kind": "script", "language": language, "entrypoint_ref": key_script,
                       "assets": [key_data], "hashes": {key_script: library[0]["version"],
                       key_data: hashlib.sha256(csv).hexdigest()}}, "options": {"parse_mode": "editable"}})
        assert imported.status_code == 201, imported.text
        endpoint = root + "/figures/" + imported.json()["figure_id"]
        plotted = browser.get(endpoint)
        assert plotted.status_code == 200, plotted.text
        result = plotted.json()
        assert result["source"]["source_hash"] == hashlib.sha256(source).hexdigest()
        assert result["source"]["library_asset_ref"] == key_script
        assert result["editability"] in {"semantic", "partial"}
        if language == "python":
            assert result["native_editability"] in {"semantic", "partial"}
        else:
            assert any(o["properties"].get("text") == "Explicit dependency plot" and "text" in o["editable"]
                       for o in result["objects"])
        assert "Explicit dependency plot" in plotted.text
        assert source.splitlines()[0].decode() not in plotted.text and str(tmp_path) not in plotted.text
        assert browser.get(endpoint + "/preview").content.startswith(b"\x89PNG\r\n\x1a\n")
        # Replaying an imported figure uses Workbench's frozen copy of the
        # selected input, even when the original can no longer be verified.
        files.get_account_file(data_ref, OWNER)["path"].write_bytes(b"changed!")
        exported = browser.post(endpoint + "/export", json={"format": "svg"})
        assert exported.status_code == 200, exported.text
        assert b"<svg" in exported.content
