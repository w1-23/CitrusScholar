"""HTTP preview receipts and real isolated cold replay, with no listener."""
import hashlib
import io
import os
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wb.api import create_app
from wb.auth import issue

BASE = "/api/v1"
HOST_KEY = b"h" * 32
PYTHON_SCRIPT = b"""import csv
import matplotlib.pyplot as plt
with open('samples.csv', newline='') as f:
    rows = list(csv.DictReader(f))
for n in range(2):
    fig, ax = plt.subplots()
    ax.plot([float(row['value']) + n for row in rows])
    ax.set_title(f'Plot {n + 1} {rows[0]["label"]}')
"""
R_SCRIPT = b"""library(ggplot2)
rows <- read.csv('samples.csv')
figures <- lapply(1:2, function(n) {
   ggplot(rows, aes(value, value)) + geom_point() + labs(title = paste('Plot', n, rows$label[1]))
})
"""
CSV = b"value,label\n3,CSV marker\n7,CSV marker\n"


def checked(response, status=200):
    assert response.status_code == status, response.text[:700]
    return response


def login(client, user="writer", project="account"):
    ticket = issue(HOST_KEY, "citrus-workbench-launch", "citrus-workbench", {
        "jti": uuid4().hex, "user_ref": user, "project_ref": project, "return_to": "/conversation/123",
    })
    checked(client.post(BASE + "/launch", json={"ticket": ticket}))


def upload(client, name, content):
    return checked(client.post(BASE + "/assets", content=content, headers={"X-Filename": name}), 201).json()


def pick_payload(source, dependency, language):
    return {"source": {"kind": "script", "entrypoint_ref": source["asset_ref"], "language": language,
        "assets": [dependency["asset_ref"]], "hashes": {x["asset_ref"]: x["source_hash"] for x in (source, dependency)}}}


def preview(client, payload, key="request-key-1"):
    return checked(client.post(BASE + "/preview-runs", json=payload, headers={"Idempotency-Key": key})).json()


def choice(card, mode="editable"):
    return {"preview_ref": card["preview_ref"], "preview_version": card["preview_version"], "options": {"parse_mode": mode}}


def test_idempotency_scope_runtime_and_missing_source_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    state = {"signature": "a" * 64, "runtime": "c" * 64}
    run_calls = []
    def fake_run(language, source, request, **kwargs):
        assert language == "python"
        assert kwargs["bundle"][1][0].body == CSV
        run_calls.append(request.copy())
        ordinal = request["figure_ordinal"]
        title = request["overrides"].get("title", {}).get("text", f"Plot {ordinal + 1}")
        with io.BytesIO() as out:
            Image.new("RGB", (30, 20), (ordinal, 0, 0)).save(out, format="PNG")
            return {"renderer": {"id": "fake", "version": "1"}, "editability": "semantic",
                "panels": [], "objects": [{"id": "title", "kind": "title", "label": "Title", "properties": {"text": title},
                                           "editable": ["text"]}], "limitations": [],
                "capture": {"ordinal": ordinal, "count": 2, "signature": state["signature"]}}, out.getvalue()
    db = tmp_path / "test.sqlite"
    settings = {"db_path": db, "secret": b"s" * 32, "host_key": HOST_KEY, "run": fake_run,
                "runtime": lambda _lang: state["runtime"]}
    with TestClient(create_app(**settings)) as client:
        login(client)
        source, dep = upload(client, "source.py", PYTHON_SCRIPT), upload(client, "samples.csv", CSV)
        payload = pick_payload(source, dep, "python")
        assert checked(client.get(BASE + "/preview-cards")).json() == {"cards": []} and not run_calls
        op = preview(client, payload)
        assert op["status"] == "ready", op
        selected = op["cards"][1]
        assert [c["label"] for c in op["cards"]] == ["Plot 1", "Plot 2"]
        assert preview(client, payload) == op and len(run_calls) == 2
        checked(client.post(BASE + "/preview-runs", json={"source": {**payload["source"], "assets": [], "hashes": {}}},
                            headers={"Idempotency-Key": "request-key-1"}), 409)
        assert checked(client.get(BASE + "/preview-runs/" + op["operation_id"])).json() == op
        assert not checked(client.get(BASE + "/figures")).json()["figures"]
        png = checked(client.get(f'{BASE}/{selected["thumbnail_url"]}'))
        assert hashlib.sha256(png.content).hexdigest() == selected["preview_sha256"] == png.headers["x-content-sha256"]
        checked(client.post(BASE + "/figures/import", json={**choice(selected), "preview_version": 9}), 404)
        imported = checked(client.post(BASE + "/figures/import", json=choice(selected)), 201).json()
        path = BASE + "/figures/" + imported["figure_id"]
        assert checked(client.get(path)).json()["source"]["receipt"]["capture"]["ordinal"] == 1
        assert len(run_calls) == 2  # confirmation uses the exact prepared revision
        state["runtime"] = "d" * 64
        edit = {"expected_revision": 1, "overrides": {"title": {"text": "Changed"}}}
        checked(client.patch(path + "/overrides", json=edit), 422)
        state.update(runtime="c" * 64, signature="b" * 64)
        checked(client.patch(path + "/overrides", json=edit), 422)
        assert checked(client.get(path)).json()["revision"] == 1
        login(client, user="reader")
        assert checked(client.get(BASE + "/preview-cards")).json() == {"cards": []}
        checked(client.get(BASE + "/preview-runs/" + op["operation_id"]), 404)
        checked(client.get(f'{BASE}/{selected["thumbnail_url"]}'), 404)
        checked(client.post(BASE + "/figures/import", json=choice(selected)), 404)
        assert preview(client, payload)["error"] == "source_missing"
        login(client, project="session:another")
        checked(client.post(BASE + "/figures/import", json=choice(selected)), 404)
        login(client)
        with sqlite3.connect(db) as sql:
            sql.execute("DELETE FROM assets WHERE id=?", (dep["asset_ref"],))
        fallback = checked(client.get(BASE + "/preview-cards")).json()["cards"][1]
        assert (fallback["source_state"], fallback["capability"]) == ("unavailable", "preview_only")
        checked(client.post(BASE + "/figures/import", json=choice(fallback)), 422)
        imported = checked(client.post(BASE + "/figures/import", json=choice(fallback, "preview_only")), 201).json()
        assert imported["editability"] == "preview_only"
        doc = checked(client.get(BASE + "/figures/" + imported["figure_id"])).json()
        assert doc["source"]["language"] == "raster" and doc["source"]["receipt"]["preview_ref"] == selected["preview_ref"]


