from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import redirect_stdout
from datetime import date, timedelta
import errno
import hashlib
import http.client
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import web
from radar.config import json_read, save_config
from radar.storage import Store, app_lock, atomic_json
from radar.transport import FetchError
from test_radar import FixtureHandler
from http.server import ThreadingHTTPServer


def hold_task_lock(directory, ready, release):
    """A separate process models a CLI owning the real platform file lock."""
    with app_lock(Path(directory)):
        ready.set()
        release.wait(10)


class CountingFixture(FixtureHandler):
    reads = []
    robots_fail = False

    def do_GET(self):
        type(self).reads.append(self.path)
        if self.path in ('/board', '/board?offset=1'):
            number = 1 if self.path == '/board' else 2
            published = (date.today() - timedelta(days=10 if number == 1 else 20)).isoformat()
            text = f'''<section class="bulletins"><div>
                <a class="entry" href="/document/{number}">第{number}场材料提交安排</a>
                <span class="pub">发布日期：{published}</span></div></section>'''
            if number == 1: text += '<a class="forward" href="/board?offset=1">继续查看</a>'
            self.respond(text)
        elif self.path.startswith('/document/'):
            number = self.path.rsplit('/', 1)[1]
            published = (date.today() - timedelta(days=10 if number == '1' else 20)).isoformat()
            self.respond(f'''<div class="doc-title">第{number}场材料提交安排</div>
                <div class="doc-date">{published}</div><div class="detailtext">请在规定时间提交材料，第{number}场详细要求。</div>''')
        elif self.path == '/robots.txt' and type(self).robots_fail:
            self.respond('unavailable', 503, 'text/plain')
        else:
            super().do_GET()


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(('127.0.0.1', 0), CountingFixture)
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever, daemon=True)
        cls.fixture_thread.start()
        cls.origin = f'http://127.0.0.1:{cls.fixture.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.fixture.shutdown()
        cls.fixture.server_close()
        cls.fixture_thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'sources.json'
        self.data, self.output = self.root / 'private', self.root / 'output'
        source = {'id': 'T01', 'name': '测试学院', 'url': self.origin + '/',
                  'allowed_hosts': ['127.0.0.1'], 'seed_urls': [self.origin + '/tzgg.htm'], 'enabled': True}
        atomic_json(self.config, {'sources': [source, {**source, 'id': 'T02', 'name': '备选学院', 'enabled': False}]})
        CountingFixture.version = 1
        CountingFixture.third = CountingFixture.robots_block = CountingFixture.robots_fail = False
        CountingFixture.reads = []
        self.start_server()

    def start_server(self):
        self.controller = web.ConsoleController(self.config, self.data, self.output, delay=0, timeout=2)
        with self.controller.store():
            pass
        self.server = web.create_server(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(5)
        self.controller.close()
        self.server.server_close()

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, headers=None, auth=True, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        outgoing = {}
        if auth:
            outgoing['X-Radar-Token'] = self.controller.token
        if method != 'GET':
            outgoing['Origin'] = f'http://127.0.0.1:{self.server.server_port}'
            outgoing['Content-Type'] = 'application/json'
        outgoing.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            connection.request(method, path, body=data, headers=outgoing)
            response = connection.getresponse()
            payload = response.read()
            result = json.loads(payload) if payload and response.getheader('Content-Type', '').startswith('application/json') else payload
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def state(self):
        status, state, _ = self.request('GET', '/api/state')
        self.assertEqual(status, 200, state)
        return state

    def wait_job(self, statuses=('completed', 'failed', 'cancelled')):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.state()['job']
            if job['status'] in statuses:
                return job
            time.sleep(.02)
        self.fail('Console task did not reach the expected state')

    def collect(self):
        status, accepted, _ = self.request('POST', '/api/collect', {'mode': 'http', 'days': 30})
        self.assertEqual(status, 202, accepted)
        job = self.wait_job()
        self.assertEqual(job['status'], 'completed', job)
        return job

    def test_security_boundaries_and_no_network_on_open(self):
        status, page, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<!', page)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        for path in ('/console.js', '/console.css'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.request('GET', '/favicon.ico')[0], 204)
        self.assertEqual(self.state()['job']['status'], 'idle')
        self.assertEqual(self.request('GET', '/api/notices')[1]['items'], [])
        self.assertEqual(CountingFixture.reads, [])
        self.assertEqual(self.request('OPTIONS', '/api/state', {}, auth=False)[0], 403)
        self.assertEqual(self.request('UNRECOGNIZED', '/api/state', {})[0], 404)
        for path in ('/api/state', '/api/sources', '/api/notices', '/api/missing'):
            self.assertEqual(self.request('GET', path, auth=False)[0], 403)
            self.assertEqual(self.request('GET', path, headers={'X-Radar-Token': 'incorrect'})[0], 403)
            self.assertEqual(self.request('GET', path, headers={'Host': 'evil.example'})[0], 403)
        for origin in ('null', 'file://', 'http://evil.example', 'http://localhost:' + str(self.server.server_port)):
            self.assertEqual(self.request('POST', '/api/collect', {}, headers={'Origin': origin})[0], 403)
        for path in ('/../sources.json', '/%2e%2e/sources.json', '/radar/web.py', '/private/browser-state.json', '/sources.json', '/console/connection.json'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('POST', '/api/collect', raw=b'x' * 65537)[0], 413)
        self.assertEqual(self.request('POST', '/api/collect', raw=b'{')[0], 400)
        self.assertEqual(self.request('POST', '/api/collect', raw=b'{"days":1,"days":2}')[0], 400)
        self.assertEqual(self.request('POST', '/api/collect', {}, headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request('POST', '/api/collect', {'path': 'private'})[0], 400)
        for body in ({'mode': []}, {'days': True}, {'days': -1}, {'mode': 'shell'}, {'days': 9999999999}):
            self.assertEqual(self.request('POST', '/api/collect', body)[0], 400)
        self.assertEqual(self.request('POST', '/api/missing', {})[0], 404)
        self.assertEqual(CountingFixture.reads, [])

    def test_omitted_enabled_is_explicitly_true_in_effective_sources(self):
        config = json_read(self.config)
        del config['sources'][0]['enabled']
        atomic_json(self.config, config)
        raw = self.config.read_bytes()
        status, sources, _ = self.request('GET', '/api/sources')
        self.assertEqual(status, 200)
        self.assertIs(sources['sources'][0]['enabled'], True)
        self.assertIs(sources['sources'][1]['enabled'], False)
        self.assertEqual(self.config.read_bytes(), raw)

    def test_sources_revisions_backups_and_dangerous_inputs(self):
        _, initial, _ = self.request('GET', '/api/sources')
        self.assertEqual(initial['revision'], hashlib.sha256(self.config.read_bytes()).hexdigest())
        status, saved, _ = self.request('PUT', '/api/sources', {'revision': initial['revision'], 'enabled_ids': ['T02']})
        self.assertEqual(status, 200, saved)
        self.assertEqual([s['id'] for s in json_read(self.config)['sources'] if s['enabled']], ['T02'])
        self.assertTrue(list(self.root.glob('sources.backup-*.json')))
        self.assertEqual(self.request('PUT', '/api/sources', {'revision': initial['revision'], 'enabled_ids': ['T01']})[0], 409)
        for ids in ([], ['unknown'], 'T01', [False]):
            self.assertEqual(self.request('PUT', '/api/sources', {'revision': saved['revision'], 'enabled_ids': ids})[0], 400)
        for url in ('file:///secret', 'https://a.xidian.edu.cn/logout', 'https://a.xidian.edu.cn/?ticket=secret', 'https://user:pass@a.xidian.edu.cn/'):
            self.assertEqual(self.request('POST', '/api/sources', {'revision': saved['revision'], 'name': '入口', 'url': url})[0], 400)
        status, added, _ = self.request('POST', '/api/sources', {'revision': saved['revision'], 'name': '手动入口', 'url': 'https://math.xidian.edu.cn/'})
        self.assertEqual(status, 201, added)
        self.assertTrue(any(s['name'] == '手动入口' and s['id'].startswith('M') for s in added['sources']))
        external = json_read(self.config)
        external['sources'][0]['name'] = '外部修改'
        save_config(self.config, external)
        self.assertEqual(self.request('PUT', '/api/sources', {'revision': added['revision'], 'enabled_ids': ['T01']})[0], 409)
        self.assertEqual(json_read(self.config)['sources'][0]['name'], '外部修改')

    def test_rule_permission_is_explicit_revocable_and_resets_with_url(self):
        _, current, _ = self.request('GET', '/api/sources')
        self.assertFalse(any(source['allow_robots_unavailable'] for source in current['sources']))
        status, saved, _ = self.request('PUT', '/api/sources', {
            'revision': current['revision'], 'enabled_ids': ['T01'], 'robots_unavailable_ids': ['T01']})
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved['sources'][0]['allow_robots_unavailable'])
        self.assertFalse(saved['sources'][1]['allow_robots_unavailable'])
        self.assertTrue(json_read(self.config)['sources'][0]['allow_robots_unavailable'])
        before = self.config.read_bytes()
        for ids in (None, 'T01', [True], ['unknown']):
            with self.subTest(ids=ids):
                self.assertEqual(self.request('PUT', '/api/sources', {
                    'revision': saved['revision'], 'enabled_ids': ['T01'], 'robots_unavailable_ids': ids})[0], 400)
                self.assertEqual(self.config.read_bytes(), before)
        # An older selection-only client must not silently revoke a saved permission.
        status, legacy, _ = self.request('PUT', '/api/sources', {
            'revision': saved['revision'], 'enabled_ids': ['T02']})
        self.assertEqual(status, 200, legacy)
        self.assertTrue(legacy['sources'][0]['allow_robots_unavailable'])
        status, changed, _ = self.request('PUT', '/api/sources', {
            'revision': legacy['revision'], 'enabled_ids': ['T01'],
            'source_urls': {'T01': self.origin + '/new-entry'}})
        self.assertEqual(status, 200, changed)
        self.assertFalse(changed['sources'][0]['allow_robots_unavailable'])
        self.assertEqual(self.request('PUT', '/api/sources', {
            'revision': saved['revision'], 'enabled_ids': ['T01'], 'robots_unavailable_ids': ['T01']})[0], 409)
        status, confirmed, _ = self.request('PUT', '/api/sources', {
            'revision': changed['revision'], 'enabled_ids': ['T01'], 'robots_unavailable_ids': ['T01']})
        self.assertEqual(status, 200, confirmed)
        self.assertTrue(confirmed['sources'][0]['allow_robots_unavailable'])
        status, revoked, _ = self.request('PUT', '/api/sources', {
            'revision': confirmed['revision'], 'enabled_ids': ['T01'], 'robots_unavailable_ids': []})
        self.assertEqual(status, 200, revoked)
        self.assertFalse(any(source['allow_robots_unavailable'] for source in revoked['sources']))
        self.assertEqual(CountingFixture.reads, [])

    def test_edit_existing_source_url_uses_new_entry_and_keeps_history(self):
        self.collect()
        previous = {item['key']: item['first_seen'] for item in self.request('GET', '/api/notices')[1]['items']}
        original = json_read(self.config)
        before = self.config.read_bytes()
        _, current, _ = self.request('GET', '/api/sources')
        url = self.origin + '/tzgg/2.htm'
        status, saved, _ = self.request('PUT', '/api/sources', {
            'revision': current['revision'], 'enabled_ids': ['T01'], 'source_urls': {'T01': url}})
        self.assertEqual(status, 200, saved)
        updated = json_read(self.config)
        self.assertEqual(updated['sources'][0]['url'], url)
        self.assertEqual(updated['sources'][0]['seed_urls'], [url])
        self.assertEqual(updated['sources'][1], original['sources'][1])
        self.assertTrue(any(path.read_bytes() == before for path in self.root.glob('sources.backup-*.json')))
        self.assertEqual(self.request('PUT', '/api/sources', {
            'revision': current['revision'], 'enabled_ids': ['T01'],
            'source_urls': {'T01': self.origin + '/obsolete'}})[0], 409)
        CountingFixture.reads = []
        self.collect()
        self.assertIn('/tzgg/2.htm', CountingFixture.reads)
        self.assertNotIn('/tzgg.htm', CountingFixture.reads)
        self.assertEqual({item['key']: item['first_seen'] for item in
                          self.request('GET', '/api/notices')[1]['items']}, previous)

    def test_edit_source_url_changes_host_boundary_without_requesting_website(self):
        _, current, _ = self.request('GET', '/api/sources')
        url = 'https://updated.example.edu.cn/notices.htm'
        status, saved, _ = self.request('PUT', '/api/sources', {
            'revision': current['revision'], 'enabled_ids': ['T01'], 'source_urls': {'T01': url}})
        self.assertEqual(status, 200, saved)
        source = saved['sources'][0]
        self.assertEqual(source['allowed_hosts'], ['updated.example.edu.cn'])
        self.assertEqual(source['seed_urls'], [url])
        self.assertEqual(CountingFixture.reads, [])

    def test_url_edits_reject_unsafe_or_unknown_entries_atomically(self):
        _, current, _ = self.request('GET', '/api/sources')
        before = self.config.read_bytes()
        invalid = [None, [], {'missing': 'https://example.edu.cn/'}, {'T01': 42},
                   {'T01': 'https://safe.example.edu.cn/', 'T02': 'https://example.edu.cn/?ticket=secret'},
                   {'T01': 'https://example.edu.cn/logout'}, {'T01': 'file:///private'},
                   {'T01': 'https://user:pass@example.edu.cn/'}, {'T01': 'https://news.xidian.edu.cn/'}]
        for urls in invalid:
            with self.subTest(urls=urls):
                status, error, _ = self.request('PUT', '/api/sources', {
                    'revision': current['revision'], 'enabled_ids': ['T01'], 'source_urls': urls})
                self.assertEqual(status, 400, error)
                self.assertNotIn('secret', json.dumps(error))
                self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.root.glob('sources.backup-*.json')), [])

    def test_seven_and_fifteen_day_collection_filter_real_detail_requests(self):
        today = date.today()
        ages = {201: 6, 202: 10, 203: 20, 204: None}
        def dated_fixture(handler):
            CountingFixture.reads.append(handler.path)
            if handler.path == '/robots.txt':
                handler.respond('', mime='text/plain')
            elif handler.path == '/range.htm':
                rows = []
                for key, age in ages.items():
                    stamp = (today - timedelta(days=age)).isoformat() if age is not None else ''
                    rows.append(f'<li><a href="/info/1/{key}.htm">第{key}场报名通知</a><time>{stamp}</time></li>')
                handler.respond('<title>通知公告</title><ul>' + ''.join(rows) + '</ul>')
            elif handler.path.startswith('/info/1/'):
                key = int(handler.path.rsplit('/', 1)[1].split('.')[0])
                age = ages[key]
                stamp = (today - timedelta(days=age)).isoformat() if age is not None else ''
                handler.respond(f'<meta name="PubDate" content="{stamp}"><h1>第{key}场报名通知</h1>'
                                f'<div class="v_news_content">第{key}场的报名要求与具体材料。</div>')
            else:
                handler.respond('not found', 404)
        _, current, _ = self.request('GET', '/api/sources')
        self.assertEqual(self.request('PUT', '/api/sources', {
            'revision': current['revision'], 'enabled_ids': ['T01'],
            'source_urls': {'T01': self.origin + '/range.htm'}})[0], 200)
        with patch.object(CountingFixture, 'do_GET', dated_fixture):
            for days, expected in ((7, {201, 204}), (15, {201, 202, 204})):
                CountingFixture.reads = []
                status, accepted, _ = self.request('POST', '/api/collect', {'mode': 'http', 'days': days})
                self.assertEqual(status, 202, accepted)
                self.assertEqual(self.wait_job()['status'], 'completed')
                self.assertEqual(self.state()['latest']['days'], days)
                requested = {int(path.rsplit('/', 1)[1].split('.')[0])
                             for path in CountingFixture.reads if path.startswith('/info/1/')}
                self.assertEqual(requested, expected)
                notices = self.request('GET', f'/api/notices?days={days}')[1]
                self.assertEqual({int(item['title'].split('场')[0][1:]) for item in notices['items']}, expected)
                self.assertIsNone(next(item['published_date'] for item in notices['items'] if '204' in item['title']))

    def test_real_collection_changes_search_and_restart(self):
        self.assertIsNone(self.state()['latest'])
        self.assertFalse(self.request('GET', '/api/notices?view=changes')[1]['recorded'])
        first = self.collect()
        self.assertEqual(first['result_summary']['outcome'], 'checked')
        recent = self.request('GET', '/api/notices')[1]
        self.assertEqual(recent['total'], 2)
        self.assertEqual({item['state'] for item in recent['items']}, {'baseline'})
        self.collect()
        self.assertEqual(self.request('GET', '/api/notices?view=changes')[1]['total'], 0)
        self.assertEqual(self.request('GET', '/api/notices')[1]['total'], 2)
        CountingFixture.version, CountingFixture.third = 2, True
        self.collect()
        changes = self.request('GET', '/api/notices?view=changes')[1]
        self.assertEqual({item['state'] for item in changes['items']}, {'new', 'updated'})
        self.assertTrue(all(item['last_checked_at'] and item['first_seen'] for item in changes['items']))
        self.assertEqual(self.request('GET', '/api/notices?limit=1&offset=1')[1]['total'], 3)
        self.assertEqual(len(self.request('GET', '/api/notices?limit=1&offset=1')[1]['items']), 1)
        self.assertEqual(self.request('GET', '/api/notices?q=%E7%AC%AC%E4%B8%89')[1]['total'], 1)
        self.assertEqual(self.request('GET', '/api/notices?source_id=T02')[1]['total'], 0)
        self.assertEqual(self.request('GET', '/api/notices?source_id=T01')[1]['total'], 3)
        for query in ('days=-1', 'limit=101', 'offset=x', 'view=all', 'limit=0', 'days=999999999', 'q=a&q=b', 'path=private'):
            self.assertEqual(self.request('GET', '/api/notices?' + query)[0], 400)
        latest = self.state()['latest']
        self.assertNotIn('items', latest)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.state()['job']['status'], 'idle')
        self.assertEqual(self.state()['latest'], latest)
        self.assertEqual(self.request('GET', '/api/notices')[1]['total'], 3)
        # A valid robots denial fails a new run but never destroys committed notices.
        CountingFixture.robots_block = True
        # Fixture's normal Disallow only blocks /blocked; explicitly deny the source here.
        with patch.object(CountingFixture, 'do_GET', lambda handler: handler.respond('User-agent: *\nDisallow: /', 200, 'text/plain')):
            failed = self.collect()
        self.assertEqual(failed['result_summary']['outcome'], 'failed')
        self.assertEqual(self.state()['latest']['outcome'], 'failed')
        self.assertEqual(self.request('GET', '/api/notices')[1]['total'], 3)

    def test_custom_site_rules_collect_real_pages_and_reject_invalid_updates(self):
        revision = self.request('GET', '/api/sources')[1]['revision']
        rules = {'seed_urls': [self.origin+'/board'], 'list_selector': '.bulletins',
                 'detail_link_selector': 'a.entry', 'list_date_selector': '.pub',
                 'next_page_selector': 'a.forward', 'title_selector': '.doc-title',
                 'date_selector': '.doc-date', 'content_selector': '.detailtext'}
        status, saved, _ = self.request('PUT', '/api/sources', {'revision': revision,
            'enabled_ids': ['T02'], 'source_rules': {'T02': rules}})
        self.assertEqual(status, 200)
        self.assertEqual(CountingFixture.reads, [])
        before = self.config.read_bytes()
        for bad in ({'list_selector': 'a['}, {'seed_urls': ['https://outside.example/list']},
                    {'request_headers': {'Cookie': 'not-accepted'}}):
            self.assertEqual(self.request('PUT', '/api/sources', {'revision': saved['revision'],
                'enabled_ids': ['T02'], 'source_rules': {'T02': bad}})[0], 400)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.request('POST', '/api/collect', {'mode': 'http', 'days': 15})[0], 202)
        self.assertEqual(self.wait_job()['status'], 'completed')
        recent = self.request('GET', '/api/notices?days=15')[1]
        self.assertEqual([item['title'] for item in recent['items']], ['第1场材料提交安排'])
        self.assertEqual(recent['items'][0]['summary'], '请在规定时间提交材料，第1场详细要求。')
        self.assertIn('/board?offset=1', CountingFixture.reads)
        self.assertNotIn('/document/2', CountingFixture.reads)
        self.assertEqual(self.request('GET', '/api/notices?days=7')[1]['total'], 0)
        self.assertEqual(self.request('POST', '/api/collect', {'mode': 'http', 'days': 0})[0], 202)
        self.assertEqual(self.wait_job()['status'], 'completed')
        self.assertEqual(self.request('GET', '/api/notices?days=0')[1]['total'], 2)
        status, _, _ = self.request('PUT', '/api/sources', {'revision': saved['revision'],
            'enabled_ids': ['T02'], 'source_urls': {'T02': self.origin+'/new-board'}})
        self.assertEqual(status, 200)
        entry = next(source for source in json_read(self.config)['sources'] if source['id']=='T02')
        self.assertNotIn('detail_link_selector', entry)
        self.assertEqual(entry['seed_urls'], [self.origin+'/new-board'])
        self.assertEqual(self.request('GET', '/api/notices?days=0')[1]['total'], 2)

    def test_single_job_cancellation_rollback_and_locks(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(config_path, data_dir, output, options, **kwargs):
            with app_lock(data_dir):
                store = Store(data_dir / 'notices.sqlite3')
                try:
                    store.remember({'key': 'uncommitted', 'title': '临时通知', 'url': 'https://example.com/a',
                        'published_date': date.today().isoformat(), 'summary': '临时', 'content_hash': 'x'}, False)
                    kwargs['progress']({'phase': 'details', 'source_id': 'T01', 'completed_sources': 0,
                                        'total_sources': 1, 'page_reads': 1, 'detail_attempts': 1})
                    entered.set()
                    while not release.wait(.02):
                        if kwargs['cancel_event'].is_set():
                            raise CancelledError()
                    raise OSError('private failure must not leak')
                finally:
                    store.rollback()
                    store.close()
        try:
            with patch.object(web, 'run_collection', blocked):
                status, accepted, _ = self.request('POST', '/api/collect', {})
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.request('POST', '/api/collect', {})[0], 409)
                self.assertEqual(self.request('POST', '/api/login', {})[0], 409)
                revision = self.request('GET', '/api/sources')[1]['revision']
                self.assertEqual(self.request('PUT', '/api/sources', {'revision': revision, 'enabled_ids': ['T02']})[0], 409)
                self.assertEqual(self.request('POST', '/api/shutdown', {})[0], 409)
                self.assertEqual(self.request('GET', '/api/notices')[1]['total'], 0)
                self.assertIsNone(self.state()['latest'])
                self.assertEqual(self.state()['job']['progress']['phase'], 'details')
                self.assertEqual(self.request('POST', '/api/cancel', {'job_id': 'old'})[0], 409)
                self.assertEqual(self.request('POST', '/api/cancel', accepted)[0], 202)
                self.assertEqual(self.wait_job()['status'], 'cancelled')
                self.assertEqual(self.request('POST', '/api/cancel', accepted)[0], 409)
                self.assertEqual(self.request('GET', '/api/notices')[1]['total'], 0)
                release.set()
                self.assertEqual(self.request('POST', '/api/collect', {})[0], 202)
                failed = self.wait_job()
                self.assertEqual(failed['status'], 'failed')
                self.assertNotIn('private failure', json.dumps(failed))
                self.assertIsNone(self.state()['latest'])
        finally:
            release.set()
        with app_lock(self.data):
            self.assertEqual(self.request('POST', '/api/collect', {})[0], 409)
            self.assertEqual(self.request('POST', '/api/login', {})[0], 409)
            self.assertEqual(self.request('PUT', '/api/sources', {'revision': revision, 'enabled_ids': ['T01']})[0], 409)

    def test_cross_process_cli_lock_is_an_immediate_conflict(self):
        context = multiprocessing.get_context('spawn')
        ready, release = context.Event(), context.Event()
        process = context.Process(target=hold_task_lock, args=(str(self.data), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            for route in ('/api/collect', '/api/login'):
                self.assertEqual(self.request('POST', route, {})[0], 409)
            revision = self.request('GET', '/api/sources')[1]['revision']
            self.assertEqual(self.request('PUT', '/api/sources', {
                'revision': revision, 'enabled_ids': ['T02']})[0], 409)
            self.assertEqual(self.state()['job']['status'], 'idle')
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)

    def test_config_and_database_errors_are_not_empty_success(self):
        with patch.object(web, 'Store', side_effect=sqlite3.DatabaseError('private details')):
            for route in ('/api/state', '/api/notices', '/api/notices?view=changes'):
                status, body, headers = self.request('GET', route)
                self.assertEqual(status, 503)
                self.assertEqual(body['error']['code'], 'storage_unavailable')
                self.assertNotIn('private details', json.dumps(body))
                self.assertEqual(headers['Cache-Control'], 'no-store')
        self.config.write_text('{', encoding='utf-8')
        self.assertEqual(self.request('GET', '/api/sources')[0], 503)
        self.assertEqual(self.request('POST', '/api/collect', {})[0], 503)

    def test_login_lifecycle_signals_and_worker_thread(self):
        calls, instances = [], []
        class Session:
            closed = False
            fail_save = False
            def __init__(session, data_dir, **kwargs):
                session.path = data_dir
                session.lock = None
                instances.append(session)
            def open(session, url):
                calls.append(('open', threading.get_ident(), url))
                session.lock = app_lock(session.path)
                session.lock.__enter__()
            def pump(session, timeout_ms=200):
                calls.append(('pump', threading.get_ident(), timeout_ms))
                time.sleep(.01)
                return not session.closed
            def finish(session):
                calls.append(('finish', threading.get_ident(), None))
                if session.fail_save:
                    raise FetchError('保存失败', code='session_invalid')
                atomic_json(session.path / 'browser-state.json', {'saved': True})
            def cancel(session):
                calls.append(('cancel', threading.get_ident(), None))
                if session.lock:
                    session.lock.__exit__(None, None, None)
                    session.lock = None
        self.data.mkdir(exist_ok=True)
        old_state = self.data / 'browser-state.json'
        atomic_json(old_state, {'old': True})
        with patch.object(web, 'ManualLoginSession', Session):
            # HTTP/non-school configured origins cannot be passed to the visible browser.
            self.assertEqual(self.request('POST', '/api/login', {'source_id': 'T01'})[0], 400)
            self.assertEqual(self.request('POST', '/api/login', {'url': 'https://evil.example'})[0], 400)
            status, first, _ = self.request('POST', '/api/login', {})
            self.assertEqual(status, 202)
            self.wait_job(('awaiting_login',))
            self.assertEqual(self.request('POST', '/api/login/finish', {'job_id': 'old'})[0], 409)
            self.assertEqual(self.request('POST', '/api/login/finish', first)[0], 202)
            self.assertEqual(self.wait_job()['status'], 'completed')
            self.assertEqual(json_read(old_state), {'saved': True})
            status, second, _ = self.request('POST', '/api/login', {})
            self.assertEqual(status, 202)
            self.wait_job(('awaiting_login',))
            self.assertEqual(self.request('POST', '/api/login/finish', first)[0], 409)
            self.assertEqual(self.request('POST', '/api/cancel', second)[0], 202)
            self.assertEqual(self.wait_job()['status'], 'cancelled')
            self.request('POST', '/api/login', {})
            self.wait_job(('awaiting_login',))
            instances[-1].closed = True
            self.assertEqual(self.wait_job()['status'], 'cancelled')
            _, fourth, _ = self.request('POST', '/api/login', {})
            self.wait_job(('awaiting_login',))
            instances[-1].fail_save = True
            self.request('POST', '/api/login/finish', fourth)
            failed = self.wait_job()
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['error']['code'], 'session_invalid')
            self.assertEqual(json_read(old_state), {'saved': True})
        self.controller.executor.submit(lambda: None).result(timeout=3)
        with app_lock(self.data):
            pass
        self.assertEqual(len({entry[1] for entry in calls}), 1)
        self.assertNotEqual(calls[0][1], threading.get_ident())
        self.assertTrue(any(entry[0] == 'finish' for entry in calls))
        self.assertEqual(sum(entry[0] == 'cancel' for entry in calls), 4)
        self.assertTrue(self.state()['session_available'])

    def test_verified_singleton_reuse_and_metadata_rejection(self):
        path = self.data / 'console' / 'connection.json'
        metadata = {'instance_id': self.controller.instance_id, 'port': self.server.server_port,
                    'token': self.controller.token, 'config_path': str(self.config.resolve()),
                    'output_path': str(self.output.resolve())}
        atomic_json(path, metadata)
        self.assertIsNotNone(web._existing_connection(path, self.config.resolve(), self.output.resolve()))
        with app_lock(self.data / 'console'), patch.object(web.webbrowser, 'open') as opened, redirect_stdout(io.StringIO()) as messages:
            self.assertEqual(web.serve_console(self.config, self.data, self.output), 0)
            opened.assert_called_once()
            self.assertNotIn(self.controller.token, messages.getvalue())
            with self.assertRaises(RuntimeError):
                web.serve_console(self.config, self.data, self.root / 'different-output', open_browser=False)
        for field in ('token', 'instance_id'):
            atomic_json(path, {**metadata, field: 'stale'})
            self.assertIsNone(web._existing_connection(path, self.config.resolve(), self.output.resolve()))
        self.assertIsNone(web._existing_connection(path, self.config.resolve(), self.root / 'other'))

    def test_sources_and_task_start_are_serialized(self):
        inside, release = threading.Event(), threading.Event()
        real_save = web.save_config
        def paused_save(*args):
            inside.set()
            release.wait(3)
            return real_save(*args)
        revision = self.request('GET', '/api/sources')[1]['revision']
        results = []
        def save():
            results.append(self.request('PUT', '/api/sources', {'revision': revision, 'enabled_ids': ['T02']}))
        def collect():
            results.append(self.request('POST', '/api/collect', {'mode': 'http'}))
        with patch.object(web, 'save_config', paused_save):
            saving = threading.Thread(target=save)
            starting = threading.Thread(target=collect)
            saving.start()
            self.assertTrue(inside.wait(2))
            starting.start()
            try:
                self.assertEqual(self.controller.job['status'], 'idle')
            finally:
                release.set()
                saving.join(5)
                starting.join(5)
        self.assertEqual(sorted(result[0] for result in results), [200, 202])
        self.assertEqual(self.wait_job()['status'], 'completed')
        self.assertEqual(self.state()['latest']['coverage'][0]['id'], 'T02')


