import base64
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from corpus.api import CorpusHTTPServer
from corpus.auth import Actor
from corpus.embeddings import HashEmbedding
from corpus.errors import CorpusError
from corpus.host_client import HostClient, HostClientError
from corpus.maintenance import reembed_scope
from corpus.parsers import ParserRouter
from corpus.queue import Queue
from corpus.service import App
from corpus.sources import Source, Sources
from corpus.storage import Database, now
from corpus.worker import Worker


def upload(word, doi=None):
    text = f'''# Root phenotyping study

## Results

The field trial measured {word} with calibrated leaf imaging and repeated observations.
The observations persisted across the greenhouse and orchard samples after analysis.
Results included controlled humidity, three technical replicates and a baseline comparison.
'''
    src = {'kind':'upload', 'filename':'sample.md', 'media_type':'text/markdown',
           'content_b64':base64.b64encode(text.encode()).decode()}
    if doi:
        src['doi'] = doi
    return src


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name)/'corpus.sqlite3')
        self.embed = HashEmbedding()
        self.worker = Worker(self.db, Sources(), ParserRouter(), self.embed)
        self.app = App(self.db, self.embed)
        self.alice = Actor('alice', {'orchard':'write'})
        self.bob = Actor('bob', {'orchard':'read'})
        self.eve = Actor('eve')
        self.admin = Actor('admin', governor=True)
        self.personal = {'personal':True}
        self.project = {'project_refs':['orchard']}
        self.shared = {'shared':True}

    def add(self, scope, word, *, key='first', actor=None, doi=None):
        actor = actor or self.alice
        queued = self.app.submit(actor, upload(word, doi), scope, key)
        self.assertTrue(self.worker.once())
        return self.app.request(actor, queued['request_ref'])

    def test_real_http_next_question_and_isolation(self):
        secret = b'local integration only: separate secret material'
        with CorpusHTTPServer(self.app, secret, '127.0.0.1', 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = HostClient(secret, f'http://127.0.0.1:{server.server_address[1]}')
            try:
                self.assertEqual(client.health()['service'], 'selfcorpus')
                original = client.submit(self.alice, upload('magnetophotometer'), 'personal:alice', 'A')
                self.assertEqual(original['status'],'queued')
                self.assertEqual(client.search(self.alice,'magnetophotometer',self.personal)['results'], [])
                self.worker.once()
                finished = client.request(self.alice, original['request_ref'])
                self.assertEqual(finished['status'],'published',finished)
                result = client.search(self.alice, 'magnetophotometer', self.personal)
                self.assertIn('magnetophotometer',result['results'][0]['content'])
                self.assertTrue(result['coverage'][0]['vector'])
                item = result['results'][0]
                self.assertEqual(item['generation_ref'], finished['output']['generation_ref'])
                self.assertEqual(result['index_snapshot'][0]['generation_ref'], item['generation_ref'])
                self.assertEqual(client.document(self.alice,item['document_ref'], chunks=True)['chunks'][0]['section'],'Results')
                self.assertTrue(client.generation(self.alice,item['generation_ref'])['active'])
                self.assertEqual(client.search(self.bob,'magnetophotometer',self.personal)['results'], [])
                with self.assertRaises(HostClientError) as caught:
                    client.document(self.bob,item['document_ref'])
                self.assertEqual(caught.exception.status,404)
                self.assertEqual(client.submit(self.alice,upload('magnetophotometer'),'personal:alice','A')['request_ref'],original['request_ref'])
                self.assertFalse(self.worker.once())
            finally:
                server.shutdown()
                thread.join()

    def test_project_and_shared_publication(self):
        p = self.add('project:orchard','zephyrometer')
        self.assertEqual(p['status'],'published',p)
        self.assertIn('zephyrometer', self.app.search(self.bob,'zephyrometer',self.project,{},5)['results'][0]['content'])
        for actor, action in [(self.eve, lambda a: self.app.search(a,'zephyrometer',self.project,{},5)),
                              (self.bob, lambda a: self.app.submit(a,upload('bad'),'project:orchard','forbidden'))]:
            with self.assertRaisesRegex(CorpusError,'not authorized'):
                action(actor)
        s = self.add('shared','chlorophyllfluorometer')
        self.assertEqual(s['status'],'pending_review',s)
        proposal = self.app.proposal(self.admin,s['request_ref'])
        self.assertEqual(proposal['source']['source_hash'], s['output']['source_hash'])
        self.assertEqual(self.app.search(self.bob,'chlorophyllfluorometer',self.shared,{},5)['results'],[])
        with self.assertRaises(CorpusError):
            self.app.document(self.bob,s['output']['document_ref'])
        with self.assertRaises(CorpusError):
            self.app.proposal(self.alice,s['request_ref'],True)
        self.assertEqual(self.app.proposal(self.admin,s['request_ref'],True)['status'],'published')
        self.assertIn('chlorophyllfluorometer',self.app.search(self.bob,'chlorophyllfluorometer',self.shared,{},5)['results'][0]['content'])
        rejected = self.add('shared','rejected-never-published',key='reject')
        self.assertEqual(self.app.proposal(self.admin,rejected['request_ref'],False)['status'],'rejected')
        with self.assertRaises(CorpusError):
            self.app.document(self.bob,rejected['output']['document_ref'])

    def test_version_rollback_withdrawal_and_reembed(self):
        doi = '10.5555/selfcorpus-fixture'
        first = self.add('personal:alice','versionmarkerone',doi=doi)['output']
        second = self.add('personal:alice','versionmarkertwo',doi=doi,key='second')['output']
        self.assertEqual(first['document_ref'],second['document_ref'])
        self.assertNotEqual(first['document_version_ref'],second['document_version_ref'])
        self.assertEqual(self.app.search(self.alice,'versionmarkertwo',self.personal,{'doi':doi},5)['results'][0]['document_version_ref'],second['document_version_ref'])
        self.app.rollback(self.alice,'personal:alice',first['generation_ref'])
        first_new = HashEmbedding(dimension=512)
        gen = reembed_scope(self.db,first_new,'personal:alice')
        self.assertNotEqual(gen,first['generation_ref'])
        self.assertEqual(self.app.search(self.alice,'versionmarkerone',self.personal,{},5)['status'],'partial')
        self.assertTrue(App(self.db,first_new).search(self.alice,'versionmarkerone',self.personal,{},5)['coverage'][0]['vector'])
        self.app.withdraw(self.alice,'personal:alice',first['document_ref'])
        self.assertEqual(self.app.search(self.alice,'versionmarkerone',self.personal,{},5)['results'],[])
        with self.assertRaisesRegex(CorpusError,'withdrawn'):
            self.app.rollback(self.alice,'personal:alice',first['generation_ref'])

    def test_failed_update_never_changes_active_and_lease_fencing(self):
        first = self.add('personal:alice','firstsuccess')['output']
        class Broken:
            signature = self.embed.signature
            dimension = self.embed.dimension
            def embed(self,*a,**kw): return [[float('nan')]*self.dimension]
        pending = self.app.submit(self.alice, upload('brokenembedding'),'personal:alice','bad')
        with self.assertLogs('corpus.worker', level='ERROR'):
            Worker(self.db,Sources(),ParserRouter(),Broken()).once()
        self.assertEqual(self.app.request(self.alice,pending['request_ref'])['status'],'failed')
        self.assertTrue(self.app.generation(self.alice,first['generation_ref'])['active'])
        with self.assertRaises(CorpusError):
            self.app.submit(self.alice,upload('different'),'personal:alice','bad')
        self.app.submit(self.alice,upload('leasecheck'),'personal:alice','lease')
        q = Queue(self.db)
        expired = q.claim()
        with self.db.transaction(write=True) as db:
            db.execute('UPDATE requests SET lease_until=? WHERE ref=?',(time.time()-1,expired['ref']))
        fresh = q.claim()
        self.assertEqual(fresh['ref'],expired['ref'])
        self.assertEqual(fresh['attempt'],2)
        with self.assertRaisesRegex(CorpusError,'no longer owns'):
            q.phase(expired,'fetched')

    def test_metadata_only_cannot_activate(self):
        class MetadataOnly:
            def fetch(self, spec):
                return Source(content=b'{"title":"Bibliography only"}', media_type='application/json',
                    uri='https://doi.org/10.5555/no-fulltext', metadata={'fulltext':False,'title':'Bibliography only'},
                    access_note='metadata', retrieved_at=now(), http_status=200, kind='doi')
        queued = self.app.submit(self.alice,{'kind':'doi','doi':'10.5555/no-fulltext'},'personal:alice','doi')
        Worker(self.db,MetadataOnly(),ParserRouter(),self.embed).once()
        self.assertEqual(self.app.request(self.alice,queued['request_ref'])['status'],'partial')
        self.assertEqual(self.app.search(self.alice,'Bibliography',self.personal,{},5)['index_snapshot'],[])
        with self.db.connection() as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')

if __name__ == '__main__':
    unittest.main()
