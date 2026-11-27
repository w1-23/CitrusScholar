import base64
import hashlib
import http.client
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from corpus.api import CorpusHTTPServer
from corpus.auth import Actor, INGEST_TARGET
from corpus.embeddings import HashEmbedding
from corpus.errors import CorpusError
from corpus.host_client import HostClient, HostClientError
from corpus.parsers import ParserRouter
from corpus.service import App
from corpus.sources import Sources
from corpus.storage import Database
from corpus.worker import Worker

FIXTURE_DIR = os.getenv('SELFCORPUS_VERIFIED_TEXT_FIXTURES')
FIXTURES = Path(FIXTURE_DIR) if FIXTURE_DIR else None
KEY = 'PMC4858230.1'
CLOUD = 'https://pmc-oa-opendata.s3.amazonaws.com'
TEXT_SHA = 'b3a0e6001bdbce07df05874fba766489d133cbb6803a5478375c962ea41539cd'
EVIDENCE_SHA = '3c729f1a0389cfee424cf1ab83dc711ef787b53a9e639fefba70d77b4d01312f'
FIXTURE_SKIP = ('set SELFCORPUS_VERIFIED_TEXT_FIXTURES to the hash-verified public fixture directory '
                '(see tests/fixtures/verified_text/ATTRIBUTION.md)')
if FIXTURES is None:
    RAW, EVIDENCE, META = b'', b'', {}
else:
    RAW, EVIDENCE = ((FIXTURES / (KEY + ext)).read_bytes() for ext in ('.txt', '.json'))
    if hashlib.sha256(RAW).hexdigest() != TEXT_SHA or hashlib.sha256(EVIDENCE).hexdigest() != EVIDENCE_SHA:
        raise ValueError('Public verified-text fixture hashes do not match')
    META = json.loads(EVIDENCE)


def b64(data):
    return base64.b64encode(data).decode('ascii')


def spec(**changes):
    return {'kind': 'verified_text', 'doi': META['doi'], 'pmid': str(META['pmid']),
            'pmcid': META['pmcid'], 'title': META['title'], 'license_id': 'cc-by',
            'evidence_url': f'{CLOUD}/metadata/{KEY}.json', 'text_url': f'{CLOUD}/{KEY}/{KEY}.txt',
            'content_b64': b64(RAW), 'metadata_b64': b64(EVIDENCE), 'sha256': TEXT_SHA, **changes}


def with_body(raw):
    evidence = {**META, 'text_url': META['text_url'].split('=')[0] + '=' + hashlib.md5(raw, usedforsecurity=False).hexdigest()}
    return spec(content_b64=b64(raw), sha256=hashlib.sha256(raw).hexdigest(),
                metadata_b64=b64(json.dumps(evidence).encode()))


