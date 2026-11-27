"""Conformance-gated Lance recall. No index, catalog or model writes."""
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version as distribution_version
from math import hypot, isfinite, sumprod
import os
from pathlib import Path
import re
import stat
from threading import Lock
from types import MappingProxyType

from ..relevance import Relevance
from .chunks import clean, document_ref
from .inventory import LegacyUnavailable, SOURCES, stamp
from .legacy_e5 import LegacyE5, RAW_SIGNATURE
from .lexical import _eligible


_LANCEDB_VERSION = '0.37.1'
_SHA256 = re.compile(r'[0-9a-f]{64}')
_COLUMNS = ('chunk_id', 'corpus_id', 'corpus_family_id', 'document_id', 'paper_id', 'chunk_index', 'l1_category')


@dataclass(frozen=True)
class DenseTable:
    version: int
    metric: str
    fingerprint: str
    relevance_floor: float
    index_uuid: str | None = None

    def __post_init__(self):
        if (type(self.version) is not int or self.version < 1 or not _SHA256.fullmatch(self.fingerprint)
                or not 0.0 < self.relevance_floor <= 1.0
                or self.metric not in ('cosine', 'l2') or (self.metric == 'cosine') != bool(self.index_uuid)):
            raise ValueError('invalid_legacy_dense_table_attestation')


@dataclass(frozen=True)
class DenseAttestation:
    inventory_fingerprint: str
    tables: dict[str, DenseTable]
    device: str = 'cpu'

    def __post_init__(self):
        if (not _SHA256.fullmatch(self.inventory_fingerprint) or not self.tables
                or self.device not in ('cpu', 'cuda')
                or not self.tables.keys() <= SOURCES.keys()
                or not all(isinstance(t, DenseTable) for t in self.tables.values())):
            raise ValueError('invalid_legacy_dense_attestation')
        object.__setattr__(self, 'tables', MappingProxyType(dict(self.tables)))


# Independently measured against FastEmbed 0.8.0 and LanceDB 0.37.1.
# Both IVF cosine and flat L2 preserved all top-12 ranks on legacy controls.
# Calibrating on cosine alone failed an unrelated literature control, so the
# runtime always additionally requires lexical passage corroboration.
PRODUCTION_DENSE_ATTESTATION = DenseAttestation(
    '0af0249348fda75de1f210bda9c7dd36b0056364a0ba8b9e60f023157912e99f', {
        'cqa_deng_book': DenseTable(1, 'l2',
            'f9ea081e85f0d2b23ee9cf55da15969aba75b69fdcc8d629dbdc60ef50450b1d', 0.800),
        'cqa_literature': DenseTable(416, 'cosine',
            'c44fcc7ab21fa7d4b1fb567d1830e7bebc380a62ae9ccbf0fdb67a935dd46e01', 0.800,
            '2bad6164-a9e7-46f7-b66f-66c32e0c54f7'),
        'cqa_methods': DenseTable(1, 'l2',
            '99640d8d3967663411e3590db2b1a7ace3743549c0b336c784bfcef1277e2509', 0.800),
        'cqa_translated': DenseTable(73, 'cosine',
            'da5d73334f711c7dc8925f74a4410b616cc5ee8b2f284df33bed5d00315bd91b', 0.800,
            '179c9c06-e24a-4475-b4e9-9b363f51f6fb'),
        'cqa_ucr_varieties': DenseTable(10, 'l2',
            'aa9d27f1c72cd90d4e58d1c1069743ae2d0ab81cfc20f39b3a15ca2f7c8236cb', 0.800),
    })


def _tree(root):
    if root.resolve(strict=True) != root:
        raise LegacyUnavailable('legacy_dense_symlink')
    entries, files = {}, []
    pending = [root]
    while pending:
        node = pending.pop()
        info = node.lstat()
        entries[node] = stamp(info)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(node.iterdir())
        elif stat.S_ISREG(info.st_mode):
            files.append(node)
        else:
            raise LegacyUnavailable('legacy_dense_tree_invalid')
    return entries, sorted(files, key=Path.as_posix)


