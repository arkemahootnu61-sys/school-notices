from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit


def default_data_dir() -> Path:
    if os.name == 'nt':
        root = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share')))
    return root / 'XidianNoticeRadar'


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def session_domain_allowed(domain, domains):
    """True when a cookie or origin host belongs to one of the configured domains."""
    domain = domain.lstrip('.').lower()
    return any(domain == allowed or domain.endswith('.' + allowed) for allowed in domains)


def save_browser_state(context, path: Path, domains=()):
    """Session cookies need an explicit local snapshot; Chrome may discard them on exit.

    Only the configured school domains are written, so unrelated browsing kept in
    the same profile never reaches the snapshot file.
    """
    state = context.storage_state()
    state['cookies'] = [c for c in state.get('cookies', [])
                        if session_domain_allowed(c.get('domain', ''), domains)]
    state['origins'] = [o for o in state.get('origins', [])
                        if session_domain_allowed(urlsplit(o.get('origin', '')).hostname or '', domains)]
    atomic_json(path, state)


def atomic_text(path: Path, text: str) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


@contextmanager
def app_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (directory / 'app.lock').open('a+b')
    try:
        try:
            # A locked byte cannot even be read on Windows. Check file size instead.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('已有采集 / 登录任务在运行，请先关闭那个任务') from None
        yield
    finally:
        # Closing the file releases the OS lock, including after process termination.
        handle.close()


