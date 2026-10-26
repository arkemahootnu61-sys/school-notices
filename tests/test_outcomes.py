from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar.engine import Collector, Options
from radar.storage import Store, atomic_text
from radar.transport import FetchError, Page, Throttle


class MemoryTransport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0
        self.blocked_methods = 0
        self.throttle = Throttle(.2)

    def fetch(self,url,hosts,*,raw=False,allow_http_detail=False):
        self.calls += 1
        if raw: return Page(url,'',200,'text/plain')
        value = self.pages[url]
        if isinstance(value,Exception): raise value
        return Page(url,value,200,'text/html')


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'db.sqlite3')
        self.url = 'https://fixture.example/tz.htm'
        self.source = {'id':'A','name':'测试学院','url':self.url,'allowed_hosts':['fixture.example']}

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def collect(self,pages,options=None,sources=None):
        return Collector(MemoryTransport(pages),self.store,options or Options(),log=lambda _:None).collect(
            sources or [self.source],self.root/'output')[0]

    def test_http_200_is_not_recognized_list(self):
        result = self.collect({self.url:'<title>学院</title><p>欢迎</p>'})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['coverage'][0]['outcome'],'unrecognized')
        self.assertEqual(result['coverage'][0]['recognized_list_pages'],0)
        self.assertEqual(self.store.list_notices()['total'],0)

    def test_explicit_empty_is_checked_not_failed(self):
        result = self.collect({self.url:'<title>通知公告</title><p>暂无相关信息</p>'})
        self.assertEqual(result['outcome'],'checked')
        self.assertEqual(result['coverage'][0]['outcome'],'checked')
        self.assertEqual(result['stats']['source_counts'],{'checked':1})

    def test_reliable_list_with_failed_detail_is_partial(self):
        detail = 'https://fixture.example/info/1/1.htm'
        result = self.collect({self.url:'<a href="/info/1/1.htm">报名通知</a>',
                               detail:FetchError('连接失败',code='connection_failed')})
        row = result['coverage'][0]
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual(row['outcome'],'partial')
        self.assertEqual(row['detail_failures'],1)
        self.assertEqual(row['issues'][0]['code'],'connection_failed')
        self.assertEqual(row['issues'][0]['stage'],'detail')

    def test_global_budget_preserves_later_source_coverage(self):
        second = {**self.source,'id':'B'}
        result = self.collect({self.url:'<title>通知公告</title>暂无信息'},Options(max_pages=1),[self.source,second])
        self.assertEqual([row['outcome'] for row in result['coverage']],['checked','not_attempted_budget'])
        self.assertEqual(result['coverage'][1]['limit_reasons'],['global_pages'])
        self.assertEqual(result['outcome'],'partial')

    def test_report_failure_keeps_previous_latest_and_committed_database(self):
        latest = self.root/'output/latest.html'
        atomic_text(latest,'complete previous report')
        detail = 'https://fixture.example/info/1/1.htm'
        pages = {self.url:'<a href="/info/1/1.htm">报名通知</a>',
                 detail:'<h1>报名通知</h1><div class="v_news_content">有效报名正文</div>'}
        def fail_latest(path,text):
            if path.name == 'latest.html':
                # Fail the actual atomic replacement after writing its temporary file.
                with patch('pathlib.Path.replace',side_effect=OSError('injected write failure')):
                    atomic_text(path,text)
            else: atomic_text(path,text)
        with patch('radar.report.atomic_text',side_effect=fail_latest):
            with self.assertRaises(OSError): self.collect(pages)
        self.assertEqual(latest.read_text(encoding='utf-8'),'complete previous report')
        self.assertEqual(self.store.completed_run_count(),0)
        self.assertEqual(self.store.list_notices()['total'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM notice_candidates').fetchone()[0],0)
        result = self.collect(pages)
        self.assertEqual(result['items'][0]['state'],'baseline')


if __name__ == '__main__': unittest.main()
