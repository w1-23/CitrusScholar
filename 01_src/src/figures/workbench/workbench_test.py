"""A-series acceptance against real rendering, owner scope and revision CAS."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from io import BytesIO
from pathlib import Path
import threading
from urllib.parse import quote
import xml.etree.ElementTree as ET

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from src.api import figure_workbench_router as router
from src.figures import projects, store
from src.figures.declarative_snapshot import prepare_source
from src.figures.resolve import resolve_spec
from src.figures.spec import parse_spec
from src.figures.trusted_render import render_resolved
from src.figures.workbench import service
from src.session import files

OWNER = 47
OTHER = 48
SVG_NS = {"s": "http://www.w3.org/2000/svg"}


class Library:
    """Temporary project DB and file registry. Matplotlib/Agg renders are real."""

    def __init__(self, tmp_path, patch):
        self.root = tmp_path
        self.files = {}
        self.published = []
        self.lock = threading.Lock()
        patch.setattr(store, "_STATE_DIR", tmp_path / "projects")
        patch.setattr(store, "_pin_files", lambda *args, **kwargs: ("", []))
        patch.setattr(files, "get_account_file", self.get)
        from src.research import artifacts
        patch.setattr(artifacts, "stage_figure_artifact", lambda **kw: self.publish(
            kw["owner"], kw["session_id"], kw["filename"], kw["content"], kw["mime"], request_id=kw["request_id"]))
        patch.setattr(artifacts, "discard_staged_figures", lambda *args: None)
        patch.setattr(files, "promote_figure_files", lambda *args: None)
        from src.core import account_leases
        patch.setattr(account_leases, "account_lease", self.lease)
        patch.setattr(router, "_read_scope", self.lease)
        patch.setattr(router, "_write_scope", self.lease)
        patch.setattr(router, "run_account_thread", self.thread)

    @contextmanager
    def lease(self, owner):
        yield owner

    async def thread(self, owner, fn, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(fn, *args, **kwargs)

    def publish(self, owner, session_id, filename, payload, mime, *, request_id=""):
        with self.lock:
            key = f"{len(self.files) + 1:032x}"
            path = self.root / key
            path.write_bytes(payload)
            entry = {"file_id": key, "filename": filename, "size": len(payload),
                     "sha256": hashlib.sha256(payload).hexdigest()}
            self.files[key] = {**entry, "path": path, "owner": owner}
            self.published.append((key, request_id))
        return entry

    def get(self, file_id, owner, **kwargs):
        entry = self.files.get(file_id)
        return entry if entry and entry["owner"] == owner else None

    def spec(self, payload, owner=OWNER, title="Synthetic overlap"):
        spec = parse_spec(payload)
        resolved, info = resolve_spec(spec, owner_user_id=owner)
        source = prepare_source(spec, resolved, info)
        render = render_resolved(resolved, formats=("svg",), dpi=90)
        return self.project(source, render.manifest, render.blobs["svg"], owner, title)

    def project(self, source, manifest, blob, owner=OWNER, title="Historical figure", kind="spec", refs=()):
        entry = self.publish(owner, "isolated-session", "figure.svg", blob, "image/svg+xml")
        row = store.create_project(owner_user_id=owner, title=title, source_kind=kind,
                                   source=source, session_id="isolated-session", data_refs=list(refs))
        assert store.update_project(row["figure_id"], owner, expected_revision=0,
                                    manifest_json=manifest, exports_json={"svg": entry}, render_version=1)
        return row["figure_id"]

    def app(self, patch):
        from src import auth, config
        patch.setattr(auth, "user_for_token", lambda token: {"user_id": OWNER} if token == "fixture" else None)
        app = FastAPI()
        app.include_router(router.router)
        app.include_router(router.page_router)
        app.mount("/assets", StaticFiles(directory=config.WEB_DIR / "assets"))

        @app.middleware("http")
        async def account(request: Request, call_next):
            request.state.user = {"user_id": int(request.headers.get("x-fixture-owner", str(OWNER)))}
            return await call_next(request)

        return app


@pytest.fixture
def library(tmp_path, monkeypatch):
    return Library(tmp_path, monkeypatch)


def leaf(kind="line", *, panel=None):
    return {"chart": kind, "title": "实验 β", "x_label": r"Time $\alpha^2$", "y_label": "读数 Y",
            "x": {"kind": "inline", "values": [0, 1, 2] * 3},
            "y": {"kind": "inline", "values": [0, 1, 2] * 3},
            "group": {"kind": "inline", "values": ["CK"] * 3 + ["BL"] * 3 + ["WL"] * 3},
            **({"panel_id": panel} if panel else {})}


def group(snapshot, label, kind=None):
    return next(row for row in snapshot["groups"] if row["label"] == label and (kind is None or row["kind"] == kind))


def command(target, prop, value):
    return {"group_id": target["id"], "property": prop, "value": value}


def test_library_ownership_read_only_and_revision_matched_bytes(library, monkeypatch):
    figure_id = library.spec(leaf())
    foreign = library.spec(leaf(), OTHER, "Foreign")
    static_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30"><rect width="40" height="30"/></svg>'
    static = library.project({}, {}, static_bytes, kind="svg")
    table = library.publish(OWNER, "isolated-session", "data.csv", b"x,y\n1,2\n", "text/csv")
    client = TestClient(library.app(monkeypatch))
    prefix = "/api/v2/figure-workbench/projects"
    baseline = len(library.published)
    assert client.get("/figure-workbench", follow_redirects=False).status_code == 303
    client.cookies.set("citrus_auth", "fixture")
    page = client.get("/figure-workbench")
    assert page.status_code == 200 and 'groupTree' in page.text and 'dataFileSelect' not in page.text
    assert {row["figure_id"] for row in client.get(prefix).json()["projects"]} == {figure_id, static}
    assert {row["figure_id"] for row in client.get(prefix, params={"query": "OVERLAP", "kind": "spec", "access": "editable"}).json()["projects"]} == {figure_id}
    assert {row["figure_id"] for row in client.get(prefix, params={"kind": "svg", "access": "read_only"}).json()["projects"]} == {static}
    assert [row["figure_id"] for row in client.get(prefix, params={"chart": "line"}).json()["projects"]] == [figure_id]
    assert client.get(prefix, params={"chart": "scatter"}).json()["projects"] == []
    assert client.get(f"{prefix}/{figure_id}").json()["render_version"] == 1
    assert client.get(f"{prefix}/{figure_id}?revision=0").status_code == 409
    for inaccessible in [foreign, table["file_id"]]:
        assert client.get(f"{prefix}/{inaccessible}").status_code == 404
        assert client.get(f"{prefix}/{inaccessible}?revision=1").status_code == 404
        assert client.get(f"{prefix}/{inaccessible}/preview/svg?revision=1").status_code == 404
    opened = client.get(f"{prefix}/{static}?revision=1").json()
    assert opened["read_only_reason"] and opened["format_capabilities"]["png"]["state"] == "unavailable"
    response = client.get(f"{prefix}/{static}/preview/svg?revision=1")
    assert response.content == static_bytes
    assert response.headers["x-figure-sha256"] == hashlib.sha256(static_bytes).hexdigest()
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.post(f"{prefix}/{static}/export/png?revision=1").status_code == 409
    target = group(service.detail(figure_id, OWNER, 1), "WL", "series")
    invalid = {"expected_revision": 1, "commands": [{**command(target, "color", "#7a3c96"), "gid": "axes_0.series_0"}]}
    assert client.post(f"{prefix}/{figure_id}/save", json=invalid).status_code == 422
    assert client.post(f"{prefix}/{figure_id}/save", json={"expected_revision": 1, "commands": [command(target, "xdata", [1, 2])] }).status_code == 422
    assert len(library.published) == baseline


def test_group_preview_styles_and_legend_are_real_and_isolated(library):
    figure_id = library.spec(leaf())
    opened = service.detail(figure_id, OWNER, 1)
    assert not opened["read_only_reason"]
    series = group(opened, "WL", "series")
    assert series["values"]["markerfacecolor"] == "auto"
    ticks = group(opened, "X 轴刻度文字")
    assert len(ticks["pick_ids"]) >= 2 and "offset_px" not in ticks["properties"]
    assert not any(row["kind"] in {"point", "bar", "ticklabel"} for row in opened["groups"])
    initial = store.get_project(figure_id, OWNER)
    changes = [command(series, prop, value) for prop, value in {
        "color": "#7a3c96", "linestyle": "--", "marker": "^", "markerfacecolor": "none", "label": "WL edited"}.items()]
    result = service.edit(figure_id, OWNER, 1, changes, draft_revision=8)
    updated = group(result, "WL edited", "series")
    assert result["draft_revision"] == 8 and updated["id"] == series["id"]
    assert updated["values"]["markerfacecolor"] == "none" and updated["values"]["marker"] == "^"
    assert updated["values"]["color"] == "#7a3c96" and updated["values"]["linestyle"] == "--"
    assert group(result, "CK", "series")["values"] == group(opened, "CK", "series")["values"]
    assert group(result, "BL", "series")["values"] == group(opened, "BL", "series")["values"]
    svg = ET.fromstring(result["svg"])
    assert any(text.text == "WL edited" for text in svg.findall('.//s:text', SVG_NS))
    assert len([node for node in svg.iter() if node.get('stroke') == '#7a3c96']) >= 2
    assert store.get_project(figure_id, OWNER) == initial


def test_save_typography_transparency_and_closed_cas(library, monkeypatch):
    figure_id = library.spec(leaf())
    opened = service.detail(figure_id, OWNER, 1)
    title = group(opened, "标题")
    ticks = group(opened, "Y 轴刻度文字")
    legend = group(opened, "图例")
    changes = [command(title, "fontsize", 14), command(title, "fontweight", "bold"),
               command(title, "offset_px", [4, 6]), command(ticks, "fontweight", "medium"),
               command(legend, "loc", "upper left"), command(group(opened, "整张图"), "transparent", True)]
    start = store.get_project(figure_id, OWNER)
    for edits, code in [([command(title, "xdata", [1, 2])], "group_property_unavailable"),
                        ([command(title, "fontfamily", "UnavailableFixtureFont")], "unavailable_font")]:
        with pytest.raises(projects.ProjectError) as error:
            service.edit(figure_id, OWNER, 1, edits, save=True)
        assert error.value.code == code
    with pytest.raises(projects.ProjectError, match="无权访问"):
        service.edit(figure_id, OTHER, 1, changes, save=True)
    real_render = projects._render_edits

    def ignored(*args, **kwargs):
        merged, _, manifest, blobs = real_render(*args, **kwargs)
        return merged, [], manifest, blobs

    with monkeypatch.context() as patch:
        patch.setattr(projects, "_render_edits", ignored)
        with pytest.raises(projects.ProjectError) as error:
            service.edit(figure_id, OWNER, 1, changes, save=True)
        assert error.value.code == "group_edit_unconfirmed"
    assert store.get_project(figure_id, OWNER) == start
    saved = service.edit(figure_id, OWNER, 1, changes, save=True)
    assert saved["render_version"] == 2
    raw = store.get_project(figure_id, OWNER)
    assert raw["source_json"] == start["source_json"] and raw["data_refs_json"] == start["data_refs_json"]
    assert group(saved, "标题")["values"]["fontsize"] == 14
    assert group(saved, "标题")["values"]["offset_px"] == [4, 6]
    assert group(saved, "Y 轴刻度文字")["values"]["fontweight"] == "medium"
    assert group(saved, "图例")["values"]["loc"] == "upper left"
    with pytest.raises(projects.ProjectError) as error:
        service.edit(figure_id, OWNER, 1, changes, save=True)
    assert error.value.code == "revision_conflict"
    svg = service.download(figure_id, OWNER, 2, "svg").body
    assert "实验" in svg.decode() and b'fill="none"' in svg
    png = service.download(figure_id, OWNER, 2, "png", generate=True)
    with Image.open(BytesIO(png.body)) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[-1] == 0
    pdf = service.download(figure_id, OWNER, 2, "pdf", generate=True)
    assert pdf.body.startswith(b"%PDF-")
    assert store.get_revision_export(figure_id, OWNER, 2, "pdf")["export"]["sha256"] == pdf.digest


def test_same_names_in_distinct_panels_and_marker_fill_states(library):
    figure_id = library.spec({"panels": [leaf(panel="p_left"), leaf(panel="p_right")]})
    opened = service.detail(figure_id, OWNER, 1)
    left, right = [row for row in opened["groups"] if row["kind"] == "series" and row["label"] == "WL"]
    assert left["id"] != right["id"] and left["parent"] != right["parent"]
    base = left["values"]["color"]
    assert left["values"]["markerfacecolor"] == "auto"
    custom = service.edit(figure_id, OWNER, 1, [command(left, "markerfacecolor", "#ffffff")])
    hollow = service.edit(figure_id, OWNER, 1, [command(left, "markerfacecolor", "none")])
    recolored = service.edit(figure_id, OWNER, 1, [command(left, "markerfacecolor", "#ffffff"), command(left, "color", "#7a3c96")])
    for result, fill in [(custom, "#ffffff"), (hollow, "none"), (recolored, "#ffffff")]:
        groups = {row["id"]: row for row in result["groups"]}
        assert groups[left["id"]]["values"]["markerfacecolor"] == fill
        assert groups[right["id"]]["values"]["markerfacecolor"] == "auto"
        assert groups[right["id"]]["values"]["color"] == base


def test_shared_legend_sync_is_server_declared_and_group_atomic(library):
    fid = library.spec({"panels": [leaf(panel="p_left"), leaf(panel="p_right")], "shared_legend": True})
    opened = service.detail(fid, OWNER, 1)
    linked = [row for row in opened["groups"] if row["kind"] == "series" and row["label"] == "WL"]
    assert len(linked) == 2 and all(not row["properties"] for row in linked)
    sync = group(opened, "同步共享图例 · WL", "sync")
    with pytest.raises(projects.ProjectError) as error:
        service.edit(fid, OWNER, 1, [command(linked[0], "color", "#7a3c96")])
    assert error.value.code == "group_property_unavailable"
    result = service.edit(fid, OWNER, 1, [command(sync, "color", "#7a3c96")])
    joined = [row for row in result["hit_manifest"]["objects"] if row.get("role") == "series" and row.get("label") == "WL"]
    assert len(joined) == 2 and all(row["color"] == "#7a3c96" for row in joined)


def test_export_does_not_commit_after_current_revision_changes(library, monkeypatch):
    figure_id = library.spec(leaf())
    started, resume = threading.Event(), threading.Event()
    real_render = projects._render_edits

    def pause(project, *args, **kwargs):
        if kwargs.get("formats") == ("pdf",):
            started.set()
            assert resume.wait(30)
        return real_render(project, *args, **kwargs)

    monkeypatch.setattr(projects, "_render_edits", pause)
    opened = service.detail(figure_id, OWNER, 1)
    changes = [command(group(opened, "标题"), "text", "Changed after export started")]
    with ThreadPoolExecutor(1) as pool:
        flight = pool.submit(service.download, figure_id, OWNER, 1, "pdf", generate=True)
        try:
            assert started.wait(30)
            saved = service.edit(figure_id, OWNER, 1, changes, save=True)
            assert saved["render_version"] == 2
        finally:
            resume.set()
        with pytest.raises(projects.ProjectError) as err:
            flight.result(timeout=45)
        assert err.value.code == "revision_conflict"
    new = service.download(figure_id, OWNER, 2, "pdf", generate=True)
    assert new.body.startswith(b"%PDF-")
    assert store.get_revision_export(figure_id, OWNER, 1, "pdf") is None


def test_frozen_script_vector_heatmap_and_confidence_band(library, monkeypatch):
    from src.config import settings
    from src.figures import sandbox
    from src.figures.script_outputs import output_slots

    renderer = """
