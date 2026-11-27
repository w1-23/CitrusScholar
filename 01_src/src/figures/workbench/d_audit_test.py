"""Source identity and revision-bound SVG projection acceptance."""
from hashlib import sha256

from fastapi.testclient import TestClient

from src.figures import store
from src.figures.workbench.nature_vectors_test import R_SVG, configure, instant_plot
from src.figures.workbench.workbench_test import OWNER, command, group, leaf, library, native_multipanel


def test_frozen_source_hash_matches_cas_receipt_and_signed_exports(library, monkeypatch):
    fid = library.spec(leaf())
    prefix = f'/api/v2/figure-workbench/projects/{fid}'
    client = TestClient(library.app(monkeypatch))
    initial = client.get(prefix).json()
    source_sha = initial['source_sha256']
    title = group(initial, '标题')
    binding = initial['object_contract']['objects'][title['id']]
    assert binding['objectId'] == title['id'] and binding['geometry']['svgIds'] == title['pick_ids']
    assert binding['role'] == 'title' and binding['revision'] == 1
    initial_svg = client.get(prefix + '/preview/svg?revision=1').content
    assert binding['renderKey'] == sha256(initial_svg).hexdigest()
    payload = {'commands': [command(title, 'offset_px', [13, 4])], 'expected_revision': 1,
               'draft_revision': 17, 'source_sha256': source_sha}
    for route in ['/preview', '/save']:
        rejected = client.post(prefix + route, json={**payload, 'source_sha256': '0' * 64})
        assert rejected.status_code == 409 and rejected.json()['code'] == 'source_conflict'
    original = store.get_project(fid, OWNER)
    draft = client.post(prefix + '/preview', json=payload).json()
    assert draft['draft_revision'] == draft['object_contract']['draftRevision'] == 17
    assert draft['object_contract']['renderKey'] == sha256(draft['svg'].encode()).hexdigest()
    assert store.get_project(fid, OWNER) == original
    saved = client.post(prefix + '/save', json=payload).json()
    assert saved['render_version'] == 2 and saved['source_sha256'] == source_sha
    assert client.post(prefix + '/save', json=payload).status_code == 409
    after = store.get_project(fid, OWNER)
    assert after['source_json'] == original['source_json']
    assert after['manifest_json']['workbench_edit_binding'] == {k: payload[k] for k in (
        'source_sha256', 'expected_revision', 'draft_revision')}
    assert after['overrides_json'] == [{'gid': title['pick_ids'][0], 'prop': 'offset_px', 'value': [13.0, 4.0]}]
    reopened = client.get(prefix + '?revision=2').json()
    assert reopened['object_contract']['objects'][title['id']]['revision'] == 2
    exported = client.get(prefix + '/export/svg?revision=2')
    assert exported.headers['x-figure-sha256'] == reopened['object_contract']['renderKey'] == sha256(exported.content).hexdigest()


def test_missing_frozen_r_source_leaves_saved_view_readable(tmp_path, monkeypatch):
    library = configure(tmp_path, monkeypatch)
    result, originals = instant_plot(library, 'r', 'r.svg', {'r.svg': R_SVG})
    fid = result['figure_id']
    client = TestClient(library.app(monkeypatch))
    prefix = f'/api/v2/figure-workbench/projects/{fid}'
    initial = client.get(prefix).json()
    projects = prefix.rsplit('/', 1)[0]
    editable_rows = client.get(projects, params={'access': 'editable'}).json()['projects']
    assert [(row['figure_id'], row['editable']) for row in editable_rows] == [(fid, True)]
    old = library.files.pop(originals['r.svg']['file_id'])
    payload = {'expected_revision': 1, 'source_sha256': old['sha256'],
               'commands': [command(initial['groups'][0], 'edgecolor', '#1d557a')]}
    read_only = client.get(prefix).json()
    assert read_only['read_only_reason'] and read_only['source_sha256'] is None
    assert all(not row['properties'] for row in read_only['groups'])
    assert client.get(projects, params={'access': 'editable'}).json()['projects'] == []
    readonly_rows = client.get(projects, params={'access': 'read_only'}).json()['projects']
    assert [(row['figure_id'], row['editable']) for row in readonly_rows] == [(fid, False)]
    assert client.get(prefix + '/export/svg?revision=1').status_code == 200
    bad = client.post(prefix + '/save', json=payload)
    assert bad.status_code == 409 and bad.json()['code'] == 'plot_source_unverified'


