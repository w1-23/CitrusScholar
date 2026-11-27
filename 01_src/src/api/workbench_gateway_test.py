from pathlib import Path

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

    def wire(request):
        seen.append((request.url.path, request.headers.get("cookie")))
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
        actor["user_id"] = 42
        assert client.get("/workbench/api/v1/session").status_code == 403
        assert seen[-1] == ("/api/v1/session", None)
        assert client.post("/workbench/api/v1/standalone", json={}).status_code == 404