from citrus_figures import register_semantic_identity
fig, ax = plt.subplots()
register_semantic_identity(ax, 'comparison.panel', role='axes')
mesh = ax.pcolormesh([[1, 2], [3, data['samples']['y'].iloc[0]]], cmap='viridis')
register_semantic_identity(mesh, 'comparison.heatmap', role='series')
band = ax.fill_between([2, 3, 4], [1, 1, 2], [3, 2, 4], color='#088080', alpha=.4, label='Confidence')
register_semantic_identity(band, 'comparison.confidence', role='series')
ax.set_title('Vector heatmap and confidence band')
ax.legend()
"""
    script = "import pandas as pd\nemit.table('samples', pd.DataFrame({'y': [4]}))\n"
    contract = {"version": 2, "tables": [{"name": "samples"}], "figures": [
        {"name": "main", "title": "Shapes", "formats": ["svg"], "parents": ["samples"], "render_script": renderer}]}
    runner = sandbox.LocalBackend()
    initial = runner.run(sandbox.SandboxJob(script=script, output_contract=contract, outputs=output_slots(contract)))
    assert initial.ok, initial.describe()
    manifest = json.loads(initial.outputs["figure:main:manifest"])
    table = json.dumps(json.loads(initial.outputs["table:samples"]), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode()
    entry = library.publish(OWNER, "isolated-session", "fixture.json", table, "application/json")
    receipt = manifest["frozen_render_receipt"]
    assert receipt["data_sha256"] == {"samples": entry["sha256"]}
    snapshot = {"variable": "samples", "sha256": entry["sha256"], "file_id": entry["file_id"],
                "size_bytes": len(table), "sandbox_name": "input_0.json"}
    source = {"kind": "script", "version": 3, "output_contract": contract, "output_key": "main",
              "frozen_render": {"version": 1, "receipt": receipt, "input_snapshots": [snapshot]}}
    fid = library.project(source, manifest, initial.outputs["figure:main:svg"], kind="script", refs=[entry])
    monkeypatch.setattr(settings, "FIGURES_SANDBOX_BACKEND", "local")
    monkeypatch.setattr(settings, "FIGURES_SANDBOX_ALLOW_LOCAL", True)
    monkeypatch.setattr(sandbox, "select_backend", lambda **kwargs: runner)
    start = service.detail(fid, OWNER, 1)
    assert not start["read_only_reason"]
    heatmap = group(start, "热图", "series")
    band = group(start, "Confidence", "series")
    assert heatmap["properties"] == ["cmap"] and "color" in band["properties"]
    changes = [command(heatmap, "cmap", "cividis"), command(band, "color", "#b64342"), command(band, "alpha", .6)]
    preview = service.edit(fid, OWNER, 1, changes)
    assert group(preview, "热图", "series")["values"]["cmap"] == "cividis"
    assert group(preview, "Confidence", "series")["values"]["color"] == "#b64342"
    assert preview["svg"].encode() != initial.outputs["figure:main:svg"]
    saved = service.edit(fid, OWNER, 1, changes, save=True)
    assert saved["render_version"] == 2
    updated = store.get_project(fid, OWNER)["manifest_json"]
    assert updated["style_replay_proof"]["coordinates_sha256"] == receipt["style_replay"]["coordinates_sha256"]


def five_series():
    names = ["CK", "WL", "BL", "Control A", "Control B"]
    return {**leaf(), "x": {"kind": "inline", "values": [0, 1, 2] * len(names)},
            "y": {"kind": "inline", "values": [val for index in range(len(names)) for val in (index, index + 1, index)]},
            "group": {"kind": "inline", "values": [name for name in names for _ in range(3)]}}


def test_five_source_series_replay_independently_and_keep_stable_ids(library):
    fid = library.spec(five_series(), title="Five independent groups")
    before = service.detail(fid, OWNER, 1)
    initial = store.get_project(fid, OWNER)
    edits = {"CK": ("#2456af", "s"), "WL": ("#ad4140", "^"), "BL": ("#526325", "v"),
             "Control A": ("#ad710b", "D"), "Control B": ("#752287", "P")}
    commands = [command(group(before, name, "series"), prop, value)
                for name, (color, marker) in edits.items() for prop, value in (("color", color), ("marker", marker))]
    preview = service.edit(fid, OWNER, 1, commands)
    assert store.get_project(fid, OWNER) == initial
    saved = service.edit(fid, OWNER, 1, commands, save=True)
    reopened = service.detail(fid, OWNER, 2)
    for snapshot in (preview, saved, reopened):
        series = [group(snapshot, name, "series") for name in edits]
        assert len({item["id"] for item in series}) == len(edits)
        for item, (name, (color, marker)) in zip(series, edits.items()):
            assert item["id"] == group(before, name, "series")["id"]
            assert item["values"]["color"] == color and item["values"]["marker"] == marker
    for body in (preview["svg"], service.download(fid, OWNER, 2, "svg").body):
        root = ET.fromstring(body)
        for name, (color, _) in edits.items():
            target_gid = group(reopened, name, "series")["pick_ids"][0]
            actual = root.find(f".//s:g[@id='{target_gid}']", SVG_NS)
            assert actual is not None and any(node.get("stroke") == color for node in actual.iter())


def test_legacy_known_address_migrates_while_conflicting_or_absent_address_stays_read_only(library):
    payload = {"panels": [leaf(panel="p_left"), leaf(panel="p_right")]}
    spec = parse_spec(payload)
    frozen, provenance = resolve_spec(spec, owner_user_id=OWNER)
    source = prepare_source(spec, frozen, provenance)
    rendered = render_resolved(frozen, formats=("svg",), dpi=90)
    left, right = [row for row in rendered.manifest["objects"] if row["role"] == "series" and row["label"] == "WL"]
    for field in ("series_id", "series_index", "panel_id"):
        del left["locator"][field]
    right["locator"]["series_id"] = left["gid"]
    missing = next(row for row in rendered.manifest["objects"] if row["role"] == "series" and row["label"] == "CK")
    for field in ("series_id", "series_index", "panel_id"):
        del missing["locator"][field]
    missing["gid"] = "not_a_registered_artist"
    fid = library.project(source, rendered.manifest, rendered.blobs["svg"])
    opened = service.detail(fid, OWNER, 1)
    by_gid = {row["pick_ids"][0]: row for row in opened["groups"] if row["kind"] == "series"}
    assert "color" in by_gid[left["gid"]]["properties"]
    for rejected in (by_gid[right["gid"]], by_gid[missing["gid"]]):
        assert not rejected["properties"] and rejected["reason"]
        with pytest.raises(projects.ProjectError) as error:
            service.edit(fid, OWNER, 1, [command(rejected, "color", "#123456")], save=True)
        assert error.value.code == "group_property_unavailable"
    saved = service.edit(fid, OWNER, 1, [command(by_gid[left["gid"]], "color", "#123456")], save=True)
    reopened = service.detail(fid, OWNER, 2)
    assert saved["render_version"] == 2
    migrated = next(row for row in reopened["groups"] if row["id"] == by_gid[left["gid"]]["id"])
    assert migrated["values"]["color"] == "#123456"
    actual = next(row for row in reopened["hit_manifest"]["objects"] if row["gid"] == left["gid"])
    assert actual["locator"]["series_id"] == left["gid"] and actual["locator"]["panel_id"] == "p_left"
    sibling = next(row for row in reopened["hit_manifest"]["objects"] if row["gid"] == right["gid"])
    assert sibling["color"] != "#123456"


def native_multipanel(library, monkeypatch):
    """Two unregistered axes with repeated categories and mixed native marks."""
    from src.config import settings
    from src.figures import sandbox
    from src.figures.script_outputs import output_slots

    renderer = """
fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.1))
fig.subplots_adjust(wspace=.34, left=.12, right=.97, bottom=.22, top=.84)
for i, ax in enumerate(axes):
    key = 'A' if i == 0 else 'B'
    x = data['categories']['x'].to_numpy()
    y = data['summary']['y'].to_numpy() + i * .6
    ax.errorbar(x, y, yerr=[.2, .3, .1, .25], fmt='s-', capsize=3,
                color='#28638a', label='Trend ' + key)
    ax.scatter(x.repeat(3), (y - .7).repeat(3) + [-.12, 0, .12] * 4,
               s=20, color='#b84a35', label='Points ' + key)
    ax.hlines(y - 1.25, x - .2, x + .2, color='#387442', label='Range ' + key)
    ax.set_xticks(x, ['G1', 'G2', 'G3', 'G4'])
    ax.set(title='Panel ' + key, xlabel='Group', ylabel='Response')
    ax.grid(axis='y', alpha=.2)
    if i:
        ax.legend(loc='lower right', fontsize=8)
"""
    script = ("import pandas as pd\n"
              "emit.table('categories', pd.DataFrame({'x': [0, 1, 2, 3]}))\n"
              "emit.table('summary', pd.DataFrame({'y': [3, 5, 4, 4.7]}))\n")
    names = ("categories", "summary")
    contract = {"version": 2, "tables": [{"name": name} for name in names], "figures": [
        {"name": "main", "title": "Native two-panel figure", "formats": ["svg"], "parents": list(names),
         "render_script": renderer}]}
    runner = sandbox.LocalBackend()
    monkeypatch.setattr(settings, "FIGURES_SANDBOX_BACKEND", "local")
    monkeypatch.setattr(settings, "FIGURES_SANDBOX_ALLOW_LOCAL", True)
    monkeypatch.setattr(sandbox, "select_backend", lambda **kwargs: runner)
    initial = runner.run(sandbox.SandboxJob(script=script, output_contract=contract, outputs=output_slots(contract)))
    assert initial.ok, initial.describe()
    manifest = json.loads(initial.outputs["figure:main:manifest"])
    snapshots, refs = [], []
    for index, name in enumerate(names):
        table = json.dumps(json.loads(initial.outputs[f"table:{name}"]), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        entry = library.publish(OWNER, "isolated-session", name + ".json", table, "application/json")
        refs.append(entry)
        snapshots.append({"variable": name, "sha256": entry["sha256"], "file_id": entry["file_id"],
                          "size_bytes": len(table), "sandbox_name": f"input_{index}.json"})
    receipt = manifest["frozen_render_receipt"]
    assert receipt["data_sha256"] == {row["variable"]: row["sha256"] for row in snapshots}
    source = {"kind": "script", "version": 3, "script": script, "output_contract": contract, "output_key": "main",
              "frozen_render": {"version": 1, "receipt": receipt, "input_snapshots": snapshots}}
    return source, manifest, initial.outputs["figure:main:svg"], refs


def test_frozen_script_unregistered_multipanel_owner_replay_and_cas(library, monkeypatch):
    from collections import Counter

    source, manifest, original, refs = native_multipanel(library, monkeypatch)
    shape = Counter((row["role"], row["locator"].get("axes_index")) for row in manifest["objects"])
    for index in (0, 1):
        assert (shape["axes", index], shape["series", index], shape["point", index],
                shape["errorbar", index]) == (1, 3, 12, 1)
    assert all(row["identity_scope"] == "render" for row in manifest["objects"])
    fid = library.project(source, manifest, original, kind="script", refs=refs)
    before = store.get_project(fid, OWNER)
    opened = service.detail(fid, OWNER, 1)
    target = group(opened, "Trend A", "series")
    sibling = group(opened, "Trend B", "series")
    assert not opened["read_only_reason"] and target["pick_ids"] and "color" in target["properties"], opened["groups"]
    assert all(not group(opened, f"子图 {n}")["reason"] for n in (1, 2))
    assert service.download(fid, OWNER, 1, "svg").body == original
    assert store.get_project(fid, OWNER) == before
    commands = [command(target, "color", "#7a3c96")]
    with pytest.raises(projects.ProjectError) as error:
        service.edit(fid, OTHER, 1, commands, save=True)
    assert error.value.code == "not_found"
    draft = service.edit(fid, OWNER, 1, commands, draft_revision=4)
    draft_target = group(draft, "Trend A", "series")
    assert draft["draft_revision"] == 4 and draft_target["id"] == target["id"]
    assert draft_target["values"]["color"] == "#7a3c96" and draft["svg"].count('stroke="#7a3c96"') >= 1
    assert group(draft, "Trend B", "series")["values"] == sibling["values"]
    assert store.get_project(fid, OWNER) == before and service.download(fid, OWNER, 1, "svg").body == original
    saved = service.edit(fid, OWNER, 1, commands, save=True)
    committed = store.get_project(fid, OWNER)
    assert saved["render_version"] == 2 and group(saved, "Trend A", "series")["id"] == target["id"]
    assert committed["source_json"] == before["source_json"] and committed["data_refs_json"] == before["data_refs_json"]
    assert committed["manifest_json"]["style_replay_proof"]["coordinates_sha256"] == (
        source["frozen_render"]["receipt"]["style_replay"]["coordinates_sha256"])
    assert library.get(before["exports_json"]["svg"]["file_id"], OWNER)["path"].read_bytes() == original
    assert store.get_revision_export(fid, OWNER, 1, "svg") is None
    for action in (lambda: service.edit(fid, OWNER, 1, commands, save=True),
                   lambda: service.download(fid, OWNER, 1, "svg")):
        with pytest.raises(projects.ProjectError) as error:
            action()
        assert error.value.code == "revision_conflict"


def test_frozen_script_rejects_unverified_geometry_and_ambiguous_targets(library, monkeypatch):
    from copy import deepcopy

    source, manifest, original, refs = native_multipanel(library, monkeypatch)
    for variant in ("saved_bounds", "ambiguous", "receipt", "foreign_locator"):
        candidate = deepcopy(manifest)
        axes = [row for row in candidate["objects"] if row["role"] == "axes"]
        if variant == "saved_bounds":
            axes[0]["bbox"]["x0"] += 1
        elif variant == "ambiguous":
            axes[0].update(identity_reason="ambiguous_key", semantic_key="not-unique")
        elif variant == "receipt":
            candidate["frozen_render_receipt"]["style_replay"]["targets_sha256"] = "0" * 64
        else:
            next(row for row in candidate["objects"] if row.get("label") == "Trend A")["locator"]["series_index"] = 99
        fid = library.project(source, candidate, original, kind="script", refs=refs)
        before = store.get_project(fid, OWNER)
        opened = service.detail(fid, OWNER, 1)
        target = group(opened, "Trend A", "series")
        assert not target["properties"] and target["reason"]
        with pytest.raises(projects.ProjectError) as error:
            service.edit(fid, OWNER, 1, [command(target, "color", "#7a3c96")], save=True)
        assert error.value.code == "group_property_unavailable"
        assert store.get_project(fid, OWNER) == before
        assert service.download(fid, OWNER, 1, "svg").body == original


def serve_fixture(path, port):
    """Run only the test router and assets on loopback for real Chromium QA."""
    import uvicorn
    with pytest.MonkeyPatch.context() as patch:
        library = Library(Path(path), patch)
        first = library.spec(leaf())
        library.spec(leaf("bar"), title="Separate bar groups")
        library.spec(five_series(), title="Five independent groups")
        source, manifest, original, refs = native_multipanel(library, patch)
        library.project(source, manifest, original, title="Native two-panel figure", kind="script", refs=refs)
        library.spec(leaf(), OTHER, "Foreign")
        static = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30"><rect width="40" height="30"/></svg>'
        library.project({}, {}, static, title="Static only", kind="svg")
        library.project({}, {}, b'broken preview', title="Unreadable asset", kind="svg")
        library.publish(OWNER, "isolated-session", "data.csv", b"x,y\n1,2\n", "text/csv")
        print("WORKBENCH_FIXTURE=" + quote(first), flush=True)
        uvicorn.run(library.app(patch), host="127.0.0.1", port=int(port))
