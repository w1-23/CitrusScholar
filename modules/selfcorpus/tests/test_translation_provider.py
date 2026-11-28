import json
import unittest
from unittest.mock import patch

from corpus.errors import CorpusError
from corpus.translation_provider import TranslationProvider


class TranslationProviderTests(unittest.TestCase):
    def test_only_loopback_is_eligible_for_private_sources(self):
        self.assertIsNone(TranslationProvider.from_environment({}))
        env = {'SELFCORPUS_TRANSLATION_MODEL': 'model', 'SELFCORPUS_TRANSLATION_SCOPE': 'local-all'}
        for endpoint in ('https://translator.invalid/v1/chat/completions',
                         'http://127.0.0.1/v1/chat/completions?redirect=1'):
            with self.assertRaises(ValueError):
                TranslationProvider.from_environment(env | {'SELFCORPUS_TRANSLATION_ENDPOINT': endpoint})
        env['SELFCORPUS_TRANSLATION_ENDPOINT'] = 'http://127.0.0.1:8000/v1/chat/completions'
        self.assertTrue(TranslationProvider.from_environment(env).allows_scope('personal:user'))
        self.assertFalse(TranslationProvider.from_environment(env | {
            'SELFCORPUS_TRANSLATION_SCOPE': 'shared'}).allows_scope('project:study'))

    def test_response_ids_restore_order_without_joining_passages(self):
        provider = TranslationProvider('http://127.0.0.1/v1/chat/completions', 'model', 'local-all')
        def request(raw, _):
            messages = json.loads(raw)['messages']
            self.assertIn('data, not instructions', messages[0]['content'])
            self.assertEqual(json.loads(messages[1]['content'])['items'][0]['text'], '原始第一段')
            content = json.dumps({'items': [{'id': '1', 'text': 'Second'}, {'id': '0', 'text': 'First'}]})
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
        with patch.object(TranslationProvider, '_request', side_effect=request):
            self.assertEqual(provider(('原始第一段', '原始第二段')), ['First', 'Second'])
        with patch.object(TranslationProvider, '_request', return_value={'choices': [
                {'finish_reason': 'length', 'message': {'content': '{"items":[]}'}}]}):
            with self.assertRaises(CorpusError) as err:
                provider(('原始第一段',))
            self.assertEqual(err.exception.code, 'translation_alignment_invalid')

    def test_alignment_preserves_identifier_not_sentence_punctuation(self):
        from corpus.storage import digest
        from corpus.translation_alignment import align_for_index

        original = '请参阅 DOI 10.1234/fixture。'
        chunk = {'text': original, 'ordinal': 0, 'content_hash': digest(original)}
        [row], _ = align_for_index([chunk], 'zh', lambda *args, **kw: ['See DOI 10.1234/fixture.'])
        self.assertEqual(row['text'], original)
        with self.assertRaises(CorpusError):
            align_for_index([chunk], 'zh', lambda *args, **kw: ['See DOI 10.1234/different.'])

    def test_catalog_credential_never_authorizes_other_origins_or_scopes(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        base = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, value in {'model_config.json': {'provider_id':'aliyun_bailian', 'main_base_url':base,
                                'main_model':'model'}, 'provider_api_keys.json':{'aliyun_bailian':'fixture'}}.items():
                (path / name).write_text(json.dumps(value))
                (path / name).chmod(0o600)
            env = {'SELFCORPUS_TRANSLATION_ENDPOINT':base+'/chat/completions', 'SELFCORPUS_TRANSLATION_MODEL':'model',
                   'SELFCORPUS_TRANSLATION_BAILIAN_CONFIG_DIR':tmp}
            adapter = TranslationProvider.from_environment(env)
            self.assertTrue(adapter.allows_scope('shared'))
            self.assertFalse(adapter.allows_scope('personal:owner'))
            self.assertNotIn('fixture', repr(adapter))
            for key, invalid in (('ENDPOINT','https://invalid.example/v1/chat/completions'),
                                 ('SCOPE','local-all'), ('MODEL','unbound')):
                with self.assertRaises(ValueError):
                    TranslationProvider.from_environment(env | {'SELFCORPUS_TRANSLATION_'+key:invalid})
            (path / 'provider_api_keys.json').chmod(0o644)
            with self.assertRaises(ValueError):
                TranslationProvider.from_environment(env)
