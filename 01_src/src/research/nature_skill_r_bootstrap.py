"""Sealed Code Worker transport. Only R draws or exports figure bytes."""
import csv
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

R_PREFIX = Path('/opt/citrus-r')


def csv_contract(spec):
    field_names = [spec['x_col'], spec['y_col']]
    if spec.get('group_col') is not None:
        field_names.append(spec['group_col'])
    with open('/input/observations.csv', encoding='utf-8-sig', newline='') as stream:
        csv.field_size_limit(32 << 20)
        reader = csv.reader(stream, strict=True)
        header = next(reader, [])
        if (not header or len(set(header)) != len(header) or not all(header)
                or not set(field_names) <= set(header) or any('\0' in col for col in header)):
            raise ValueError('csv_column_mapping_invalid')
        rows = 0
        for row in reader:
            if len(row) != len(header) or any('\0' in cell for cell in row):
                raise ValueError('csv_record_width_or_encoding_invalid')
            rows += 1
        if rows == 0:
            raise ValueError('csv_has_no_observations')
    return header, rows


def audit_module(directory, name):
    module_spec = spec_from_file_location('_nature_r_' + name, directory / (name + '.py'))
    module = module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def cairo_collisions(auditor, path):
    """Use exact Cairo text lines rather than unioning loosely matched trace runs.

    Cairo can batch separate tick/legend labels in one PDF trace. Upstream's
    geometric matcher can then attach the union to the first label. Preserve
    real rendered glyph boxes, strokes, fills, image and clipping checks and
    run the unchanged upstream geometry policy over that evidence.
    """
    import pymupdf

    pages = auditor.extract_pdf_geometry(path)
    upstream = auditor.audit_geometries(pages)
    if len(pages) != 1:
        raise ValueError('single_page_nature_pdf_required')
    with pymupdf.open(path) as pdf:
        for page, geometry in zip(pdf, pages, strict=True):
            boxes = []
            for block in page.get_text('rawdict', flags=pymupdf.TEXTFLAGS_TEXT)['blocks']:
                for line in block.get('lines', []):
                    glyphs = [glyph for span in line['spans'] for glyph in span['chars']]
                    visible = [glyph for glyph in glyphs if glyph['c'].strip()]
                    bbox = auditor.union_rects(auditor.normalize_rect(glyph['bbox']) for glyph in visible)
                    if bbox is not None:
                        label = ''.join(glyph['c'] for glyph in glyphs).strip()
                        boxes.append(auditor.TextBox(index=len(boxes), text=label, bbox=bbox))
            geometry.texts = boxes
    return auditor.audit_geometries(pages), upstream['verdict']


def finalize(directory, stem):
    counts = json.loads((directory / 'counts.json').read_bytes())
    layout = json.loads((directory / 'layout.json').read_bytes())
    if layout['backend'] != 'r-patchwork' or len(layout['panels']) != 3:
        raise ValueError('missing_rendered_panel_geometry')
    alignment = audit_module(directory, 'audit_panel_alignment')
    aligned = alignment.audit_layout_manifest(layout)
    if alignment.exit_code(aligned, strict=True) != 0:
        raise ValueError('rendered_panel_alignment_failed')
    path = Path('/outputs') / (stem + '.pdf')
    if not 0 < path.stat().st_size <= 64 << 20:
        raise ValueError('ill_sized_rendered_pdf')
    pdf = path.read_bytes()
    text = audit_module(directory, 'audit_pdf_text').audit_pdf(pdf)
    if not text['auditable'] or text['below_minimum_count']:
        raise ValueError('rendered_pdf_text_failed')
    collisions = audit_module(directory, 'audit_figure_collisions')
    collision, upstream_collision = cairo_collisions(collisions, path)
    if collisions.exit_code(collision) != 0:
        raise ValueError('rendered_pdf_collisions_failed')
    validator = audit_module(directory, 'validate_figure')
    source_findings = validator.validate_source(_PAYLOAD['nature_marginal.R'].decode('utf-8'), 'r')
    counts['figure_geometry'] = {
        'pdf_sha256': hashlib.sha256(pdf).hexdigest(), 'alignment': aligned['verdict'],
        'layout': layout, 'upstream_layout': json.loads((directory / 'upstream-layout.json').read_bytes()),
        'pdf_text': {'below_minimum': text['below_minimum_count'], 'warnings': len(text['warnings'])},
        'pdf_collisions': {'verdict': collision['verdict'], 'summary': collision['summary'],
                           'measurement': 'cairo_glyph_lines', 'upstream_trace_verdict': upstream_collision},
        'source_advisory': validator.summarize(source_findings)['counts'],
        'visual_review': 'not_run',
    }
    counts['runtime']['lock_sha256'] = _RUNTIME_LOCK_SHA256
    (Path('/outputs') / (stem + '.qa.json')).write_text(json.dumps(counts, allow_nan=False), encoding='utf-8')


def main():
    if len(sys.argv) != 2:
        raise ValueError('fixed_r_request_required')
    spec = json.loads(sys.argv[1])
    if not re.fullmatch(r'nature-marginal-[a-f0-9]{12}', spec['stem']) or spec['plot']['template'] != 'marginal':
        raise ValueError('fixed_r_template_required')
    marker = R_PREFIX / 'citrus-runtime-lock.sha256'
    if marker.read_text(encoding='ascii').strip() != _RUNTIME_LOCK_SHA256:
        raise ValueError('pinned_r_runtime_unavailable')
    header, rows = csv_contract(spec['plot'])
    with tempfile.TemporaryDirectory(prefix='nature-r-', dir='/workspace') as temporary:
        directory = Path(temporary)
        for filename, content in _PAYLOAD.items():
            (directory / filename).write_bytes(content)
        (directory / 'request.json').write_text(json.dumps({**spec, 'header': header, 'rows_input': rows}),
                                              encoding='utf-8')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'HOME': '/workspace', 'TMPDIR': '/tmp', 'R_LIBS_USER': '/nonexistent',
               'R_LIBS_SITE': '/nonexistent', 'R_DEFAULT_PACKAGES': 'methods,stats,graphics,grDevices,utils,datasets',
               'FONTCONFIG_FILE': str(R_PREFIX / 'etc/fonts/fonts.conf'),
               'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'}
        subprocess.run([str(R_PREFIX / 'bin/Rscript'), '--vanilla', str(directory / 'nature_marginal.R'), temporary],
                       env=env, cwd=directory, stdin=subprocess.DEVNULL, check=True)
        finalize(directory, spec['stem'])
