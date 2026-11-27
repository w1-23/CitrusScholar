import json
import logging
from threading import Event, Thread

from .catalog import active_doi, document_rows, save_chunks
from .chunking import CHUNKER_VERSION, chunk
from .errors import CorpusError
from .generations import build
from .queue import LEASE_SECONDS, VERIFIED_SHARED_JOB, Queue, assert_lease, finish
from .sources import VERIFIED_TEXT
from .storage import event
from .vector_index import encode_vector

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, database, sources, parser, embedding):
        self.database = database
        self.queue = Queue(database)
        self.sources = sources
        self.parser = parser
        self.embedding = embedding

    def once(self):
        job = self.queue.claim()
        if job is None:
            return False
        stop = Event()

        def renew():
            while not stop.wait(LEASE_SECONDS / 4):
                try:
                    if not self.queue.heartbeat(job):
                        return
                except Exception:
                    log.warning('Lease renewal failed for %s', job['ref'])

        heartbeat = Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            self._run(job)
        except CorpusError as exc:
            self._failure(job, exc)
        except Exception as exc:
            log.error('Worker failed for %s (%s)', job['ref'], type(exc).__name__)
            self._failure(job, CorpusError('processing_failed', 'Processing failed before publication.', 500))
        finally:
            stop.set()
            heartbeat.join()
        return True

    def _failure(self, job, exc):
        if exc.code != 'lost_lease':
            try:
                self.queue.fail(job, exc)
            except CorpusError as lease:
                if lease.code != 'lost_lease':
                    raise

    @staticmethod
    def _skip_active_doi(db, job, doi):
        present = active_doi(db, job['scope'], doi)
        if present:
            assert_lease(db, job)
            finish(db, job, 'already_present', dict(present))
        return bool(present)

    def _run(self, job):
        spec = json.loads(job['spec'])
        shared_intake = spec.get('kind') == VERIFIED_SHARED_JOB
        if shared_intake:
            if job['scope'] != 'shared' or spec.keys() != {'kind', 'source'}:
                raise CorpusError('forbidden', 'Requested scope is not authorized.', 403)
            spec = spec['source']
            if not isinstance(spec, dict) or spec.get('kind') != VERIFIED_TEXT:
                raise CorpusError('invalid_source', 'Shared automatic intake requires verified PMC Cloud text.')
        verified = spec.get('kind') == VERIFIED_TEXT
        if verified and not shared_intake and job['scope'] != f"personal:{job['owner']}":
            raise CorpusError('forbidden', 'Requested scope is not authorized.', 403)
        source = self.sources.fetch(spec)
        self.queue.phase(job, 'fetched')
        if verified:
            with self.database.transaction(write=True) as db:
                if self._skip_active_doi(db, job, source.metadata['doi']):
                    return
        self.queue.phase(job, 'parsing')
        parsed = self.parser.parse(source)
        self.queue.phase(job, 'parsed')
        active = parsed.quality.get('status') == 'complete' and source.metadata.get('fulltext') is not False
        self.queue.phase(job, 'chunking')
        chunks = chunk(parsed) if active else []
        if not chunks:
            if verified:
                raise CorpusError('invalid_verified_text', 'Verified text is not indexable.', 422)
            with self.database.transaction(write=True) as db:
                assert_lease(db, job)
                result = document_rows(db, job['scope'], source, parsed)
                finish(db, job, 'partial', {**result, 'limitations':['no_indexable_fulltext']})
            return
        self.queue.phase(job, 'embedding')
        model, dimension = self.embedding.signature, self.embedding.dimension
        title = parsed.metadata.get('title') or source.metadata.get('title')
        vectors = self.embedding.embed(['\n'.join(x for x in [title, *ch['heading_path'], ch['text']] if x) for ch in chunks])
        if len(chunks) != len(vectors):
            raise CorpusError('embedding_invalid', 'Embedding count does not match the document chunks.', 422)
        encode_vector(vectors[0], dimension)
        self.queue.phase(job, 'indexing')
        with self.database.transaction(write=True) as db:
            assert_lease(db, job)
            # Another intake may have published the same DOI after this request queued.
            if verified and self._skip_active_doi(db, job, source.metadata['doi']):
                return
            result = document_rows(db, job['scope'], source, parsed)
            save_chunks(db, result, chunks, vectors, model, dimension, CHUNKER_VERSION, title)
            result.update({'embedding':model, 'dimension':dimension, 'chunker':CHUNKER_VERSION, 'chunks':len(chunks)})
            event(db, job['scope'], 'worker', 'validating', request_ref=job['ref'])
            assert_lease(db, job)
            if job['scope'] == 'shared' and not shared_intake:
                finish(db, job, 'pending_review', result)
            else:
                result['generation_ref'] = build(db, job['scope'], model, dimension, CHUNKER_VERSION, job['ref'], replacement=result)
                assert_lease(db, job)
                finish(db, job, 'published', result)

    def run(self, *, poll=0.5, stop=None):
        stop = stop or Event()
        while not stop.is_set():
            if not self.once():
                stop.wait(poll)
