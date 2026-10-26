from __future__ import annotations

from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import CancelledError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar.parsing import identity
from radar.engine import Collector, Options
from radar.storage import Store
from radar.transport import HttpTransport


class CollectionHandler(BaseHTTPRequestHandler):
    count = 26
    success_path = ''
    fail_details = False
    robots_disconnect = False
    fail_history_page = ''
    requests = []

    def log_message(self,*args): pass

    def do_GET(self):
        type(self).requests.append(self.path)
        if self.path == '/robots.txt' and self.robots_disconnect:
            self.close_connection = True
            return
        today = date.today().isoformat()
        status, mime = 200, 'text/html'
        if self.path == self.fail_history_page:
            payload = b'unavailable'
            self.send_response(503); self.send_header('Content-Type', 'text/plain'); self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/robots.txt': text, mime = '', 'text/plain'
        elif self.path == '/login-required':
            text = '<title>统一身份认证</title><input type="password">'
        elif self.path == '/forbidden':
            status, text = 403, 'access denied'
        elif self.path == '/tz.htm':
            text = '<title>学院</title><div><a href="/info/1/999.htm">旧的报名通知</a><span>2020-01-01</span></div>'
            text += f'<div><a href="/info/1/1.htm">新的报名通知</a><span>{today}</span></div><a href="/tz/2.htm">下一页</a>'
        elif self.path == '/tz/2.htm':
            text = '<title>学院</title><a href="/info/1/2.htm">国家助学金申请安排</a><a href="/info/1/888.htm">学院成功举办活动</a>'
        elif self.path == '/sections':
            text = '<title>学院</title>'+''.join(f'<a href="/s{i}/tz.htm">通知公告</a>' for i in range(9))
        elif self.path.startswith('/s') and self.path.endswith('/tz.htm'):
            i = self.path.split('/')[1][1:]
            text = f'<title>通知公告</title><a href="/info/1/{int(i)+1}.htm">栏目{i}报名通知</a>'
        elif self.path == '/history/tz.htm' or self.path in ('/history/2.htm', '/history/3.htm', '/history/4.htm'):
            page = 1 if self.path.endswith('/tz.htm') else int(self.path.rsplit('/', 1)[-1].split('.')[0])
            published = date.today() - timedelta(days=(0, 10, 20, 100)[page-1])
            text = f'''<title>通知公告</title><li><a href="/info/1/{page}.htm" title="第{page}场报名通知">
                <div class="date"><b>{published.day}</b><span>{published:%Y-%m}</span></div>第{page}场报名通知</a></li>'''
            if page < 4: text += f'<a href="/history/{page+1}.htm" rel="next"><i></i></a>'
        elif self.path == '/many/tz.htm':
            text = '<title>通知公告</title>'+''.join(f'<div><a href="/info/1/{i}.htm">第{i}场报名通知</a><time>{today}</time></div>' for i in range(1,self.count+1))
        elif self.path.startswith('/info/1/'):
            number = self.path.split('/')[-1].split('.')[0]
            if self.fail_details and self.path != self.success_path: status, text = 503, 'unavailable'
            else:
                title = '国家助学金申请安排' if number == '2' else f'第{number}场报名通知'
                detail_date = ((date.today() - timedelta(days=(0, 10, 20, 100)[int(number)-1])).isoformat()
                               if number in ('1', '2', '3', '4') and any(path.startswith('/history/') for path in self.requests) else today)
                text = f'<meta name="PubDate" content="{detail_date}"><h1>{title}</h1><div class="v_news_content">'+('详细报名条件和材料要求。'*12)+f'场次{number}</div>'
        else: status, text = 404, 'not found'
        payload = text.encode('utf-8')
        self.send_response(status); self.send_header('Content-Type',mime+'; charset=utf-8'); self.end_headers()
        self.wfile.write(payload)


class CollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1',0),CollectionHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        CollectionHandler.count = 26; CollectionHandler.fail_details = False; CollectionHandler.requests = []
        CollectionHandler.robots_disconnect = False
        CollectionHandler.fail_history_page = ''

    def tearDown(self): self.temp.cleanup()

    def source(self,sid='T',path='/many/tz.htm'):
        return {'id':sid,'name':'测试'+sid,'url':self.origin+path,'seed_urls':[self.origin+path],'allowed_hosts':['127.0.0.1']}

    def collect(self,sources=None,options=None,**kwargs):
        store = Store(self.root/'db.sqlite3'); transport = HttpTransport(timeout=2,delay=.2,retries=0)
        try:
            with patch.object(transport.throttle,'wait'):
                return Collector(transport,store,options or Options(),log=lambda _:None,**kwargs).collect(sources or [self.source()],self.root/'output')[0]
        finally: store.close(); transport.close()

    def test_historical_list_frontier_advances_and_date_windows_expand(self):
        source = self.source(path='/history/tz.htm')
        for days, expected in ((7, 1), (15, 2), (90, 3), (0, 4)):
            with self.subTest(days=days):
                result = self.collect([source], Options(days=days, include_seen=True))
                self.assertEqual(len(result['items']), expected)
        store = Store(self.root/'db.sqlite3')
        try:
            self.assertEqual(store.list_notices()['total'], 4)
        finally: store.close()
        self.assertIn('/history/4.htm', CollectionHandler.requests)

    def test_single_page_budget_also_resumes_historical_lists(self):
        for _ in range(4):
            self.collect([self.source(path='/history/tz.htm')], Options(days=0, pages_per_seed=1))
        self.assertEqual([path for path in CollectionHandler.requests if path.startswith('/history/')],
                         ['/history/tz.htm', '/history/2.htm', '/history/3.htm', '/history/4.htm'])

    def test_cancel_rolls_back_historical_frontier(self):
        source = self.source(path='/history/tz.htm')
        self.collect([source], Options(days=0))
        scope = 'T:'+self.origin+'/history/tz.htm'
        event = threading.Event()
        def progress(state):
            if state['phase'] == 'details': event.set()
        with self.assertRaises(CancelledError):
            self.collect([source], Options(days=0), progress=progress, cancel_event=event)
        store = Store(self.root/'db.sqlite3')
        try:
            self.assertEqual(store.list_checkpoints(scope), {self.origin+'/history/tz.htm': (self.origin+'/history/3.htm', True)})
        finally: store.close()

    def test_failed_historical_page_is_retried_without_resetting_frontier(self):
        source = self.source(path='/history/tz.htm')
        self.collect([source], Options(days=0))
        CollectionHandler.fail_history_page = '/history/3.htm'
        failed = self.collect([source], Options(days=0))
        self.assertEqual(failed['coverage'][0]['outcome'], 'partial')
        CollectionHandler.fail_history_page = ''
        before = len(CollectionHandler.requests)
        self.collect([source], Options(days=0))
        self.assertIn('/history/3.htm', CollectionHandler.requests[before:])
        self.assertNotIn('/history/2.htm', CollectionHandler.requests[before:])

    def test_report_write_failure_preserves_committed_historical_frontier(self):
        source = self.source(path='/history/tz.htm')
        self.collect([source], Options(days=0))
        with patch('radar.engine.write_report', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.collect([source], Options(days=0))
        before = len(CollectionHandler.requests)
        self.collect([source], Options(days=0))
        self.assertIn('/history/3.htm', CollectionHandler.requests[before:])
        self.assertNotIn('/history/2.htm', CollectionHandler.requests[before:])


    def test_div_dates_and_neutral_title_pagination(self):
        result = self.collect([self.source(path='/tz.htm')])
        self.assertEqual(len(result['items']),2)
        self.assertIn('/info/1/1.htm',CollectionHandler.requests)
        self.assertNotIn('/info/1/999.htm',CollectionHandler.requests)
        self.assertNotIn('/info/1/888.htm',CollectionHandler.requests)
        self.assertIn('国家助学金申请安排',{item['title'] for item in result['items']})

    def test_ninth_section_is_not_silently_ignored(self):
        result = self.collect([self.source(path='/sections')])
        self.assertIn('/s8/tz.htm',CollectionHandler.requests)
        self.assertEqual(len(result['items']),9)

    def test_reopened_queue_reaches_twenty_sixth_and_preserves_baseline(self):
        first = self.collect(); self.assertEqual(len(first['items']),25)
        self.assertEqual(first['coverage'][0]['limit_reasons'],['source_details'])
        self.assertEqual(first['coverage'][0]['pending_unattempted'],1)
        second = self.collect()
        self.assertEqual([item['state'] for item in second['items']],['baseline'])
        store = Store(self.root/'db.sqlite3')
        try: self.assertEqual(store.list_notices()['total'],26)
        finally: store.close()
        requested = {path for path in CollectionHandler.requests if path.startswith('/info/')}
        self.assertEqual(len(requested),26)

    def test_failures_do_not_starve_unattempted_candidate(self):
        CollectionHandler.success_path = max((f'/info/1/{i}.htm' for i in range(1,27)),
                                            key=lambda path: identity(self.origin+path))
        CollectionHandler.fail_details = True
        self.collect(); second = self.collect()
        self.assertEqual(len(second['items']),1)
        self.assertEqual(second['items'][0]['state'],'baseline')

    def test_rechecks_cover_finite_queue(self):
        self.collect(); self.collect()
        before = len(CollectionHandler.requests)
        self.collect(); self.collect()
        rechecked = {path for path in CollectionHandler.requests[before:] if path.startswith('/info/')}
        self.assertEqual(len(rechecked),26)

    def test_global_budget_rotates_sources(self):
        CollectionHandler.count = 1
        sources = [self.source('A'),self.source('B'),self.source('C')]
        checked = []
        for _ in range(3):
            result = self.collect(sources,Options(max_details=1))
            checked.extend(row['id'] for row in result['coverage'] if row['details'])
            self.assertEqual(sum(row['outcome']=='not_attempted_budget' for row in result['coverage']),2)
        self.assertEqual(checked,['A','B','C'])

    def test_cancel_rolls_back_notices_queue_and_initialization(self):
        event = threading.Event()
        def progress(state):
            if state['phase']=='details': event.set()
        with self.assertRaises(CancelledError): self.collect(progress=progress,cancel_event=event)
        store = Store(self.root/'db.sqlite3')
        try:
            self.assertEqual(store.list_notices()['total'],0)
            self.assertEqual(store.completed_run_count(),0)
            self.assertEqual(store.pending_candidates('T:'+self.origin+'/many/tz.htm',cutoff=None,keywords=()),[])
            self.assertFalse(store.initialized('T:'+self.origin+'/many/tz.htm'))
        finally: store.close()

    def test_report_failure_rolls_back_every_collection_mutation(self):
        with patch('radar.engine.write_report',side_effect=OSError('fixture')):
            with self.assertRaises(OSError): self.collect()
        store = Store(self.root/'db.sqlite3')
        try:
            self.assertEqual(store.list_notices()['total'],0)
            self.assertEqual(store.completed_run_count(),0)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM notice_candidates').fetchone()[0],0)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM source_state').fetchone()[0],0)
        finally: store.close()

    def test_explicit_source_permission_is_partial_and_does_not_allow_other_source(self):
        CollectionHandler.count = 1
        CollectionHandler.robots_disconnect = True
        result = self.collect([self.source('A'), {**self.source('B'), 'allow_robots_unavailable': True}])
        rows = {row['id']: row for row in result['coverage']}
        self.assertEqual(rows['A']['outcome'], 'failed')
        self.assertEqual(rows['A']['list_pages'], 0)
        self.assertEqual(rows['B']['outcome'], 'partial')
        self.assertEqual(rows['B']['details'], 1)
        self.assertIn('robots_unverified', {item['code'] for item in rows['B']['issues']})
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(CollectionHandler.requests.count('/robots.txt'), 1)
        store = Store(self.root/'db.sqlite3')
        try:
            self.assertEqual(store.list_notices(source_id='A')['total'], 0)
            self.assertEqual(store.list_notices(source_id='B')['total'], 1)
        finally:
            store.close()

    def test_explicit_rules_permission_does_not_bypass_business_access_checks(self):
        CollectionHandler.robots_disconnect = True
        for path, code in [('/login-required', 'login_required'), ('/forbidden', 'access_denied')]:
            with self.subTest(path=path):
                result = self.collect([{**self.source(path=path), 'allow_robots_unavailable': True}])
                self.assertEqual(result['outcome'], 'failed')
                self.assertEqual(result['items'], [])
                self.assertIn(code, {issue['code'] for issue in result['coverage'][0]['issues']})


if __name__ == '__main__': unittest.main()
