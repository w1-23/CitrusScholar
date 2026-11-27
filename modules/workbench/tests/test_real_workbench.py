"""End to end through HTTP, persistence, and real Apptainer renderers."""
import hashlib
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wb.api import create_app

BASE = "/api/v1"
FIXTURE = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(not os.getenv("WORKBENCH_IMAGE") or not os.getenv("WORKBENCH_TAVOTTO"),
                                reason="Set real worker image and pinned Tavotto checkout")


def require(response, status=200):
    assert response.status_code == status, response.content[:650]
    return response


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    with TestClient(create_app(db_path=tmp_path / "state.sqlite", standalone_token="test")) as api:
        require(api.post(BASE + "/standalone", json={"token": "test"}))
        yield api


def load(client, name, body=None, *, kind="script", mode="auto"):
    source = (FIXTURE / name).read_bytes() if body is None else body
    asset = require(client.post(BASE + "/assets", content=source, headers={"X-Filename": name}), 201).json()["asset_ref"]
    result = require(client.post(BASE + "/figures/import", json={
        "source": {"kind": kind, "language": "unknown", "entrypoint_ref": asset}, "options": {"parse_mode": mode},
    }), 201).json()
    return BASE + "/figures/" + result["figure_id"], result, hashlib.sha256(source).hexdigest()


def get_element(document, gid):
    return next(el for el in document["native_manifest"]["elements"] if el["gid"] == gid)


def prop(document, gid, name):
    return next(f["value"] for f in get_element(document, gid)["editable"] if f["prop"] == name)


def test_real_matplotlib_native_pick_edit_export_restore(client):
    url, imported, source_sha = load(client, "matplotlib_native.py", mode="editable")
    assert imported["renderer_id"] == "tavotto-matplotlib"
    initial = require(client.get(url)).json()
    assert initial["source"]["source_hash"] == source_sha
    x, y, w, h = get_element(initial, "axes_0.title")["bbox"]
    selection = require(client.post(url + "/pick", json={"x": x + w / 2, "y": y + h / 2, "revision": 1})).json()
    assert selection["picked_gid"] == "axes_0.title", selection
    preview = require(client.get(url + "/preview")).content
    assert Image.open(io.BytesIO(preview)).format == "PNG"
    fields = [
        {"gid": "axes_0.title", "prop": "text", "value": "Native title"},
        {"gid": "axes_0.title", "prop": "fontsize", "value": 23},
        {"gid": "axes_0.lines_0", "prop": "linewidth", "value": 4},
        {"gid": "axes_0.lines_0", "prop": "color", "value": "#aabbcc"},
    ]
    updated = require(client.patch(url + "/native-overrides", json={"expected_revision": 1, "patches": fields})).json()
    assert updated["revision"] == 2 and prop(updated, "axes_0.title", "fontsize") == 23
    assert prop(updated, "axes_0.lines_0", "linewidth") == 4
    assert updated["source"]["source_hash"] == source_sha
    unwanted = {"expected_revision": 2, "patches": [{"gid": "axes_0.lines_0", "prop": "color", "value": "invalid"}]}
    require(client.patch(url + "/native-overrides", json=unwanted), 422)
    assert require(client.get(url)).json()["revision"] == 2
    for fmt in ("svg", "png", "pdf"):
        response = require(client.post(url + "/export", json={"format": fmt, "width_mm": 127, "height_mm": 76.2, "dpi": 100}))
        assert response.headers["x-figure-revision"] == "2"
        assert hashlib.sha256(response.content).hexdigest() == response.headers["x-content-sha256"]
        if fmt == "svg":
            assert b"Native title" in response.content and b"#aabbcc" in response.content
        elif fmt == "png":
            assert Image.open(io.BytesIO(response.content)).size == (500, 300)
        else:
            assert response.content.startswith(b"%PDF-")
    reverted = require(client.post(url + "/restore", json={"expected_revision": 2, "revision": 1})).json()
    assert reverted["revision"] == 3 and reverted["native_manifest"] == initial["native_manifest"]
    assert require(client.get(url + "/preview")).content == preview


def test_real_shared_colorbar_group_expands_and_moves_atomically(client):
    url, _, _ = load(client, "matplotlib_groups.py")
    before = require(client.get(url)).json()
    group = before["native_manifest"]["groups"][0]
    gid, members = group["gid"], group["members"]
    assert len(members) == 3 and len(group["subplot_gids"]) == 2
    assert require(client.get(f"{url}/groups/{gid}")).json()["group"]["members"] == members
    blocked = require(client.post(url + "/groups/resolve", json={
        "gids": [gid, members[0]], "locked_gids": [group["colorbar_gid"]],
    })).json()
    assert blocked["blocked"][gid] == "locked" and blocked["expanded_gids"] == []
    resolution = require(client.post(url + "/groups/resolve", json={"gids": [gid]})).json()
    assert resolution["expanded_gids"] == members
    move = {"expected_revision": 1, "dx_mm": 3, "dy_mm": 1}
    require(client.post(f"{url}/groups/{gid}/move", json={**move, "locked_gids": [group["colorbar_gid"]]}), 422)
    after = require(client.post(f"{url}/groups/{gid}/move", json=move)).json()
    assert after["revision"] == 2
    for member in members:
        old = prop(before, member, "position")
        new = prop(after, member, "position")
        assert new[0] - old[0] == pytest.approx(3 / before["layout"]["width_mm"], abs=.001)
        assert new[1] - old[1] == pytest.approx(-1 / before["layout"]["height_mm"], abs=.001)
    scaled = require(client.post(f"{url}/groups/{gid}/scale", json={"expected_revision": 2, "factor": 1.1})).json()
    assert scaled["revision"] == 3
    for member in members:
        old = prop(after, member, "position")
        new = prop(scaled, member, "position")
        assert new[2] == pytest.approx(old[2] * 1.1, abs=.001)
        assert new[3] == pytest.approx(old[3] * 1.1, abs=.001)


def test_real_r_semantic_and_base_static_downgrade(client):
    url, imported, _ = load(client, "ggplot_native.R", mode="editable")
    assert imported["detected_language"] == "r" and imported["renderer_id"] == "r-ggplot2"
    edited = require(client.patch(url + "/overrides", json={"expected_revision": 1,
                    "overrides": {"plot.title": {"text": "Native R title"}}})).json()
    assert any(obj["properties"]["text"] == "Native R title" for obj in edited["objects"])
    assert b"Native R title" in require(client.post(url + "/export", json={"format": "svg"})).content
    static, base, _ = load(client, "base_static.R")
    assert base["renderer_id"] == "r-base" and base["editability"] == "layout_only"
    assert require(client.get(static)).json()["objects"] == []
    assert require(client.post(static + "/export", json={"format": "pdf"})).content.startswith(b"%PDF-")


def test_real_svg_static_preview_and_export(client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80"><rect width="30" height="30" fill="red"/></svg>'
    url, imported, _ = load(client, "static.svg", svg, kind="rendered_asset")
    assert imported["editability"] == "layout_only"
    assert Image.open(io.BytesIO(require(client.get(url + "/preview")).content)).format == "PNG"
    assert require(client.post(url + "/export", json={"format": "pdf"})).content.startswith(b"%PDF-")