@pytest.mark.skipif(not os.getenv("WORKBENCH_IMAGE") or not os.getenv("WORKBENCH_TAVOTTO"), reason="requires configured sandbox")
@pytest.mark.parametrize("language,name,script", [("python", "two.py", PYTHON_SCRIPT), ("r", "two.R", R_SCRIPT)])
def test_real_second_figure_survives_cold_app_and_csv(language, name, script, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    settings = {"db_path": tmp_path / "state.sqlite", "secret": b"s" * 32, "host_key": HOST_KEY}
    with TestClient(create_app(**settings)) as client:
        login(client)
        source, dep = upload(client, name, script), upload(client, "samples.csv", CSV)
        result = preview(client, pick_payload(source, dep, language))
        assert result["status"] == "ready", result
        assert [c["figure_ordinal"] for c in result["cards"]] == [0, 1]
        assert result["cards"][0]["preview_sha256"] != result["cards"][1]["preview_sha256"]
        imported = checked(client.post(BASE + "/figures/import", json=choice(result["cards"][1])), 201).json()
        path = BASE + "/figures/" + imported["figure_id"]
    with TestClient(create_app(**settings)) as cold:
        login(cold)
        doc = checked(cold.get(path)).json()
        assert doc["source"]["receipt"]["capture"]["ordinal"] == 1
        assert any(obj["properties"].get("text") == "Plot 2 CSV marker" for obj in doc["objects"])
        if language == "python":
            updated = checked(cold.patch(path + "/native-overrides", json={"expected_revision": 1,
                "patches": [{"gid": "axes_0.title", "prop": "text", "value": "Cold selected plot"}]})).json()
        else:
            updated = checked(cold.patch(path + "/overrides", json={"expected_revision": 1,
                "overrides": {"plot.title": {"text": "Cold selected plot"}}})).json()
        assert updated["revision"] == 2
        svg = checked(cold.post(path + "/export", json={"format": "svg", "width_mm": 110, "height_mm": 80}))
        assert b"Cold selected plot" in svg.content
        assert hashlib.sha256(svg.content).hexdigest() == svg.headers["x-content-sha256"]


def test_legacy_revision_omits_only_new_null_fields(tmp_path, monkeypatch):
    from wb.models import FigureDocument
    from wb.store import Store

    store = Store(tmp_path / "compatibility.sqlite")
    body = b"import matplotlib.pyplot as plt\nplt.plot([1, 2])"
    asset = store.input_asset("owner", store.put_asset("owner", "chart.py", body))
    doc = FigureDocument.model_validate({"figure_id": "figure_test", "name": "chart.py", "revision": 1,
        "renderer": {"id": "renderer", "version": "1"}, "editability": "semantic", "panels": [],
        "objects": [], "limitations": [], "source": {"language": "python", "source_ref": asset.ref,
        "source_hash": asset.sha}})
    store.create("owner", doc, b"preview", asset)
    import json
    stored = json.loads(store.revision("owner", doc.figure_id)["doc"])
    assert "capture" not in stored and "receipt" not in stored["source"]
    assert stored["native_manifest"] is None and stored["source"]["library_version"] is None
    assert FigureDocument.model_validate(stored) == doc
    provenance = {"ordinal": 0, "count": 1, "signature": "c" * 64}
    stored["capture"] = provenance
    stored["revision"] = 2
    stored["source"]["receipt"] = {"preview_ref": "preview_a", "preview_version": 1,
        "bundle_sha256": "a" * 64, "runtime_ref": "b" * 64, "capture": provenance}
    updated = FigureDocument.model_validate(stored)
    store.save("owner", updated, {}, b"preview", 1)
    saved = json.loads(store.revision("owner", doc.figure_id)["doc"])
    assert saved["capture"] == provenance and saved["source"]["receipt"] == stored["source"]["receipt"]
