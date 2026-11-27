import base64
import io
import json
import socket
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from corpus import sources as s
from corpus.errors import CorpusError
from corpus.sources import _doi as doi_source
from corpus.sources import _http as http_source


class Wire:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, limit, _deadline):
        self.calls.append((url, limit))
        key = url.split('?', 1)[0]
        if key in self.routes:
            status, headers, content = self.routes[key]
        elif key.endswith('/robots.txt'):
            status, headers, content = 404, {}, b''
        else:
            status, headers, content = 404, {}, b''
        return s.HTTPResponse(status, headers, content, url)


def payload(record):
    return 200, {'content-type': 'application/json'}, json.dumps(record).encode()


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.addCleanup(patch.stopall)
        patch.object(http_source.time, 'monotonic', lambda: self.clock).start()
        patch.object(http_source.time, 'sleep', self.advance).start()

    def advance(self, seconds):
        self.clock += seconds

    def test_normalizes_doi(self):
        self.assertEqual(s.normalize_doi(' DOI:10.12345/Research '), '10.12345/research')
        self.assertEqual(s.normalize_doi('https://doi.org/10.1234/M%C3%BCller?x=1'), '10.1234/müller')
        for invalid in ('example.com/10.1234/test', '10.1/abc', '10.1234/ ', 'https://example.com/10.1234/x'):
            with self.subTest(invalid=invalid), self.assertRaises(CorpusError) as error:
                s.normalize_doi(invalid)
            self.assertEqual(error.exception.code, 'invalid_doi')

    def test_upload_contract_and_guards(self):
        config = {'kind': 'upload', 'filename': 'scientific report.txt', 'media_type': 'text/plain; charset=UTF-8',
                  'content_b64': base64.b64encode(b'local text').decode(), 'doi': '10.1234/WoRk'}
        result = s.Sources().fetch(config)
        self.assertEqual((result.content, result.media_type, result.uri),
                         (b'local text', 'text/plain', 'upload:scientific%20report.txt'))
        self.assertIsNone(result.http_status)
        self.assertEqual(result.metadata['doi'], '10.1234/work')
        self.assertEqual(result.kind, 'upload')
        for replacement, code in (({'filename': '../x'}, 'invalid_upload'), ({'content_b64': 'not b64'}, 'invalid_upload'),
                                  ({'media_type': 'application/pdf'}, 'source_media_mismatch'),
                                  ({'media_type': 'application/json'}, 'unsupported_media_type')):
            with self.subTest(replacement=replacement), self.assertRaises(CorpusError) as error:
                s.Sources().fetch(config | replacement)
            self.assertEqual(error.exception.code, code)
        with patch.object(s, 'MAX_BYTES', 3), self.assertRaises(CorpusError) as error:
            s.Sources().fetch(config)
        self.assertEqual(error.exception.status, 413)

    def test_redirect_checks_robots_at_each_origin_and_caches(self):
        wire = Wire({
            'https://one.test/document': (302, {'location': 'https://two.test/paper'}, b''),
            'https://two.test/robots.txt': (200, {'content-type': 'text/plain'},
                                           b'User-agent: SelfCorpus\nDisallow: /p*\nAllow: /paper$\n'),
            'https://two.test/paper': (200, {'content-type': 'text/plain'}, b'Document'),
        })
        client = s.Sources(s.PublicHTTP(wire))
        result = client.fetch({'kind': 'url', 'url': 'https://one.test/document', 'access_note': 'Public copy'})
        self.assertEqual(result.uri, 'https://two.test/paper')
        self.assertEqual(result.metadata['requested_url'], 'https://one.test/document')
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.access_note, 'Public copy')
        with self.assertRaises(CorpusError) as error:
            client.fetch({'kind': 'url', 'url': 'https://two.test/%70aper?mode=print', 'access_note': 'Public copy'})
        self.assertEqual(error.exception.code, 'robots_denied')
        self.assertEqual([url for url, _ in wire.calls], ['https://one.test/robots.txt', 'https://one.test/document',
                                                         'https://two.test/robots.txt', 'https://two.test/paper'])

    def test_robots_crawl_delay_and_rate_retry(self):
        host = 'https://paper.test'
        wire = Wire({host + '/robots.txt': (200, {}, b'User-Agent: *\nCrawl-delay: 2\n'),
                     host + '/paper': (429, {'retry-after': '120'}, b'')})
        http = s.PublicHTTP(wire)
        with self.assertRaises(CorpusError) as error:
            http.get(host + '/paper', 256, 130)
        self.assertEqual((error.exception.status, error.exception.retryable), (429, True))
        self.assertEqual(self.clock, 102)
        with self.assertRaises(CorpusError) as error:
            http.get(host + '/other', 256, 130)
        self.assertEqual(error.exception.code, 'source_rate_limited')
        self.assertEqual(len(wire.calls), 2)

    def test_public_url_rejects_credential_and_traversal_syntax(self):
        for url in ('http://u:p@paper.test/xx', 'http://paper.test:8080/xx', 'file:///x',
                    'https://paper.test/a/%2e%2e/xx', 'https://paper.test/a?%61ccess_token=x'):
            with self.subTest(url=url), self.assertRaises(CorpusError) as error:
                http_source._canonical_url(url)
            self.assertEqual(error.exception.code, 'invalid_url')

    def test_direct_url_checks_redirect_downgrade_and_private_targets(self):
        host = 'https://paper.test'
        for target, code in (('http://example.test/a', 'source_insecure_redirect'),
                             ('https://127.0.0.1/a', 'source_target_blocked'),
                             ('https://[::ffff:127.0.0.1]/a', 'source_target_blocked')):
            with self.subTest(target=target):
                wire = Wire({host + '/paper': (302, {'location': target}, b'')})

                def guarded(url, limit, deadline):
                    if urlsplit(url).hostname != 'paper.test':
                        http_source._resolve(urlsplit(url).hostname, 443, deadline)
                    return wire(url, limit, deadline)

                with self.assertRaises(CorpusError) as error:
                    s.PublicHTTP(guarded).get(host + '/paper', 250, 130)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(len(wire.calls), 2)

    def test_doi_metadata_survives_openalex_html_landing_page(self):
        doi = '10.1234/study'
        wire = Wire({'https://api.crossref.org/works/' + doi: payload({'message': {'DOI': doi, 'title': ['Study'],
                     'issued': {'date-parts': [[2020]]}}}),
                     'https://api.openalex.org/works/https://doi.org/' + doi:
                         payload({'doi': 'https://doi.org/' + doi, 'best_oa_location': {
                             'is_oa': True, 'pdf_url': 'https://journal.test/paper'}}),
                     'https://journal.test/paper': (200, {'content-type': 'text/html'}, b'<html>Sign in</html>')})
        result = s.Sources(s.PublicHTTP(wire)).fetch({'kind': 'doi', 'doi': doi})
        self.assertEqual(result.media_type, 'application/json')
        self.assertFalse(result.metadata['fulltext'])
        self.assertEqual(result.metadata['coverage'], 'metadata_only')
        self.assertEqual(json.loads(result.content)['title'], 'Study')
        self.assertEqual(result.metadata['source_diagnostics'][-1]['code'], 'source_not_fulltext')

    def test_doi_datacite_fallback_and_actual_europe_pmc_fulltext(self):
        doi = '10.1234/study'
        api = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'
        jats = b'<!DOCTYPE article SYSTEM "https://unused.test/JATS.dtd"><article><body><p>Study body</p></body></article>'
        wire = Wire({'https://api.datacite.org/dois/' + doi: payload({'data': {'attributes': {'doi': doi,
                     'titles': [{'title': 'DataCite study'}], 'creators': [{'name': 'Example, A.'}]}}}),
                     api + 'search': payload({'resultList': {'result': [
                         {'doi': '10.1234/other', 'isOpenAccess': 'Y', 'pmcid': 'PMC9999'},
                         {'doi': doi, 'isOpenAccess': 'Y', 'pmcid': 'PMC1234', 'abstractText': 'Study abstract'}]}}),
                     api + 'PMC1234/fullTextXML': (200, {'content-type': 'application/xml'}, jats)})
        result = s.resolve_doi(doi, http=s.PublicHTTP(wire))
        self.assertEqual(result.content, jats)
        self.assertTrue(result.metadata['fulltext'])
        self.assertEqual(result.metadata['fulltext_provider'], 'europe_pmc')
        self.assertEqual(result.metadata['title'], 'DataCite study')
        self.assertEqual(result.metadata['abstract'], 'Study abstract')
        self.assertNotIn(api + 'PMC9999/fullTextXML', [url for url, _ in wire.calls])

    def test_openalex_requires_actual_pdf(self):
        doi = '10.1234/study'
        url = 'https://journal.test/paper'
        wire = Wire({'https://api.openalex.org/works/https://doi.org/' + doi: payload({'doi': doi, 'title': 'Study',
                     'best_oa_location': {'is_oa': True, 'pdf_url': url}}),
                     url: (200, {'content-type': 'application/octet-stream'}, b'%PDF-1.4\nDocument')})
        result = s.resolve_doi(doi, http=s.PublicHTTP(wire))
        self.assertEqual(result.media_type, 'application/pdf')
        self.assertEqual(result.metadata['fulltext_provider'], 'openalex')

    def test_jats_body_validation(self):
        for body in (b'<article><front>just metadata</front></article>', b'<article><body><p/></body></article>',
                     b'<article><body><p>truncated', b'<other><body>not an article</body></other>',
                     b'<!DOCTYPE article [<!ENTITY hi "x">]><article><body>&hi;</body></article>'):
            with self.subTest(body=body):
                self.assertFalse(doi_source._has_jats_body('application/xml', body))