class ConsoleLifetimeTests(unittest.TestCase):
    def test_default_occupied_port_falls_back_without_touching_other_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'sources.json'
            atomic_json(config, {'sources': [{'id': 'T01', 'url': 'https://example.com/'}]})
            real_create = web.create_server
            ports = []
            def bind(controller, *, port):
                ports.append(port)
                if port == 8765:
                    raise OSError(errno.EADDRINUSE, 'occupied')
                server = real_create(controller, port=port)
                server.serve_forever = lambda **kwargs: None
                return server
            with patch.object(web, 'create_server', bind), redirect_stdout(io.StringIO()):
                self.assertEqual(web.serve_console(config, root / 'private', root / 'output', open_browser=False), 0)
            self.assertEqual(ports, [8765, 0])
            self.assertFalse((root / 'private' / 'console' / 'connection.json').exists())

    def test_stale_metadata_shutdown_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, data, output = root / 'sources.json', root / 'private', root / 'output'
            atomic_json(config, {'sources': [{'id': 'T01', 'name': '离线', 'url': 'https://example.com/', 'enabled': True}]})
            path = data / 'console' / 'connection.json'
            atomic_json(path, {'stale': True})
            errors = []
            def run():
                try:
                    web.serve_console(config, data, output, port=0, open_browser=False)
                except Exception as error:
                    errors.append(error)
            previous_token = None
            for _ in range(2):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 5
                metadata = None
                while time.monotonic() < deadline:
                    try:
                        value = json_read(path)
                        if value.get('token') and value.get('token') != previous_token:
                            metadata = value
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.02)
                self.assertIsNotNone(metadata, errors)
                previous_token = metadata['token']
                connection = http.client.HTTPConnection('127.0.0.1', metadata['port'], timeout=3)
                connection.request('POST', '/api/shutdown', body=b'{}', headers={
                    'X-Radar-Token': metadata['token'], 'Origin': f"http://127.0.0.1:{metadata['port']}",
                    'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertFalse(path.exists())
                with app_lock(data / 'console'):
                    pass


if __name__ == '__main__':
    unittest.main()
