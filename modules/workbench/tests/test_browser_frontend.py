# SPDX-License-Identifier: AGPL-3.0-only
"""In-process browser-shell checks. No service binds a port."""
from html.parser import HTMLParser
from uuid import uuid4

from fastapi.testclient import TestClient

from wb.api import create_app
from wb.auth import issue


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.assets = []
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script" and attrs.get("type") == "module":
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


def test_frontend_route_static_contract_and_asset_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SOURCE_URL", "/corresponding-source.tar.gz?version=1&target=web")
    app = create_app(db_path=tmp_path / "ui.db", secret=b"s" * 32)
    with TestClient(app, root_path="/prefix", base_url="https://testserver") as c:
        response = c.get("/prefix/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "workbench/"
        page = c.get("/prefix/workbench/")
        assert page.status_code == 200
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert page.headers["Cache-Control"] == "no-store"
        assert "AGPL-3.0-only" in page.text
        markup = Markup(page.text)
        assert len(set(markup.ids)) == len(markup.ids)
        assert {"studio", "preview", "object-list", "version-list", "import-form", "restore-form", "export-form"} <= set(markup.ids)
        assert "/corresponding-source.tar.gz?version=1&target=web" in markup.links
        for path in [*markup.assets, "static/api.mjs", "static/views.mjs"]:
            asset = c.get("/prefix/workbench/" + path)
            assert asset.status_code == 200
            assert "AGPL-3.0-only" in asset.text
            assert asset.headers["Content-Type"].startswith(("text/css", "text/javascript", "application/javascript"))
        assert c.get("/prefix/workbench/static/index.html").status_code == 404
        assert c.get("/prefix/api/v1/session").status_code == 401
        assert "/workbench/" not in c.get("/prefix/openapi.json").json()["paths"]


def test_signed_launch_supplies_browser_session_and_prefixed_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SOURCE_URL", "//not-a-source.invalid/script")
    monkeypatch.setenv("WORKBENCH_PUBLIC_ORIGIN", "https://testserver")
    key = b"h" * 32
    app = create_app(db_path=tmp_path / "ui.db", secret=b"s" * 32, host_key=key)
    ticket = issue(key, "citrus-workbench-launch", "citrus-workbench", {
        "jti": uuid4().hex, "user_ref": "owner", "project_ref": "account", "return_to": "/?figure_session=abc",
        "source_assets": ["a" * 32],
    })
    with TestClient(app, root_path="/prefix", base_url="https://testserver") as c:
        response = c.post("/prefix/api/v1/launch", headers={"Origin": "https://testserver"}, json={"ticket": ticket})
        assert response.status_code == 200
        assert response.json()["workbench_url"] == "/prefix/workbench/"
        assert "HttpOnly" in response.headers["Set-Cookie"]
        assert "SameSite=strict" in response.headers["Set-Cookie"]
        assert c.get("/prefix/api/v1/session").json()["source_assets"] == ["a" * 32]
        page = c.get(response.json()["workbench_url"])
        assert "当前实例未配置对应源码下载地址" in page.text
        assert "not-a-source.invalid" not in page.text
        assert ticket not in page.text
