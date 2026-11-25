"""Nature artifact compatibility and real workbench revision contracts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import pytest

from src.figures import projects, store, svg_projects
from src.figures.nature_plot_project import PRODUCER, open_nature_plot
from src.figures.plot_vectors import normalize_nature_svg
from src.figures.workbench.workbench_test import Library, OWNER, OTHER

SESSION = "isolated-nature-session"
PYTHON_SVG = b'''<?xml version="1.0" encoding="utf-8" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 60 40">
 <defs><style type="text/css">*{stroke-linejoin: round; stroke-linecap: butt}</style>
 <path id="dot" d="M 0 0 L 1 1 Z"/></defs>
 <use xlink:href="#dot" x="7" y="3" style="fill: #112233"/>
 <text x="12" y="15" style="font-family: 'DejaVu Sans'; font-size: 6px">n = 6</text>
</svg>'''
R_SVG = b'''<?xml version='1.0' encoding='UTF-8' ?>
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 60 40' width='60pt' height='40pt'>
<g class='svglite'>
<defs><style type='text/css'><![CDATA[
.svglite line, .svglite polyline, .svglite polygon, .svglite path, .svglite rect, .svglite circle {
fill: none; stroke: #000000; stroke-linecap: round; stroke-linejoin: round; stroke-miterlimit: 10.00; }
.svglite text { white-space: pre; }
.svglite g.glyphgroup path { fill: inherit; stroke: none; }
]]></style><clipPath id='cpYWJjZA=='><rect width='60' height='40'/></clipPath></defs>
<g clip-path='url(#cpYWJjZA==)'><line x1='4' y1='4' x2='56' y2='30' style='stroke: #112233; stroke-width: 1.07;'/>
<text x='12' y='15' textLength='26.14px' lengthAdjust='spacingAndGlyphs' style='font-family: "Liberation Sans"; font-size: 6px;'>n = 6</text>
</g></g></svg>'''


def add_original(library, name, content, *, owner=OWNER, producer=PRODUCER):
    mime = projects.MIME_BY_FORMAT[name.rsplit(".", 1)[-1]]
    entry = library.publish(owner, SESSION, name, content, mime)
    library.files[entry["file_id"]].update(origin="generated", mime_type=mime, session_id=SESSION,
        producer_tool=producer, size_bytes=entry["size"])
    return entry


def add_upload(library, name, content, *, owner=OWNER):
    entry = add_original(library, name, content, owner=owner, producer="")
    library.files[entry["file_id"]]["origin"] = "uploaded"
    return entry


def configure(tmp_path, patch):
    from src.session.manager import session_manager

    patch.setattr(session_manager, "session_belongs_to", lambda sid, owner: sid == SESSION and owner in {OWNER, OTHER})
    return Library(tmp_path, patch)


def instant_plot(library, backend, name, data, *, title=None):
    entries = {filename: add_original(library, filename, blob) for filename, blob in data.items()}
    result = open_nature_plot(owner=OWNER, session_id=SESSION, title=title or f"Actual {backend} plot",
                              backend=backend, original_exports=entries, primary_svg=name)
    return result, entries


def geometry(blob):
    ignore = {"fill", "stroke", "stroke-width"}
    return [(tag.tag, {key: val for key, val in tag.attrib.items() if key not in ignore}, tag.text)
            for tag in ET.fromstring(blob).iter()]


@pytest.mark.parametrize("backend,svg", [("python", PYTHON_SVG), ("r", R_SVG)])
def test_edit_real_native_style_preview_save_and_reopen(tmp_path, monkeypatch, backend, svg):
    library = configure(tmp_path, monkeypatch)
    originals = {"native.svg": svg, "native.pdf": b"%PDF-immutable", "native.tiff": b"II\x2a\x00immutable"}
    result, entries = instant_plot(library, backend, "native.svg", originals)
    assert result["editable"] is True
    fid = result["figure_id"]
    assert result["workspace_url"] == f"/figure-workbench?project={fid}"
    prefix = f"/api/v2/figure-workbench/projects/{fid}"
    client = TestClient(library.app(monkeypatch))
    client.cookies.set("citrus_auth", "fixture")
    assert client.get(result["workspace_url"]).status_code == 200
    detail = client.get(prefix, params={"revision": 1}).json()
    assert not detail["read_only_reason"]
    assert {row["native_tag"] for row in detail["groups"]} >= {"text", "path" if backend == "python" else "line"}
    text = next(row for row in detail["groups"] if row["native_tag"] == "text")
    assert "n = 6" in text["label"]
    before = client.get(result["preview_url"]).content
    assert b"stroke-linejoin=\"round\"" in before and b"<style" not in before
    if backend == "r":
        assert b'textLength="26.14px"' in before and b'url(#cpYWJjZA)' in before
    else:
        assert b'transform="translate(7 3)"' in before and b'<use' not in before
    change = {"group_id": text["id"], "property": "color", "value": "#286445"}
    payload = {"commands": [change], "expected_revision": 1, "draft_revision": 9}
    draft = client.post(prefix + "/preview", json=payload).json()
    assert draft["draft_revision"] == 9
    assert geometry(before) == geometry(draft["svg"])
    assert b'#286445' not in client.get(result["preview_url"]).content
    assert client.post(prefix + "/save", json={**payload, "commands": [{**change, "property": "text"}]}).status_code == 422
    assert client.post(prefix + "/save", json=payload).json()["render_version"] == 2
    assert client.post(prefix + "/save", json=payload).status_code == 409
    detail2 = client.get(prefix, params={"revision": 2}).json()
    assert detail2["original_exports"] == detail["original_exports"]
    assert next(row for row in detail2["groups"] if row["id"] == text["id"])["values"]["color"] == "#286445"
    saved = client.get(prefix + "/export/svg", params={"revision": 2})
    assert b'fill="#286445"' in saved.content and saved.content == draft["svg"].encode()
    assert saved.headers["x-figure-sha256"] == hashlib.sha256(saved.content).hexdigest()
    assert {row["file_id"] for row in store.get_project(fid, OWNER)["data_refs_json"]} == {d["file_id"] for d in entries.values()}
    for filename, entry in entries.items():
        assert library.files[entry["file_id"]]["path"].read_bytes() == originals[filename]


@pytest.mark.parametrize("svg", [
    R_SVG.replace(b"stroke: #000000", b"stroke: url(https://example.invalid/paint)"),
    R_SVG.replace(b"</g></g>", b"<foreignObject/></g></g>"),
    R_SVG.replace(b"<line", b"<line onload='bad()'"),
    R_SVG.replace(b"<line", b"<line transform='url(https://example.invalid/)'"),
    R_SVG.replace(b".svglite text", b".svglitetext"),
    R_SVG.replace(b"<text", b"<text id='nature-text-1'"),
    R_SVG.replace(b"<text", b"<?xml-stylesheet href='bad.css'?><text"),
])
def test_unsupported_svg_does_not_create_editable_or_replace_original(tmp_path, monkeypatch, svg):
    library = configure(tmp_path, monkeypatch)
    result, entries = instant_plot(library, "r", "native.svg", {"native.svg": svg})
    assert result["editable"] is False and result["reason_code"] == "plot_svg_unsupported"
    assert "figure_id" not in result and "workbench_url" not in result
    assert store.list_projects(OWNER) == []
    original = entries["native.svg"]
    assert library.files[original["file_id"]]["path"].read_bytes() == svg
    assert result["original_exports"]["native.svg"]["sha256"] == original["sha256"]


@pytest.mark.parametrize("owner,producer,tamper", [(OTHER, PRODUCER, False), (OWNER, "draw_svg", False), (OWNER, PRODUCER, True)])
def test_originals_require_correct_owner_producer_and_sha(tmp_path, monkeypatch, owner, producer, tamper):
    library = configure(tmp_path, monkeypatch)
    entry = add_original(library, "native.svg", R_SVG, producer=producer)
    if tamper:
        library.files[entry["file_id"]]["path"].write_bytes(R_SVG.replace(b"n = 6", b"n = 7"))
    with pytest.raises(projects.ProjectError, match="归属和摘要"):
        open_nature_plot(owner=owner, session_id=SESSION, title="x", backend="r",
                         original_exports={"native.svg": entry}, primary_svg="native.svg")
    assert store.list_projects(owner) == []


ACTUAL = [("python", "plot-python.xJvq", "volcano.svg", ("volcano.pdf", "volcano.tiff"), 51),
          ("r", "plot-r.Z84b", "panels.svg", ("panels.pdf", "panels.tiff"), 66),
          ("r", "plot-r.Z84b", "heatmap.svg", (), 123)]


@pytest.mark.parametrize("backend,folder,name,extra,objects", ACTUAL)
def test_actual_outputs(tmp_path, monkeypatch, backend, folder, name, extra, objects):
    evidence = os.environ.get("CITRUS_NATURE_PLOT_TEST_EVIDENCE")
    if not evidence:
        pytest.skip("Acceptance evidence directory not mounted")
    library = configure(tmp_path, monkeypatch)
    files = {member: (Path(evidence)/folder/member).read_bytes() for member in (name, *extra)}
    result, _ = instant_plot(library, backend, name, files)
    assert result["editable"], result
    client = TestClient(library.app(monkeypatch))
    detail = f"/api/v2/figure-workbench/projects/{result['figure_id']}"
    groups = client.get(detail, params={"revision": 1}).json()["groups"]
    assert len(groups) == objects
    text = next(row for row in groups if row["native_tag"] == "text")
    payload = {"expected_revision": 1, "commands": [{"group_id": text["id"], "property": "color", "value": "#a11566"}]}
    assert client.post(detail + "/save", json=payload).status_code == 200
    view = client.get(detail, params={"revision": 2}).json()
    assert next(row for row in view["groups"] if row["id"] == text["id"])["values"]["color"] == "#a11566"
    receipt = store.get_project(result["figure_id"], OWNER)["manifest_json"]["current_qa_receipt"]
    assert receipt["artifacts"][0]["role"] == "figure"
    derivative = normalize_nature_svg(files[name], backend)
    assert derivative.startswith('<svg')
    for member, metadata in result["original_exports"].items():
        assert hashlib.sha256(files[member]).hexdigest() == metadata["sha256"]


def serve_plot_fixture(root, evidence, port=0):
    """Launch only the fixture app, not the production API or project store."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    import json
    import uvicorn

    with pytest.MonkeyPatch.context() as patch:
        library = configure(Path(root), patch)
        app = library.app(patch)

        @app.get("/api/v2/files/{file_id}/download")
        async def original(file_id: str):
            entry = library.get(file_id, OWNER)
            if not entry:
                raise HTTPException(404)
            return FileResponse(entry["path"], filename=entry["filename"], media_type=entry["mime_type"])

        details = []
        for backend, folder, name, extra, _ in ACTUAL:
            files = {member: (Path(evidence)/folder/member).read_bytes() for member in (name, *extra)}
            result, _ = instant_plot(library, backend, name, files, title=f"Actual {backend} {name}")
            assert result["editable"], result
            details.append(result)
            if backend == "r":
                upload = add_upload(library, name, files[name])
                details.append({"uploaded": upload["file_id"], "filename": name})
        print("NATURE_PLOT_FIXTURE=" + json.dumps(details), flush=True)
        uvicorn.run(app, host="127.0.0.1", port=int(port))


