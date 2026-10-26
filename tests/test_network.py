"""Deterministic network failures, bounded fallback and fail-closed robots gates."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.parsing import AccessIssue, access_check
from radar.transport import AutoTransport, FetchError, HttpTransport, Page, Robots, Throttle, USER_AGENT


class NetworkHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.reads.append((self.path, self.headers.get('User-Agent')))
        if self.path == '/robots.txt':
            response = self.server.rules
            if response == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
        elif self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/authserver/login?ticket=do-not-expose')
            self.end_headers()
            return
        elif self.path == '/large':
            response = (200, 'text/plain', 'x' * 3_000_001)
        elif self.path == '/truncated':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '10000')
            self.end_headers()
            self.wfile.write(b'incomplete')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        else:
            response = (200, 'text/html', '<title>通知公告</title>')
        status, content_type, text = response
        self.send_response(status)
        if content_type is not None:
            self.send_header('Content-Type', content_type)
        if status == 302:
            self.send_header('Location', text)
            text = ''
        body = text.encode('utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def local_server(rules=(200, 'text/plain', 'User-agent: *\nAllow: /')):
    server = ThreadingHTTPServer(('127.0.0.1', 0), NetworkHandler)
    server.rules, server.reads = rules, []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class FakeTransport:
    def __init__(self, responses, *, throttle=None):
        self.responses = responses
        self.throttle = throttle if throttle is not None else Throttle(0)
        self.calls = 0
        self.blocked_methods = 0
        self.reads = []
        self.closed = False

    def fetch(self, url, hosts, *, raw=False, allow_http_detail=False):
        self.calls += 1
        self.reads.append((url, raw))
        value = self.responses[url] if isinstance(self.responses, dict) else self.responses
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class NetworkTests(unittest.TestCase):
    def assert_code(self, expected, operation):
        with self.assertRaises((FetchError, AccessIssue)) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)
        return str(caught.exception)

    def test_direct_default_and_explicit_broken_system_proxy(self):
        with local_server() as (_, base):
            proxy = 'http://127.0.0.1:1'
            environment = {key: proxy for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                 'http_proxy', 'https_proxy', 'all_proxy')}
            environment.update({'NO_PROXY': '', 'no_proxy': ''})
            with patch.dict(os.environ, environment):
                direct = HttpTransport(timeout=2, retries=0)
                system = HttpTransport(timeout=2, retries=0, direct=False)
                try:
                    page = direct.fetch(base + '/list', {'127.0.0.1'})
                    self.assertEqual(page.status, 200)
                    self.assertIn('通知公告', page.text)
                    self.assert_code('proxy_failed', lambda: system.fetch(base + '/list', {'127.0.0.1'}))
                finally:
                    direct.close()
                    system.close()

    def test_http_status_content_type_limits_and_safe_redirect(self):
        with local_server() as (server, base):
            transport = HttpTransport(timeout=2, retries=0, delay=0)
            try:
                for status in (401, 403, 404, 410):
                    server.rules = (status, 'text/plain; charset=utf-8', 'not returned')
                    page = transport.fetch(base + '/robots.txt', {'127.0.0.1'}, raw=True)
                    self.assertEqual((page.status, page.content_type), (status, 'text/plain; charset=utf-8'))
                self.assert_code('resource_limit', lambda: transport.fetch(base + '/large', {'127.0.0.1'}))
                self.assert_code('read_interrupted', lambda: transport.fetch(base + '/truncated', {'127.0.0.1'}))
                message = self.assert_code('login_required', lambda: transport.fetch(base + '/redirect', {'127.0.0.1'}))
                self.assertNotIn('do-not-expose', message)
                self.assertFalse(any(path.startswith('/authserver') for path, _ in server.reads))
                self.assertTrue(all(agent == USER_AGENT for _, agent in server.reads))
            finally:
                transport.close()

    def test_request_exception_categories_are_public_and_sanitized(self):
        cases = [(requests.exceptions.ProxyError('secret'), 'proxy_failed'),
                 (requests.exceptions.SSLError('secret'), 'tls_failed'),
                 (requests.exceptions.Timeout('secret'), 'timeout'),
                 (requests.exceptions.ConnectionError('secret'), 'connection_failed')]
        transport = HttpTransport(retries=0)
        try:
            for error, code in cases:
                with self.subTest(code=code), patch.object(transport.session, 'get', side_effect=error):
                    message = self.assert_code(code, lambda: transport.fetch('https://fixture.example/list', {'fixture.example'}))
                    self.assertNotIn('secret', message)
        finally:
            transport.close()

    def test_access_categories(self):
        for content, code in [('<title>统一身份认证</title>', 'login_required'),
                              ('<title>校外访问</title>校外访问 aTrust 校园网', 'campus_network_required'),
                              ('<title>人机验证</title>', 'verification_required'),
                              ('<title>Access Denied</title>', 'access_denied')]:
            with self.subTest(code=code):
                self.assert_code(code, lambda: access_check(content, 'https://fixture.example/'))

    def test_robots_status_and_content_rules(self):
        cases = [(404, 'text/html', '', None), (410, 'text/html', '', None),
                 (401, 'text/plain', '', 'robots_denied'), (403, 'text/plain', '', 'robots_denied'),
                 (429, 'text/plain', '', 'robots_unavailable'), (503, 'text/plain', '', 'robots_unavailable'),
                 (200, 'text/plain', '', None), (200, None, '', None),
                 (200, None, 'User-agent: *\nAllow: /', None),
                 (200, 'text/html', '<html><body>error</body></html>', 'robots_invalid'),
                 (200, None, '<div>system error</div>', 'robots_invalid'),
                 (200, 'text/plain', '<form><input type="password"></form>', 'robots_invalid'),
                 (200, 'text/html', '<title>人机验证</title>', 'robots_invalid'),
                 (200, 'application/octet-stream', 'binary', 'robots_invalid'),
                 (200, 'text/plain', '<html><body><pre>User-agent: *\nAllow: /</pre></body></html>', 'robots_invalid'),
                 (200, 'text/plain', 'User-agent: *\nDisallow: /', 'robots_denied')]
        with local_server() as (server, base):
            for status, mime, text, code in cases:
                with self.subTest(status=status, mime=mime, text=text):
                    server.rules = status, mime, text
                    transport = HttpTransport(timeout=2, retries=0, delay=0)
                    try:
                        gate = Robots(transport)
                        if code:
                            self.assert_code(code, lambda: gate.allowed(base + '/list', {'127.0.0.1'}))
                        else:
                            self.assertTrue(gate.allowed(base + '/list', {'127.0.0.1'}))
                    finally:
                        transport.close()

    def test_robots_longest_match_allows_public_exception(self):
        rules = 'User-agent: *\nDisallow: /\nAllow: /public/\n'
        with local_server((200, 'text/plain', rules)) as (_, base):
            transport = HttpTransport(timeout=2, retries=0, delay=0)
            try:
                gate = Robots(transport)
                self.assertTrue(gate.allowed(base + '/public/notices', {'127.0.0.1'}))
                self.assert_code('robots_denied', lambda: gate.allowed(base + '/private', {'127.0.0.1'}))
            finally:
                transport.close()

    def test_robots_merges_agent_groups_and_honors_utf8_bom_and_decimal_delay(self):
        rules = ('\ufeffUser-agent: XidianNoticeRadar\nCrawl-delay: 0.5\nDisallow: /private/\n'
                 'Allow: /notice/\n\nUser-agent: XidianNoticeRadar\nDisallow: /notice/internal/\n')
        with local_server((200, 'text/plain; charset=utf-8', rules)) as (_, base):
            transport = HttpTransport(timeout=2, retries=0, delay=0)
            try:
                gate = Robots(transport)
                self.assertTrue(gate.allowed(base + '/notice/index', {'127.0.0.1'}))
                self.assert_code('robots_denied', lambda: gate.allowed(base + '/private/data', {'127.0.0.1'}))
                self.assert_code('robots_denied', lambda: gate.allowed(base + '/notice/internal/data', {'127.0.0.1'}))
                self.assertEqual(transport.throttle.crawl_delays[base.removeprefix('http://')], 0.5)
            finally:
                transport.close()

    def test_extracted_browser_plain_text_and_shared_crawl_delay(self):
        url = 'https://fixture.example/robots.txt'
        response = Page(url, 'User-agent: *\nCrawl-delay: 3\nDisallow: /private', 200, 'text/plain')
        transport = FakeTransport(response)
        gate = Robots(transport)
        self.assertTrue(gate.allowed('https://fixture.example/list', {'fixture.example'}))
        self.assert_code('robots_denied', lambda: gate.allowed('https://fixture.example/private', {'fixture.example'}))
        self.assertEqual(transport.throttle.crawl_delays['fixture.example'], 3)
        self.assertEqual(len(transport.reads), 1)

    def test_explicit_permission_does_not_leak_to_strict_calls_on_same_origin(self):
        with local_server('disconnect') as (server, base), tempfile.TemporaryDirectory() as directory:
            browser = FakeTransport(FetchError('browser timed out', code='timeout'))
            with patch('radar.transport.BrowserTransport', return_value=browser):
                transport = AutoTransport(directory, timeout=2, delay=0)
                transport.http.retries = 0
                try:
                    gate = Robots(transport)
                    self.assert_code('robots_unavailable', lambda: gate.allowed(base + '/list', {'127.0.0.1'}))
                    self.assertTrue(gate.allowed(base + '/list', {'127.0.0.1'}, allow_unavailable=True))
                    self.assertIsNotNone(gate.unverified_warning(base + '/list'))
                    self.assertEqual(transport.fetch(base + '/list', {'127.0.0.1'}).status, 200)
                    self.assert_code('robots_unavailable', lambda: gate.allowed(base + '/other-section', {'127.0.0.1'}))
                    self.assertEqual([path for path, _ in server.reads], ['/robots.txt', '/list'])
                    self.assertEqual(browser.reads, [(base + '/robots.txt', True)])
                finally:
                    transport.close()

    def test_permission_only_covers_network_rule_failures(self):
        for code in ('connection_failed', 'timeout', 'read_interrupted',
                     'tls_failed', 'proxy_failed', 'http_error', 'unsafe_redirect',
                     'browser_unavailable', 'session_invalid'):
            with self.subTest(code=code):
                transport = FakeTransport(FetchError('curated failure', code=code))
                gate = Robots(transport)
                if code in {'connection_failed', 'timeout', 'read_interrupted'}:
                    self.assertTrue(gate.allowed('https://fixture.example/list', {'fixture.example'}, allow_unavailable=True))
                    self.assertIsNotNone(gate.unverified_warning('https://fixture.example/list'))
                else:
                    self.assert_code('robots_unavailable', lambda: gate.allowed(
                        'https://fixture.example/list', {'fixture.example'}, allow_unavailable=True))
                    self.assertIsNone(gate.unverified_warning('https://fixture.example/list'))

    def test_permission_never_overrides_denial_html_or_http_errors(self):
        cases = [(200, 'text/plain', 'User-agent: *\nDisallow: /', 'robots_denied'),
                 (401, 'text/plain', '', 'robots_denied'),
                 (403, 'text/plain', '', 'robots_denied'),
                 (429, 'text/plain', '', 'robots_unavailable'),
                 (503, 'text/plain', '', 'robots_unavailable'),
                 (200, 'text/html', '<html>ordinary error</html>', 'robots_invalid'),
                 (200, 'text/html', '<input type="password">', 'robots_invalid'),
                 (200, 'text/html', '<title>人机验证</title>', 'robots_invalid')]
        with local_server() as (server, base):
            transport = HttpTransport(timeout=2, retries=0, delay=0)
            try:
                for status, mime, body, code in cases:
                    with self.subTest(status=status, body=body):
                        server.rules = status, mime, body
                        gate = Robots(transport)
                        self.assert_code(code, lambda: gate.allowed(
                            base + '/list', {'127.0.0.1'}, allow_unavailable=True))
                        self.assertIsNone(gate.unverified_warning(base + '/list'))
                self.assertFalse(any(path == '/list' for path, _ in server.reads))
            finally:
                transport.close()

    def test_robots_failure_is_cached_and_no_business_reads_occur(self):
        with local_server('disconnect') as (server, base), tempfile.TemporaryDirectory() as directory:
            browser = FakeTransport(FetchError('browser timed out', code='timeout'))
            with patch('radar.transport.BrowserTransport', return_value=browser):
                transport = AutoTransport(directory, timeout=2, delay=0)
                transport.http.retries = 0
                try:
                    gate = Robots(transport)
                    for path in ('/list', '/other-section'):
                        message = self.assert_code('robots_unavailable', lambda: gate.allowed(base + path, {'127.0.0.1'}))
                        self.assertIn('connection_failed', message)
                        self.assertIn('timeout', message)
                    self.assertEqual(server.reads, [('/robots.txt', USER_AGENT)])
                    self.assertEqual(browser.reads, [(base + '/robots.txt', True)])
                finally:
                    transport.close()

    def test_network_fallback_only_requests_original_robots_then_business(self):
        with local_server('disconnect') as (server, base), tempfile.TemporaryDirectory() as directory:
            browser = FakeTransport(Page(base + '/robots.txt', 'User-agent: *\nAllow: /', 200, 'text/plain'))
            with patch('radar.transport.BrowserTransport', return_value=browser):
                transport = AutoTransport(directory, timeout=2, delay=0)
                transport.http.retries = 0
                try:
                    gate = Robots(transport)
                    if gate.allowed(base + '/list', {'127.0.0.1'}):
                        transport.fetch(base + '/list', {'127.0.0.1'})
                    self.assertEqual([path for path, _ in server.reads], ['/robots.txt', '/list'])
                    self.assertEqual(browser.reads, [(base + '/robots.txt', True)])
                finally:
                    transport.close()

    def test_fallback_denied_or_invalid_robots_never_reads_business(self):
        for status, mime, text, code in [(200, 'text/plain', 'User-agent: *\nDisallow: /', 'robots_denied'),
                                         (401, 'text/plain', '', 'robots_denied'),
                                         (403, 'text/plain', '', 'robots_denied'),
                                         (200, 'text/html', '<title>统一身份认证</title>', 'robots_invalid'),
                                         (200, 'text/html', '<html>error</html>', 'robots_invalid')]:
            with self.subTest(code=code), local_server('disconnect') as (server, base), tempfile.TemporaryDirectory() as directory:
                browser = FakeTransport(Page(base + '/robots.txt', text, status, mime))
                with patch('radar.transport.BrowserTransport', return_value=browser):
                    transport = AutoTransport(directory, timeout=2, delay=0)
                    transport.http.retries = 0
                    try:
                        gate = Robots(transport)
                        self.assert_code(code, lambda: gate.allowed(base + '/list', {'127.0.0.1'}))
                        self.assertFalse(any(path == '/list' for path, _ in server.reads))
                    finally:
                        transport.close()

    def test_auto_fallback_eligibility_and_single_attempt(self):
        url = 'https://fixture.example/list'
        for code in ('connection_failed', 'timeout', 'read_interrupted', 'tls_failed',
                     'proxy_failed', 'http_error', 'resource_limit', 'unsafe_redirect'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                http = FakeTransport(FetchError('sanitized', code=code))
                browser = FakeTransport(Page(url, '<title>通知</title>', 200, 'text/html'))
                with patch('radar.transport.HttpTransport', return_value=http), patch('radar.transport.BrowserTransport', return_value=browser):
                    transport = AutoTransport(directory)
                    try:
                        if code in ('connection_failed', 'timeout', 'read_interrupted'):
                            self.assertEqual(transport.fetch(url, {'fixture.example'}).status, 200)
                            self.assertEqual(browser.reads, [(url, False)])
                            self.assertEqual(transport.calls, 2)
                        else:
                            self.assert_code(code, lambda: transport.fetch(url, {'fixture.example'}))
                            self.assertEqual(browser.reads, [])
                    finally:
                        transport.close()

    def test_login_fallback_requires_existing_dedicated_session_and_business_page(self):
        url = 'https://fixture.example/list'
        for code in ('login_required', 'access_denied', 'campus_network_required', 'verification_required'):
            for session in (False, True):
                for raw in (False, True):
                    with self.subTest(code=code, session=session, raw=raw), tempfile.TemporaryDirectory() as directory:
                        if session:
                            (Path(directory) / 'browser-profile').mkdir()
                        http = FakeTransport(AccessIssue('sanitized', code=code))
                        browser = FakeTransport(Page(url, '', 200, 'text/html'))
                        with patch('radar.transport.HttpTransport', return_value=http), patch('radar.transport.BrowserTransport', return_value=browser):
                            transport = AutoTransport(directory)
                            try:
                                if code == 'login_required' and session and not raw:
                                    self.assertEqual(transport.fetch(url, {'fixture.example'}, raw=raw).status, 200)
                                else:
                                    self.assert_code(code, lambda: transport.fetch(url, {'fixture.example'}, raw=raw))
                                    self.assertEqual(browser.reads, [])
                            finally:
                                transport.close()

    def test_missing_browser_does_not_break_healthy_http_and_is_not_relaunched(self):
        first, second = 'https://fixture.example/fail', 'https://healthy.example/list'
        http = FakeTransport({first: FetchError('network', code='timeout'), second: Page(second, '', 200)})
        with tempfile.TemporaryDirectory() as directory, patch('radar.transport.HttpTransport', return_value=http), patch(
                'radar.transport.BrowserTransport', side_effect=FetchError('missing', code='browser_unavailable')) as launch:
            transport = AutoTransport(directory)
            try:
                for _ in range(2):
                    self.assert_code('browser_unavailable', lambda: transport.fetch(first, {'fixture.example'}))
                self.assertEqual(transport.fetch(second, {'healthy.example'}).status, 200)
                self.assertEqual(launch.call_count, 1)
            finally:
                transport.close()

    def test_forced_modes_and_no_parse_heuristic_fallback(self):
        url = 'https://fixture.example/list'
        with tempfile.TemporaryDirectory() as directory:
            http = FakeTransport(Page(url, '<html>unrecognized page</html>', 200))
            browser = FakeTransport(Page(url, 'browser', 200))
            with patch('radar.transport.HttpTransport', return_value=http), patch('radar.transport.BrowserTransport', return_value=browser):
                for mode in ('auto', 'http'):
                    transport = AutoTransport(directory, mode=mode)
                    self.assertEqual(transport.fetch(url, {'fixture.example'}).text, '<html>unrecognized page</html>')
                    self.assertEqual(browser.reads, [])
                    transport.close()
                transport = AutoTransport(directory, mode='browser')
                self.assertEqual(transport.fetch(url, {'fixture.example'}).text, 'browser')
                transport.close()
                with self.assertRaises(ValueError):
                    AutoTransport(directory, mode='invalid')

    def test_crawl_delay_applies_across_http_and_browser_fallback(self):
        origin = 'https://fixture.example'
        class WaitingTransport(FakeTransport):
            def fetch(self, url, hosts, *, raw=False, allow_http_detail=False):
                self.throttle.wait(url)
                return super().fetch(url, hosts, raw=raw, allow_http_detail=allow_http_detail)

        def http_factory(*args, throttle, **kwargs):
            return WaitingTransport({
                origin + '/robots.txt': Page(origin + '/robots.txt', 'User-agent: *\nCrawl-delay: 4', 200, 'text/plain'),
                origin + '/list': FetchError('network', code='timeout'),
            }, throttle=throttle)

        def browser_factory(*args, throttle, **kwargs):
            return WaitingTransport(Page(origin + '/list', '', 200), throttle=throttle)

        with tempfile.TemporaryDirectory() as directory, patch('radar.transport.HttpTransport', side_effect=http_factory), patch(
                'radar.transport.BrowserTransport', side_effect=browser_factory), patch(
                'radar.transport.time.monotonic', return_value=100), patch('radar.transport.time.sleep') as sleep:
            transport = AutoTransport(directory, delay=0)
            try:
                self.assertTrue(Robots(transport).allowed(origin + '/list', {'fixture.example'}))
                transport.fetch(origin + '/list', {'fixture.example'})
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [4, 4])
            finally:
                transport.close()

    def test_http_rules_redirect_is_stopped_before_following(self):
        with local_server() as (server, base):
            transport = HttpTransport(timeout=2, retries=0, delay=0)
            try:
                for target in ('/other-rules', 'http://other.example/robots.txt'):
                    server.rules = (302, 'text/plain', target)
                    server.reads.clear()
                    self.assert_code('unsafe_redirect', lambda: transport.fetch(
                        base + '/robots.txt', {'127.0.0.1', 'other.example'}, raw=True))
                    self.assertEqual([path for path, _ in server.reads], ['/robots.txt'])
            finally:
                transport.close()

    def test_rules_redirect_does_not_expand_path_or_origin(self):
        for final in ('https://fixture.example/login', 'https://other.example/robots.txt', 'http://fixture.example/robots.txt'):
            transport = FakeTransport(Page(final, 'User-agent: *\nAllow: /', 200, 'text/plain'))
            self.assert_code('robots_invalid', lambda: Robots(transport).allowed('https://fixture.example/list', {'fixture.example', 'other.example'}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
