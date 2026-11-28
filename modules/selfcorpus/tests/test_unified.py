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

    def test_repeated_evolution_content_keeps_generation_and_new_version_is_separate(self):
        doi = '10.1234/duplicate-fixture'
        def ingest(word, key, actor=self.alice):
            submission = self.app.submit(actor, upload(word, doi), f'personal:{actor.subject}', key)
            self.assertTrue(self.worker.once())
            return self.app.request(actor, submission['request_ref'])
        first = ingest('initial measured samples', 'original')
        self.assertEqual(first['status'], 'published')
        duplicate = ingest('initial measured samples', 'repeat-under-different-key')
        self.assertEqual(duplicate['status'], 'already_present')
        self.assertEqual(duplicate['output']['generation_ref'], first['output']['generation_ref'])
        private_copy = ingest('initial measured samples', 'bob-independent-copy', self.bob)
        distinct = ingest('revised measured samples', 'changed-content')
        self.assertEqual(private_copy['status'], 'published')
        self.assertEqual(distinct['status'], 'published')
        self.assertNotEqual(private_copy['output']['document_ref'], first['output']['document_ref'])
        self.assertEqual(distinct['output']['document_ref'], first['output']['document_ref'])
        self.assertNotEqual(distinct['output']['document_version_ref'], first['output']['document_version_ref'])
        with self.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM generations').fetchone()[0], 3)

    def test_identical_public_passage_collapses_but_original_refs_and_distinct_text_survive(self):
        doi = META['doi']
        self.app.submit_verified_shared(self.alice, spec(), 'shared-copy')
        self.assertTrue(self.worker.once())
        scope = {'shared': True}
        shared = self.wire(self.alice, SEARCH, payload(doi, scope=scope, max_chars=2400))['results'][0]
        text = self.wire(self.alice, READ, {'evidence_ref': shared['evidence_ref']})['text']
        distinct = 'This passage has materially different experiments and results.'
        source = 'cqa_literature'
        def rows(source, query, topics, filters, caps, check):
            return [dict(source_id=source, generation_ref=self.base.generation, document_ref='legacy-doc',
                         version_ref='legacy-version', passage_ref=f'legacy-{i}', title='legacy fixture',
                         text=value, passage_hash=digest(value), doi=doi.upper(), locator={'id':str(i)})
                    for i,value in enumerate((text,distinct))], [], 2
        self.base.search = rows
        result = self.wire(self.alice, SEARCH, payload(doi, sources=(source,'evolution'), scope=scope, max_candidates=40))
        self.assertEqual(sum(row['passage_hash']==digest(text) for row in result['results']), 1)
        self.assertTrue(any(row['excerpt']==distinct for row in result['results']))
        self.assertEqual(self.wire(self.alice, READ, {'evidence_ref': shared['evidence_ref']})['text'], text)
        with self.assertRaises(CorpusError):
            self.wire(self.eve, READ, {'evidence_ref': shared['evidence_ref']})

    def test_source_timeout_does_not_hide_exact_evidence_from_another_source(self):
        from corpus.unified import Deadline

        original = self.base.search
        def retrieve(source, query, topics, filters, caps, check):
            if source == 'cqa_literature':
                raise Deadline
            return original(source, query, topics, filters, caps, check)
        self.base.search = retrieve
        reply = self.wire(self.alice, SEARCH, payload('evidence', sources=('cqa_literature', 'cqa_methods'),
                                                     max_candidates=24, max_results=4))
        self.assertEqual(reply['status'], 'partial')
        outcomes = {item['source_id']: item for item in reply['source_outcomes']}
        self.assertEqual(outcomes['cqa_literature']['reason_codes'], ['timeout'])
        self.assertEqual(outcomes['cqa_literature']['returned_count'], 0)
        self.assertEqual(outcomes['cqa_methods']['status'], 'hit')
        self.assertLessEqual(reply['budget_used']['candidates'], 24)
        [row] = reply['results']
        self.assertEqual(row['source_id'], 'cqa_methods')
        self.assertEqual(self.wire(self.alice, READ, {'evidence_ref': row['evidence_ref']})['text'], row['excerpt'])

    def test_bilingual_calls_english_first_and_deduplicates_within_one_budget(self):
        legs = []
        search = self.base.search
        def observe(source, query, topics, filters, caps, check):
            legs.append((query, sum(caps.values())))
            return search(source, query, topics, filters, caps, check)
        self.base.search = observe
        request = payload('柑橘', sources=('cqa_literature', 'cqa_methods'), max_candidates=24, max_results=1)
        request['english_query'] = 'citrus'
        response = self.wire(self.alice, SEARCH, request)
        self.assertEqual(legs, [('citrus', 10), ('citrus', 10), ('柑橘', 2), ('柑橘', 2)])
        self.assertEqual(sum(cap for _, cap in legs), 24)
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(sum(r['status'] == 'hit' for r in response['source_outcomes']), 1)
        self.assertEqual(response['budget_used']['candidates'], 4)

    def test_new_evolution_translation_matches_english_but_cites_only_original(self):
        import base64
        from unittest.mock import patch

        scopes = {"personal": True, "shared": False}
        old = self.app.submit(self.alice, upload('historic apples'), 'personal:alice', 'before-alignment')
        self.assertTrue(self.worker.once())
        old_gen = self.app.request(self.alice, old['request_ref'])['output']['generation_ref']
        text = '这是一段记录。秋季样本比较了不同测量方法。'
        translation = 'Autumn samples compared different calibrated measurement methods.'
        job = self.app.submit(self.alice, {"kind": "upload", "filename": "capture.txt", "media_type": "text/plain",
            "content_b64": base64.b64encode(text.encode()).decode()}, 'personal:alice', 'new-aligned')
        calls = []
        def translate(passages, *, source_language):
            calls.append((passages, source_language))
            return [translation]
        self.worker.translator = translate
        self.assertTrue(self.worker.once())
        self.assertEqual(calls, [((text,), None)])
        receipt = self.app.request(self.alice, job['request_ref'])
        self.assertEqual(receipt['status'], 'published')
        self.assertNotEqual(receipt['output']['generation_ref'], old_gen)
        request = payload(text, scope=scopes, max_candidates=40)
        request['english_query'] = 'Autumn calibrated measurement methods'
        with patch.object(self.embedding, 'embed', wraps=self.embedding.embed) as embed:
            response = self.wire(self.alice, SEARCH, request)
            queried = [item.args[0][0] for item in embed.call_args_list if item.kwargs.get('query')]
            self.assertEqual(queried, [request['english_query'], text])
        english_only = self.wire(self.alice, SEARCH, payload(request['english_query'], scope=scopes, max_candidates=40))
        [result] = english_only['results']
        self.assertEqual([row['passage_ref'] for row in response['results']], [result['passage_ref']])
        self.assertEqual(result['excerpt'], text)
        self.assertEqual(result['passage_hash'], digest(text))
        self.assertEqual(self.wire(self.alice, READ, {'evidence_ref': result['evidence_ref']})['text'], text)
        with self.assertRaises(CorpusError) as denied:
            self.wire(self.bob, READ, {'evidence_ref': result['evidence_ref']})
        self.assertEqual(denied.exception.status, 404)
        with self.db.transaction() as db:
            [aligned] = db.execute('SELECT c.text,t.text_en,t.translation_sha256 FROM chunk_translations t JOIN chunks c ON c.id=t.chunk_id')
            self.assertEqual(tuple(aligned), (text, translation, digest(translation)))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_standalone_image_never_creates_corpus_records(self):
        import base64

        request = self.app.submit(self.alice, {"kind":"upload", "filename":"looks-like-text.txt",
            "media_type":"text/plain", "content_b64":base64.b64encode(b"GIF89a image fixture").decode()},
            'personal:alice', 'image-excluded')
        self.worker.translator = lambda *a, **kw: self.fail('image reached translation')
        self.assertTrue(self.worker.once())
        receipt = self.app.request(self.alice, request['request_ref'])
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['error']['code'], 'image_not_indexed')
        with self.db.transaction() as db:
            for table in ('documents', 'versions', 'parsed', 'chunks', 'chunk_translations', 'active'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_misaligned_translation_does_not_switch_generation(self):
        import base64
        from corpus.generations import current

        previous = self.app.submit(self.alice, upload('prior sample'), 'personal:alice', 'prior-active')
        self.assertTrue(self.worker.once())
        prior_gen = self.app.request(self.alice, previous['request_ref'])['output']['generation_ref']
        original = '保留原始标识编号 PMC12345。'
        request = self.app.submit(self.alice, {'kind': 'upload', 'filename': 'mismatch.txt', 'media_type': 'text/plain',
            'content_b64': base64.b64encode(original.encode()).decode()}, 'personal:alice', 'wrong-alignment')
        self.worker.translator = lambda values, **kwargs: ['English text with a dropped identifier.']
        self.assertTrue(self.worker.once())
        receipt = self.app.request(self.alice, request['request_ref'])
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['error']['code'], 'translation_alignment_invalid')
        with self.db.transaction() as db:
            self.assertEqual(current(db, 'personal:alice')['ref'], prior_gen)
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_translations').fetchone()[0], 0)

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