def test_owned_upload_requires_opt_in_and_never_promotes_worker_origin(tmp_path, monkeypatch):
    library = configure(tmp_path, monkeypatch)
    client = TestClient(library.app(monkeypatch))
    endpoint = "/api/v2/figure-workbench/import-svg"
    foreign = add_upload(library, "foreign.svg", R_SVG, owner=OTHER)
    generated = add_original(library, "trusted.svg", R_SVG)
    not_svg = add_upload(library, "native.pdf", b"%PDF-immutable")
    malicious = add_upload(library, "bad.svg", R_SVG.replace(b"<line", b"<line onload='forbidden()'"))
    malicious_python = add_upload(library, "badpython.svg", PYTHON_SVG.replace(
        b"<defs>", b"<metadata><script>forbidden()</script></metadata><defs>"))
    for rejected in (foreign, generated):
        assert client.post(endpoint, json={"file_id": rejected["file_id"]}).status_code == 404
    assert client.post(endpoint, json={"file_id": "../../danger.svg"}).status_code == 422
    assert client.post(endpoint, json={"file_id": not_svg["file_id"]}).status_code == 422
    for rejected in (malicious, malicious_python):
        result = client.post(endpoint, json={"file_id": rejected["file_id"]}).json()
        assert result["origin"] == "uploaded" and result["editable"] is False
        assert result["reason_code"] == "plot_svg_unsupported"
        assert "backend" not in result and "workbench_url" not in result
    assert store.list_projects(OWNER) == []