def _digest_tree(path, files, stamps, check):
    result = sha256()
    for file in files:
        check()
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or stamp(before) != stamps[file]:
                raise LegacyUnavailable('legacy_dense_tree_changed')
            hasher = sha256()
            while block := stream.read(4 * 1024 * 1024):
                check()
                hasher.update(block)
            if stamp(os.fstat(fd)) != stamps[file]:
                raise LegacyUnavailable('legacy_dense_tree_changed')
        result.update(file.relative_to(path).as_posix().encode() + b'\0' + hasher.hexdigest().encode() + b'\n')
    return result.hexdigest()


def table_fingerprint(path):
    """Read-only auditor helper, not a runtime source of approvals."""
    path = Path(os.path.abspath(path))
    stamps, files = _tree(path)
    value = _digest_tree(path, files, stamps, lambda: None)
    if _tree(path)[0] != stamps:
        raise LegacyUnavailable('legacy_dense_tree_changed')
    return value


def _open(path, table, source):
    try:
        import lancedb
        import pyarrow as pa
        if (distribution_version('lancedb') != _LANCEDB_VERSION
                or lancedb.__version__ != _LANCEDB_VERSION):
            raise LegacyUnavailable('legacy_dense_reader_version_unapproved')
        # Existing, fully fingerprinted path only. Production should mount the
        # whole legacy root read-only. No dataset/catalog write method is used.
        dataset = lancedb.connect(path.parent, read_consistency_interval=None,
                                  manifest_enabled=False).open_table(source.physical)
        if dataset.version != table.version:
            raise LegacyUnavailable('legacy_dense_table_version_mismatch')
        dataset.checkout(table.version)
        vector = dataset.schema.field('vector').type
        if (not pa.types.is_fixed_size_list(vector) or vector.list_size != 1024
                or vector.value_type != pa.float32() or dataset.version != table.version
                or set(dataset.schema.names) != {'vector', *_COLUMNS} or dataset.count_rows() != source.count):
            raise LegacyUnavailable('legacy_dense_table_schema_mismatch')
        indices = dataset.list_indices()
        if table.index_uuid:
            if (len(indices) != 1 or indices[0].index_uuid != table.index_uuid
                    or indices[0].columns != ['vector'] or indices[0].index_type != 'IvfHnswFlat'):
                raise LegacyUnavailable('legacy_dense_index_mismatch')
            stats = dataset.index_stats(indices[0].name)
            if (stats.num_indexed_rows != source.count or stats.num_unindexed_rows != 0
                    or stats.distance_type != table.metric):
                raise LegacyUnavailable('legacy_dense_metric_mismatch')
        elif indices:
            raise LegacyUnavailable('legacy_dense_unexpected_index')
        return dataset
    except LegacyUnavailable:
        raise
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        raise LegacyUnavailable('legacy_dense_table_unavailable') from exc


def _sql_values(values):
    # Values are from canonical, validated metadata and quoted independently.
    return ','.join("'" + value.replace("'", "''") + "'" for value in values)


def _query(dataset, source, table, vector, topics, filters, index, cap):
    try:
        clauses = [f'corpus_family_id IN ({_sql_values([source.family])})']
        if topics:
            valid = sorted(t for t in index.topics if t.strip().casefold() in topics)
            if not valid:
                return []
            clauses.append(f'l1_category IN ({_sql_values(valid)})')
        clauses.append(f'corpus_id IN ({_sql_values([source.physical])})')
        if filters:
            docs = sorted(doc for doc, (doi, year, _) in index.doc_metadata.items()
                          if (not filters.get('doi') or filters['doi'] == doi)
                          and (not filters.get('year') or filters['year'] == year)
                          and (not filters.get('document_ref') or filters['document_ref'] == document_ref(source, doc)))
            if not docs:
                return []
            clauses.append(f'document_id IN ({_sql_values(docs)})')
        return (dataset.search(list(vector)).distance_type(table.metric)
                .where(' AND '.join(clauses), prefilter=True).limit(cap)
                .select([*_COLUMNS, 'vector', '_distance']).to_list())
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        raise LegacyUnavailable('legacy_dense_query_unavailable') from exc


