"""Canonical rows and verified readback of indexed text, not original files.

Reconstruction and chain checks adapt Citrus QA Agent corpus_source.py (MIT).
Copyright (c) 2026 Citrus QA Agent contributors.
"""
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
from unicodedata import normalize

from ..errors import CorpusError
from ..sources import normalize_doi
from .inventory import LegacyChanged, LegacyUnavailable, digest, identity, stamp, unique_object
from .record_label import _GENERIC_SECTIONS, display_label, is_internal_record_key, parse_record_key, scrub_internal_keys

_PATH = re.compile(r'(?:/(?:home|root|mnt|opt|srv|var|Users|data)/[^\s"\'<>`)\]]*|[A-Za-z]:\\[^\s"\'<>`)\]]*)')
_MAX_LINE = 1_048_576
_MAX_RECONSTRUCTED_CHARS = 4_000_000


def _row(raw, source):
    try:
        row = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        doc, n, text = row['paper_id'], row['chunk_index'], row['text']
        source_file = row['source_file']
        if (row['corpus_id'] != source.physical or row['document_id'] != doc
                or row['schema_version'] != 2 or row['corpus_family_id'] != source.family
                or row['_source_batch'] != source.source_id
                or not isinstance(doc, str) or not 1 <= len(doc) <= 512 or doc.strip() != doc
                or any(ord(c) < 32 for c in doc)
                or not isinstance(source_file, str) or not source_file.strip()
                or type(n) is not int or n < 0 or row['chunk_id'] != identity(source.physical, doc, n)
                or not isinstance(text, str) or not text.strip()
                or type(row.get('char_len')) is not int or row['char_len'] != len(text)
                or not all(isinstance(row.get(k, ''), str) for k in (
                    'doi', 'title', 'section_name', 'l1_category', 'prev_chunk_id', 'next_chunk_id'))):
            raise ValueError
        return row
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyUnavailable('legacy_chunk_invalid') from exc


def clean(value):
    return _PATH.sub('[路径已隐藏]', scrub_internal_keys(str(value or '')))


def _norm_heading(value):
    return re.sub(r'[\W_]+', '', normalize('NFKC', value).casefold())


def _label(first):
    section = ' '.join(str(first.get('section_name') or first.get('section') or '').split())
    if section and not is_internal_record_key(section) and section.casefold() not in _GENERIC_SECTIONS:
        return clean(section)[:200]
    return clean(display_label(first))[:200]


def year(row):
    raw = str(row.get('year') or '').strip()
    if not re.fullmatch(r'[12][0-9]{3}', raw):
        raw = (parse_record_key(row.get('paper_id')) or {}).get('year', '')
    return int(raw) if re.fullmatch(r'[12][0-9]{3}', raw) else None


def doi(row):
    try:
        return normalize_doi(row['doi']) if row.get('doi') else None
    except CorpusError:
        return None


def _chain(rows, source, doc):
    if not rows:
        return False
    for n, row in enumerate(rows):
        if row['chunk_index'] != n or row['source_file'] != rows[0]['source_file']:
            return False
        for key, index in (('prev_chunk_id', n - 1), ('next_chunk_id', n + 1)):
            choices = ('',) if index < 0 or index >= len(rows) else (
                identity(source.physical, doc, index), f'{doc}::{index:04d}')
            if row.get(key) not in choices:
                return False
    return True


def _reconstruction(rows, n):
    title = _label(rows[0])
    title_key = _norm_heading(title)
    cursor, last_section = len(title) + 4, None
    for pos, row in enumerate(rows):
        label = ' '.join(str(row.get('section_name') or row.get('section') or '').split())
        section = clean(label) or '正文'
        if section != last_section and _norm_heading(section) != title_key:
            cursor += len(f'## {section}\n\n')
        last_section = section
        text = clean(row['text'])
        if pos == n:
            result = text, cursor, cursor + len(text)
        cursor += len(text) + (2 if pos + 1 < len(rows) else 0)
        if cursor > _MAX_RECONSTRUCTED_CHARS:
            return None
    return result