def test_visual_marks_are_verified_supplemental_addresses(library, monkeypatch):
    source, manifest, svg, refs = native_multipanel(library, monkeypatch, visual_marks=True)
    marks = manifest['workbench_marks']
    assert sorted(mark['role'] for mark in marks) == ['arrow', 'arrow', 'patch']
    # Emulate the pre-paint-contract manifest, which exposed inspection only.
    manifest['objects'] = [row for row in manifest['objects'] if row['role'] not in {'patch', 'arrow'}]
    assert {mark['gid'] for mark in marks}.isdisjoint(row['gid'] for row in manifest['objects'])
    assert b'id="author-patch"' in svg

    forged = [{'gid': 'author-patch', 'axes_index': 0, 'role': 'patch', 'parent_gid': None},
              {**marks[-1], 'axes_index': 1}]
    # Reject an unreserved author ID, cross-panel ownership, and duplicate declarations.
    manifest['workbench_marks'] = [*marks[:-1], *forged, dict(marks[0])]
    fig = library.project(source, manifest, svg, kind='script', refs=refs)
    client = TestClient(library.app(monkeypatch))
    prefix = f'/api/v2/figure-workbench/projects/{fig}'
    detail = client.get(prefix).json()
    groups = [g for g in detail['groups'] if g['kind'] in {'arrow', 'patch'}]
    assert len(groups) == 1 and groups[0]['pick_ids'] == [marks[1]['gid']]
    group = groups[0]
    binding = detail['object_contract']['objects'][group['id']]
    assert binding['revision'] == 1
    assert not binding['capabilities'] and binding['readonlyReason']
    payload = {'expected_revision': 1, 'draft_revision': 18, 'source_sha256': detail['source_sha256'],
               'commands': [command(group, 'color', '#173255')]}
    response = client.post(prefix + '/save', json=payload)
    assert response.status_code == 422 and response.json()['code'] == 'group_property_unavailable'
    assert client.get(prefix + '/preview/svg?revision=1').content == svg



def test_native_mark_paint_preview_save_reopen_and_export(library, monkeypatch):
    import xml.etree.ElementTree as ET

    source, manifest, svg, refs = native_multipanel(library, monkeypatch, visual_marks=True)
    fid = library.project(source, manifest, svg, kind='script', refs=refs)
    client = TestClient(library.app(monkeypatch))
    prefix = f'/api/v2/figure-workbench/projects/{fid}'
    initial = client.get(prefix).json()
    patch = next(row for row in initial['groups'] if row['kind'] == 'patch')
    solid_arrow = next(row for row in initial['groups'] if row['kind'] == 'arrow' and 'facecolor' in row['properties'])
    open_arrow = next(row for row in initial['groups'] if row['kind'] == 'arrow' and 'edgecolor' in row['properties'])
    assert patch['properties'] == ['edgecolor', 'facecolor', 'linewidth']
    assert solid_arrow['properties'] == ['facecolor'] and '描边' in solid_arrow['reason']
    assert open_arrow['properties'] == ['edgecolor'] and '填充' in open_arrow['reason']
    assert '线宽参与箭头几何计算' in open_arrow['reason']
    commands = [command(patch, 'facecolor', '#624193'), command(patch, 'edgecolor', '#153355'),
                command(patch, 'linewidth', 2.7), command(open_arrow, 'edgecolor', '#884488'),
                command(solid_arrow, 'facecolor', '#552299')]
    payload = {'expected_revision': 1, 'draft_revision': 12, 'source_sha256': initial['source_sha256'],
               'commands': commands}
    before = store.get_project(fid, OWNER)
    denied = client.post(prefix + '/save', json={**payload, 'commands': [command(open_arrow, 'facecolor', '#223355')]})
    assert denied.status_code == 422 and denied.json()['code'] == 'group_property_unavailable'
    draft = client.post(prefix + '/preview', json=payload)
    assert draft.status_code == 200, draft.text
    assert store.get_project(fid, OWNER) == before
    saved = client.post(prefix + '/save', json=payload)
    assert saved.status_code == 200, saved.text
    reopened = client.get(prefix + '?revision=2').json()
    for result in (draft.json(), saved.json(), reopened):
        values = {row['id']: row['values'] for row in result['groups']}
        for edit in commands:
            assert values[edit['group_id']][edit['property']] == edit['value']
    exported = client.get(prefix + '/export/svg?revision=2')
    assert exported.status_code == 200
    assert sha256(exported.content).hexdigest() == reopened['object_contract']['renderKey']
    ns = {'s': 'http://www.w3.org/2000/svg'}
    for previous, updated in ((svg, draft.json()['svg']), (svg, exported.content)):
        old, new = [ET.fromstring(item) for item in (previous, updated)]
        target = f".//s:g[@id='{patch['pick_ids'][0]}']/s:path"
        assert old.find(target, ns).get('d') == new.find(target, ns).get('d')
        assert new.find(target, ns).get('fill') == '#624193'
        assert new.find(target, ns).get('stroke') == '#153355'
        assert float(new.find(target, ns).get('stroke-width')) == 2.7
        arrow = f".//s:g[@id='{solid_arrow['pick_ids'][0]}']/s:path"
        assert old.find(arrow, ns).get('fill-opacity') == new.find(arrow, ns).get('fill-opacity') == '0.6'
    committed = store.get_project(fid, OWNER)
    assert committed['source_json'] == before['source_json']
    assert committed['manifest_json']['style_replay_proof']['coordinates_sha256'] == (
        source['frozen_render']['receipt']['style_replay']['coordinates_sha256'])
    assert client.post(prefix + '/save', json=payload).status_code == 409
