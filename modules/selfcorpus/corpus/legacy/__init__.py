"""Independent read-only adapter for the five approved legacy collections."""
from collections import OrderedDict
from threading import Lock
from types import MappingProxyType

from ..errors import CorpusError
from ..sources import normalize_doi
from .chunks import ChunkIndex, ensure_open_stamp, evidence_row
from .dense import DenseAttestation, DenseTable, LegacyDense, PRODUCTION_DENSE_ATTESTATION
from .legacy_e5 import LegacyE5
from .inventory import ApprovedInventory, Inventory, LegacyChanged, LegacyUnavailable, Mount, PRODUCTION
from .lexical import rank

__all__ = ['LegacyCorpus', 'LegacyChanged', 'LegacyUnavailable', 'ApprovedInventory',
           'LegacyDense', 'DenseAttestation', 'DenseTable', 'LegacyE5', 'PRODUCTION_DENSE_ATTESTATION']


class LegacyCorpus:
    def __init__(self, root, embedding=None, *, approved_inventory=None, dense_attestation=None):
        self._mount = Mount(root)
        self._approved = MappingProxyType({**(approved_inventory or {}), **PRODUCTION})
        self._dense = LegacyDense(embedding, dense_attestation) if dense_attestation is not None else None
        self._cache = OrderedDict()
        self._lock = Lock()

    def pin(self):
        return LegacySnapshot(self, Inventory.load(self._mount, None, self._approved, active=True))

    def prepare_dense(self, check=lambda: None):
        """Verify Lance files before a request deadline, if explicitly configured."""
        if self._dense is not None:
            inventory = self.pin()._inventory
            self._dense.prepare(inventory, check)
            inventory.check_current()

    def _index(self, stream, inventory, source, observed, check):
        key = inventory.fingerprint, source.source_id, observed
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        index = ChunkIndex.build(stream, source, check)
        ensure_open_stamp(stream, source, inventory, observed)
        inventory.check_current()
        with self._lock:
            self._cache[key] = index
            self._cache.move_to_end(key)
            while len(self._cache) > 10:
                self._cache.popitem(last=False)
        return index

    def read(self, source_id, generation_ref, locator):
        if not isinstance(locator, dict) or not isinstance(source_id, str):
            return None
        try:
            inventory = Inventory.load(self._mount, generation_ref, self._approved)
            source = inventory.sources.get(source_id)
            if source is None or inventory.fingerprint != locator.get('fingerprint'):
                return None
            doc, ordinal = locator.get('document'), locator.get('ordinal')
            if not isinstance(doc, str) or type(ordinal) is not int or ordinal < 0:
                return None
            with inventory.mount.open(source.path) as (stream, observed):
                ensure_open_stamp(stream, source, inventory, observed)
                index = self._index(stream, inventory, source, observed, lambda: None)
                rows, version = index.read_document(stream, source, doc, lambda: None)
                if ordinal >= len(rows):
                    return None
                result = evidence_row(inventory, source, index, rows[ordinal], rows, version)
                actual = result.get('locator')
                if actual is None or actual != locator:
                    return None
                ensure_open_stamp(stream, source, inventory, observed)
            inventory.check_current()
            return {'text': result['text'], 'span': {'start': actual['start'], 'end': actual['end']}}
        except (LegacyUnavailable, OSError):
            return None


class LegacySnapshot:
    def __init__(self, corpus, inventory):
        self._corpus = corpus
        self._inventory = inventory
        self.generation = inventory.generation
        self.fingerprint = inventory.fingerprint

    def check_current(self):
        self._inventory.check_current()

    def search(self, source_id, query, topics, filters, caps, check):
        check()
        self.check_current()
        inventory = self._inventory
        source = inventory.sources.get(source_id)
        if source is None:
            raise LegacyUnavailable('legacy_source_unapproved')
        cap, dense = caps.get('lexical', 0), caps.get('dense', 0)
        if any(type(n) is not int or n < 0 for n in (cap, dense)):
            raise LegacyUnavailable('legacy_invalid_budget')
        lane = self._corpus._dense
        dense_enabled = dense and lane is not None and lane.permits(inventory, source)
        reasons = ['legacy_dense_unverified'] if dense and not dense_enabled else []
        if not cap and not dense_enabled:
            return [], reasons or ['legacy_lexical_budget_exhausted'], 0
        aliases = {k.strip().casefold(): v.strip().casefold() for k, v in inventory.aliases.items()}
        requested = {aliases.get(k, k) for t in (topics or ()) if (k := t.strip().casefold())}
        filters = dict(filters or {})
        if filters.get('doi'):
            try:
                filters['doi'] = normalize_doi(filters['doi'])
            except CorpusError as exc:
                raise LegacyUnavailable('legacy_invalid_doi_filter') from exc
        try:
            with inventory.mount.open(source.path) as (stream, observed):
                ensure_open_stamp(stream, source, inventory, observed)
                index = self._corpus._index(stream, inventory, source, observed, check)
                if requested and not index.topics:
                    check()
                    ensure_open_stamp(stream, source, inventory, observed)
                    self.check_current()
                    return [], [*reasons, 'legacy_topics_unavailable'], 0
                stream.seek(0)
                lexical = rank(stream, source, index, query, requested, filters, cap, check)
                dense_rows, dense_used = [], 0
                if dense_enabled:
                    try:
                        dense_rows, dense_used = lane.search(inventory, source, stream, index, query,
                                                             requested, filters, dense, check)
                    except LegacyUnavailable:
                        # Reserve the full lane budget on failure because the query
                        # may have reached the backend before validation failed.
                        check()
                        dense_used = dense
                        reasons.append('legacy_dense_unavailable')
                matched = _fuse(lexical, dense_rows)
                rows = []
                previous_doc = None
                for match in matched:
                    check()
                    doc = match['paper_id']
                    if doc != previous_doc:
                        document, version = index.read_document(stream, source, doc, check)
                        previous_doc = doc
                    rows.append(evidence_row(inventory, source, index, match, document, version))
                if any('locator' not in row for row in rows):
                    reasons.append('legacy_exact_unverified')
                ensure_open_stamp(stream, source, inventory, observed)
        except OSError as exc:
            raise LegacyUnavailable('legacy_mount_unavailable') from exc
        check()
        self.check_current()
        return rows, reasons, len(lexical) + dense_used


def _fuse(lexical, dense):
    # Reciprocal rank fusion, adapted from Citrus QA Agent bm25.rrf_fuse (MIT).
    rows, scores = {}, {}
    for stream in (lexical, dense):
        for rank, row in enumerate(stream, 1):
            cid = row['chunk_id']
            rows.setdefault(cid, row)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)
    return [rows[cid] for cid in sorted(rows, key=scores.__getitem__, reverse=True)]
