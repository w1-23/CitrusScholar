import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from corpus.api import _dispatch
from corpus.auth import Actor, AuthenticationError, BASE_SOURCES, signed_headers, verify_signed_actor
from corpus.embeddings import HashEmbedding
from corpus.errors import CorpusError
from corpus.parsers import ParserRouter
from corpus.queue import enqueue
from corpus.service import App
from corpus.sources import Sources
from corpus.storage import Database, digest
from corpus.worker import Worker
from test_service import upload
from test_verified_text import META, spec

SEARCH = '/api/v1/retrieval/evidence-set'
READ = '/api/v1/retrieval/evidence/read'
SECRET = b'offline transport interaction fixture'


def payload(query, *, sources=('evolution',), scope=None, **budget):
    out = {'query': query, 'sources': [{'source_id': s} for s in sources], 'budget': budget}
    if scope is not None:
        out['evolution_scope'] = scope
    return out


class StubBase:
    generation = 'g20261028T165637Z_851926a9'
    fingerprint = 'approved-fixture'
    changed = False
    calls = ()

    def pin(self):
        return self

    def check_current(self):
        if self.changed:
            raise ValueError('changed')

    def search(self, source, query, topics, filters, caps, check):
        self.calls += ((source, caps),)
        value = '芒果😀芽变 evidence for ' + source
        values = {'source_id': source, 'generation_ref': self.generation, 'document_ref': 'doc_' + source,
                  'version_ref': 'ver_' + source, 'passage_ref': 'passage_' + source,
                  'title': source, 'text': value, 'passage_hash': digest(value), 'locator': {'id': source}}
        return [values], [], 1

    def read(self, source, generation, locator):
        if generation == self.generation and locator['id'] == source:
            text = '芒果😀芽变 evidence for ' + source
            return {'text': text, 'span': {'start': 11, 'end': 11 + len(text)}}


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name)/'corpus.db')
        self.embedding = HashEmbedding()
        self.base = StubBase()
        self.app = App(self.db, self.embedding, legacy=self.base)
        self.worker = Worker(self.db, Sources(), ParserRouter(grobid_url='', docling=False), self.embedding)
        self.alice = Actor('alice', {'orchard': 'write'}, base_sources=BASE_SOURCES)
        self.bob = Actor('bob', {'orchard': 'read'})
        self.eve = Actor('eve', shared=False)

    def wire(self, actor, target, request):
        body = json.dumps(request).encode()
        headers = signed_headers(SECRET, 'POST', target, body, actor)
        verified = verify_signed_actor(SECRET, 'POST', target, body, headers)
        return _dispatch(self.app, verified, 'POST', tuple(target.split('/')[3:]), body)

    def test_same_subject_fresh_acl_exact_unicode_prefix_and_lane_budget(self):
        request = payload('芽变', sources=BASE_SOURCES, max_candidates=9, max_results=3, max_chars=11)
        self.assertEqual(self.wire(self.alice, SEARCH, request)['budget_used'], {'candidates': 5, 'results': 1, 'chars': 11})
        self.assertEqual(sum(sum(c.values()) for _, c in self.base.calls), 9)
        response = self.wire(self.alice, SEARCH, request)
        self.assertEqual(response['status'], 'hit')
        row = response['results'][0]
        ref = row['evidence_ref']
        self.assertEqual(self.wire(self.alice, READ, {'evidence_ref': ref, 'max_chars': 11})['text'], row['excerpt'])
        read = self.wire(self.alice, READ, {'evidence_ref': ref, 'offset': 2, 'max_chars': 1})
        self.assertEqual((read['text'], read['next_offset'], read['passage_hash']), ('😀', 3, row['passage_hash']))
        denied = (self.bob, Actor('alice'))
        for actor in denied:
            with self.assertRaises(CorpusError) as caught:
                self.wire(actor, READ, {'evidence_ref': ref})
            self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(CorpusError) as caught:
            self.wire(self.bob, SEARCH, request)
        self.assertEqual(caught.exception.status, 403)
        self.base.changed = True
        changed = self.wire(self.alice, SEARCH, request)
        self.assertEqual((changed['status'], changed['results']), ('unavailable', []))
        self.assertTrue(all(not s['generations'] for s in changed['index_snapshot']['sources']))

    @unittest.skipUnless(META, "External verified article fixture not supplied")
    def test_new_shared_is_verified_auto_publish_and_cross_account_read(self):
        scope = dict(shared=True)
        request = payload(META['doi'], scope=scope)
        queued = self.app.submit_verified_shared(self.alice, spec(), 'paper')
        self.assertEqual(self.wire(self.bob, SEARCH, request)['status'], 'empty')
        self.assertTrue(self.worker.once())
        published = self.app.request(self.alice, queued['request_ref'])
        self.assertEqual(published['status'], 'published')
        result = self.wire(self.bob, SEARCH, request)
        self.assertEqual(result['status'], 'hit')
        row = result['results'][0]
        self.assertEqual(row['scope_ref'], 'shared')
        self.assertEqual(row['generation_ref'], published['output']['generation_ref'])
        self.assertEqual(self.wire(self.bob, READ, {'evidence_ref': row['evidence_ref']})['text'], row['excerpt'])
        self.assertEqual(self.wire(self.bob, SEARCH, payload('totally unrelatedword', scope=scope))['status'], 'empty')
        for actor in (self.alice, Actor('admin', governor=True)):
            with self.assertRaises(CorpusError) as caught:
                self.app.submit(actor, upload('private'), 'shared', 'unverified')
            self.assertEqual(caught.exception.status, 403)
        with self.db.transaction(write=True) as db:
            legacy = enqueue(db, 'alice', 'shared', 'old-queued', upload('private'))
        self.assertTrue(self.worker.once())
        self.assertEqual(self.app.request(self.alice, legacy['request_ref'])['status'], 'unavailable')
        with self.assertRaises(CorpusError) as caught:
            self.wire(self.eve, SEARCH, request)
        self.assertEqual(caught.exception.status, 403)

    def test_project_revocation_and_immutable_generation_withdrawal(self):
        doi = '10.5555/project-evidence'
        def publish(marker):
            receipt = self.app.submit(self.alice, upload(marker, doi), 'project:orchard', marker)
            self.assertTrue(self.worker.once())
            return self.app.request(self.alice, receipt['request_ref'])['output']
        old = publish('firstcolumn')
        selection = dict(projects=['orchard'])
        request = payload('firstcolumn', scope=selection)
        row = self.wire(self.bob, SEARCH, request)['results'][0]
        publish('secondcolumn')
        self.assertEqual(self.wire(self.bob, SEARCH, request)['status'], 'empty')
        self.assertEqual(self.wire(self.bob, READ, {'evidence_ref': row['evidence_ref']})['generation_ref'], old['generation_ref'])
        revoked = Actor('bob')
        with self.assertRaises(CorpusError) as denied:
            self.wire(revoked, SEARCH, request)
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(CorpusError) as caught:
            self.wire(revoked, READ, {'evidence_ref': row['evidence_ref']})
        self.assertEqual(caught.exception.status, 404)
        self.app.withdraw(self.alice, 'project:orchard', old['document_ref'])
        with self.assertRaises(CorpusError) as caught:
            self.wire(self.bob, READ, {'evidence_ref': row['evidence_ref']})
        self.assertEqual(caught.exception.status, 404)

    def test_hmac_binds_route_and_body_and_old_claims_deny_base(self):
        signed = signed_headers(SECRET, 'POST', SEARCH, b'{}', self.alice)
        for route, data in ((READ, b'{}'), (SEARCH, b'{ }')):
            with self.assertRaises(AuthenticationError):
                verify_signed_actor(SECRET, 'POST', route, data, signed)
        old = signed_headers(SECRET, 'POST', SEARCH, b'{}', Actor('old'))
        self.assertEqual(verify_signed_actor(SECRET, 'POST', SEARCH, b'{}', old).base_sources, ())
