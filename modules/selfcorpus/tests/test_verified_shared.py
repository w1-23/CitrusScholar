import http.client
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from corpus.api import CorpusHTTPServer
from corpus.auth import Actor, INGEST_TARGET, MAX_BODY_BYTES, VERIFIED_SHARED_TARGET, signed_headers
from corpus.embeddings import HashEmbedding
from corpus.host_client import HostClient, HostClientError
from corpus.parsers import ParserRouter
from corpus.queue import VERIFIED_SHARED_JOB, enqueue
from corpus.service import App
from corpus.sources import Sources
from corpus.storage import Database
from corpus.worker import Worker
from test_verified_text import META, TEXT_SHA, b64, spec

SECRET = b'isolated shared corpus signed integration'


def upload(doi, word):
    body = f'The original field report documented {word} from the orchard and replicated the seasonal observation.'
    return {'kind': 'upload', 'filename': 'private.txt', 'doi': doi, 'media_type': 'text/plain',
            'content_b64': b64(body.encode())}


class VerifiedSharedTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / 'corpus.db')
        embedding = HashEmbedding()
        self.worker = Worker(self.db, Sources(), ParserRouter(grobid_url='', docling=False), embedding)
        self.app = App(self.db, embedding)
        server = CorpusHTTPServer(self.app, SECRET, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.client = HostClient(SECRET, f'http://127.0.0.1:{self.port}')
        self.alice, self.bob, self.denied = Actor('alice'), Actor('bob'), Actor('denied', shared=False)
        self.governor = Actor('governor', governor=True)

    def _error(self, status, code, call, *args):
        with self.assertRaises(HostClientError) as caught:
            call(*args)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def _wire(self, target, payload, *, signed_target=None):
        body = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if signed_target is not None:
            headers.update(signed_headers(SECRET, 'POST', signed_target, body, self.alice))
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.request('POST', target, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def _search(self, actor, doi, *, term='Banksia', **scope):
        return self.client.search(actor, term, scope, {'doi': doi})['results']

    def _finished(self, actor, queued, status='published'):
        self.assertTrue(self.worker.once())
        done = self.client.request(actor, queued['request_ref'])
        self.assertEqual(done['status'], status, done)
        with self.db.transaction() as db:
            self.assertEqual(db.execute('SELECT spec FROM requests WHERE ref=?', (queued['request_ref'],)).fetchone()[0], '{}')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        return done

    def test_signed_shared_publication_is_cross_account_personal_upload_is_not(self):
        payload = spec()
        status, queued = self._wire(VERIFIED_SHARED_TARGET, {'spec': payload, 'idempotency_key': 'paper'},
                                    signed_target=VERIFIED_SHARED_TARGET)
        self.assertEqual((status, queued['status'], queued['scope_ref']), (202, 'queued', 'shared'))
        self.assertEqual(self._search(self.bob, META['doi'], shared=True), [])
        with self.db.transaction() as db:
            root = json.loads(db.execute('SELECT spec FROM requests WHERE ref=?', (queued['request_ref'],)).fetchone()[0])
            self.assertEqual(root, {'kind': VERIFIED_SHARED_JOB, 'source': payload})
        done = self._finished(self.alice, queued)
        self.assertEqual(done['output']['source_hash'], TEXT_SHA)
        self.assertEqual(done['output']['parser'], 'plain-text/1')
        self.assertNotIn('pending_review', [event['action'] for event in done['events']])
        hits = self._search(self.bob, META['doi'], shared=True)
        self.assertTrue(hits)
        self.assertTrue(all(hit['scope_ref'] == 'shared' and hit['source_hash'] == TEXT_SHA for hit in hits))
        doc = self.client.document(self.bob, hits[0]['document_ref'])
        self.assertEqual((doc['source_kind'], doc['source_uri']), ('verified_text', payload['text_url']))
        self.assertEqual(doc['metadata']['license_id'], 'cc-by')
        self._error(404, 'not_found', self.client.request, self.bob, queued['request_ref'])
        self._error(403, 'forbidden', self._search_denied)
        self._error(404, 'not_found', self.client.document, self.denied, hits[0]['document_ref'])
        private_doi = '10.1000/personal'
        private = self.client.submit(self.alice, upload(private_doi, 'privateambernote'), 'personal:alice', 'personal')
        private_done = self._finished(self.alice, private)
        self.assertTrue(self._search(self.alice, private_doi, term='privateambernote', personal=True))
        self.assertEqual(self._search(self.bob, private_doi, term='privateambernote', personal=True, shared=True), [])
        self._error(404, 'not_found', self.client.document, self.bob, private_done['output']['document_ref'])

    def _search_denied(self):
        return self._search(self.denied, META['doi'], shared=True)

    def test_concurrent_cross_actor_duplicate_has_no_foreign_receipt(self):
        ticket = threading.Barrier(2)
        def submit(actor):
            ticket.wait(timeout=2)
            try:
                return actor, self.client.submit_verified_shared(actor, spec(), 'same-proof')
            except HostClientError as exc:
                return actor, exc
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, (self.alice, self.bob)))
        self.assertEqual(sum(isinstance(item, dict) for _, item in outcomes), 1)
        owner, queued = next((actor, item) for actor, item in outcomes if isinstance(item, dict))
        other, conflict = next((actor, item) for actor, item in outcomes if not isinstance(item, dict))
        self.assertEqual((conflict.status, conflict.code), (409, 'already_pending'))
        self.assertIsNone(getattr(conflict, 'request_ref', None))
        self.assertEqual(self.client.submit_verified_shared(owner, spec(), 'same-proof'), queued)
        done = self._finished(owner, queued)
        self._error(409, 'already_present', self.client.submit_verified_shared, other, spec(), 'same-proof')
        self.assertEqual(self.client.submit_verified_shared(owner, spec(), 'same-proof'), done)
        self.assertFalse(self.worker.once())
        with self.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 1)

    def test_proof_failure_and_route_authentication_leave_nothing_public(self):
        source = spec()
        payload = {'spec': source, 'idempotency_key': 'signed'}
        for signature in (None, INGEST_TARGET):
            with self.subTest(signature=signature):
                self.assertEqual(self._wire(VERIFIED_SHARED_TARGET, payload, signed_target=signature)[0], 401)
        self.assertEqual(self._wire(VERIFIED_SHARED_TARGET, {**payload, 'scope_ref': 'shared'},
                                    signed_target=VERIFIED_SHARED_TARGET)[0], 400)
        self._error(403, 'forbidden', self.client.submit_verified_shared, self.denied, source, 'denied')
        self._error(400, 'invalid_source', self.client.submit_verified_shared, self.alice,
                    upload('10.1000/example', 'ordinary'), 'upload')
        self._error(403, 'forbidden', self.client.submit, self.alice, source, 'shared', 'generic')
        self._error(403, 'forbidden', self.client.submit, self.governor,
                    {'kind': VERIFIED_SHARED_JOB, 'source': source}, 'shared', 'marker')
        oversized_for_normal = b'x' * (MAX_BODY_BYTES + 1)
        damaged = spec(content_b64=b64(oversized_for_normal))
        queued = self.client.submit_verified_shared(self.alice, damaged, 'damaged')
        self.assertEqual(self._finished(self.alice, queued, 'failed')['error']['code'], 'invalid_verified_text')
        self.assertEqual(self._search(self.bob, META['doi'], shared=True), [])
        good = self.client.submit_verified_shared(self.bob, source, 'good')
        self._finished(self.bob, good)

    def test_new_shared_upload_rejected_and_old_pending_receipt_read_decline(self):
        self._error(403, 'forbidden', self.client.submit, self.alice,
                    upload(META['doi'], 'manualorchard'), 'shared', 'manual')
        with self.db.transaction(write=True) as db:
            pending = enqueue(db, self.alice.subject, 'shared', 'historical-pending',
                              upload(META['doi'], 'manualorchard'))
            db.execute("UPDATE requests SET status='pending_review',spec='{}' WHERE ref=?", (pending['request_ref'],))
        ref = pending['request_ref']
        self.assertEqual(self.client.proposal(self.alice, ref)['status'], 'pending_review')
        with self.assertRaises(HostClientError) as caught:
            self.client.proposal(self.governor, ref, approve=True)
        self.assertEqual((caught.exception.status, caught.exception.code), (403, 'forbidden'))
        self.assertEqual(self.client.proposal(self.governor, ref, approve=False)['status'], 'rejected')
        self.assertEqual(self._search(self.bob, META['doi'], shared=True), [])

    def test_worker_rejects_durable_shared_envelope_with_nonverified_source(self):
        with self.db.transaction(write=True) as db:
            queued = enqueue(db, self.alice.subject, 'shared', 'internal',
                             {'kind': VERIFIED_SHARED_JOB, 'source': upload(META['doi'], 'ineligible')})
        self.assertEqual(self._finished(self.alice, queued, 'failed')['error']['code'], 'invalid_source')
        self.assertFalse(self._search(self.bob, META['doi'], shared=True))
