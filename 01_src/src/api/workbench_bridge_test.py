from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
    script = put(SESSION, OWNER, "script.py")
    put(SESSION, OWNER, "hidden.svg", visibility="internal")
    query = {"type": "figure"}
    assert client.get(LIST, params=query).status_code == 401
    inventory = client.get(LIST, params=query, headers=assertion(key))
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