@unittest.skipUnless(FIXTURES is not None, FIXTURE_SKIP)
class VerifiedTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'corpus.db')
        self.embedding = HashEmbedding()
        self.app = App(self.db, self.embedding)
        self.http = Mock()
        self.sources = Sources(self.http)
        self.worker = Worker(self.db, self.sources, ParserRouter(grobid_url='', docling=False), self.embedding)
        self.alice = Actor('alice', {'farm': 'write'}, governor=True)
        self.bob = Actor('bob', shared=False)

    def serve(self):
        secret = b'isolated verified text contract test'
        server = CorpusHTTPServer(self.app, secret, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        return HostClient(secret, f'http://127.0.0.1:{port}'), port

    def test_signed_queue_publish_retrieve_and_exact_provenance(self):
        client, _ = self.serve()
        submission = spec()
        queued = client.submit(self.alice, submission, 'personal:alice', 'original')
        self.assertEqual(queued['status'], 'queued')
        self.assertFalse(client.search(self.alice, 'Banksia', {'personal': True})['results'])
        self.assertTrue(self.worker.once())
        ref = queued['request_ref']
        done = client.request(self.alice, ref)
        self.assertEqual(done['status'], 'published', done)
        self.assertEqual(done['output']['source_hash'], TEXT_SHA)
        self.assertEqual(done['output']['parser'], 'plain-text/1')
        doc = done['output']['document_ref']
        self.assertEqual(client.document(self.alice, doc)['source_uri'], submission['text_url'])
        self.assertEqual(client.document(self.alice, doc)['source_kind'], 'verified_text')
        metadata = client.document(self.alice, doc)['metadata']
        for key in ('doi', 'pmid', 'pmcid', 'title', 'license_id', 'evidence_url', 'text_url', 'sha256'):
            self.assertEqual(metadata[key], submission[key])
        self.assertEqual(metadata['evidence_sha256'], EVIDENCE_SHA)
        self.assertEqual(metadata['text_md5'], '7b9b6bff0b456a5ca475f8def6dc7254')
        self.assertEqual(metadata['pmc_version'], 1)
        self.assertIs(metadata['fulltext'], True)
        self.assertNotIn('content_b64', metadata)
        self.assertNotIn('metadata_b64', metadata)
        with self.db.transaction() as db:
            row = db.execute('SELECT original,media_type FROM versions').fetchone()
            self.assertEqual((row['original'], row['media_type']), (RAW, 'text/plain'))
            self.assertEqual(db.execute('SELECT spec FROM requests WHERE ref=?', (ref,)).fetchone()[0], '{}')
        self.assertTrue(client.search(self.alice, 'Banksia', {'personal': True})['results'])
        self.assertFalse(client.search(self.bob, 'Banksia', {'personal': True})['results'])
        with self.assertRaises(HostClientError) as denied:
            client.request(self.bob, ref)
        self.assertEqual(denied.exception.status, 404)
        self.assertEqual(client.submit(self.alice, submission, 'personal:alice', 'original'), done)
        self.assertFalse(self.worker.once())
        self.assertEqual(self.http.mock_calls, [])

    def test_signed_boundary_rejects_wrong_scopes_and_missing_signature(self):
        client, port = self.serve()
        for scope in ('shared', 'project:farm', 'personal:bob'):
            with self.subTest(scope=scope), self.assertRaises(HostClientError) as caught:
                client.submit(self.alice, spec(), scope, 'wrong-scope')
            self.assertEqual((caught.exception.status, caught.exception.code), (403, 'forbidden'))
        with self.assertRaises(HostClientError) as caught:
            client.submit(self.alice, spec(text_url='https://publisher.example/original.txt'), 'personal:alice', 'wrong-url')
        self.assertEqual((caught.exception.status, caught.exception.code), (422, 'invalid_verified_text'))
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            conn.request('POST', INGEST_TARGET, json.dumps({'spec': spec(), 'scope_ref': 'personal:alice',
                                                           'idempotency_key': 'unsigned'}),
                         {'Content-Type': 'application/json'})
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()
        self.assertFalse(self.worker.once())
        self.assertEqual(self.http.mock_calls, [])

    def test_adapter_rejects_nonliteral_paths_and_unsupported_license(self):
        official = spec()
        for change in (
            {'text_url': official['text_url'] + '?download=1'},
            {'text_url': official['text_url'].replace('.txt', '.xml')},
            {'text_url': official['text_url'].replace('https:', 'http:')},
            {'evidence_url': official['evidence_url'].replace('.1.json', '.2.json')},
            {'evidence_url': official['evidence_url'].replace('/metadata/', '/metadata/%2e%2e/')},
            {'evidence_url': official['evidence_url'].replace('s3.amazonaws.com', 's3.amazonaws.com.example')},
            {'license_id': 'cc-by-nc'}, {'license_id': 'tdm'}, {'kind': 'verified_jats'},
            {'content_b64': '!' }, {'metadata_b64': ''}, {'fulltext': True},
        ):
            with self.subTest(change=change), self.assertRaises(CorpusError):
                self.sources.fetch(spec(**change))
        self.assertEqual(self.http.mock_calls, [])

    def test_adapter_rejects_forged_malformed_or_metadata_only_proof(self):
        delimiter = '\x9f==============================\x9f'.encode()
        preface = RAW.split(delimiter)[0]
        alternatives = [spec(sha256='0' * 64), spec(content_b64=b64(RAW + b'.'), sha256=hashlib.sha256(RAW+b'.').hexdigest()),
                        spec(metadata_b64=b64(b'{"doi": 1, "doi": 2}')), spec(doi='10.1000/wrong'), spec(pmid='1'),
                        with_body(preface), with_body(preface + delimiter + b'Abstract only.' * 150),
                        with_body(RAW.replace(b'PMCID: PMC4858230', b'PMCID: PMC4858231', 1)),
                        with_body(RAW.replace(META['doi'].encode(), b'10.1000/wrong', 1)),
                        with_body(RAW.replace(b'Article version: 1', b'Article version: 2', 1)),
                        with_body(RAW + b'\x00'), with_body(RAW + b'\xff')]
        for fields in ({'pmcid': 'PMC4858231'}, {'pmid': False}, {'version': True}, {'is_retracted': True},
                       {'is_pmc_openaccess': False}, {'title': 'Another article'}, {'license_code': 'CC BY-ND'},
                       {'text_url': META['text_url'].replace('.txt', '.xml')}):
            alternatives.append(spec(metadata_b64=b64(json.dumps({**META, **fields}).encode())))
        for index, proof in enumerate(alternatives):
            with self.subTest(index=index), self.assertRaises(CorpusError) as rejected:
                self.sources.fetch(proof)
            self.assertEqual(rejected.exception.code, 'invalid_verified_text')
        failed = self.app.submit(self.alice, alternatives[1], 'personal:alice', 'malformed')
        self.assertTrue(self.worker.once())
        self.assertEqual(self.app.request(self.alice, failed['request_ref'])['status'], 'failed')
        with self.db.transaction() as db:
            self.assertEqual(db.execute('SELECT spec FROM requests').fetchone()[0], '{}')
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)

    def test_active_doi_is_never_replaced_and_scope_is_not_global(self):
        first = self.app.submit(self.alice, spec(), 'personal:alice', 'first')
        self.worker.once()
        done = self.app.request(self.alice, first['request_ref'])
        self.assertEqual(done['status'], 'published')
        with self.assertRaises(CorpusError) as caught:
            self.app.submit(self.alice, spec(), 'personal:alice', 'other')
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'already_present'))
        self.assertEqual(self.app.submit(self.bob, spec(), 'personal:bob', 'first')['status'], 'queued')

    def test_pending_exact_doi_deduplicates_but_failed_proof_can_be_retried(self):
        invalid = spec(sha256='0' * 64)
        first = self.app.submit(self.alice, invalid, 'personal:alice', 'bad')
        self.assertEqual(self.app.submit(self.alice, invalid, 'personal:alice', 'bad'), first)
        with self.assertRaises(CorpusError) as caught:
            self.app.submit(self.alice, spec(), 'personal:alice', 'changed')
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'already_pending'))
        self.assertTrue(self.worker.once())
        self.assertEqual(self.app.request(self.alice, first['request_ref'])['status'], 'failed')
        queued = self.app.submit(self.alice, spec(), 'personal:alice', 'changed')
        self.assertEqual(queued['status'], 'queued')
        self.assertTrue(self.worker.once())
        self.assertEqual(self.app.request(self.alice, queued['request_ref'])['status'], 'published')

    def test_publication_rechecks_exact_doi_after_embedding_race(self):
        pending = self.app.submit(self.alice, spec(), 'personal:alice', 'queued-first')
        embedding = Mock(wraps=self.embedding)
        embedding.signature, embedding.dimension = self.embedding.signature, self.embedding.dimension
        embedding.embed.side_effect = self._publish_same_doi_as_upload
        self.worker.embedding = embedding
        self.assertTrue(self.worker.once())
        done = self.app.request(self.alice, pending['request_ref'])
        self.assertEqual(done['status'], 'already_present', done)
        self.assertEqual(self.app.document(self.alice, done['output']['document_ref'])['source_kind'], 'upload')
        with self.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT spec FROM requests WHERE ref=?', (pending['request_ref'],)).fetchone()[0], '{}')

    def _publish_same_doi_as_upload(self, texts):
        other = self.app.submit(self.alice, {'kind': 'upload', 'filename': 'other.txt', 'media_type': 'text/plain',
                                           'doi': META['doi'], 'content_b64': b64(RAW)}, 'personal:alice', 'manual')
        self.assertTrue(Worker(self.db, self.sources, ParserRouter(), self.embedding).once())
        self.assertEqual(self.app.request(self.alice, other['request_ref'])['status'], 'published')
        return self.embedding.embed(texts)