@dataclass(frozen=True)
class ChunkIndex:
    offsets: dict
    doc_metadata: dict
    topics: frozenset
    avg_chars: float

    @classmethod
    def build(cls, stream, source, check):
        hasher = sha256()
        offset = 0
        offsets = defaultdict(dict)
        metadata = {}
        topics = set()
        count = chars = 0
        while raw := stream.readline(_MAX_LINE + 1):
            if count % 128 == 0:
                check()
            if len(raw) > _MAX_LINE:
                raise LegacyUnavailable('legacy_chunk_too_large')
            hasher.update(raw)
            row = _row(raw, source)
            doc, ordinal = row['paper_id'], row['chunk_index']
            if ordinal in offsets[doc]:
                raise LegacyUnavailable('legacy_duplicate_chunk')
            offsets[doc][ordinal] = offset
            offset += len(raw)
            count += 1
            chars += len(row['text'])
            topic = row.get('l1_category', '')
            if topic.strip():
                topics.add(topic)
            if doc not in metadata or ordinal == 0:
                old_doi, old_year, _ = metadata.get(doc, (None, None, None))
                metadata[doc] = doi(row) or old_doi, year(row) or old_year, _label(row)
            elif (not metadata[doc][0] and row.get('doi')) or (not metadata[doc][1] and row.get('year')):
                old_doi, old_year, title = metadata[doc]
                metadata[doc] = old_doi or doi(row), old_year or year(row), title
        if count != source.count or hasher.hexdigest() != source.hash:
            raise LegacyUnavailable('legacy_chunks_hash_or_count_mismatch')
        return cls(dict(offsets), metadata, frozenset(topics), chars / max(1, count))

    def read_row(self, stream, source, doc, ordinal):
        offset = self.offsets.get(doc, {}).get(ordinal)
        if offset is None:
            return None
        stream.seek(offset)
        return _row(stream.readline(_MAX_LINE + 1), source)

    def read_document(self, stream, source, doc, check):
        rows, raw_content = [], sha256()
        raw_size = 0
        positions = self.offsets.get(doc, {})
        for n in sorted(positions):
            offset = positions[n]
            check()
            stream.seek(offset)
            raw = stream.readline(_MAX_LINE + 1)
            raw_size += len(raw)
            if not raw or raw_size > 4 * _MAX_RECONSTRUCTED_CHARS:
                return (), None
            row = _row(raw, source)
            if row['paper_id'] != doc:
                raise LegacyChanged('legacy_chunk_offset_changed')
            rows.append(row)
            raw_content.update(len(raw).to_bytes(4, 'big'))
            raw_content.update(raw)
        return rows, raw_content.hexdigest()


def ensure_open_stamp(stream, source, inventory, observed):
    if observed != inventory.stamps[source.path] or stamp(os.fstat(stream.fileno())) != observed:
        raise LegacyChanged('legacy_snapshot_changed')


def document_ref(source, doc):
    return 'legacy_doc_' + identity(source.source_id, doc, length=40)


def evidence_row(inventory, source, index, matched, rows, version_hash):
    doc, pos = matched['paper_id'], matched['chunk_index']
    dref = document_ref(source, doc)
    ver = 'legacy_ver_' + identity(dref, version_hash or source.hash, length=40)
    exact = (_reconstruction(rows, pos) if _chain(rows, source, doc) and pos < len(rows)
             and rows[pos] == matched else None)
    text = exact[0] if exact else clean(matched['text'])
    passage_hash = digest(text.encode('utf-8'))
    pref = 'legacy_pass_' + identity(ver, matched['chunk_id'], passage_hash, length=40)
    canonical_doi, publication_year, title = index.doc_metadata[doc]
    candidate = dict(source_id=source.source_id, generation_ref=inventory.generation,
                     document_ref=dref, version_ref=ver, passage_ref=pref,
                     title=title, text=text, passage_hash=passage_hash)
    if canonical_doi:
        candidate['doi'] = canonical_doi
    if publication_year:
        candidate['year'] = publication_year
    if exact:
        candidate['locator'] = dict(document=doc, ordinal=pos, chunk=matched['chunk_id'],
                                    input_sha256=digest(matched['text'].encode('utf-8')),
                                    version_ref=ver, passage_ref=pref, passage_hash=passage_hash,
                                    fingerprint=inventory.fingerprint,
                                    start=exact[1], end=exact[2])
    return candidate
