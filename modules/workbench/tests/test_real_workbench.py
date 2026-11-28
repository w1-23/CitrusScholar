"""End to end through HTTP, persistence, and real Apptainer renderers."""
import hashlib
import io
import os
import subprocess
import sys
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


def test_real_explicit_csv_survives_new_api_process(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    database = tmp_path / "cold.sqlite"
    with TestClient(create_app(db_path=database, standalone_token="test")) as client:
        require(client.post(BASE + "/standalone", json={"token": "test"}))
        code = b"""import csv
from pathlib import Path
import matplotlib.pyplot as plt
with open('observations.csv', newline='') as file:
    rows = list(csv.DictReader(file))
assert Path(__file__).with_name('observations.csv').is_file()
fig, ax = plt.subplots()
ax.plot([float(row['value']) for row in rows])
ax.set_xlabel(rows[0]['series'])
ax.set_title('Before restart')
"""
        assets = [require(client.post(BASE + "/assets", content=body, headers={"X-Filename": name}), 201).json()
                  for name, body in [("chart.py", code), ("observations.csv", b"series,value\nCSV series,3\nCSV series,7\n")]]
        source = {"kind": "script", "language": "python", "entrypoint_ref": assets[0]["asset_ref"],
                  "hashes": {assets[0]["asset_ref"]: assets[0]["source_hash"]}}
        payload = {"source": source, "options": {"parse_mode": "editable"}}
        require(client.post(BASE + "/figures/import", json=payload), 422)
        assert require(client.get(BASE + "/figures")).json() == {"figures": []}
        source["assets"] = [assets[1]["asset_ref"]]
        source["hashes"][assets[1]["asset_ref"]] = assets[1]["source_hash"]
        result = require(client.post(BASE + "/figures/import", json=payload), 201).json()
        assert result["renderer_id"] == "tavotto-matplotlib"
        url = BASE + "/figures/" + result["figure_id"]
        assert prop(require(client.get(url)).json(), "axes_0.xlabel", "text") == "CSV series"
    # A fresh API interpreter with no import upload handles, cache, or Host access.
    child = r'''import hashlib, sys
from fastapi.testclient import TestClient
from wb.api import create_app

database, url, sha = sys.argv[1:]
with TestClient(create_app(db_path=database, standalone_token="test")) as client:
    assert client.post("/api/v1/standalone", json={"token": "test"}).is_success
    edit = client.patch(url + "/native-overrides", json={"expected_revision": 1,
        "patches": [{"gid": "axes_0.title", "prop": "text", "value": "After cold start"}]})
    assert edit.status_code == 200, edit.text
    assert edit.json()["revision"] == 2 and edit.json()["source"]["source_hash"] == sha
    svg = client.post(url + "/export", json={"format": "svg"})
    assert svg.status_code == 200, svg.text[:500]
    assert b"After cold start" in svg.content and b"CSV series" in svg.content
    assert svg.headers["x-figure-revision"] == "2"
    assert hashlib.sha256(svg.content).hexdigest() == svg.headers["x-content-sha256"]
'''
    result = subprocess.run([sys.executable, "-c", child, str(database), url, assets[0]["source_hash"]],
                            capture_output=True, text=True, timeout=80)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_r_script_resolves_only_passed_relative_data(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    code = b"""library(ggplot2)
observations <- read.csv('observations.csv')
p <- ggplot(observations, aes(x, y, colour=series)) + geom_point() + labs(title=observations$label[1])
print(p)
"""
    data = b"x,y,label,series\n1,2,R CSV title,Frozen series\n3,4,R CSV title,Frozen series\n"
    db = tmp_path / "r-cold.sqlite"
    with TestClient(create_app(db_path=db, standalone_token="test")) as client:
        require(client.post(BASE + "/standalone", json={"token": "test"}))
        inputs = [require(client.post(BASE + "/assets", content=body, headers={"X-Filename": name}), 201).json()
                  for name, body in [("chart.R", code), ("observations.csv", data)]]
        result = require(client.post(BASE + "/figures/import", json={"source": {"kind": "script", "language": "r",
            "entrypoint_ref": inputs[0]["asset_ref"], "assets": [inputs[1]["asset_ref"]]},
            "options": {"parse_mode": "editable"}}), 201).json()
        assert result["renderer_id"] == "r-ggplot2"
        url = BASE + "/figures/" + result["figure_id"]
        assert b"R CSV title" in require(client.post(url + "/export", json={"format": "svg"})).content
    with TestClient(create_app(db_path=db, standalone_token="test")) as reopened:
        require(reopened.post(BASE + "/standalone", json={"token": "test"}))
        before = require(reopened.get(url)).json()
        edited = require(reopened.patch(url + "/overrides", json={"expected_revision": 1,
            "overrides": {"plot.title": {"text": "After R reopen"}}})).json()
        assert edited["revision"] == 2
        assert before["source"] == edited["source"]
        assert edited["source"]["source_hash"] == hashlib.sha256(code).hexdigest()
        svg = require(reopened.post(url + "/export", json={"format": "svg"}))
        assert b"After R reopen" in svg.content and b"Frozen series" in svg.content
        assert svg.headers["x-figure-revision"] == "2"
        assert svg.headers["x-content-sha256"] == hashlib.sha256(svg.content).hexdigest()


def test_real_native_canvas_resize_updates_preview_and_allows_explicit_export_width(client):
    url, _, _ = load(client, "matplotlib_native.py", mode="editable")
    before = require(client.get(url)).json()
    figure = next(element for element in before["native_manifest"]["elements"] if element["role"] == "figure")
    updates = {"expected_revision": 1, "patches": [
        {"gid": figure["gid"], "prop": "size_mm", "value": [127, 76.2]},
    ]}
    resized = require(client.patch(url + "/native-overrides", json=updates)).json()
    assert resized["revision"] == 2
    assert resized["native_manifest"]["size_mm"] == pytest.approx([127, 76.2])
    assert resized["layout"] == pytest.approx({"width_mm": 127, "height_mm": 76.2})
    expected = [(None, None, (500, 300)), (101.6, None, (400, 300)), (101.6, 50.8, (400, 200))]
    for width, height, pixels in expected:
        params = {"format": "png", "dpi": 100}
        if width:
            params["width_mm"] = width
        if height:
            params["height_mm"] = height
        png = require(client.post(url + "/export", json=params))
        assert Image.open(io.BytesIO(png.content)).size == pixels
    assert require(client.get(url)).json()["layout"] == resized["layout"]



@pytest.mark.skipif(not os.getenv("WORKBENCH_FROZEN_PACKAGES"), reason="Frozen codec runtime not configured")
@pytest.mark.parametrize("previous", [False, True])
def test_real_frozen_import_cold_edit_and_export(tmp_path, monkeypatch, previous):
    import json
    from uuid import uuid4
    from wb.auth import issue
    from wb.bundle import InputAsset, digest
    from wb.frozen_inputs import FrozenInputs

    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    ref = "1" * 32
    source = (b"fig, ax = plt.subplots()\n"
              b"frame = data['chart']\n"
              b"assert frame['value'].tolist() == [2.25, 4.125]\n"
              b"ax.bar(frame['sample'], frame['value'])\n"
              b"ax.set_title('Baseline')\n")
    integers = {"dtype": {"kind": "dtype", "name": "int64"}}
    int_values = lambda values: [{"t": "int", "v": str(v)} for v in values]
    table = json.dumps({"format": "citrus.dataset", "version": 1, "columns": ["sample", "value"],
        "index": {**integers, "values": int_values(range(2)), "name": {"t": "none"}, "range": [0, 2, 1]},
        "series": [{**integers, "values": int_values([1, 2])},
                   {"dtype": {"kind": "dtype", "name": "float64"},
                    "values": [{"t": "float", "v": v.hex()} for v in (2.25, 4.125)]}]}).encode()

    class HostLibrary:
        def frozen_inputs(self, actor, project_ref):
            assert actor.project == "session:frozen-render-fixture" and project_ref == ref
            return FrozenInputs(InputAsset.snapshot("render.py", digest(source), source),
                                (InputAsset.snapshot("input_0.json", digest(table), table),), ("chart",))

    key = b"f" * 32
    options = {"db_path": tmp_path / "frozen.sqlite", "secret": b"s" * 32, "host_key": key,
               "library": HostLibrary()}

    def launch(client):
        ticket = issue(key, "citrus-workbench-launch", "citrus-workbench", {
            "jti": uuid4().hex, "user_ref": "51", "project_ref": "session:frozen-render-fixture",
            "frozen_project": ref, "return_to": "/"})
        require(client.post(BASE + "/launch", json={"ticket": ticket}))

    if previous:
        historical = os.environ.get("WORKBENCH_PREVIOUS_SOURCE")
        if not historical:
            pytest.skip("Previous frozen release not configured")
        (tmp_path / "script.py").write_bytes(source)
        (tmp_path / "input.json").write_bytes(table)
        code = """
from pathlib import Path
import sys
from wb.auth import Session
from wb.bundle import InputAsset, digest
from wb.figures import Figures
from wb.frozen_inputs import FrozenInputs
from wb.runtime_pin import identity
from wb.sandbox import run
from wb.store import Store
root=Path(sys.argv[1]); source=(root/'script.py').read_bytes(); table=(root/'input.json').read_bytes()
inputs=FrozenInputs(InputAsset.snapshot('render.py',digest(source),source),
  (InputAsset.snapshot('input_0.json',digest(table),table),), ('chart',))
owner=Session('host:51','session:frozen-render-fixture','/').owner
print(Figures(Store(root/'frozen.sqlite'),run,identity).import_frozen(owner,'1'*32,1,inputs).figure_id)
"""
        figure_id = subprocess.check_output([sys.executable, "-B", "-c", code, str(tmp_path)],
            cwd=Path(historical), env=os.environ | {"PYTHONPATH": historical}, text=True, timeout=60).strip()
    else:
        with TestClient(create_app(**options)) as api:
            launch(api)
            imported = require(api.post(BASE + "/figures/import-frozen",
                                        json={"project_ref": ref, "render_version": 1}), 201).json()
            figure_id = imported["figure_id"]
    with TestClient(create_app(**options)) as api:
        launch(api)
        url = BASE + "/figures/" + figure_id
        before = require(api.get(url)).json()
        assert prop(before, "axes_0.title", "text") == "Baseline"
        assert before["source"]["source_hash"] == digest(source)
    with TestClient(create_app(**options)) as api:
        launch(api)
        patch = {"expected_revision": 1, "patches": [{"gid": "axes_0.title", "prop": "text", "value": "Revised"}]}
        if previous:
            with monkeypatch.context() as missing:
                missing.delenv("WORKBENCH_PREVIOUS_SOURCE", raising=False)
                missing.delenv("WORKBENCH_OLDER_SOURCE", raising=False)
                assert api.patch(url + "/native-overrides", json=patch).status_code >= 400
            assert require(api.get(url)).json()["revision"] == 1
        after = require(api.patch(url + "/native-overrides", json=patch)).json()
        assert after["revision"] == 2 and prop(after, "axes_0.title", "text") == "Revised"
        png = require(api.post(url + "/export", json={"format": "png"}))
        assert Image.open(io.BytesIO(png.content)).format == "PNG"
        assert png.headers["x-content-sha256"] == digest(png.content)


@pytest.mark.skipif(not os.getenv("WORKBENCH_PREVIOUS_SOURCE"), reason="Previous sealed runtime not configured")
@pytest.mark.parametrize("language", ["python", "r"])
def test_real_reopen_previous_runtime_receipt(tmp_path, monkeypatch, language):
    import json

    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    db = tmp_path / "previous.sqlite"
    boot = r"""
import json, sys
from fastapi.testclient import TestClient
from wb.api import create_app
with TestClient(create_app(db_path=sys.argv[1], standalone_token='test')) as api:
    assert api.post('/api/v1/standalone', json={'token':'test'}).is_success
    language = sys.argv[2]
    refs = []
    r_source = [('chart.R', b"library(ggplot2)\nd <- read.csv('exact.csv')\nprint(ggplot(d,aes(x,y))+geom_point()+labs(title='Old title'))"),
                       ('exact.csv', b'x,y\n1,2\n3,4\n')]
    py_source = [('chart.py', b"import csv\nimport matplotlib.pyplot as plt\nwith open('exact.csv') as f: data=list(csv.DictReader(f))\nfig,ax=plt.subplots()\nax.plot([float(r['x']) for r in data],[float(r['y']) for r in data])\nax.set_title('Old title')\n"), r_source[1]]
    for name, blob in r_source if language == 'r' else py_source:
        reply = api.post('/api/v1/assets', headers={'X-Filename':name}, content=blob)
        assert reply.status_code == 201, reply.text
        refs.append(reply.json()['asset_ref'])
    run = api.post('/api/v1/preview-runs', headers={'Idempotency-Key':'previous-receipt-fixture'},
        json={'source':{'kind':'script','language':language,'entrypoint_ref':refs[0],'assets':refs[1:]}})
    assert run.is_success, run.text
    card, = run.json()['cards']
    reply = api.post('/api/v1/figures/import', json={'preview_ref':card['preview_ref'],
        'preview_version':card['preview_version'], 'options':{'parse_mode':'editable'}})
    assert reply.status_code == 201, reply.text
    url = '/api/v1/figures/' + reply.json()['figure_id']
    print(json.dumps([url, api.get(url).json()['source'], {'preview_ref':card['preview_ref'], 'preview_version':card['preview_version']}]))
"""
    previous = os.environ["WORKBENCH_PREVIOUS_SOURCE"]
    old = subprocess.check_output([sys.executable, '-B', '-c', boot, str(db), language], cwd=previous,
                                  env=os.environ | {'PYTHONPATH': previous}, text=True, timeout=80)
    url, identity, prepared = json.loads(old)
    assert identity["receipt"]["runtime_ref"]
    with TestClient(create_app(db_path=db, standalone_token="test")) as api:
        require(api.post(BASE + "/standalone", json={"token": "test"}))
        imported = require(api.post(BASE + "/figures/import", json=prepared), 201).json()
        assert imported["detected_language"] == language
        imported_url = BASE + "/figures/" + imported["figure_id"]
        assert require(api.get(imported_url)).json()["source"]["receipt"] == identity["receipt"]
        patch = {"expected_revision": 1, "overrides": {"plot.title": {"text": "New app, old renderer"}}}
        edit_url = url + "/overrides"
        if language == "python":
            edit_url = url + "/native-overrides"
            patch = {"expected_revision": 1, "patches": [{"gid":"axes_0.title", "prop":"text", "value":"New app, old renderer"}]}
        with monkeypatch.context() as missing:
            missing.setenv("WORKBENCH_PREVIOUS_SOURCE", str(tmp_path))
            assert api.patch(edit_url, json=patch).status_code >= 400
        assert require(api.get(url)).json()["revision"] == 1
        changed = require(api.patch(edit_url, json=patch)).json()
        assert changed["revision"] == 2 and {k:v for k,v in changed["source"].items() if v is not None} == {k:v for k,v in identity.items() if v is not None}
        output = require(api.post(url + "/export", json={"format":"svg"}))
        assert b"New app, old renderer" in output.content
        assert hashlib.sha256(output.content).hexdigest() == output.headers["x-content-sha256"]


@pytest.mark.skipif(not os.getenv("WORKBENCH_TEST_HOST_SANDBOX") or not os.getenv("WORKBENCH_FROZEN_PACKAGES"),
                    reason="Set sealed Host sandbox and frozen package paths")
def test_real_styled_import_survives_disabled_intake_in_new_process(tmp_path, monkeypatch):
    from uuid import uuid4
    from wb.auth import issue
    from wb.bundle import InputAsset, digest
    from wb.frozen_inputs import FrozenInputs

    script = ("fig, ax = plt.subplots()\nframe = data['tidy']\n"
              "assert frame['y'].tolist() == [2.25, 4.125]\nax.bar(frame['x'], frame['y'])\n"
              "ax.set_title('Original title')\n")
    (tmp_path / "render.py").write_text(script)
    make = """
from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import pandas as pd
from citrus_figures.core import RenderedFigure
from citrus_figures.dataset_codec import encode_frame, decode_frame
from citrus_figures.manifest import assign_locators, build_manifest
from citrus_figures.overrides import apply_overrides, validate_overrides
from citrus_figures.style_replay import coordinate_sha256, replay_baseline, typed_payload_sha256
from citrus_figures.styling import prepare_figure_fonts
frame = encode_frame(pd.DataFrame({'x': [1, 2], 'y': [2.25, 4.125]}))
Path('input_0.json').write_text(json.dumps(frame))
state = {'plt': plt, 'data': {'tidy': decode_frame(frame)}}
exec(Path('render.py').read_text(), state)
fig = state['fig']
prepare_figure_fonts(fig)
rendered = assign_locators(RenderedFigure(figure=fig, axes=list(fig.axes), qa_profile={}))
manifest = build_manifest(rendered, source='script', include_qa=False)
base = replay_baseline(fig, manifest['objects'])
gid = next(obj['gid'] for obj in manifest['objects'] if obj['role'] == 'title')
edits = validate_overrides([{'gid': gid, 'prop': 'text', 'value': 'Host edited'}], manifest)
assert apply_overrides(rendered, manifest, edits) == [gid]
assert coordinate_sha256(fig) == base['coordinates_sha256']
Path('style.json').write_text(json.dumps({'baseline': base, 'overrides': edits,
    'overrides_sha256': typed_payload_sha256(edits)}))
"""
    packages = os.environ["WORKBENCH_FROZEN_PACKAGES"]
    subprocess.run(["apptainer", "exec", "--cleanenv", "--env", f"PYTHONPATH={packages},MPLCONFIGDIR={tmp_path}",
                    os.environ["WORKBENCH_TEST_HOST_SANDBOX"], "python", "-B", "-c", make],
                   cwd=tmp_path, check=True, timeout=40)

    def snapshot(name):
        data = (tmp_path / name).read_bytes()
        return InputAsset.snapshot(name, digest(data), data)

    class Host:
        def frozen_inputs(self, actor, ref, version):
            assert actor.project == "session:styled-fixture" and ref == "1" * 32 and version == 2
            return FrozenInputs(snapshot("render.py"), (snapshot("input_0.json"),), ("tidy",),
                                version, (tmp_path / "style.json").read_bytes())

    monkeypatch.setenv("WORKBENCH_SECURE_COOKIE", "0")
    monkeypatch.setenv("WORKBENCH_STYLED_IMPORT", "1")
    key = b"h" * 32
    db = tmp_path / "figures.sqlite3"
    with TestClient(create_app(db_path=db, host_key=key, library=Host())) as api:
        ticket = issue(key, "citrus-workbench-launch", "citrus-workbench", {"jti": uuid4().hex,
                       "user_ref": "51", "project_ref": "session:styled-fixture", "return_to": "/",
                       "frozen_project": "1" * 32, "frozen_render_version": 2})
        require(api.post(BASE + "/launch", json={"ticket": ticket}))
        doc = require(api.post(BASE + "/figures/import-frozen", json={"project_ref": "1" * 32,
                                                                     "render_version": 2}), 201).json()
        url = BASE + "/figures/" + doc["figure_id"]
        assert prop(require(api.get(url)).json(), "axes_0.title", "text") == "Host edited"

    cold = """
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import sys
from fastapi.testclient import TestClient
from wb.api import create_app
from wb.auth import issue
key = b'h' * 32
with TestClient(create_app(db_path=Path(sys.argv[1]), host_key=key)) as api:
    ticket = issue(key, 'citrus-workbench-launch', 'citrus-workbench', {'jti': uuid4().hex,
        'user_ref':'51', 'project_ref':'session:styled-fixture', 'return_to':'/',
        'frozen_project':'1'*32, 'frozen_render_version':2})
    assert api.post('/api/v1/launch', json={'ticket':ticket}).status_code == 200
    assert api.post('/api/v1/figures/import-frozen', json={'project_ref':'1'*32, 'render_version':2}).status_code == 404
    url = sys.argv[2]
    edit = api.patch(url+'/native-overrides', json={'expected_revision':1, 'patches':[
        {'gid':'axes_0.title','prop':'text','value':'Edited while intake off'}]})
    assert edit.status_code == 200 and edit.json()['revision'] == 2, edit.status_code
    svg = api.post(url+'/export',json={'format':'svg'})
    assert svg.status_code == 200 and b'Edited while intake off' in svg.content
    assert sha256(svg.content).hexdigest() == svg.headers['x-content-sha256']
"""
    env = os.environ.copy()
    env.pop("WORKBENCH_STYLED_IMPORT", None)
    subprocess.run([sys.executable, "-B", "-c", cold, str(db), url], env=env,
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=70)
