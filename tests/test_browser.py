"""Read-only guard regressions; real Chrome fixtures are explicitly opt-in."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.transport import BrowserTransport, FetchError, restore_browser_state, USER_AGENT
from radar.parsing import AccessIssue, extract_listing


class GuardTests(unittest.TestCase):
    class Request:
        def __init__(self, url, method='GET', kind='document'):
            self.url, self.method, self.resource_type = url, method, kind

    class Route:
        def __init__(self, request):
            self.request, self.result = request, None

        def abort(self):
            self.result = 'abort'

        def fallback(self):
            self.result = 'continue'

    def test_read_only_host_limits_and_authentication_navigation_code(self):
        transport = BrowserTransport.__new__(BrowserTransport)
        transport.hosts = {'eme.xidian.edu.cn'}
        transport.blocked_methods = 0
        cases = [
            ('https://eme.xidian.edu.cn/tz.htm', 'GET', 'document', 'continue'),
            ('https://eme.xidian.edu.cn/submit', 'POST', 'xhr', 'abort'),
            ('https://eme.xidian.edu.cn/logout', 'GET', 'document', 'abort'),
            ('https://other.example/image.png', 'GET', 'image', 'abort'),
            ('https://ids.xidian.edu.cn/authserver/login?ticket=private', 'GET', 'document', 'abort'),
            ('https://eme.xidian.edu.cn/?ticket=private', 'GET', 'document', 'abort'),
        ]
        for url, method, kind, expected in cases:
            transport._navigation_issue = None
            route = self.Route(self.Request(url, method, kind))
            transport._guard(route)
            self.assertEqual(route.result, expected)
            if '/authserver/login' in url:
                self.assertEqual(transport._navigation_issue.code, 'login_required')
                self.assertNotIn('private', str(transport._navigation_issue))
        self.assertEqual(transport.blocked_methods, 1)

    def test_http_detail_permission_only_applies_to_opted_in_fetches(self):
        transport = BrowserTransport.__new__(BrowserTransport)
        transport.hosts = {'eme.xidian.edu.cn'}
        transport.blocked_methods = 0
        transport._navigation_issue = None
        route = self.Route(self.Request('http://eme.xidian.edu.cn/info/1/1.htm'))
        transport._guard(route)
        self.assertEqual(route.result, 'abort')
        self.assertEqual(transport._navigation_issue.code, 'unsafe_redirect')

        transport._navigation_issue = None
        transport._allow_http = True
        route = self.Route(self.Request('http://eme.xidian.edu.cn/info/1/1.htm'))
        transport._guard(route)
        self.assertEqual(route.result, 'continue')

        # 规则文件 (raw) 的许可在 fetch 中始终为 False，HTTP 仍被拒绝。
        transport._navigation_issue = None
        transport._allow_http = False
        transport._raw_target = 'http://eme.xidian.edu.cn/robots.txt'
        route = self.Route(self.Request('http://eme.xidian.edu.cn/robots.txt'))
        transport._guard(route)
        self.assertEqual(route.result, 'abort')
        self.assertEqual(transport._navigation_issue.code, 'unsafe_redirect')

    def test_raw_rules_cannot_redirect_to_another_path_or_origin(self):
        transport = BrowserTransport.__new__(BrowserTransport)
        transport.hosts = {'eme.xidian.edu.cn', 'other.xidian.edu.cn'}
        transport.blocked_methods = 0
        transport._raw_target = 'https://eme.xidian.edu.cn/robots.txt'
        for url in ('https://eme.xidian.edu.cn/rules.txt',
                    'https://other.xidian.edu.cn/robots.txt',
                    'http://eme.xidian.edu.cn/robots.txt'):
            route = self.Route(self.Request(url))
            transport._guard(route)
            self.assertEqual(route.result, 'abort')
            self.assertEqual(transport._navigation_issue.code, 'unsafe_redirect')

    def test_late_previous_page_navigation_does_not_mask_current_connection_error(self):
        from types import SimpleNamespace
        transport = BrowserTransport.__new__(BrowserTransport)
        old_frame = SimpleNamespace(parent_frame=None)
        current_frame = SimpleNamespace(parent_frame=None)
        old_request = self.Request('https://old.xidian.edu.cn/robots.txt')
        old_request.frame = old_frame
        stale_route = self.Route(old_request)

        class Page:
            main_frame = current_frame

            def goto(self, *args, **kwargs):
                transport._guard(stale_route)
                raise RuntimeError('net::ERR_CONNECTION_RESET')

        transport.page = Page()
        transport._page_origin = ('https', 'current.xidian.edu.cn')
        transport.throttle = SimpleNamespace(wait=lambda url: None)
        transport.calls = transport.blocked_methods = 0
        transport.timeout = 1000
        with self.assertRaises(FetchError) as caught:
            transport.fetch('https://current.xidian.edu.cn/robots.txt',
                            {'current.xidian.edu.cn'}, raw=True)
        self.assertEqual(stale_route.result, 'abort')
        self.assertEqual(caught.exception.code, 'connection_failed')

    def test_invalid_state_never_overwrites_saved_file(self):
        class Context:
            def add_cookies(self, cookies):
                raise AssertionError('invalid cookies must not reach the browser')

        for text in ('not JSON', '[]', '{"cookies": "invalid"}',
                     '{"cookies":[{"name":"x","value":"secret","domain":"outside.example","path":"/"}]}'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'browser-state.json'
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(FetchError) as caught:
                    restore_browser_state(Context(), path)
                self.assertEqual(caught.exception.code, 'session_invalid')
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(path.read_text(encoding='utf-8'), text)


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'Set RUN_BROWSER_TESTS=1 to open Chrome')
class BrowserSmokeTests(unittest.TestCase):
    HOST = 'test-fixture.xidian.edu.cn'

    def install_fixture(self, transport, received):
        def fixture(route):
            request = route.request
            headers = request.all_headers()
            received.append((request.url, request.method, headers))
            if request.url.endswith('/redirect-login'):
                route.fulfill(status=302, headers={'Location': 'https://ids.xidian.edu.cn/authserver/login?ticket=fixture-secret'})
            elif request.url.endswith('/robots.txt'):
                route.fulfill(status=200, content_type='text/plain', body='User-agent: *\nAllow: /')
            elif request.url.endswith('/missing-rules'):
                route.fulfill(status=410, content_type='text/plain', body='gone')
            else:
                route.fulfill(status=200, content_type='text/html', body='<title>通知公告</title><a href="/info/1/2.htm">关于测试报名的通知</a>')

        # Guard runs first and falls through to this fixture. No fixture bypasses the guard.
        transport.context.unroute('**/*', transport._guard)
        transport.context.route('https://**/*', fixture)
        transport.context.route('**/*', transport._guard)

    def test_visible_render_and_session_cookie_is_consumed_after_reopen(self):
        with tempfile.TemporaryDirectory(prefix='xidian-browser-fixture-') as directory:
            root = Path(directory)
            transport = BrowserTransport(root, timeout=8, delay=0)
            received = []
            try:
                self.install_fixture(transport, received)
                page = transport.fetch(f'https://{self.HOST}/tzgg.htm', {self.HOST})
                listing = extract_listing(page.text, page.url, {self.HOST}, {})
                self.assertEqual(len(listing.candidates), 1)
                self.assertEqual(page.content_type, 'text/html')
                transport.context.add_cookies([{'name': 'radar_fixture', 'value': 'not-a-real-credential',
                                               'domain': self.HOST, 'path': '/', 'secure': True}])
                transport.page.evaluate("localStorage.setItem('radar_fixture_storage', 'profile-value')")
            finally:
                transport.close()
            restored = BrowserTransport(root, timeout=8, delay=0)
            received = []
            try:
                self.install_fixture(restored, received)
                restored.fetch(f'https://{self.HOST}/tzgg.htm', {self.HOST})
                document_headers = [headers for url, method, headers in received if url.endswith('/tzgg.htm')]
                self.assertTrue(document_headers)
                self.assertIn('radar_fixture=not-a-real-credential', document_headers[-1].get('cookie', ''))
                self.assertEqual(document_headers[-1].get('user-agent'), USER_AGENT)
                self.assertEqual(restored.page.evaluate("localStorage.getItem('radar_fixture_storage')"), 'profile-value')
            finally:
                restored.close()

    def test_real_guard_blocks_post_cross_site_and_auth_redirect(self):
        with tempfile.TemporaryDirectory(prefix='xidian-browser-guard-') as directory:
            transport = BrowserTransport(Path(directory), timeout=8, delay=0)
            received = []
            try:
                self.install_fixture(transport, received)
                transport.fetch(f'https://{self.HOST}/tzgg.htm', {self.HOST})
                for url, method in ((f'https://{self.HOST}/submit', 'POST'),
                                    ('https://outside.example/private', 'GET')):
                    outcome = transport.page.evaluate("""async ({url, method}) => {
                        try { await fetch(url, {method}); return 'allowed'; }
                        catch (_) { return 'blocked'; }
                    }""", {'url': url, 'method': method})
                    self.assertEqual(outcome, 'blocked')
                self.assertFalse(any(method == 'POST' or 'outside.example' in url for url, method, _ in received))
                with self.assertRaises(AccessIssue) as caught:
                    transport.fetch(f'https://{self.HOST}/redirect-login', {self.HOST})
                self.assertEqual(caught.exception.code, 'login_required')
                self.assertNotIn('fixture-secret', str(caught.exception))
                self.assertFalse(any('ids.xidian.edu.cn' in url for url, _, _ in received))
                self.assertEqual(transport.blocked_methods, 1)
            finally:
                transport.close()
            state_text = (Path(directory) / 'browser-state.json').read_text(encoding='utf-8')
            self.assertNotIn('fixture-secret', state_text)

    def test_browser_raw_plain_text_and_status(self):
        with tempfile.TemporaryDirectory(prefix='xidian-browser-rules-') as directory:
            transport = BrowserTransport(Path(directory), timeout=8, delay=0)
            try:
                self.install_fixture(transport, [])
                page = transport.fetch(f'https://{self.HOST}/robots.txt', {self.HOST}, raw=True)
                self.assertEqual(page.content_type, 'text/plain')
                self.assertIn('User-agent: *', page.text)
                self.assertNotIn('<html', page.text)
                page = transport.fetch(f'https://{self.HOST}/missing-rules', {self.HOST}, raw=True)
                self.assertEqual((page.status, page.content_type), (410, 'text/plain'))
            finally:
                transport.close()

    def test_failed_origin_does_not_poison_next_origin_rules(self):
        from urllib.parse import urlsplit
        old_host, next_host = 'failed-fixture.xidian.edu.cn', self.HOST
        with tempfile.TemporaryDirectory(prefix='xidian-browser-origin-') as directory:
            transport = BrowserTransport(Path(directory), timeout=8, delay=0)
            try:
                def fixture(route):
                    if urlsplit(route.request.url).hostname == old_host:
                        route.abort('connectionreset')
                    else:
                        route.fulfill(status=200, content_type='text/plain',
                                      body='User-agent: *\nAllow: /')
                transport.context.unroute('**/*', transport._guard)
                transport.context.route('https://**/*', fixture)
                transport.context.route('**/*', transport._guard)
                with self.assertRaises(FetchError) as caught:
                    transport.fetch(f'https://{old_host}/robots.txt', {old_host}, raw=True)
                self.assertEqual(caught.exception.code, 'connection_failed')
                page = transport.fetch(f'https://{next_host}/robots.txt', {next_host}, raw=True)
                self.assertEqual(page.status, 200)
                self.assertIn('User-agent: *', page.text)
            finally:
                transport.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
