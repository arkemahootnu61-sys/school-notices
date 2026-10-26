from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.parsing import Candidate, identity
from radar.storage import Store, atomic_text, merge_notices


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'notices.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def notice(self, url='https://a.example/info/1/1.htm', **kwargs):
        return dict(key=identity(url), title='报名通知', url=url, published_date='2026-10-25',
                    summary='公开报名要求，按时参加。' * 15, content_hash='same', **kwargs)

    def commit(self, store, items=None):
        result = {'items': items or [], 'stats': {}}
        store.finish('2026-10-25', {}, result=result)

    def test_old_database_migrates_without_inventing_metadata(self):
        db = sqlite3.connect(self.path)
        db.executescript('''CREATE TABLE notices(key TEXT PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL,
            published_date TEXT,summary TEXT NOT NULL,content_hash TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
            CREATE TABLE source_state(source_id TEXT PRIMARY KEY,initialized_at TEXT NOT NULL);
            CREATE TABLE runs(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,stats_json TEXT NOT NULL);''')
        notice = self.notice()
        db.execute('INSERT INTO notices VALUES(?,?,?,?,?,?,?,?)',
                   (*[notice[k] for k in ('key','title','url','published_date','summary','content_hash')], 'first', 'last'))
        db.execute('INSERT INTO runs VALUES(1,?,?,?)', ('old','old','{}'))
        db.commit(); db.close()
        for _ in range(2):
            store = Store(self.path)
            try:
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 3)
                item = store.list_notices()['items'][0]
                self.assertEqual((item['first_seen'],item['last_seen'],item['content_hash']), ('first','last','same'))
                self.assertEqual(item['sources'], [])
                self.assertEqual(item['source_attribution'], 'unknown')
                self.assertIsNone(item['detail_parsed'])
                self.assertIsNone(store.latest_result())
            finally: store.close()
        store = Store(self.path)
        try:
            source = {'id':'T01','name':'学院'}
            store.discover_candidates(source,'scope',[Candidate(notice['url'],notice['title'],None,'list',True)],initialized=True)
            self.assertEqual(store.remember(notice,True), 'seen')
            self.assertEqual(store.list_notices(source_id='T01')['total'], 1)
            self.assertEqual(store.list_notices()['items'][0]['first_seen'], 'first')
        finally: store.close()

    def test_future_version_leaves_bytes_unchanged(self):
        db = sqlite3.connect(self.path); db.execute('PRAGMA user_version=4'); db.close()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_v1_upgrade_preserves_history_and_new_frontier_rolls_back(self):
        store = Store(self.path)
        notice = self.notice(detail_parsed=True)
        store.remember(notice, False)
        self.commit(store)
        store.db.execute('DROP TABLE list_checkpoints')
        store.db.execute('ALTER TABLE notices DROP COLUMN read_at')
        store.db.execute('ALTER TABLE notices DROP COLUMN favorite')
        store.db.execute('PRAGMA user_version=1')
        store.db.commit()
        store.close()
        store = Store(self.path)
        try:
            self.assertEqual(store.list_notices()['items'][0]['url'], notice['url'])
            self.assertEqual(store.completed_run_count(), 1)
            store.save_list_checkpoint('scope', 'head', 'next', True)
            self.commit(store)
            store.save_list_checkpoint('scope', 'head', 'later', False)
            store.rollback()
            self.assertEqual(store.list_checkpoints('scope'), {'head': ('next', True)})
            self.assertEqual(store.list_checkpoints('another-scope'), {})
        finally: store.close()

    def test_unattempted_then_oldest_attempt_and_rollback(self):
        source = {'id':'T','name':'测试'}
        candidates = [Candidate(f'https://a.example/info/1/{i}.htm', f'报名通知{i}', '2026-10-25','list',True) for i in range(3)]
        store = Store(self.path)
        store.discover_candidates(source,'scope',candidates,initialized=False)
        first = store.pending_candidates('scope',cutoff=None,keywords=())[0]
        store.record_attempt('scope',first['notice_key'],'fetch_error')
        pending = store.pending_candidates('scope',cutoff=None,keywords=())
        self.assertEqual(pending[-1]['notice_key'], first['notice_key'])
        self.commit(store); store.close()
        store = Store(self.path)
        try:
            pending = store.pending_candidates('scope',cutoff=None,keywords=())
            self.assertIsNone(pending[0]['last_attempt_seq'])
            self.assertTrue(all(item['baseline_candidate'] for item in pending))
            store.record_attempt('scope',pending[0]['notice_key'],'success')
            store.rollback()
            self.assertIsNone(store.pending_candidates('scope',cutoff=None,keywords=())[0]['last_attempt_seq'])
        finally: store.close()

    def test_redirect_attribution_and_full_search_before_pagination(self):
        store = Store(self.path)
        try:
            source = {'id':'T','name':'目标学院'}
            original = 'https://a.example/info/1/99.htm'
            notice = self.notice(detail_parsed=True)
            store.discover_candidates(source,'scope',[Candidate(original,'报名通知',None,'list',True)],initialized=False)
            store.remember(notice,False)
            store.record_attempt('scope',identity(original),'success',stored_notice_key=notice['key'])
            store.discover_candidates(source,'scope',[Candidate(original,'报名通知',None,'list',True)],initialized=True)
            store.record_attempt('scope',identity(original),'fetch_error')
            for i in range(2,5):
                item = self.notice(f'https://a.example/info/1/{i}.htm')
                item['summary'] = '另一篇正文'; item['content_hash'] = str(i)
                store.remember(item,True)
            self.commit(store)
            self.assertEqual(store.list_notices(source_id='T')['total'],1)
            self.assertEqual(store.list_notices(query='目标学院',limit=1)['total'],1)
            self.assertEqual(store.list_notices(limit=1,offset=3)['total'],4)
            self.assertEqual(len(store.list_notices(limit=1,offset=3)['items']),1)
            self.assertEqual(store.list_notices(source_id='T')['items'][0]['key'],notice['key'])
        finally: store.close()

    def test_merge_substantial_copies_only(self):
        a = self.notice(); a.update(sources=[{'id':'A','name':'甲'}],state='seen')
        b = self.notice('https://b.example/info/1/2.htm'); b.update(sources=[{'id':'B','name':'乙'}],state='updated')
        items = merge_notices([a,b])
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['state'],'updated')
        self.assertEqual({s['id'] for s in items[0]['sources']},{'A','B'})
        self.assertEqual(items[0]['alternative_urls'],[b['url']])
        b['content_hash']='different'
        self.assertEqual(len(merge_notices([a,b])),2)
        a['summary']=b['summary']='简短通知'
        b['content_hash']=a['content_hash']
        self.assertEqual(len(merge_notices([a,b])),2)

    def test_recent_merge_search_and_latest_state_cover_alternative_urls(self):
        store = Store(self.path)
        try:
            a = self.notice()
            b = self.notice('https://b.example/info/1/2.htm')
            c = self.notice('https://c.example/info/1/3.htm')
            c['content_hash'] = 'different-body'
            for sid, item in [('A', a), ('B', b), ('C', c)]:
                store.discover_candidates({'id':sid,'name':'学院'+sid}, sid,
                    [Candidate(item['url'],item['title'],None,'list',True)], initialized=False)
                store.remember(item,False)
            self.commit(store,[{**a,'state':'updated','alternative_urls':[b['url']]}])
            result = store.list_notices(source_id='B', query='学院A',limit=1)
            self.assertEqual(result['total'],1)
            self.assertEqual(result['items'][0]['state'],'updated')
            self.assertEqual({s['id'] for s in result['items'][0]['sources']},{'A','B'})
            self.assertEqual(store.list_notices()['total'],2)
            self.commit(store)
            self.assertEqual({item['state'] for item in store.list_notices()['items']},{'seen'})
            store.db.execute('INSERT INTO runs(started_at,finished_at,stats_json) VALUES (?,?,?)',
                             ('legacy','legacy','{}'))
            store.db.commit()
            self.assertIsNone(store.latest_result())
        finally:
            store.close()

    def test_fresh_only_hides_dismissed_and_seen_but_keeps_favorites(self):
        store = Store(self.path)
        try:
            old = self.notice('https://a.example/info/1/1.htm')
            new = self.notice('https://a.example/info/1/2.htm'); new['content_hash'] = 'newer'
            for item in (old, new):
                store.remember(item, True)
            self.commit(store, [{**old, 'state': 'seen'}, {**new, 'state': 'new'}])
            self.assertEqual(store.list_notices()['total'], 2)
            self.assertEqual([i['key'] for i in store.list_notices(fresh_only=True)['items']], [new['key']])
            store.set_notice_flags(new['key'], read=True)
            self.assertEqual(store.list_notices(fresh_only=True)['total'], 0)
            store.set_notice_flags(old['key'], favorite=True)
            self.assertEqual([i['key'] for i in store.list_notices(fresh_only=True)['items']], [old['key']])
        finally:
            store.close()

    def test_atomic_failure_preserves_latest(self):
        path = self.path.with_name('latest.html'); path.write_text('complete',encoding='utf-8')
        with patch('pathlib.Path.replace',side_effect=OSError('injected write failure')):
            with self.assertRaises(OSError): atomic_text(path,'replacement')
        self.assertEqual(path.read_text(encoding='utf-8'),'complete')
        self.assertEqual(list(path.parent.glob('*.tmp')),[])


if __name__ == '__main__': unittest.main()
