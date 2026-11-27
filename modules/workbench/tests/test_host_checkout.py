"""Opt-in bridge smoke with the real Host library module and isolated test data."""
import importlib.util
import io
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

from integrations.citrus_host import make_router
from wb.api import create_app
from wb.library import Library

pytestmark = pytest.mark.skipif(not os.getenv("CITRUS_HOST_CHECKOUT") or not os.getenv("WORKBENCH_IMAGE"),
                                reason="Set read-only Host checkout and a trusted Workbench SIF")


def test_real_host_files_to_independent_workbench(tmp_path, monkeypatch):
    checkout = Path(os.environ["CITRUS_HOST_CHECKOUT"])
    path = checkout / "01_src/src/session/files.py"
    project = tmp_path / "host-project"
    config = ModuleType("src.config")
    config.PROJECT_ROOT = project
    monkeypatch.setitem(sys.modules, "src.config", config)
    spec = importlib.util.spec_from_file_location("host_checkout_session_files", path)
    assert spec and spec.loader
    storage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(storage)
    assert storage.DB_PATH.is_relative_to(project) and storage.FILES_ROOT.is_relative_to(project)
    stream = io.BytesIO()
    Image.new("RGB", (31, 18), "orange").save(stream, format="PNG")
    sid = "session_test_51"
    png = storage.register_file(session_id=sid, owner_user_id=51, filename="figure.png",
                                content=stream.getvalue(), mime="image/png")
    storage.register_file(session_id=sid, owner_user_id=51, filename="article.pdf",
                          content=b"%PDF-1.4", mime="application/pdf")
    foreign = storage.register_file(session_id="session_test_52", owner_user_id=52, filename="foreign.png",
                                    content=stream.getvalue(), mime="image/png")

    def identity(request: Request):
        if request.headers.get("X-Test-User") != "51":
            raise HTTPException(401)
        return {"user_id": 51}

    def owned_session(selection, user):
        if selection != sid or user["user_id"] != 51:
            raise HTTPException(404)

    key = b"t" * 32
    host = FastAPI()

    @host.middleware("http")
    async def simulate_host_auth_boundary(request, call_next):
        # Host account_auth_guard requires user auth on paths under /api/.
        if request.url.path.startswith("/api/") and request.headers.get("X-Test-User") != "51":
            return JSONResponse({"detail": "Login required"}, status_code=401)
        return await call_next(request)

    host.include_router(make_router(key=key, current_user=identity, require_owned_session=owned_session,
                                    list_account_files=storage.list_account_files,
                                    get_account_file=storage.get_account_file))
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    with TestClient(host, base_url="http://127.0.0.1") as host_client:
        assert host_client.get("/_internal/workbench/assets?type=figure").status_code == 401
        bridge = Library("http://127.0.0.1", key, client=host_client)
        app = create_app(db_path=tmp_path / "workbench/state.sqlite3", library=bridge, host_key=key)
        with TestClient(app) as workbench:
            opened = host_client.post("/api/internal/workbench/open", headers={"X-Test-User": "51"},
                                      json={"session_ref": sid, "source_assets": [png["file_id"]]})
            assert opened.status_code == 200, opened.text
            assert workbench.post("/api/v1/launch", json={"ticket": opened.json()["launch_ticket"]}).status_code == 200
            rows = workbench.get("/api/v1/library?format=png").json()["assets"]
            assert len(rows) == 1 and rows[0]["version"] == png["sha256"]
            assert workbench.get(rows[0]["preview_url"]).content == stream.getvalue()
            assert workbench.get(f"/api/v1/library/{foreign['file_id']}/preview").status_code == 404
            request = {"source": {"kind": "rendered_asset", "language": "unknown", "entrypoint_ref": rows[0]["asset_ref"]}}
            imported = workbench.post("/api/v1/figures/import", json=request)
            assert imported.status_code == 201, imported.text
            result = workbench.get("/api/v1/figures/" + imported.json()["figure_id"]).json()
            assert result["source"]["source_hash"] == png["sha256"] and result["editability"] == "layout_only"
            assert str(project) not in str(result)
