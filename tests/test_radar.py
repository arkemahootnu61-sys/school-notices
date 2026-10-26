from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.parsing import (AccessIssue, Candidate, access_check, allowed_url, clean_title,
                           extract_detail, extract_listing, identity, normal_url, notice_title, parse_date)
from radar.engine import Collector, Options
from radar.storage import Store, app_lock, save_browser_state
from radar.transport import FetchError, HttpTransport as ProductionHttpTransport, Robots
from radar.config import make_default, validate_config


class HttpTransport(ProductionHttpTransport):
    """Loopback fixtures must never be sent to a developer's system proxy."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('direct', True)
        super().__init__(*args, **kwargs)


class ParsingTests(unittest.TestCase):
    def test_notice_and_news(self):
        for title in ('关于竞赛报名的通知','2026 年竞赛结果公示','学生招募','奖学金申报'):
            self.assertTrue(notice_title(title))
        for title in ('学院成功举办创新大赛','我校荣获全国一等奖','青春纪实','欢迎来到学院'):
            self.assertFalse(notice_title(title))
        self.assertFalse(notice_title('学院成功举办大赛', True))
        self.assertTrue(notice_title('国家助学金申请安排', True))

    def test_title_dedup(self):
        self.assertEqual(clean_title('关于选课的通知 关于选课的通知2026-10-24'), '关于选课的通知')
        self.assertEqual(clean_title('通知公告通知公告'), '通知公告')

    def test_dates(self):
        self.assertEqual(parse_date('发布日期：2026年10月24日'), '2026-10-24')
        self.assertEqual(parse_date('2026/1/2'), '2026-01-02')
        self.assertIsNone(parse_date('2026-02-31'))

    def test_identity(self):
        self.assertEqual(identity('https://math.xidian.edu.cn/info/123/456.htm'), identity('https://math.xidian.edu.cn/content.jsp?wbnewsid=456&wbtreeid=123'))
        self.assertNotEqual(identity('https://math.xidian.edu.cn/info/123/456.htm'), identity('https://eme.xidian.edu.cn/info/123/456.htm'))

    def test_url_bounds(self):
        for url in ('javascript:alert(1)','file:///C:/secrets','https://a:b@example.com/','https://a.xidian.edu.cn/logout','https://a.xidian.edu.cn/?ticket=secret'):
            with self.assertRaises(ValueError): normal_url(url)
        with self.assertRaises(ValueError): allowed_url('https://evil.example/a', {'math.xidian.edu.cn'})
        self.assertEqual(normal_url('https://math.xidian.edu.cn/a.htm?utm_source=a&x=1#top'), 'https://math.xidian.edu.cn/a.htm?x=1')

    def test_restricted_and_login(self):
        for text in ('<title>系统提示</title>抱歉，您是在校外访问本网站，请先下载学校的aTrust零信任系统',
                     '<title>统一身份认证平台</title>', '<input type="password">', '<title>Just a moment</title>'):
            with self.assertRaises(AccessIssue): access_check(text,'https://a.xidian.edu.cn/')

    def test_listing_scope(self):
        content='''<title>通知公告</title><ul>
        <li><a href="/info/1/2.htm" title="关于报名的通知">关于报名的通知</a><span>2026-10-24</span></li>
        <li><a href="/info/1/3.htm">学院成功举办交流活动</a></li>
        <li><a href="https://evil.example/info/1/4.htm">重要通知</a></li>
        <li><a href="/logout">退出登录</a></li></ul>
        <a href="/tzgg/2.htm">下一页</a><a href="/attachment.pdf">附件通知</a>'''
        result=extract_listing(content,'https://math.xidian.edu.cn/tzgg.htm',{'math.xidian.edu.cn'}, {})
        self.assertEqual(len(result.candidates),1)
        self.assertEqual(result.candidates[0].date,'2026-10-24')
        self.assertEqual(result.next_pages,['https://math.xidian.edu.cn/tzgg/2.htm'])

    def test_detail_extraction(self):
        candidate=Candidate('https://a.xidian.edu.cn/info/1/2.htm','报名通知','2026-10-24','https://a.xidian.edu.cn/tz.htm',True)
        detail=extract_detail('<title>报名通知</title><h1>报名通知</h1><div class="v_news_content">请于月底前报名<script>evil()</script><form>私人表单</form><p>报名说明。</p></div>',candidate.url,candidate,{})
        self.assertIn('报名说明',detail['summary'])
        self.assertNotIn('evil',detail['summary'])
        self.assertNotIn('私人表单',detail['summary'])
        self.assertEqual(detail['published_date'],'2026-10-24')

    def test_logo_h1_does_not_replace_notice_title(self):
        candidate=Candidate('https://a.xidian.edu.cn/info/1/2.htm','关于报名的通知',None,'https://a.xidian.edu.cn/tz.htm',True)
        detail=extract_detail('<h1>西安电子科技大学</h1><div class="v_news_content">报名详情</div>',candidate.url,candidate,{})
        self.assertEqual(detail['title'],'关于报名的通知')

    def test_custom_link_selector(self):
        listing=extract_listing('<a class="item" href="/article?id=3">关于选课的通知</a>', 'https://a.xidian.edu.cn/list', {'a.xidian.edu.cn'}, {'detail_link_selector':'a.item'})
        self.assertEqual(len(listing.candidates),1)


class StoreTests(unittest.TestCase):
    def test_baseline_new_seen_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3')
            item={'key':'k','title':'通知','url':'https://example.com/notice','published_date':None,'summary':'摘要','content_hash':'v1'}
            self.assertEqual(store.remember(item,False),'baseline')
            self.assertEqual(store.remember(item,True),'seen')
            item['content_hash']='v2'
            self.assertEqual(store.remember(item,True),'updated')
            item['key']='k2'
            self.assertEqual(store.remember(item,True),'new')
            store.close()

    def test_rollback_without_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            store=Store(path);store.initialize('test');store.close()
            store=Store(path);self.assertFalse(store.initialized('test'));store.close()

    def test_session_snapshot_filters_other_sites(self):
        class FakeContext:
            def storage_state(self):
                return {'cookies':[{'domain':'.xidian.edu.cn','name':'fake','value':'test'},{'domain':'.other.example','name':'private','value':'not-saved'}],
                        'origins':[{'origin':'https://ids.xidian.edu.cn','localStorage':[]},{'origin':'https://other.example','localStorage':[]}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';save_browser_state(FakeContext(),path)
            state=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(state['cookies']),1)
            self.assertEqual(len(state['origins']),1)

    def test_app_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with app_lock(Path(directory)):
                with self.assertRaises(RuntimeError):
                    with app_lock(Path(directory)):pass


class ConfigTests(unittest.TestCase):
    def test_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sources.json';make_default(path)
            config=json.loads(path.read_text(encoding='utf-8'))
            enabled = {s['id'] for s in config['sources'] if s.get('enabled', True)}
            self.assertEqual(enabled, {f'S{i:02}' for i in range(1,10)} | {'S38','S44'})
            self.assertFalse(any(s['url'].startswith('https://news.xidian.edu.cn/') for s in config['sources']))

    def test_bad_config(self):
        for source in ({'id':'bad id','url':'https://example.com'}, {'id':'a','url':'https://news.xidian.edu.cn/'},
                       {'id':'a','url':'https://example.com','seed_urls':['https://other.example']},
                       {'id':'a','url':'https://example.com','allow_robots_unavailable':'false'},
                       {'id':'a','url':'https://example.com','list_selector':'a['}):
            with self.assertRaises(ValueError):validate_config({'sources':[source]})

    def test_blank_selectors_preserve_generic_list_and_body_extraction(self):
        source = validate_config({'sources': [{'id':'a','url':'https://example.com/',
            'list_selector':'  ', 'content_selector':'\t'}]})['sources'][0]
        listing = extract_listing('''<title>通知公告</title><li>
            <a href="/info/1/1.htm">关于材料提交的通知</a><time>2026-10-25</time></li>''',
            source['url'], {'example.com'}, source)
        detail = extract_detail('<h1>关于材料提交的通知</h1><div class="v_news_content">请于周一提交材料。</div>',
                                listing.candidates[0].url, listing.candidates[0], source)
        self.assertEqual(detail['published_date'], '2026-10-25')
        self.assertEqual(detail['summary'], '请于周一提交材料。')


class FixtureHandler(BaseHTTPRequestHandler):
    version=1
    third=False
    robots_block=False
    today=date.today().isoformat()

    def do_GET(self):
        path=self.path.split('?')[0]
        if path=='/robots.txt':
            self.respond('User-agent: *\nDisallow: /blocked\n' if self.robots_block else '',404 if not self.robots_block else 200,'text/plain');return
        if path=='/redirect':
            self.send_response(302);self.send_header('Location','https://evil.example/exfil');self.end_headers();return
        if path=='/restricted':self.respond('<title>系统提示</title>校外访问需要aTrust零信任');return
        if path in ('/','/tzgg.htm'):
            third=f'<li><a href="/info/1/3.htm">关于第三场比赛报名的通知</a><span>{self.today}</span></li>' if self.third else ''
            self.respond(f'<title>通知公告</title><ul><li><a href="/info/1/1.htm">关于第一场比赛报名的通知</a><span>{self.today}</span></li><li><a href="/info/1/99.htm">学院成功举办庆祝活动</a><span>{self.today}</span></li>{third}</ul><a href="/tzgg/2.htm">下一页</a>');return
        if path=='/tzgg/2.htm':
            old=(date.today()-timedelta(days=100)).isoformat()
            self.respond(f'<title>通知公告</title><ul><li><a href="/info/1/2.htm">关于第二场比赛报名的通知</a><span>{self.today}</span></li><li><a href="/info/1/88.htm">旧的考试安排通知</a><span>{old}</span></li></ul>');return
        if path.startswith('/info/1/'):
            number=path.split('/')[-1].split('.')[0]
            title={'1':'关于第一场比赛报名的通知','2':'关于第二场比赛报名的通知','3':'关于第三场比赛报名的通知'}.get(number,'旧的考试安排通知')
            body=('请各位同学按时提交报名材料，认真阅读赛事安排并参加相关培训。'*5)+f'场次{number}，版本{self.version if number=="1" else 1}'
            self.respond(f'<meta name="PubDate" content="{self.today}"><h1>{title}</h1><div class="v_news_content">{body}</div>');return
        self.respond('not found',404)

    def respond(self,text,status=200,mime='text/html'):
        self.send_response(status);self.send_header('Content-Type',mime+'; charset=utf-8');self.end_headers();self.wfile.write(text.encode())
    def log_message(self,*args):pass


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        FixtureHandler.version=1;FixtureHandler.third=False;FixtureHandler.robots_block=False
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.source={'id':'T01','name':'测试学院','url':self.origin+'/','allowed_hosts':['127.0.0.1'],'seed_urls':[self.origin+'/tzgg.htm'],'enabled':True}

    def tearDown(self):self.temporary.cleanup()

    def collect(self,options=None,source=None):
        transport=HttpTransport(timeout=3,delay=.2,retries=0);store=Store(self.root/'state.sqlite3')
        try:
            with patch.object(transport.throttle,'wait'):
                return Collector(transport,store,options or Options(),log=lambda _:None).collect([source or self.source],self.root/'output')
        finally:store.close();transport.close()

    def test_first_second_update_and_new(self):
        first,paths=self.collect()
        self.assertEqual([item['state'] for item in first['items']],['baseline','baseline'])
        self.assertTrue(paths[0].exists())
        self.assertFalse(any('庆祝活动' in item['title'] for item in first['items']))
        second,_=self.collect();self.assertEqual(second['items'],[]);self.assertEqual(second['seen_omitted'],2)
        FixtureHandler.version=2;FixtureHandler.third=True
        third,_=self.collect()
        self.assertEqual({item['state'] for item in third['items']},{'new','updated'})
        self.assertEqual(len(third['items']),2)

    def test_page_and_detail_limits(self):
        result,_=self.collect(Options(pages_per_seed=1,max_details_per_source=1))
        self.assertEqual(len(result['items']),1)
        self.assertTrue(result['coverage'][0]['limited'])

    def test_restricted_not_success(self):
        source={**self.source,'seed_urls':[self.origin+'/restricted']}
        result,_=self.collect(source=source)
        self.assertEqual(result['coverage'][0]['list_pages'],0)
        self.assertIn('campus_network_required', {issue['code'] for issue in result['coverage'][0]['issues']})
        self.assertEqual(result['items'],[])

    def test_redirect_host_blocked(self):
        transport=HttpTransport(timeout=2,retries=0)
        try:
            with self.assertRaises(FetchError):transport.fetch(self.origin+'/redirect',{'127.0.0.1'})
        finally:transport.close()

    def test_robots_disallow(self):
        FixtureHandler.robots_block=True
        transport=HttpTransport(timeout=2,retries=0)
        try:
            robots=Robots(transport)
            with self.assertRaises(FetchError):robots.allowed(self.origin+'/blocked',{'127.0.0.1'})
        finally:transport.close()

    def test_seen_explicitly_included(self):
        self.collect()
        result,_=self.collect(Options(include_seen=True))
        self.assertEqual(len(result['items']),2)
        self.assertEqual({item['state'] for item in result['items']},{'seen'})

    def test_failure_does_not_initialize_source(self):
        source={**self.source,'seed_urls':[self.origin+'/restricted']}
        self.collect(source=source)
        store=Store(self.root/'state.sqlite3')
        try:self.assertFalse(store.initialized('T01:'+self.origin+'/restricted'))
        finally:store.close()

    def test_report_failure_rolls_back_seen_state(self):
        with patch('radar.engine.write_report',side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):self.collect()
        result,_=self.collect()
        self.assertEqual({item['state'] for item in result['items']},{'baseline'})


    def test_unknown_date_retained(self):
        result,_=self.collect()
        self.assertTrue(all(item['published_date'] for item in result['items']))
        candidate=Candidate(self.origin+'/info/1/1.htm','报名通知',None,self.origin+'/tzgg.htm',True)
        detail=extract_detail('<h1>报名通知</h1><div class="v_news_content">无日期通知</div>',candidate.url,candidate,{})
        self.assertIsNone(detail['published_date'])

    def test_report_escapes_text(self):
        from radar.report import write_report
        from radar.storage import utc_now
        result={'started_at':utc_now(),'transport_mode':'auto','days':30,'seen_omitted':0,'coverage':[],
                'items':[{'state':'baseline','sources':[{'name':'测试'}],'url':self.origin+'/info/1/1.htm','title':'通知<script>alert(1)</script>',
                          'published_date':None,'summary':'<img src=x onerror=alert(1)>','alternative_urls':[]}]}
        path,_=write_report(self.root/'output',result);text=path.read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)</script>',text)
        self.assertIn('&lt;script&gt;',text)
        self.assertNotIn('<img src=x',text)


if __name__=='__main__':unittest.main(verbosity=2)
