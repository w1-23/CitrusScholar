import unittest
from types import SimpleNamespace

from corpus.discovery import discover
from corpus.errors import CorpusError


class DiscoveryTests(unittest.TestCase):
    def test_bounded_and_never_crosses_origins(self):
        class Fixture:
            calls = []
            def get(self,url,limit,deadline):
                self.calls.append(url)
                if '/first' in url:
                    content = '<a href="/from-first">2nd degree</a>'
                else:
                    content = '''<a href="/first#discard">first</a><a href="/second">second</a>
                           <a href="https://another.example/">foreign</a><a href="file:///tmp">ignored</a>'''
                return SimpleNamespace(url=url, content=content.encode(), headers={'content-type':'text/html'})
        fixture = Fixture()
        result = discover('https://papers.example/',max_pages=2,depth=1,http=fixture)
        self.assertEqual(fixture.calls,['https://papers.example/','https://papers.example/first'])
        self.assertEqual([x['url'] for x in result['candidates']], ['https://papers.example/first',
            'https://papers.example/second', 'https://papers.example/from-first'])
        self.assertTrue(result['limited'])
        with self.assertRaises(CorpusError):
            discover('file:///tmp',http=fixture)