class MemorySocket:
    def __init__(self, wire):
        self.wire = wire
        self.address = None
        self.request = b''

    def settimeout(self, _timeout):
        pass

    def connect(self, address):
        self.address = address

    def sendall(self, data):
        self.request += data

    def makefile(self, _mode):
        return io.BytesIO(self.wire)

    def close(self):
        pass


class PublicTransportTest(unittest.TestCase):
    @staticmethod
    def dns(ip):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80))]

    def test_pins_checked_dns_address_and_resolves_every_hop(self):
        sock = MemorySocket(b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc')
        with patch.object(http_source.socket, 'getaddrinfo', side_effect=[self.dns('8.8.8.8'), self.dns('127.0.0.1')]) as resolver, \
                patch.object(http_source.socket, 'socket', return_value=sock) as socket_factory:
            deadline = time.monotonic() + 3
            self.assertEqual(http_source._request('http://paper.test/doc', 10, deadline).content, b'abc')
            with self.assertRaises(CorpusError) as error:
                http_source._request('http://paper.test/another', 10, deadline)
        self.assertEqual(error.exception.code, 'source_target_blocked')
        self.assertEqual(sock.address, ('8.8.8.8', 80))
        self.assertIn(b'\r\nHost: paper.test\r\n', sock.request)
        self.assertEqual(resolver.call_count, 2)
        socket_factory.assert_called_once()

    def test_rejects_mixed_dns_results(self):
        for address in ('10.1.2.3', '169.254.169.254', '100.64.0.1', '224.0.0.1'):
            with self.subTest(address=address), patch.object(http_source.socket, 'getaddrinfo', return_value=(
                    self.dns('8.8.8.8') + self.dns(address))), self.assertRaises(CorpusError) as error:
                http_source._resolve('paper.test', 80, time.monotonic() + 3)
            self.assertEqual(error.exception.code, 'source_target_blocked')

    def test_bounds_declared_and_streamed_bodies_and_rejects_compression(self):
        cases = [(b'Content-Length: 3\r\n\r\nabc', 'source_too_large'),
                 (b'Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n', 'source_too_large'),
                 (b'Content-Encoding: gzip\r\n\r\nxx', 'source_encoding'),
                 (b'Content-Length: 2\r\n\r\na', 'source_invalid_response')]
        for body, code in cases:
            with self.subTest(body=body), patch.object(http_source.socket, 'socket', return_value=MemorySocket(
                    b'HTTP/1.1 200 OK\r\n' + body)), self.assertRaises(CorpusError) as error:
                http_source._request('http://8.8.8.8/paper', 2, time.monotonic() + 3)
            self.assertEqual(error.exception.code, code)
