"""Corpus-owned mappings keep opaque evidence refs free of backend locators."""
import json
import re

from .errors import CorpusError
from .evolution_read import read_passage
from .storage import digest, dumps
from .unified_request import EVOLUTION, fields

REFERENCE = re.compile(r'ev1_[0-9a-f]{64}')
IDENTITY_KEYS = ('source_id', 'scope_ref', 'generation_ref', 'document_ref',
                 'version_ref', 'passage_ref', 'passage_hash', 'locator')
PUBLIC_KEYS = tuple(k for k in IDENTITY_KEYS if k not in ('locator', 'scope_ref'))


def build(subject, row):
    encoded = dumps({key: row[key] for key in IDENTITY_KEYS if key in row})
    return 'ev1_' + digest(dumps([subject, encoded])), subject, encoded


def save(db, refs):
    db.executemany('INSERT OR IGNORE INTO evidence_refs(ref,subject,identity) VALUES(?,?,?)', refs)


def read(database, legacy, actor, payload):
    fields(payload, ('evidence_ref',), ('offset', 'max_chars'))
    ref, offset, width = payload['evidence_ref'], payload.get('offset', 0), payload.get('max_chars', 4000)
    missing = CorpusError('not_found', 'No matching accessible evidence.', 404)
    if not isinstance(ref, str) or not REFERENCE.fullmatch(ref):
        raise missing
    if type(offset) is not int or offset < 0 or type(width) is not int or not 1 <= width <= 30000:
        raise CorpusError('invalid_range', 'Invalid evidence range.')
    with database.transaction() as db:
        mapping = db.execute('SELECT identity FROM evidence_refs WHERE ref=? AND subject=?', (ref, actor.subject)).fetchone()
        if mapping is None or ref != 'ev1_' + digest(dumps([actor.subject, mapping['identity']])):
            raise missing
        identity = json.loads(mapping['identity'])
        if identity['source_id'] == EVOLUTION:
            source = read_passage(db, actor, identity)
        elif legacy is not None and identity['source_id'] in actor.base_sources:
            try:
                source = legacy.read(identity['source_id'], identity['generation_ref'], identity['locator'])
            except (LookupError, ValueError, OSError, RuntimeError):
                source = None
        else:
            source = None
    if not source or not isinstance(text := source.get('text'), str) or digest(text) != identity['passage_hash']:
        raise missing
    if offset >= len(text):
        raise missing
    end = min(len(text), offset + width)
    result = {'schema_version': 1, 'evidence_ref': ref, **{k: identity[k] for k in PUBLIC_KEYS},
              'text': text[offset:end], 'offset': offset, 'next_offset': end if end < len(text) else None,
              'total_chars': len(text)}
    if span := source.get('span'):
        result['span'] = span
        result['page'] = {'start': span['start'] + offset, 'end': span['start'] + end}
    return result
