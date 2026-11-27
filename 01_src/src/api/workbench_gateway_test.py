from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from src.api.workbench_bridge_protocol import launch_ticket
from src.api.workbench_gateway import workbench_gateway


KEY = b"fixture-workbench-protocol-key-32-bytes"


def test_workbench_gateway_binds_issued_cookie_to_host_actor(monkeypatch, tmp_path):
    from src.api import workbench_gateway as gateway_module

    keyfile = tmp_path / "gateway.key"
    keyfile.write_bytes(KEY)
    monkeypatch.setenv("WORKBENCH_HOST_KEY_FILE", str(keyfile))
    monkeypatch.setenv("CITRUS_WORKBENCH_UPSTREAM", "http://127.0.0.1:8062")
    seen = []
    metadata = []

    def wire(request):
        seen.append((request.url.path, request.headers.get("cookie")))
        metadata.append((request.headers.get("x-filename"), request.headers.get("idempotency-key")))
        if request.url.path == "/api/v1/launch":
            return httpx.Response(200, json={"ready": True}, headers={
                "set-cookie": "workbench_session=issued-token; Path=/; Secure; HttpOnly; SameSite=Strict"})
        return httpx.Response(200 if request.headers.get("cookie") == "workbench_session=issued-token" else 403,
                              json={"checked": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", lambda **kwargs: real(
        transport=httpx.MockTransport(wire), **kwargs))
    actor = {"user_id": 41}
    app = FastAPI()
    app.include_router(workbench_gateway(lambda _request: actor))
    with TestClient(app, base_url="https://host.local", cookies={"citrus_auth": "not-for-workbench"}) as client:
        assert client.post("/workbench/api/v1/launch", json={"ticket": launch_ticket(KEY,
            "citrus-workbench", 42, "", [])}).status_code == 403
        assert not seen
        assert client.post("/workbench/api/v1/launch", json={"ticket": launch_ticket(KEY,
            "citrus-workbench", 41, "", [])}).status_code == 200
        assert seen[-1] == ("/api/v1/launch", None)
        assert client.get("/workbench/api/v1/session").status_code == 200
        assert seen[-1] == ("/api/v1/session", "workbench_session=issued-token")
        assert client.post("/workbench/api/v1/assets", content=b"print(1)",
                           headers={"X-Filename": "drawing.py", "Idempotency-Key": "not-forwarded"}).status_code == 200
        assert metadata[-1] == ("drawing.py", None)
        assert client.post("/workbench/api/v1/preview-runs", json={},
                           headers={"Idempotency-Key": "preview-test-key", "X-Filename": "not-forwarded"}).status_code == 200
        assert metadata[-1] == (None, "preview-test-key")
        before = len(seen)
        assert client.post("/workbench/api/v1/preview-runs", json={},
                           headers=[("idempotency-key", "key-one"), ("idempotency-key", "key-two")]).status_code == 400
        assert len(seen) == before
        actor["user_id"] = 42
        assert client.get("/workbench/api/v1/session").status_code == 403
        assert seen[-1] == ("/api/v1/session", None)
        assert client.post("/workbench/api/v1/standalone", json={}).status_code == 404


def test_new_browser_routes_load_the_real_host_account(monkeypatch):
    from fastapi import Request
    from src.api import main
    from src import auth

    monkeypatch.setenv("CITRUS_WORKBENCH_BRIDGE_ENABLED", "1")
    monkeypatch.setattr(auth, "user_for_token_readonly", lambda token: {"user_id": 59} if token == "example" else None)
    app = FastAPI()
    app.middleware("http")(main.account_auth_guard)

    def expected(request: Request):
        return main.current_user(request)

    for path in ("/figures", "/workbench/workbench/", "/workbench/api/v1/session"):
        app.add_api_route(path, expected)
    with TestClient(app) as client:
        for path in ("/figures", "/workbench/workbench/", "/workbench/api/v1/session"):
            assert client.get(path).status_code == 401
        client.cookies.set("citrus_auth", "example")
        for path in ("/figures", "/workbench/workbench/", "/workbench/api/v1/session"):
            assert client.get(path).json() == {"user_id": 59}