def merge_notices(items: list[dict]) -> list[dict]:
    """Merge substantive exact copies, never titles alone."""
    merged = {}
    rank = {'seen': 0, 'baseline': 1, 'new': 2, 'updated': 3}
    for item in items:
        merge_key = ('hash:' + item['content_hash'] if len(item.get('summary', '')) > 60
                     else 'key:' + item['key'])
        if merge_key not in merged:
            merged[merge_key] = {**item, 'sources': list(item.get('sources', [])),
                                 'alternative_urls': list(item.get('alternative_urls', []))}
            continue
        current = merged[merge_key]
        known = {source['id'] for source in current['sources']}
        for source in item.get('sources', []):
            if source['id'] not in known:
                current['sources'].append(source)
                known.add(source['id'])
        for url in [item['url'], *item.get('alternative_urls', [])]:
            if url != current['url'] and url not in current['alternative_urls']:
                current['alternative_urls'].append(url)
        if rank.get(item.get('state'), 0) > rank.get(current.get('state'), 0):
            current['state'] = item['state']
        # Exact copies are one notice to the user; a flag on any copy wins.
        for field in ('favorite', 'read'):
            if field in current or field in item:
                current[field] = bool(current.get(field) or item.get(field))
        for field in ('last_seen', 'last_checked_at'):
            if item.get(field, '') > current.get(field, ''):
                current[field] = item[field]
        if current['sources']:
            current['source_attribution'] = 'known'
    return list(merged.values())


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=2000')
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version > 4:
                raise ValueError('通知数据库版本高于本程序支持的版本，未修改文件')
            if version < 4:
                # Every schema step and version stamp commit together, including legacy v0.
                self.db.execute('BEGIN IMMEDIATE')
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version > 4:
                    raise ValueError('通知数据库版本高于本程序支持的版本，未修改文件')
                if version == 0:
                    self.db.execute('''CREATE TABLE IF NOT EXISTS notices (
                        key TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
                        published_date TEXT, summary TEXT NOT NULL, content_hash TEXT NOT NULL,
                        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)''')
                    self.db.execute('''CREATE TABLE IF NOT EXISTS source_state (
                        source_id TEXT PRIMARY KEY, initialized_at TEXT NOT NULL)''')
                    self.db.execute('''CREATE TABLE IF NOT EXISTS runs (
                        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL, stats_json TEXT NOT NULL)''')
                    self.db.execute('ALTER TABLE notices ADD COLUMN detail_parsed INTEGER')
                    self.db.execute('ALTER TABLE runs ADD COLUMN result_json TEXT')
                    self.db.execute('''CREATE TABLE notice_candidates (
                        scope_key TEXT NOT NULL, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
                        notice_key TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                        listed_date TEXT, listed_on TEXT NOT NULL, trusted INTEGER NOT NULL,
                        baseline_candidate INTEGER NOT NULL, first_discovered_at TEXT NOT NULL,
                        last_listed_at TEXT NOT NULL, last_attempt_seq INTEGER,
                        last_attempt_at TEXT, last_outcome TEXT, stored_notice_key TEXT,
                        PRIMARY KEY (scope_key, notice_key))''')
                    self.db.execute('CREATE INDEX idx_candidates_notice ON notice_candidates(notice_key)')
                    self.db.execute('CREATE INDEX idx_candidates_stored_notice ON notice_candidates(stored_notice_key)')
                    self.db.execute('CREATE INDEX idx_candidates_order ON notice_candidates(scope_key, last_attempt_seq, notice_key)')
                    self.db.execute('CREATE INDEX idx_notices_recent ON notices(published_date, key)')
                if version < 2:
                    self.db.execute('''CREATE TABLE list_checkpoints (
                        scope_key TEXT NOT NULL, section TEXT NOT NULL, next_url TEXT NOT NULL,
                        trusted INTEGER NOT NULL, PRIMARY KEY (scope_key, section))''')
                    self.db.execute('PRAGMA user_version = 2')
                if version < 3:
                    self.db.execute('ALTER TABLE notices ADD COLUMN read_at TEXT')
                    self.db.execute('ALTER TABLE notices ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0')
                    self.db.execute('PRAGMA user_version = 3')
                if version < 4:
                    # Cleaned notices keep identity, fingerprint and read state so a
                    # later collection cannot present them as new again.
                    self.db.execute('ALTER TABLE notices ADD COLUMN cleaned_at TEXT')
                    self.db.execute('PRAGMA user_version = 4')
                self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def initialized(self, source_id):
        return self.db.execute('SELECT 1 FROM source_state WHERE source_id=?', (source_id,)).fetchone() is not None

    def remember(self, notice, initialized):
        row = self.db.execute('SELECT content_hash FROM notices WHERE key=?', (notice['key'],)).fetchone()
        status = ('seen' if row[0] == notice['content_hash'] else 'updated') if row else ('new' if initialized else 'baseline')
        now = utc_now()
        self.db.execute('''INSERT INTO notices
            (key,title,url,published_date,summary,content_hash,first_seen,last_seen,detail_parsed)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET title=excluded.title, url=excluded.url,
            published_date=excluded.published_date, summary=excluded.summary,
            content_hash=excluded.content_hash, last_seen=excluded.last_seen,
            detail_parsed=COALESCE(excluded.detail_parsed,notices.detail_parsed),
            cleaned_at=CASE WHEN notices.content_hash<>excluded.content_hash
                THEN NULL ELSE notices.cleaned_at END,
            read_at=CASE WHEN notices.content_hash<>excluded.content_hash
                    AND notices.cleaned_at IS NOT NULL
                THEN NULL ELSE notices.read_at END''',
            (notice['key'], notice['title'], notice['url'], notice['published_date'],
             notice['summary'], notice['content_hash'], now, now,
             int(notice['detail_parsed']) if 'detail_parsed' in notice else None))
        if row is None:
            self._join_group_flags(notice['key'], notice['summary'])
        return status

    def _join_group_flags(self, key, summary):
        """A later copy of an already handled notice must not split its state."""
        if len(summary) <= 60:
            return
        sibling = self.db.execute('''SELECT favorite,read_at,cleaned_at FROM notices
            WHERE content_hash=(SELECT content_hash FROM notices WHERE key=?)
              AND key<>? AND length(summary)>60 LIMIT 1''', (key, key)).fetchone()
        if sibling is not None:
            self.db.execute('UPDATE notices SET favorite=?,read_at=?,cleaned_at=? WHERE key=?',
                            (sibling['favorite'], sibling['read_at'], sibling['cleaned_at'], key))

    def initialize(self, source_id):
        self.db.execute('INSERT OR IGNORE INTO source_state(source_id,initialized_at) VALUES (?,?)', (source_id, utc_now()))

    def discover_candidates(self, source: dict, scope_key: str, candidates: list, *, initialized: bool) -> None:
        from .parsing import identity
        now = utc_now()
        for candidate in candidates:
            key = identity(candidate.url)
            exists = self.db.execute('SELECT 1 FROM notices WHERE key=?', (key,)).fetchone()
            self.db.execute('''INSERT INTO notice_candidates (
                scope_key,source_id,source_name,notice_key,url,title,listed_date,listed_on,
                trusted,baseline_candidate,first_discovered_at,last_listed_at,last_attempt_seq)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scope_key,notice_key) DO UPDATE SET
                source_name=excluded.source_name,url=excluded.url,title=excluded.title,
                listed_date=excluded.listed_date,listed_on=excluded.listed_on,
                trusted=excluded.trusted,last_listed_at=excluded.last_listed_at''',
                (scope_key, source['id'], source['name'], key, candidate.url, candidate.title,
                 candidate.date, candidate.listed_on, int(candidate.trusted), int(not initialized),
                 now, now, 0 if exists else None))

    def list_checkpoints(self, scope_key):
        return {row['section']: (row['next_url'], bool(row['trusted'])) for row in
                self.db.execute('SELECT section,next_url,trusted FROM list_checkpoints WHERE scope_key=?',
                                (scope_key,))}

    def save_list_checkpoint(self, scope_key, section, next_url, trusted):
        if next_url:
            self.db.execute('''INSERT INTO list_checkpoints VALUES(?,?,?,?)
                ON CONFLICT(scope_key,section) DO UPDATE SET next_url=excluded.next_url,trusted=excluded.trusted''',
                (scope_key, section, next_url, int(trusted)))
        else:
            self.db.execute('DELETE FROM list_checkpoints WHERE scope_key=? AND section=?', (scope_key, section))

    def pending_candidates(self, scope_key: str, *, cutoff: str | None, keywords: tuple[str, ...]) -> list[dict]:
        rows = self.db.execute('''SELECT * FROM notice_candidates WHERE scope_key=?
            AND (? IS NULL OR listed_date IS NULL OR listed_date>=?)
            ORDER BY (last_attempt_seq IS NOT NULL),
                CASE WHEN last_attempt_seq IS NULL THEN listed_date END DESC,
                last_attempt_seq ASC,notice_key ASC''', (scope_key, cutoff, cutoff))
        return [dict(row) for row in rows if not keywords or
                any(k.casefold() in row['title'].casefold() for k in keywords)]

    def record_attempt(self, scope_key: str, notice_key: str, outcome: str, *, stored_notice_key: str | None = None) -> None:
        if outcome not in {'success', 'fetch_error', 'access_issue', 'parse_error', 'excluded'}:
            raise ValueError('未知详情尝试结果')
        self.db.execute('''UPDATE notice_candidates SET
            last_attempt_seq=(SELECT COALESCE(MAX(last_attempt_seq),0)+1 FROM notice_candidates),
            last_attempt_at=?,last_outcome=?,
            stored_notice_key=COALESCE(?,stored_notice_key)
            WHERE scope_key=? AND notice_key=?''',
            (utc_now(), outcome, stored_notice_key, scope_key, notice_key))

    def list_notices(self, *, cutoff: str | None = None, source_id: str | None = None,
                     query: str = '', offset: int = 0, limit: int = 50, fresh_only: bool = False,
                     favorite_only: bool = False) -> dict:
        rows = self.db.execute('''SELECT n.*,c.source_id,c.source_name FROM notices n
            LEFT JOIN notice_candidates c ON COALESCE(c.stored_notice_key,c.notice_key)=n.key
            WHERE (n.cleaned_at IS NULL OR n.favorite=1)
              AND (? IS NULL OR n.published_date IS NULL OR n.published_date>=?)''', (cutoff, cutoff))
        notices = {}
        for row in rows:
            key = row['key']
            if key not in notices:
                item = {name: row[name] for name in
                        ('key','title','url','published_date','summary','content_hash','first_seen','last_seen','detail_parsed')}
                if item['detail_parsed'] is not None:
                    item['detail_parsed'] = bool(item['detail_parsed'])
                item.update(sources=[], source_attribution='unknown', alternative_urls=[],
                            last_checked_at=item['last_seen'], state='seen',
                            favorite=bool(row['favorite']), read=row['read_at'] is not None)
                notices[key] = item
            if row['source_id'] and not any(s['id'] == row['source_id'] for s in notices[key]['sources']):
                notices[key]['sources'].append({'id': row['source_id'], 'name': row['source_name']})
                notices[key]['source_attribution'] = 'known'
        latest = self.latest_result()
        states = {}
        for item in latest.get('items', []) if latest else []:
            for url in [item['url'], *item.get('alternative_urls', [])]:
                states[url] = item['state']
        for item in notices.values():
            item['state'] = states.get(item['url'], 'seen')
        items = merge_notices(list(notices.values()))
        if fresh_only:
            # Inbox rule: hide already-dismissed (read) and previously-collected
            # (state 'seen') notices, but never hide a favorite.
            items = [item for item in items
                     if item.get('favorite') or (not item.get('read') and item.get('state') != 'seen')]
        if favorite_only:
            items = [item for item in items if item.get('favorite')]
        text = query.casefold()
        items = [item for item in items
                 if (not source_id or any(s['id'] == source_id for s in item['sources'])) and
                 (not text or text in (' '.join([item['title'], item['summary'],
                                                 *(s['name'] for s in item['sources'])])).casefold())]
        # Stable multi-key ordering: date DESC, first seen DESC, key ASC.
        items.sort(key=lambda item: item['key'])
        items.sort(key=lambda item: item['first_seen'], reverse=True)
        items.sort(key=lambda item: item['published_date'] or '', reverse=True)
        return {'items': items[offset:offset + limit], 'total': len(items)}

    def notice_timestamps(self, urls):
        """Durable first_seen/last_seen for notices whose url is in the given set."""
        if not urls:
            return {}
        placeholders = ','.join('?' for _ in urls)
        rows = self.db.execute(f'SELECT url, first_seen, last_seen FROM notices WHERE url IN ({placeholders})',
                               list(urls))
        return {row['url']: {'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
                             'last_checked_at': row['last_seen']} for row in rows}

    def notice_flags(self, keys):
        """favorite/read for notices whose key is in the given set.

        Merged copies are one card for the user, so a flag on any copy applies to
        every view of that notice, including rows written by older releases.
        """
        if not keys:
            return {}
        placeholders = ','.join('?' for _ in keys)
        rows = self.db.execute(
            f'SELECT key, content_hash, length(summary) AS size, favorite, read_at '
            f'FROM notices WHERE key IN ({placeholders})', list(keys)).fetchall()
        shared = {row['content_hash'] for row in rows if row['size'] > 60}
        group = {}
        if shared:
            for row in self.db.execute(
                    f'SELECT content_hash, favorite, read_at FROM notices '
                    f'WHERE content_hash IN ({",".join("?" for _ in shared)}) AND length(summary)>60',
                    list(shared)):
                favorite, read = group.get(row['content_hash'], (False, False))
                group[row['content_hash']] = (favorite or bool(row['favorite']),
                                              read or row['read_at'] is not None)
        result = {}
        for row in rows:
            matched = row['size'] > 60 and group.get(row['content_hash'])
            favorite, read = matched or (bool(row['favorite']), row['read_at'] is not None)
            result[row['key']] = {'favorite': favorite, 'read': read}
        return result

    def _merge_group(self, key):
        """Keys merged into one card by merge_notices (substantial identical bodies)."""
        row = self.db.execute('SELECT content_hash, summary FROM notices WHERE key=?', (key,)).fetchone()
        if row is None or len(row['summary']) <= 60:
            return [key]
        return [item['key'] for item in self.db.execute(
            'SELECT key FROM notices WHERE content_hash=? AND length(summary)>60', (row['content_hash'],))]

    def set_notice_flags(self, key, *, favorite=None, read=None):
        """Set favorite and/or read on a notice; returns the resulting flags."""
        keys = self._merge_group(key)
        placeholders = ','.join('?' for _ in keys)
        if favorite is not None:
            self.db.execute(f'UPDATE notices SET favorite=? WHERE key IN ({placeholders})',
                            (1 if favorite else 0, *keys))
        if read is not None:
            self.db.execute(f'UPDATE notices SET read_at=? WHERE key IN ({placeholders})',
                            (utc_now() if read else None, *keys))
        self.db.commit()
        row = self.db.execute('SELECT favorite, read_at FROM notices WHERE key=?', (key,)).fetchone()
        if row is None:
            return {'favorite': False, 'read': False}
        return {'favorite': bool(row['favorite']), 'read': row['read_at'] is not None}

    def cleanup_read_notices(self):
        """Hide read, non-favorited notices; returns the number of rows cleaned."""
        # Cleanup follows the merged card, so one copy cannot resurface on its own.
        groups = {}
        for row in self.db.execute('''SELECT key,content_hash,length(summary) AS size,
                favorite,read_at FROM notices WHERE cleaned_at IS NULL'''):
            group_key = 'hash:' + row['content_hash'] if row['size'] > 60 else 'key:' + row['key']
            groups.setdefault(group_key, []).append(row)
        now, cleaned = utc_now(), 0
        for members in groups.values():
            if not any(member['read_at'] is not None for member in members):
                continue
            if any(member['favorite'] for member in members):
                continue
            for member in members:
                self.db.execute('UPDATE notices SET cleaned_at=? WHERE key=?', (now, member['key']))
                cleaned += 1
        self.db.commit()
        return cleaned

    def latest_result(self) -> dict | None:
        row = self.db.execute('SELECT result_json FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def completed_run_count(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]

    def finish(self, started_at: str, stats: dict, *, result: dict) -> None:
        self.db.execute('INSERT INTO runs(started_at,finished_at,stats_json,result_json) VALUES (?,?,?,?)',
                        (started_at, utc_now(), json.dumps(stats, ensure_ascii=False),
                         json.dumps(result, ensure_ascii=False)))
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def close(self):
        self.db.close()
