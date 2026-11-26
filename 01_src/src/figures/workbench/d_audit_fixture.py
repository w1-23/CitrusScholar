"""Isolated D-series ASGI fixture using a JSONL pipe, with no listening socket."""
from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from PIL import Image
from pytest import MonkeyPatch

from src.figures import store, svg_projects
from src.figures.workbench.nature_vectors_test import SESSION, configure, instant_plot
from src.figures.workbench.workbench_test import OWNER, OTHER, leaf, native_multipanel


def seed(library, patch, r_source):
    tss = {**leaf('errorbar'), 'title': 'TSS synthetic reconstruction',
           'x_label': 'Time (days)', 'y_label': 'Total suspended solids (mg/L)',
           'x': {'kind': 'inline', 'values': [0, 0, 1, 1, 2, 2] * 3},
           'y': {'kind': 'inline', 'values': [6, 10, 12, 16, 20, 24] * 3},
           'group': {'kind': 'inline', 'values': [name for name in ('CK', 'BL', 'WL') for _ in range(6)]},
           'error': 'sd'}
    # The three equal series make overlapping lines, bars and marker ink deterministic.
    library.spec(tss, title='TSS synthetic reconstruction')
    source, manifest, original, refs = native_multipanel(library, patch)
    library.project(source, manifest, original, title='Native frozen two-panel', kind='script', refs=refs)
    source, manifest, original, refs = native_multipanel(library, patch, visual_marks=True)
    library.project(source, manifest, original, title='Frozen arrow and patch ink', kind='script', refs=refs)
    library.spec(leaf(), OTHER, 'Foreign')
    ordinary = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"><title>Ordinary</title>' \
               '<desc>Static sample</desc><circle id="standalone" cx="45" cy="35" r="24" fill="#174488"/></svg>'
    svg_projects.create_svg_project(owner=OWNER, session_id=SESSION, svg=ordinary, title='Ordinary SVG', alt_text='Static sample')
    bitmap = BytesIO()
    Image.new('RGB', (120, 80), '#8c5e3c').save(bitmap, format='PNG')
    entry = library.publish(OWNER, SESSION, 'flat.png', bitmap.getvalue(), 'image/png')
    row = store.create_project(owner_user_id=OWNER, title='Bitmap PNG', source_kind='svg', source={}, session_id=SESSION)
    store.update_project(row['figure_id'], OWNER, expected_revision=0, manifest_json={},
                         exports_json={'png': entry}, render_version=1)
    r_project, _ = instant_plot(library, 'r', 'ggplot.svg', {'ggplot.svg': r_source.read_bytes()}, title='Pinned ggplot2')
    assert r_project['editable'], r_project


def run(data, r_source, request_pipe, response_pipe):
    with MonkeyPatch.context() as patch, request_pipe.open() as requests, response_pipe.open('w', buffering=1) as responses:
        library = configure(data, patch)
        seed(library, patch, r_source)
        with TestClient(library.app(patch), base_url='http://localhost') as client:
            client.cookies.set('citrus_auth', 'fixture')
            for line in requests:
                request = json.loads(line)
                url = request['url']
                if url.startswith('/api/v2/files/') and url.endswith('/download'):
                    file = library.get(url.split('/')[4], OWNER)
                    body = file['path'].read_bytes()
                    code, headers = 200, {'content-type': file['mime_type'], 'x-file-sha256': sha256(body).hexdigest()}
                else:
                    response = client.request(request['method'], url, content=request.get('content'),
                                              headers=request.get('headers'), follow_redirects=False)
                    body, code, headers = response.content, response.status_code, dict(response.headers)
                responses.write(json.dumps({'body': base64.b64encode(body).decode('ascii'),
                                            'status': code, 'headers': headers}) + '\n')


if __name__ == '__main__':
    run(*(Path(value) for value in sys.argv[1:]))