class LegacyDense:
    def __init__(self, embedding, attestation):
        if (not isinstance(embedding, LegacyE5) or embedding.signature != RAW_SIGNATURE
                or not isinstance(attestation, DenseAttestation) or attestation.device != embedding.device):
            raise LegacyUnavailable('legacy_dense_conformance_missing')
        self.embedding = embedding
        self.attestation = attestation
        self._tables = {}
        self._lock = Lock()

    def _table(self, inventory, source, check):
        path = inventory.mount.root / 'lancedb_generations' / inventory.generation / f'{source.physical}.lance'
        proof = self.attestation.tables[source.source_id]
        try:
            stamps, files = _tree(path)
            with self._lock:
                cached = self._tables.get(source.source_id)
            if cached is not None:
                if cached[0] != stamps:
                    raise LegacyUnavailable('legacy_dense_tree_changed')
                return path, stamps, cached[1]
            if _digest_tree(path, files, stamps, check) != proof.fingerprint:
                raise LegacyUnavailable('legacy_dense_fingerprint_mismatch')
            check()
            dataset = _open(path, proof, source)
            if _tree(path)[0] != stamps:
                raise LegacyUnavailable('legacy_dense_tree_changed')
            with self._lock:
                self._tables[source.source_id] = (stamps, dataset)
            return path, stamps, dataset
        except OSError as exc:
            raise LegacyUnavailable('legacy_dense_tree_unavailable') from exc

    def permits(self, inventory, source):
        return (inventory.fingerprint == self.attestation.inventory_fingerprint
                and source.source_id in self.attestation.tables)

    def prepare(self, inventory, check):
        for sid in self.attestation.tables:
            source = inventory.sources[sid]
            if not self.permits(inventory, source):
                raise LegacyUnavailable('legacy_dense_conformance_missing')
            self._table(inventory, source, check)

    def search(self, inventory, source, stream, index, query, topics, filters, cap, check):
        path, stamps, dataset = self._table(inventory, source, check)
        proof = self.attestation.tables[source.source_id]
        check()
        try:
            vector = self.embedding.query_vector(query)
        except Exception as exc:
            raise LegacyUnavailable('legacy_dense_embedding_unavailable') from exc
        check()
        norm = hypot(*vector)
        if len(vector) != 1024 or not isfinite(norm) or norm <= 0:
            raise LegacyUnavailable('legacy_dense_embedding_invalid')
        raw = _query(dataset, source, proof, vector, topics, filters, index, cap)
        if len(raw) > cap:
            raise LegacyUnavailable('legacy_dense_budget_mismatch')
        relevance = Relevance(query, RAW_SIGNATURE)
        rows, seen = [], set()
        for candidate in raw:
            check()
            doc, ordinal = candidate['document_id'], candidate['chunk_index']
            if not isinstance(doc, str) or type(ordinal) is not int:
                raise LegacyUnavailable('legacy_dense_row_identity_mismatch')
            row = index.read_row(stream, source, doc, ordinal)
            if (row is None or candidate['paper_id'] != doc or row['paper_id'] != doc
                    or any(row.get(k, '') != candidate[k] for k in (
                        'chunk_id', 'chunk_index', 'corpus_id', 'corpus_family_id', 'l1_category'))
                    or not _eligible(row, source, index, topics, filters) or row['chunk_id'] in seen):
                raise LegacyUnavailable('legacy_dense_row_identity_mismatch')
            seen.add(row['chunk_id'])
            stored = candidate['vector']
            if not isinstance(stored, list) or any(type(x) not in (int, float) for x in stored):
                raise LegacyUnavailable('legacy_dense_vector_invalid')
            length = hypot(*stored)
            if len(stored) != 1024 or not isfinite(length) or length <= 0:
                raise LegacyUnavailable('legacy_dense_vector_invalid')
            cosine = sumprod(vector, stored) / (norm * length)
            if (isfinite(cosine) and cosine >= proof.relevance_floor
                    and relevance.lexical(clean(row['text']))):
                rows.append(row)
        check()
        try:
            if _tree(path)[0] != stamps:
                raise LegacyUnavailable('legacy_dense_tree_changed')
        except OSError as exc:
            raise LegacyUnavailable('legacy_dense_tree_unavailable') from exc
        return rows, len(raw)
