"""Revision-bound canvas projection of the existing workbench object groups."""
from __future__ import annotations

from hashlib import sha256


def contract(groups, revision, *, svg, draft_revision=None, source_sha256=None, geometry=None):
    render_key = sha256(svg).hexdigest() if isinstance(svg, bytes) else None
    geometry = geometry or {}
    objects = {}
    for group in groups:
        ident = group['id']
        role, ids, clips, order = geometry.get(ident, (group['kind'], (), (), None))
        objects[ident] = {
            'objectId': ident, 'parentId': group['parent'], 'role': role,
            'geometry': {'svgIds': list(ids)} if ids else {'svgIds': [], 'mode': 'tree_only'},
            'clip': list(clips), 'zOrder': order, 'capabilities': group['properties'],
            'readonlyReason': None if group['properties'] else (group['reason'] or '此对象没有可编辑的样式。'),
            'revision': revision, 'renderKey': render_key,
        }
    return {'object_contract': {'revision': revision, 'draftRevision': draft_revision,
                                'renderKey': render_key, 'objects': objects},
            'source_sha256': source_sha256}
