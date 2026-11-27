import base64
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock

from corpus.api import CorpusHTTPServer, DEFAULT_PORT, MAX_BODY_BYTES, MAX_INGEST_BODY_BYTES
from corpus.auth import (Actor, AuthenticationError, CLAIMS_HEADER, MAX_CLOCK_SKEW_SECONDS,
                         signed_headers, verify_signed_actor)
from corpus.errors import CorpusError
from corpus.host_client import DEFAULT_URL, HostClient, HostClientError


TEST_KEY = b'independent-test-key'
SEARCH = '/api/v1/retrieval/search'
INGEST = '/api/v1/ingestion/requests'


class AuthTests(unittest.TestCase):
    def test_actor_is_immutable_and_permissions_are_scope_specific(self):
        grants = {'one': 'read', 'two': 'write'}
        actor = Actor('alice', grants)
        grants['one'] = 'write'
        self.assertEqual(actor.projects['one'], 'read')
        self.assertTrue(actor.can_write('personal:alice'))
        self.assertFalse(actor.can_read('personal:bob'))
        self.assertTrue(actor.can_read('project:one'))
        self.assertFalse(actor.can_write('project:one'))
        self.assertTrue(actor.can_read('project:two'))
        self.assertTrue(actor.can_write('project:two'))
        self.assertFalse(actor.can_read('project:three'))
        self.assertTrue(actor.can_read('shared'))
        self.assertFalse(actor.can_write('shared'))
        self.assertTrue(Actor('alice', governor=True).can_write('shared'))
        self.assertFalse(Actor('alice', shared=False, governor=True).can_write('shared'))
        with self.assertRaises(TypeError):
            actor.projects['new'] = 'write'

    def test_references_fit_app_scope_identifier_format(self):
        for invalid in ('', 'space here', 'external:subject', '项目', 'a' * 129):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Actor(invalid)
                with self.assertRaises(ValueError):
                    Actor('alice', {invalid: 'read'})

    def test_signing_binds_all_host_inputs_and_expires(self):
        actor = Actor('contributor@example.org', {'project-one': 'read'}, governor=True)
        target = SEARCH + '?a=%20'
        body = b'{"query":"hello"}'
        stamp = 1720000000
        headers = signed_headers(TEST_KEY, 'POST', target, body, actor, now=stamp)
        self.assertEqual(verify_signed_actor(TEST_KEY, 'POST', target, body, headers,
                                             now=stamp + MAX_CLOCK_SKEW_SECONDS), actor)
        bad_claims = {**headers, CLAIMS_HEADER: headers[CLAIMS_HEADER][:-1]}
        for key, method, path, data, auth, now in (
            (b'wrong-key', 'POST', target, body, headers, stamp),
            (TEST_KEY, 'GET', target, body, headers, stamp),
            (TEST_KEY, 'POST', SEARCH, body, headers, stamp),
            (TEST_KEY, 'POST', target, body + b' ', headers, stamp),
            (TEST_KEY, 'POST', target, body, bad_claims, stamp),
            (TEST_KEY, 'POST', target, body, headers, stamp - MAX_CLOCK_SKEW_SECONDS - 1),
            (TEST_KEY, 'POST', target, body, headers, stamp + MAX_CLOCK_SKEW_SECONDS + 1),
        ):
            with self.subTest(key=key, method=method, path=path, now=now):
                with self.assertRaises(AuthenticationError):
                    verify_signed_actor(key, method, path, data, auth, now=now)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Mock()
        cls.server = CorpusHTTPServer(cls.app, TEST_KEY, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05})
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.app.reset_mock(return_value=True, side_effect=True)
        self.actor = Actor('alice', {'mine': 'write'})
        self.client = HostClient(TEST_KEY, f'http://127.0.0.1:{self.port}')

    def request(self, method, path, *, body=None, headers=None, signed=False, now=None):
        headers = {} if headers is None else headers.copy()
        if body is not None:
            headers.setdefault('Content-Type', 'application/json')
        if signed:
            headers.update(signed_headers(TEST_KEY, method, path, body or b'', self.actor, now=now))
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertEqual(response.getheader('Content-Type'), 'application/json; charset=utf-8')
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_only_health_works_without_host_signature(self):
        self.app.health.return_value = {'status': 'ok'}
        self.assertEqual(self.request('GET', '/api/v1/health'), (200, {'status': 'ok'}))
        self.assertEqual(self.client.health(), {'status': 'ok'})
        status, result = self.request('POST', SEARCH, body=b'{"subject":"alice","governor":true}')
        self.assertEqual((status, result['error']['code']), (401, 'unauthorized'))
        self.app.search.assert_not_called()

    def test_search_dispatch_uses_signed_actor_and_preserves_requested_scope(self):
        scope = {'personal': True, 'project_refs': ['mine'], 'shared': True}
        self.app.search.return_value = {'results': []}
        self.assertEqual(self.client.search(self.actor, '文档', scope, {'x': 1}, 4), {'results': []})
        self.app.search.assert_called_once_with(self.actor, '文档', scope, {'x': 1}, 4)
        body = json.dumps({'query': 'x', 'scope': scope, 'governor': True}).encode()
        self.assertEqual(self.request('POST', SEARCH, body=body, signed=True)[0], 400)

    def test_submit_returns_accepted_and_uses_signed_idempotency_key(self):
        self.app.submit.return_value = {'request_ref': 'one'}
        payload = {'scope_ref': 'shared', 'idempotency_key': 'test-once', 'spec': {'title': 'demo'}}
        self.assertEqual(self.request('POST', INGEST, body=json.dumps(payload).encode(), signed=True),
                         (202, {'request_ref': 'one'}))
        self.app.submit.assert_called_once_with(self.actor, {'title': 'demo'}, 'shared', 'test-once')

    def test_host_client_reaches_records_and_governance(self):
        self.app.submit.return_value = {'request_ref': 'req'}
        self.app.request.return_value = {'request_ref': 'req'}
        self.app.document.return_value = {'document_ref': 'a/文'}
        self.app.generation.return_value = {'generation_ref': 'gen'}
        self.app.proposal.return_value = {'request_ref': 'req'}
        self.app.withdraw.return_value = {'status': 'withdrawn'}
        self.app.rollback.return_value = {'status': 'restored'}
        actor = self.actor
        self.assertEqual(self.client.submit(actor, {}, 'personal:alice', 'k'), {'request_ref': 'req'})
        self.assertEqual(self.client.request(actor, 'req'), {'request_ref': 'req'})
        self.app.request.assert_called_once_with(actor, 'req')
        self.client.document(actor, 'a/文')
        self.app.document.assert_called_with(actor, 'a/文', chunks=False)
        self.client.document(actor, 'a/文', chunks=True)
        self.app.document.assert_called_with(actor, 'a/文', chunks=True)
        self.client.generation(actor, 'gen')
        self.app.generation.assert_called_once_with(actor, 'gen')
        self.client.proposal(actor, 'req')
        self.app.proposal.assert_called_with(actor, 'req', approve=None)
        self.client.proposal(actor, 'req', approve=False)
        self.app.proposal.assert_called_with(actor, 'req', approve=False)
        self.assertEqual(self.request('GET', '/api/v1/publication/proposals/req', signed=True)[0], 200)
        self.app.proposal.assert_called_with(actor, 'req')
        self.client.withdraw(actor, 'project:mine', 'doc')
        self.app.withdraw.assert_called_once_with(actor, 'project:mine', 'doc')
        self.client.rollback(actor, 'project:mine', 'gen')
        self.app.rollback.assert_called_once_with(actor, 'project:mine', 'gen')

    def test_tampering_and_expiration_do_not_reach_app(self):
        body = b'{"query":"q","scope":{}}'
        auth = signed_headers(TEST_KEY, 'POST', SEARCH, body, self.actor)
        self.assertEqual(self.request('POST', SEARCH, body=body + b' ', headers=auth)[0], 401)
        self.assertEqual(self.request('POST', SEARCH, body=body, signed=True,
                                      now=time.time() - MAX_CLOCK_SKEW_SECONDS - 5)[0], 401)
        self.app.search.assert_not_called()

    def test_body_bounds_and_json_validation(self):
        response = self.request('POST', SEARCH, body=b'', headers={
            'Content-Length': str(MAX_BODY_BYTES + 1)})
        self.assertEqual(response[0], 413)
        response = self.request('POST', SEARCH, body=b'{}', headers={'Content-Type': 'text/plain'})
        self.assertEqual(response[0], 415)
        response = self.request('POST', SEARCH, body=b'{}', headers={'Transfer-Encoding': 'chunked'})
        self.assertEqual(response[0], 400)
        for body in (b'[]', b'{"x": 1, "x": 2}', b'{"x":NaN}', b'{"x":1e400}', b'\xff',
                     b'{"query":"a","scope":{"project_refs":"one"}}',
                     b'{"query":"a","scope":{"personal":"true"}}',
                     b'{"query":"a","scope":{},"limit":true}'):
            with self.subTest(body=body):
                self.assertEqual(self.request('POST', SEARCH, body=body, signed=True)[0], 400)
        self.app.search.assert_not_called()

    def test_ingestion_accepts_full_base64_upload_but_other_routes_keep_lower_cap(self):
        self.assertEqual(DEFAULT_PORT, 8061)
        self.assertEqual(DEFAULT_URL, 'http://127.0.0.1:8061')
        # Exercise the full current Sources upload ceiling, not just the small-route cutoff.
        upload = base64.b64encode(b'a' * (16 * 1024 * 1024)).decode('ascii')
        spec = {'kind': 'upload', 'filename': 'full.pdf', 'media_type': 'application/pdf',
                'content_b64': upload}
        self.app.submit.return_value = {'request_ref': 'full'}
        self.assertEqual(self.client.submit(self.actor, spec, 'personal:alice', 'full'),
                         {'request_ref': 'full'})
        self.app.submit.assert_called_once_with(self.actor, spec, 'personal:alice', 'full')
        self.assertEqual(self.request('POST', INGEST, body=b'', headers={
            'Content-Length': str(MAX_INGEST_BODY_BYTES + 1)})[0], 413)
        with self.assertRaises(HostClientError) as caught:
            self.client.search(self.actor, 'a' * MAX_BODY_BYTES, {})
        self.assertEqual((caught.exception.status, caught.exception.code), (413, 'request_too_large'))
        self.app.search.assert_not_called()

    def test_rejects_incomplete_or_conflicting_body_length(self):
        for lengths in (b'Content-Length: 100\r\n', b'Content-Length: 2\r\nContent-Length: 3\r\n'):
            with self.subTest(lengths=lengths), socket.create_connection(('127.0.0.1', self.port), 2) as conn:
                conn.sendall(b'POST /api/v1/ingestion/requests HTTP/1.1\r\n'
                             b'Host: localhost\r\nContent-Type: application/json\r\n' + lengths + b'\r\n{}')
                conn.shutdown(socket.SHUT_WR)
                with http.client.HTTPResponse(conn) as response:
                    response.begin()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.read())['error']['code'], 'invalid_request')
        self.app.submit.assert_not_called()

    def test_rejects_lone_surrogates_without_leaking_bad_output(self):
        bad_request = br'{"query":"\ud800","scope":{}}'
        self.assertEqual(self.request('POST', SEARCH, body=bad_request, signed=True)[0], 400)
        self.app.search.assert_not_called()
        self.app.generation.return_value = {'bad': '\ud800'}
        self.app.request.side_effect = CorpusError('denied', 'bad \udfff', status=403)
        for target in ('/api/v1/index-generations/gen', INGEST + '/req'):
            with self.subTest(target=target):
                code, result = self.request('GET', target, signed=True)
                self.assertEqual((code, result['error']['code']), (500, 'internal_error'))

    def test_errors_are_consistent_and_unexpected_errors_do_not_leak(self):
        self.app.request.side_effect = CorpusError('denied', 'Not accessible.', status=403, retryable=False)
        with self.assertRaises(HostClientError) as caught:
            self.client.request(self.actor, 'req')
        self.assertEqual((caught.exception.code, str(caught.exception), caught.exception.status,
                          caught.exception.retryable), ('denied', 'Not accessible.', 403, False))
        self.app.generation.side_effect = RuntimeError('private details')
        status, answer = self.request('GET', '/api/v1/index-generations/gen', signed=True)
        self.assertEqual((status, answer), (500, {'error': {'code': 'internal_error',
                          'message': 'Unexpected service error.', 'retryable': False}}))
        self.assertEqual(self.request('PATCH', SEARCH)[0], 405)
        self.assertEqual(self.request('GET', '/api/v1/missing', signed=True)[0], 404)


if __name__ == '__main__':
    unittest.main()
