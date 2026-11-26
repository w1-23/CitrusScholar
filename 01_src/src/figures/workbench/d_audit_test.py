"""Source identity and revision-bound SVG projection acceptance."""
from hashlib import sha256

from fastapi.testclient import TestClient

from src.figures import store
from src.figures.workbench.nature_vectors_test import R_SVG, configure, instant_plot
from src.figures.workbench.workbench_test import OWNER, command, group, leaf, library


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
    old = library.files.pop(originals['r.svg']['file_id'])
    payload = {'expected_revision': 1, 'source_sha256': old['sha256'],
               'commands': [command(initial['groups'][0], 'edgecolor', '#1d557a')]}
    read_only = client.get(prefix).json()
    assert read_only['read_only_reason'] and read_only['source_sha256'] is None
    assert all(not row['properties'] for row in read_only['groups'])
    assert client.get(prefix + '/export/svg?revision=1').status_code == 200
    bad = client.post(prefix + '/save', json=payload)
    assert bad.status_code == 409 and bad.json()['code'] == 'plot_source_unverified'
