import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from corpus.auth import Actor
from corpus.embeddings import HashEmbedding
from corpus.parsers import ParserRouter
from corpus.relevance import Relevance, _E5_REVISION
from corpus.service import App
from corpus.sources import Sources
from corpus.storage import Database
from corpus.vector_index import ExactVectorIndex, encode_vector
from corpus.worker import Worker


class CandidateRelevanceTests(unittest.TestCase):
    def test_lexical_evidence_requires_substantive_passage_terms(self):
        query = Relevance('What does somatic mutation change?', _E5_REVISION)
        self.assertFalse(query.lexical('The study does show a genetic mutation in plants.'))
        self.assertTrue(query.lexical('A Somatic MUTATION can change phenotypes.'))
        self.assertFalse(Relevance('芽变', _E5_REVISION).lexical('叶芽随气候发生变化'))
        self.assertTrue(Relevance('芽变', _E5_REVISION).lexical('观察到少量柑橘芽变。'))

    def test_semantic_branch_respects_calibration_and_script(self):
        english = 'The flowering period predicted extinction risk.'
        chinese = Relevance('开花期如何影响灭绝风险？', _E5_REVISION)
        self.assertFalse(chinese.lexical(english))
        self.assertFalse(chinese.semantic(english, .7942))
        self.assertTrue(chinese.semantic(english, .8230))
        self.assertFalse(Relevance('开花期如何影响灭绝风险？', 'new-model').semantic(english, .98))
        self.assertFalse(Relevance('Which mutation causes bud sports?', _E5_REVISION).semantic(english, .95))
        self.assertTrue(Relevance('Does flowering period predict extinction risk?', _E5_REVISION).semantic(english, .854))

    def test_vector_gate_runs_before_top_k(self):
        rows = [{'chunk_ref':str(i), 'vector':encode_vector([1, 0], 2)} for i in range(30)]
        rows.append({'chunk_ref':'qualified', 'vector':encode_vector([.8, .6], 2)})
        hits = ExactVectorIndex().rank(rows, encode_vector([1, 0], 2), 2, 1,
                                      admit=lambda row, cosine: row['chunk_ref']=='qualified' and cosine >= .8)
        self.assertEqual([row['chunk_ref'] for row in hits], ['qualified'])


class EvidenceAppTests(unittest.TestCase):
    DOI = '10.5555/relevance-fixture'

    def setUp(self):
        root = TemporaryDirectory(dir='.')
        self.addCleanup(root.cleanup)
        db = Database(Path(root.name)/'test.sqlite3')
        model = HashEmbedding()
        self.reranker = Mock(side_effect=lambda _, rows: tuple(ref for ref, _ in rows))
        self.app = App(db, model, reranker=self.reranker)
        self.actor = Actor('reader')
        self.scope = {'personal':True}
        data = ('# Floristic bud sport survey\n\n## Results\n\nFlowering period predicted extinction risk. '
                'Other predictors included range size and canopy height.')
        src = {'kind':'upload', 'filename':'sample.md', 'media_type':'text/markdown', 'doi':self.DOI,
               'content_b64':base64.b64encode(data.encode()).decode()}
        queued = self.app.submit(self.actor, src, 'personal:reader', 'seed')
        self.assertTrue(Worker(db, Sources(), ParserRouter(), model).once())
        published = self.app.request(self.actor, queued['request_ref'])
        self.assertEqual(published['status'], 'published')
        self.document = published['output']['document_ref']

    def search(self, query, filters=None, actor=None):
        return self.app.search(actor or self.actor, query, self.scope, filters or {}, 10)

    def test_empty_results_persist_after_filtering_and_reranking(self):
        for query in ('bud sport', 'somatic mutation', '芽变', 'What are the results of the clinical trial?'):
            for filters in ({}, {'doi':self.DOI}, {'document_ref':self.document}):
                with self.subTest(query=query, filters=filters):
                    response = self.search(query, filters)
                    self.assertEqual(response['results'], [])
                    self.assertEqual(response['status'], 'complete')
                    self.assertEqual(len(response['index_snapshot']), 1)
        self.reranker.assert_not_called()

    def test_relevant_passage_and_exact_identifier_lookup(self):
        for query, filters in (
            ('extinction risk', {'doi':self.DOI}), ('flowering period', {'document_ref':self.document}),
            ('https://doi.org/'+self.DOI.upper(), {'document_ref':self.document}),
            (self.document, {'doi':'doi:'+self.DOI.upper()}),
        ):
            with self.subTest(query=query, filters=filters):
                self.assertEqual({hit['document_ref'] for hit in self.search(query, filters)['results']}, {self.document})
        self.assertEqual(self.search('10.5555/absent')['results'], [])
        self.assertEqual(self.search(self.DOI, {'document_ref':'doc_'+'0'*40})['results'], [])
        self.assertEqual(self.search(self.DOI, {'doi':'10.5555/absent'})['results'], [])
        self.assertEqual(self.search(self.document, {'doi':'10.5555/absent'})['results'], [])
        self.assertEqual(self.search('extinction risk', {'doi':'10.5555/absent'})['results'], [])
        self.assertEqual(self.search(self.document, actor=Actor('outsider'))['results'], [])
