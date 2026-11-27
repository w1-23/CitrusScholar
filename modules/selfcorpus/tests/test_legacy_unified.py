from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from corpus.legacy import (ApprovedInventory, DenseAttestation, DenseTable, LegacyChanged,
                           LegacyCorpus, LegacyDense, LegacyE5, LegacyUnavailable)
from corpus.legacy.inventory import SOURCES, identity


GEN_1 = 'g20261127T000000Z_00000001'
GEN_2 = 'g20261127T000000Z_00000002'
LIT = 'cqa_literature'
CHUNK_0 = '开头 🍊 标本记录。'
CHUNK_1 = '记录表明，这是可核验的芽变。'


def chunk(source, doc, position, size, text, *, title, section, topic=''):
    return {
        'schema_version': 2, 'l1_category': topic, 'corpus_id': source,
        'corpus_family_id': SOURCES[source], '_source_batch': source,
        'paper_id': doc, 'document_id': doc, 'chunk_index': position,
        'text': text, 'char_len': len(text), 'chunk_id': identity(source, doc, position),
        'title': title, 'section_name': section, 'source_file': 'metadata-only/original.md',
        'prev_chunk_id': f'{doc}::{position - 1:04d}' if position else '',
        'next_chunk_id': identity(source, doc, position + 1) if position + 1 < size else '',
        'doi': 'https://doi.org/10.1234/FIXTURE', 'year': '2024',
    }


def rows_for(text):
    return [chunk(LIT, 'record_one', 0, 2, CHUNK_0, title='柑橘 🧪', section='柑橘 🧪'),
            chunk(LIT, 'record_one', 1, 2, text, title='柑橘 🧪', section='结果', topic='育种与生物技术'),
            chunk(LIT, 'record_two', 0, 1, 'A citrus bud sport is a mutation.',
                  title='柑橘 🧪', section='Abstract'),
            chunk(LIT, 'title_only', 0, 1, 'Study on a different fruit.',
                  title='芽变', section='Abstract')]


def fixture(root, generation, rows, *, access='public', active=True):
    prefix = f'corpus_generations/{generation}'
    registry = {'schema_version': 1, 'registry_version': 'fixture-1', 'corpora': {},
                'topic_aliases': {'breeding/biotechnology': '育种与生物技术'}}
    hashes = {}
    for source, family in SOURCES.items():
        relative = f'{prefix}/corpora/{source}/chunks.jsonl'
        path = root / relative
        path.parent.mkdir(parents=True)
        items = rows if source == LIT else []
        data = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in items).encode()
        path.write_bytes(data)
        hashes[source] = sha256(data).hexdigest()
        registry['corpora'][source] = {'family_id': family, 'source': source,
                                       'source_batch': source, 'batch': source,
                                       'table_name': source, 'chunks_path': relative,
                                       'access_level': access, 'chunk_count': len(items)}
    registry_path = f'{prefix}/corpus_registry.json'
    manifest = {'generation': generation, 'generation_root': prefix,
                'registry_path': registry_path, 'registry_version': 'fixture-1',
                'index_schema_version': 2, 'table_count': 5, 'chunk_count': len(rows),
                'lancedb_path': f'lancedb_generations/{generation}'}
    raw_registry, raw_manifest = (json.dumps(v).encode() for v in (registry, manifest))
    (root / registry_path).write_bytes(raw_registry)
    (root / prefix / 'index_manifest.json').write_bytes(raw_manifest)
    if active:
        (root / 'active_index.json').write_bytes(raw_manifest)
    return ApprovedInventory(sha256(raw_manifest).hexdigest(), sha256(raw_registry).hexdigest(), hashes)


class LegacyAdapterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rows = rows_for(CHUNK_1)

    def corpus(self, **kwargs):
        return LegacyCorpus(self.root, approved_inventory={GEN_1: fixture(self.root, GEN_1, self.rows, **kwargs)})

    def query(self, pin, query='芽变', topics=(), filters=None, lexical=1, dense=5):
        return pin.search(LIT, query, topics, filters or {}, {'lexical': lexical, 'dense': dense}, lambda: None)

    def test_bounded_content_and_native_topic_filter_with_exact_unicode_offsets(self):
        corpus = self.corpus()
        pin = corpus.pin()
        rows, reasons, count = self.query(pin, topics=['Breeding/Biotechnology'], filters={
            'doi': 'DOI:10.1234/FIXTURE', 'year': 2024})
        self.assertEqual((count, reasons), (1, ['legacy_dense_unverified']))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], CHUNK_1)
        self.assertEqual(rows[0]['passage_hash'], sha256(CHUNK_1.encode()).hexdigest())
        exact = corpus.read(LIT, pin.generation, rows[0]['locator'])
        # The native reconstruction emits one H1, suppresses duplicate H2,
        # inserts interchunk paragraphs, and counts Unicode code points.
        old_document = f'# 柑橘 🧪\n\n{CHUNK_0}\n\n## 结果\n\n{CHUNK_1}'
        start, end = exact['span']['start'], exact['span']['end']
        self.assertEqual(old_document[start:end], CHUNK_1)
        self.assertEqual(exact['text'], CHUNK_1)
        self.assertEqual(self.query(pin, lexical=20)[2], 1)  # title alone is not evidence
        self.assertEqual(self.query(pin, filters={'year': 2025})[2], 0)
        self.assertEqual(self.query(pin, filters={'document_ref': 'different'})[2], 0)
        self.assertEqual(self.query(pin, filters={'document_ref': rows[0]['document_ref']})[2], 1)
        other = self.query(pin, 'bud sport')[0][0]
        self.assertEqual(other['title'], rows[0]['title'])
        self.assertNotEqual(other['document_ref'], rows[0]['document_ref'])

    def test_switch_and_retained_exact_with_stable_document_identity(self):
        a = fixture(self.root, GEN_1, self.rows)
        corpus = LegacyCorpus(self.root, approved_inventory={GEN_1: a, GEN_2: fixture(
            self.root, GEN_2, rows_for('这是第二代的芽变。'), active=False)})
        original = corpus.pin()
        old_hit = self.query(original)[0][0]
        (self.root / 'active_index.json').write_bytes((self.root / f'corpus_generations/{GEN_2}/index_manifest.json').read_bytes())
        with self.assertRaises(LegacyChanged):
            original.check_current()
        new_hit = self.query(corpus.pin())[0][0]
        self.assertEqual(old_hit['document_ref'], new_hit['document_ref'])
        self.assertNotEqual(old_hit['version_ref'], new_hit['version_ref'])
        self.assertEqual(corpus.read(LIT, GEN_1, old_hit['locator'])['text'], CHUNK_1)
        self.assertIsNone(corpus.read(LIT, GEN_2, old_hit['locator']))
        self.assertIsNone(corpus.read(LIT, GEN_1, {**old_hit['locator'], 'end': 0}))
        self.assertIsNone(corpus.read('cqa_translated', GEN_1, old_hit['locator']))

    def test_chunk_mutation_is_detected_by_pin_and_rehash(self):
        corpus = self.corpus()
        pin = corpus.pin()
        hit = self.query(pin)[0][0]
        chunks = self.root / f'corpus_generations/{GEN_1}/corpora/{LIT}/chunks.jsonl'
        chunks.write_bytes(chunks.read_bytes().replace('芽变'.encode(), '春芽'.encode()))
        with self.assertRaises(LegacyChanged):
            pin.check_current()
        with self.assertRaises(LegacyUnavailable):
            self.query(corpus.pin())
        self.assertIsNone(corpus.read(LIT, GEN_1, hit['locator']))

    def test_unapproved_generation_symlink_and_private_access_fail_closed(self):
        corpus = self.corpus(access='private')
        with self.assertRaisesRegex(LegacyUnavailable, 'access'):
            corpus.pin()
        with self.assertRaisesRegex(LegacyUnavailable, 'unapproved'):
            LegacyCorpus(self.root).pin()
        pointer = self.root / 'active_index.json'
        replacement = self.root / 'replacement'
        pointer.rename(replacement)
        pointer.symlink_to(replacement)
        with self.assertRaises(LegacyUnavailable):
            corpus.pin()

    def test_mismatched_chain_remains_excerpt_only(self):
        self.rows[1]['prev_chunk_id'] = 'wrong'
        corpus = self.corpus()
        rows, reasons, count = self.query(corpus.pin())
        self.assertEqual(count, 1)
        self.assertNotIn('locator', rows[0])
        self.assertIn('legacy_exact_unverified', reasons)

    def test_noncanonical_row_fails_without_reaching_candidates(self):
        self.rows[2]['document_id'] = 'incorrect'
        with self.assertRaisesRegex(LegacyUnavailable, 'chunk_invalid'):
            self.query(self.corpus().pin())

    def test_checkpoint_exception_is_not_degraded_or_swallowed(self):
        pin = self.corpus().pin()
        def stop():
            raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            pin.search(LIT, '芽变', [], {}, {'lexical': 1, 'dense': 1}, stop)


    def test_unverified_embedding_never_used_and_invalid_caps_rejected(self):
        corpus = self.corpus()
        self.assertEqual(self.query(corpus.pin(), lexical=0), ([], ['legacy_dense_unverified'], 0))
        with self.assertRaisesRegex(LegacyUnavailable, 'budget'):
            self.query(corpus.pin(), dense=-1)

    def test_attestation_requires_pinned_embedding_and_its_verified_provider(self):
        proof = DenseAttestation('0' * 64, {LIT: DenseTable(1, 'l2', '1' * 64, 0.8)})
        e5 = object.__new__(LegacyE5)
        e5._legacy_device = 'cuda'
        with self.assertRaisesRegex(LegacyUnavailable, 'conformance'):
            LegacyDense(e5, proof)
        with self.assertRaisesRegex(LegacyUnavailable, 'conformance'):
            LegacyDense(object(), proof)

    def test_dense_attempts_count_before_fusion_and_degrade_on_mount_failure(self):
        allow = fixture(self.root, GEN_1, self.rows)
        proof = DenseAttestation(allow.fingerprint, {LIT: DenseTable(1, 'l2', '1' * 64, 0.8)})
        e5 = object.__new__(LegacyE5)
        e5._legacy_device = 'cpu'
        corpus = LegacyCorpus(self.root, embedding=e5, approved_inventory={GEN_1: allow}, dense_attestation=proof)
        pin = corpus.pin()
        with patch.object(LegacyDense, 'search', return_value=([self.rows[1]], 3)):
            rows, reasons, count = self.query(pin, lexical=1, dense=3)
        self.assertEqual((len(rows), reasons, count), (1, [], 4))
        rows, reasons, count = self.query(pin, lexical=1, dense=3)
        self.assertEqual((len(rows), reasons, count), (1, ['legacy_dense_unavailable'], 4))


if __name__ == '__main__':
    unittest.main()