@pytest.mark.parametrize("backend,folder,name,extra,objects", ACTUAL)
def test_actual_owned_uploads_have_truthful_origin(tmp_path, monkeypatch, backend, folder, name, extra, objects):
    evidence = os.environ.get("CITRUS_NATURE_PLOT_TEST_EVIDENCE")
    if not evidence:
        pytest.skip("Acceptance evidence directory not mounted")
    library = configure(tmp_path, monkeypatch)
    original = (Path(evidence)/folder/name).read_bytes()
    file = add_upload(library, name, original)
    client = TestClient(library.app(monkeypatch))
    response = client.post("/api/v2/figure-workbench/import-svg", json={"file_id": file["file_id"]})
    assert response.status_code == 200
    result = response.json()
    assert result["editable"] and result["origin"] == "uploaded", result
    assert result["vector_dialect"] == ("svglite" if backend == "r" else "matplotlib")
    assert "backend" not in result
    assert result["original_exports"][name]["file_id"] == file["file_id"]
    detail = f"/api/v2/figure-workbench/projects/{result['figure_id']}"
    summary = client.get(detail, params={"revision": 1}).json()
    assert "未执行绘图" in summary["presentation_note"]
    assert len(summary["groups"]) == objects
    target = next(row for row in summary["groups"] if row["native_tag"] == "text")
    reply = client.post(detail + "/save", json={"expected_revision": 1,
       "commands": [{"group_id": target["id"], "property": "color", "value": "#7a3c96"}]})
    assert reply.status_code == 200
    assert client.get(detail, params={"revision": 2}).json()["original_exports"] == summary["original_exports"]
    assert library.files[file["file_id"]]["path"].read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == result["original_exports"][name]["sha256"]
