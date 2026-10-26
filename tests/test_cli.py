from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import CancelledError
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from test_radar import FixtureHandler
from http.server import ThreadingHTTPServer
import threading

from radar.config import add_source, json_read, validate_config
from radar.storage import Store, app_lock

class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='radar-cli-test-')
        self.root=Path(self.temporary.name)
        self.config=self.root/'sources.json'
        self.data=self.root/'private'
    def tearDown(self):self.temporary.cleanup()
    def call(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'notices.py'),'--config',str(self.config),'--data-dir',str(self.data),*args],capture_output=True,text=True,encoding='utf-8',timeout=30)

    def test_source_crud_and_backups(self):
        self.assertEqual(self.call('init').returncode,0)
        result=self.call('sources','add','手动考试通知','https://example.edu.cn/notices/')
        self.assertEqual(result.returncode,0,result.stderr)
        sources=json.loads(self.config.read_text(encoding='utf-8'))['sources']
        manual=next(s for s in sources if s['name']=='手动考试通知')
        sid=manual['id']
        self.assertEqual(self.call('sources','disable',sid).returncode,0)
        self.assertFalse(next(s for s in json.loads(self.config.read_text(encoding='utf-8'))['sources'] if s['id']==sid)['enabled'])
        self.assertEqual(self.call('sources','enable',sid).returncode,0)
        self.assertEqual(self.call('sources','remove',sid).returncode,0)
        self.assertTrue(list(self.root.glob('sources.backup-*.json')))
        self.assertFalse(any(s['id']==sid for s in json.loads(self.config.read_text(encoding='utf-8'))['sources']))

    def test_invalid_import_preserves_config(self):
        self.call('init')
        original=self.config.read_bytes()
        bad=self.root/'bad.json';bad.write_text('{"sources":[{"id":"bad id","url":"https://example.com"}]}',encoding='utf-8')
        result=self.call('sources','import',str(bad))
        self.assertEqual(result.returncode,1)
        self.assertEqual(self.config.read_bytes(),original)

    def test_command_line_collects_once(self):
        FixtureHandler.version=1;FixtureHandler.third=False;FixtureHandler.robots_block=False
        server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        self.config.write_text(json.dumps({'sources':[{'id':'T01','name':'测试通知','url':origin+'/','seed_urls':[origin+'/tzgg.htm'],'allowed_hosts':['127.0.0.1'],'enabled':True}]}),encoding='utf-8')
        try:
            args=['run','--direct','--no-open','--delay','0.2','--output',str(self.root/'out')]
            first=self.call(*args)
            self.assertEqual(first.returncode,0,first.stdout+first.stderr)
            result=json.loads((self.root/'out/latest.json').read_text(encoding='utf-8'))
            self.assertEqual(len(result['items']),2)
            self.assertEqual({item['state'] for item in result['items']},{'baseline'})
            second=self.call(*args, '--http')
            self.assertEqual(second.returncode,0,second.stdout+second.stderr)
            result=json.loads((self.root/'out/latest.json').read_text(encoding='utf-8'))
            self.assertEqual(result['items'],[])
            self.assertEqual(result['seen_omitted'],2)
            self.assertEqual(result['schema_version'],2)
            self.assertEqual(result['transport_mode'],'http')
            store=Store(self.data/'notices.sqlite3')
            try:
                self.assertEqual(store.list_notices()['total'],2)
                self.assertEqual(store.latest_result()['items'],[])
            finally:store.close()
        finally:server.shutdown();server.server_close();thread.join()

    def test_rejects_credential_fields_on_import(self):
        self.call('init')
        original=self.config.read_bytes()
        bad=self.root/'credentials.json'
        bad.write_text(json.dumps({'sources':[], 'cookies':[{'value':'DO_NOT_ECHO'}]}),encoding='utf-8')
        result=self.call('sources','import',str(bad))
        self.assertEqual(result.returncode,1)
        self.assertEqual(self.config.read_bytes(),original)
        self.assertNotIn('DO_NOT_ECHO',result.stdout+result.stderr)

    def test_malformed_source_is_readable_error_not_traceback(self):
        self.config.write_text('{"sources":[42]}',encoding='utf-8')
        result=self.call('sources','list')
        self.assertEqual(result.returncode,1)
        self.assertNotIn('Traceback',result.stdout+result.stderr)

    def test_rejects_sensitive_url(self):
        self.call('init')
        result=self.call('sources','add','凭据不应存入来源','https://example.com/?ticket=secret')
        self.assertEqual(result.returncode,1)
        self.assertNotIn('secret',result.stdout+result.stderr)

    def test_source_write_respects_running_task_lock(self):
        self.assertEqual(self.call('init').returncode,0)
        original=self.config.read_bytes()
        with app_lock(self.data):
            result=self.call('sources','add','不能并发写入','https://example.edu.cn/notices/')
        self.assertEqual(result.returncode,1)
        self.assertEqual(self.config.read_bytes(),original)
        self.assertEqual(self.call('sources','add','锁已释放','https://example.edu.cn/notices/').returncode,0)

    def test_source_write_reloads_configuration_after_lock(self):
        import notices
        initial={'sources':[{'id':'T01','name':'原来源','url':'https://example.edu.cn/','enabled':True}]}
        fresh=add_source(validate_config(initial),'外部新增','https://second.example.edu.cn/')
        self.config.write_text(json.dumps(initial),encoding='utf-8')

        @contextmanager
        def replace_before_lock(directory):
            self.config.write_text(json.dumps(fresh),encoding='utf-8')
            yield

        args=SimpleNamespace(source_action='disable',ids=['T01'])
        with mock.patch('radar.storage.app_lock',replace_before_lock):
            notices.source_command(args,self.config,self.data)
        saved=json_read(self.config)
        self.assertEqual({s['id'] for s in saved['sources']},{'T01','M01'})
        self.assertFalse(saved['sources'][0]['enabled'])

    def test_add_source_validation_does_not_mutate_input(self):
        config=validate_config({'sources':[]})
        for url in ('https://example.edu.cn/?ticket=private-ticket','https://news.xidian.edu.cn/'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    add_source(config,'不安全来源',url)
                self.assertEqual(config['sources'],[])

    def test_runner_rolls_back_and_releases_resources_on_abnormal_exit(self):
        from radar.engine import Options
        from radar.runner import run_collection
        self.config.write_text(json.dumps({'sources':[{'id':'T01','name':'测试通知',
                               'url':'https://example.edu.cn/','enabled':True}]}),encoding='utf-8')
        for failure in (CancelledError(),OSError('report write failed')):
            with self.subTest(failure=type(failure).__name__):
                transport=SimpleNamespace(close=mock.Mock())

                def failing_collector(transport,store,options,**kwargs):
                    def collect(sources,output):
                        store.initialize('T01')
                        store.remember({'key':'test-key','title':'测试通知','url':'https://example.edu.cn/notice',
                                        'published_date':None,'summary':'正文','content_hash':'test-hash'},False)
                        raise failure
                    return SimpleNamespace(collect=collect)

                with mock.patch('radar.runner.AutoTransport',return_value=transport), \
                     mock.patch('radar.runner.Collector',side_effect=failing_collector):
                    with self.assertRaises(type(failure)):
                        run_collection(self.config,self.data,self.root/'out',Options(),log=lambda message:None)
                with app_lock(self.data):
                    store=Store(self.data/'notices.sqlite3')
                    try:
                        self.assertFalse(store.initialized('T01'))
                        self.assertEqual(store.list_notices()['total'],0)
                        self.assertEqual(store.completed_run_count(),0)
                    finally:store.close()

    def test_cli_cancellation_returns_130(self):
        import notices
        self.config.write_text('{"sources":[]}',encoding='utf-8')
        with mock.patch('radar.runner.run_collection',side_effect=CancelledError()):
            result=notices.main(['--config',str(self.config),'--data-dir',str(self.data),'run','--no-open'])
        self.assertEqual(result,130)


    @contextmanager
    def manual_browser(self):
        """A lifecycle fixture, not a transport: no real browser or school request."""
        events=[]
        state={'closed':False,'save_error':False}
        def record(action):
            events.append((action,threading.get_ident()))
        class Page:
            def is_closed(self):return state['closed']
            def goto(self,*args,**kwargs):record('goto')
            def wait_for_timeout(self,milliseconds):
                self_test.assertLessEqual(milliseconds,200)
                record('pump')
        page=Page()
        class Context:
            @property
            def pages(self):return [] if state['closed'] else [page]
            def close(self):
                record('close')
                state['closed']=True
            def add_cookies(self,cookies):record('restore')
            def storage_state(self):
                record('save')
                if state['save_error']:raise OSError('fixture write failure')
                return {'cookies':[{'name':'fixture','value':'private-fixture','domain':'.xidian.edu.cn',
                                    'path':'/','expires':-1,'httpOnly':True,'secure':True,'sameSite':'Lax'}],
                        'origins':[]}
        context=Context()
        def launch(*args,**kwargs):
            record('launch')
            return context
        def stop():record('stop')
        playwright=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=stop)
        def start():
            record('start')
            return playwright
        api=SimpleNamespace(sync_playwright=lambda:SimpleNamespace(start=start))
        self_test=self
        with mock.patch.dict(sys.modules,{'playwright.sync_api':api}):
            yield state,events

    def test_manual_login_explicit_finish_pumps_and_saves_on_worker_thread(self):
        from radar.runner import ManualLoginSession
        errors=[]
        with self.manual_browser() as (state,events), \
             mock.patch('builtins.input',side_effect=AssertionError('runner must not read stdin')):
            def worker():
                try:
                    session=ManualLoginSession(self.data)
                    session.open('https://ids.xidian.edu.cn/authserver/login')
                    self.assertFalse((self.data/'browser-state.json').exists())
                    self.assertTrue(session.pump(500))
                    session.finish()
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=worker)
            thread.start();thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual({owner for action,owner in events},{thread.ident})
            self.assertTrue(state['closed'])
        saved=json_read(self.data/'browser-state.json')
        self.assertEqual(saved['cookies'][0]['name'],'fixture')
        with app_lock(self.data):pass

    def test_manual_login_cancel_closed_window_and_save_failure_preserve_old_state(self):
        from radar.runner import ManualLoginSession
        from radar.transport import FetchError
        self.data.mkdir(parents=True)
        path=self.data/'browser-state.json'
        original=b'{"cookies":[],"origins":[]}'
        for outcome in ('cancel','closed','save_failure'):
            with self.subTest(outcome=outcome),self.manual_browser() as (state,events):
                path.write_bytes(original)
                session=ManualLoginSession(self.data)
                session.open('https://www.xidian.edu.cn/')
                if outcome=='cancel':
                    session.cancel()
                elif outcome=='closed':
                    state['closed']=True
                    self.assertFalse(session.pump())
                    with self.assertRaises(CancelledError):session.finish()
                else:
                    state['save_error']=True
                    with self.assertRaises(FetchError) as error:session.finish()
                    self.assertEqual(error.exception.code,'session_invalid')
                self.assertEqual(path.read_bytes(),original)
                self.assertTrue(state['closed'])
                with app_lock(self.data):pass

    def test_manual_login_invalid_snapshot_is_not_discarded(self):
        from radar.runner import ManualLoginSession
        from radar.transport import FetchError
        self.data.mkdir(parents=True)
        path=self.data/'browser-state.json'
        path.write_bytes(b'not-json-private-content')
        with self.manual_browser() as (state,events):
            with self.assertRaises(FetchError) as error:
                ManualLoginSession(self.data).open('https://www.xidian.edu.cn/')
            self.assertEqual(error.exception.code,'session_invalid')
            self.assertNotIn('private-content',str(error.exception))
            self.assertTrue(state['closed'])
        self.assertEqual(path.read_bytes(),b'not-json-private-content')
        with app_lock(self.data):pass


if __name__=='__main__':unittest.main(verbosity=2)
