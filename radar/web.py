"""Loopback-only, token-authenticated console with one same-thread task worker."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
import errno
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .config import add_source, json_read, save_config, update_source_rules, update_source_urls, validate_config
from .engine import Options
from .runner import ManualLoginSession, run_collection
from .storage import Store, app_lock, atomic_json
from .transport import FetchError

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {'running', 'awaiting_login'}
CSP = ("default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")


class APIError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _summary(result):
    return {key: value for key, value in result.items() if key != 'items'} if result is not None else None


def _fields(body, allowed, required=()):
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise APIError(400, 'invalid_fields', '请求字段不正确')


def _integer(value, name, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise APIError(400, 'invalid_parameter', name + ' 必须为范围内的非负整数')
    return value


def _safe_message(message):
    # Logs are status text, not a transport for credentials or remote responses.
    text = re.sub(r'https?://[^\s<>]+', '[网址]', str(message))
    text = re.sub(r'(?i)(cookie|authorization|ticket|password|token)\s*[:=]\s*\S+', r'\1=[隐藏]', text)
    return text.replace('\r', ' ').replace('\n', ' ')[:500]


class ConsoleController:
    def __init__(self, config_path: Path, data_dir: Path, output: Path, *, direct=True, timeout=15, delay=1):
        self.config_path, self.data_dir, self.output = (Path(path).resolve() for path in (config_path, data_dir, output))
        self.direct, self.timeout, self.delay = direct, timeout, delay
        self.instance_id, self.token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='radar-task')
        self.cancel_event, self.finish_event = threading.Event(), threading.Event()
        self.closing = False
        self.server = None
        self.job = {'id': None, 'type': None, 'status': 'idle', 'progress': None,
                    'logs': [], 'error': None, 'result_summary': None}

    @contextmanager
    def store(self):
        store = None
        try:
            store = Store(self.data_dir / 'notices.sqlite3')
            yield store
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise APIError(503, 'storage_unavailable', '无法读取通知数据库，请检查本地文件后重试') from None
        finally:
            if store is not None:
                store.close()

    def sources(self):
        try:
            raw = self.config_path.read_bytes()
            config = validate_config(json.loads(raw.decode('utf-8-sig')))
        except (OSError, ValueError, TypeError, KeyError):
            raise APIError(503, 'config_unavailable', '无法读取来源配置，请检查本地文件后重试') from None
        effective = [{**source, 'enabled': source.get('enabled', True),
                      'allow_robots_unavailable': source.get('allow_robots_unavailable', False)}
                     for source in config['sources']]
        return {'revision': hashlib.sha256(raw).hexdigest(), 'sources': effective}, config

    def state(self):
        with self.store() as store:
            latest = _summary(store.latest_result())
        with self.lock:
            job = deepcopy(self.job)
        return {'instance_id': self.instance_id, 'job': job, 'latest': latest,
                'session_available': ((self.data_dir / 'browser-profile').exists() or
                                      (self.data_dir / 'browser-state.json').exists())}

    def _idle(self):
        if self.closing or self.job['status'] in ACTIVE:
            raise APIError(409, 'task_conflict', '已有任务正在运行或控制台正在关闭')

    @contextmanager
    def _task_lock(self):
        lock = app_lock(self.data_dir)
        try:
            lock.__enter__()
        except RuntimeError:
            raise APIError(409, 'task_conflict', '已有采集或登录任务正在运行') from None
        except OSError:
            raise APIError(503, 'storage_unavailable', '无法取得本地任务锁') from None
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def mutate_sources(self, body, adding=False):
        _fields(body, {'revision', 'name', 'url'} if adding else {'revision', 'enabled_ids', 'source_urls', 'robots_unavailable_ids', 'source_rules'},
                {'revision', 'name', 'url'} if adding else {'revision', 'enabled_ids'})
        if not isinstance(body['revision'], str):
            raise APIError(400, 'invalid_fields', 'revision 必须是文本')
        with self.lock:
            self._idle()
            with self._task_lock():
                current, config = self.sources()
                if not hmac.compare_digest(current['revision'], body['revision']):
                    raise APIError(409, 'revision_conflict', '来源配置已改变，请重新载入后再保存；未保存选择仍保留')
                try:
                    if adding:
                        config = add_source(config, body['name'], body['url'])
                    else:
                        ids = body['enabled_ids']
                        if not isinstance(ids, list) or not ids or any(not isinstance(sid, str) for sid in ids):
                            raise ValueError('至少选择一个来源，enabled_ids 必须为来源编号数组')
                        if set(ids) - {source['id'] for source in config['sources']}:
                            raise ValueError('来源编号不存在')
                        config = update_source_urls(config, body.get('source_urls', {}))
                        config = update_source_rules(config, body.get('source_rules', {}))
                        permissions = body.get('robots_unavailable_ids')
                        if 'robots_unavailable_ids' in body:
                            if (not isinstance(permissions, list) or
                                    any(not isinstance(sid, str) for sid in permissions) or
                                    set(permissions) - {source['id'] for source in config['sources']}):
                                raise ValueError('规则许可必须为已知来源编号数组')
                        for source in config['sources']:
                            source['enabled'] = source['id'] in ids
                            if 'robots_unavailable_ids' in body:
                                source['allow_robots_unavailable'] = source['id'] in permissions
                        validate_config(config)
                except (ValueError, TypeError) as error:
                    raise APIError(400, 'invalid_source', '来源配置无效：' + _safe_message(error)) from None
                try:
                    save_config(self.config_path, config)
                except (OSError, ValueError):
                    raise APIError(503, 'config_unavailable', '来源配置保存失败，未确认保存成功') from None
                return self.sources()[0]

    def notices(self, query):
        if set(query) - {'view', 'days', 'q', 'source_id', 'offset', 'limit', 'fresh'} or any(len(v) != 1 for v in query.values()):
            raise APIError(400, 'invalid_parameter', '查询参数不正确')
        values = {key: value[0] for key, value in query.items()}
        view = values.get('view', 'recent')
        if view not in {'recent', 'changes'}:
            raise APIError(400, 'invalid_parameter', 'view 必须为 recent 或 changes')
        if values.get('fresh', '0') not in {'0', '1'}:
            raise APIError(400, 'invalid_parameter', 'fresh 必须为 0 或 1')
        numbers = {}
        for key, default in (('days', 30), ('offset', 0), ('limit', 50)):
            text = values.get(key, str(default))
            if not re.fullmatch(r'[0-9]+', text) or len(text) > 9:
                raise APIError(400, 'invalid_parameter', key + ' 参数无效')
            numbers[key] = _integer(int(text), key, 100 if key == 'limit' else None)
        days, offset, limit = numbers['days'], numbers['offset'], numbers['limit']
        if limit == 0:
            raise APIError(400, 'invalid_parameter', 'limit 必须为 1 至 100')
        try:
            cutoff = (date.today() - timedelta(days=days)).isoformat() if days else None
        except OverflowError:
            raise APIError(400, 'invalid_parameter', 'days 超出日期范围') from None
        source_id, text = values.get('source_id') or None, values.get('q', '')
        with self.store() as store:
            if view == 'recent':
                result = store.list_notices(cutoff=cutoff, source_id=source_id, query=text, offset=offset,
                                            limit=limit, fresh_only=values.get('fresh') == '1')
            else:
                latest = store.latest_result()
                if latest is None:
                    return {'items': [], 'total': 0, 'offset': offset, 'limit': limit,
                            'recorded': False, 'message': '尚未记录本轮变化报告，请手动开始采集'}
                items = [item for item in latest.get('items', []) if item.get('state') in {'baseline', 'new', 'updated'}]
                if items:
                    # Result items preserve this run's states; SQLite supplies the
                    # durable timestamps and user flags for the notices referenced by this run.
                    by_url = store.notice_timestamps([item['url'] for item in items])
                    for item in items:
                        stored = by_url.get(item['url'], {})
                        for field in ('first_seen', 'last_seen', 'last_checked_at'):
                            if field in stored:
                                item[field] = stored[field]
                    flags = store.notice_flags([item['key'] for item in items])
                    for item in items:
                        item['favorite'] = flags.get(item.get('key'), {}).get('favorite', False)
                        item['read'] = flags.get(item.get('key'), {}).get('read', False)
                items = [item for item in items if
                         (not cutoff or not item.get('published_date') or item['published_date'] >= cutoff) and
                         (not source_id or any(source['id'] == source_id for source in item.get('sources', []))) and
                         (not text or text.casefold() in ' '.join([item.get('title', ''), item.get('summary', ''),
                          *(source['name'] for source in item.get('sources', []))]).casefold())]
                items.sort(key=lambda item: item.get('key', ''))
                items.sort(key=lambda item: item.get('first_seen', ''), reverse=True)
                items.sort(key=lambda item: item.get('published_date') or '', reverse=True)
                result = {'items': items[offset:offset + limit], 'total': len(items), 'recorded': True}
        return {**result, 'offset': offset, 'limit': limit}

    def set_notice_flags(self, body):
        _fields(body, {'key', 'favorite', 'read'}, {'key'})
        key = body['key']
        if not isinstance(key, str) or not key:
            raise APIError(400, 'invalid_parameter', 'key 必须为通知编号文本')
        favorite, read = body.get('favorite'), body.get('read')
        if favorite is not None and not isinstance(favorite, bool):
            raise APIError(400, 'invalid_parameter', 'favorite 必须为布尔值')
        if read is not None and not isinstance(read, bool):
            raise APIError(400, 'invalid_parameter', 'read 必须为布尔值')
        with self.store() as store:
            flags = store.set_notice_flags(key, favorite=favorite, read=read)
        return {'key': key, **flags}

    def cleanup_notices(self, body):
        _fields(body, set())
        with self.store() as store:
            removed = store.cleanup_read_notices()
        return {'removed': removed}

    def _log(self, text):
        with self.lock:
            self.job['logs'].append(_safe_message(text))
            del self.job['logs'][:-100]

    def _progress(self, value):
        with self.lock:
            self.job['progress'] = deepcopy(value)

    def start(self, kind, body):
        if kind == 'collect':
            _fields(body, {'mode', 'days'})
            mode, days = body.get('mode', 'auto'), _integer(body.get('days', 30), 'days')
            if not isinstance(mode, str) or mode not in {'auto', 'http', 'browser'}:
                raise APIError(400, 'invalid_parameter', 'mode 必须为 auto/http/browser')
            try:
                date.today() - timedelta(days=days)
            except OverflowError:
                raise APIError(400, 'invalid_parameter', 'days 超出日期范围') from None
            argument = Options(days=days, transport_mode=mode)
        else:
            _fields(body, {'source_id'})
            if 'source_id' in body and not isinstance(body['source_id'], str):
                raise APIError(400, 'invalid_parameter', 'source_id 必须为文本')
            argument = 'https://ids.xidian.edu.cn/authserver/login'
        with self.lock:
            self._idle()
            # Reject an already-running CLI before accepting a background task.
            # The worker acquires its own real lock again; it never uses this handle.
            with self._task_lock():
                _, config = self.sources()
                if kind == 'collect' and not any(source.get('enabled', True) for source in config['sources']):
                    raise APIError(400, 'no_sources', '没有启用的来源，请先保存来源选择')
                if kind == 'login' and 'source_id' in body:
                    source = next((source for source in config['sources'] if source['id'] == body['source_id']), None)
                    parts = urlsplit(source['url']) if source else None
                    host = parts.hostname if parts else ''
                    if not parts or parts.scheme != 'https' or not (host == 'xidian.edu.cn' or host.endswith('.xidian.edu.cn')):
                        raise APIError(400, 'invalid_source', '学校登录只允许当前配置中的学校 HTTPS 来源')
                    argument = source['url']
            self.cancel_event, self.finish_event = threading.Event(), threading.Event()
            self.job = {'id': secrets.token_urlsafe(18), 'type': kind, 'status': 'running',
                        'progress': None, 'logs': [], 'error': None, 'result_summary': None}
            self.executor.submit(self._work, kind, argument, self.cancel_event, self.finish_event)
            return {'job_id': self.job['id']}

    def _work(self, kind, argument, cancel, finish):
        session = None
        terminal = {'status': 'failed', 'error': {'code': 'task_failed', 'message': '任务未完成'}}
        try:
            if cancel.is_set():
                raise CancelledError()
            if kind == 'collect':
                result, _ = run_collection(self.config_path, self.data_dir, self.output, argument,
                    direct=self.direct, timeout=self.timeout, delay=self.delay,
                    log=self._log, progress=self._progress, cancel_event=cancel)
                summary = _summary(result)
            else:
                session = ManualLoginSession(self.data_dir, direct=self.direct)
                session.open(argument)
                with self.lock:
                    self.job['status'] = 'awaiting_login'
                self._log('请在专用 Chrome 本人登录，并访问目标站点；完成后点击“完成并保存”。')
                while True:
                    if cancel.is_set():
                        raise CancelledError()
                    if not session.pump(timeout_ms=200):
                        raise CancelledError()
                    if finish.is_set():
                        if cancel.is_set():
                            raise CancelledError()
                        session.finish()
                        break
                summary = {'message': '会话已保存；不代表所有站点认证均有效，请手动开始采集'}
                self._log('会话已保存')
            terminal = {'status': 'completed', 'result_summary': summary}
        except CancelledError:
            terminal = {'status': 'cancelled', 'error': None}
            self._log('任务已停止；未保存本次未提交结果。')
        except Exception as error:
            if isinstance(error, FetchError):
                code, message = error.code, _safe_message(error)
            elif isinstance(error, RuntimeError):
                code, message = 'task_conflict', '任务锁冲突，请等待其他采集或登录任务结束'
            else:
                code, message = 'task_failed', '任务未完成，请检查来源配置、数据库、输出目录或浏览器后重试'
            terminal = {'status': 'failed', 'error': {'code': code, 'message': message}}
            self._log(message)
        finally:
            try:
                if session is not None:
                    session.cancel()
            except Exception:
                terminal = {'status': 'failed', 'error': {
                    'code': 'session_invalid', 'message': '专用登录窗口清理失败，请关闭窗口后重试'}}
            finally:
                # A terminal job is also a promise that its profile lock is released.
                with self.lock:
                    self.job.update(terminal)

    def signal(self, body, finish=False):
        _fields(body, {'job_id'}, {'job_id'})
        if not isinstance(body['job_id'], str):
            raise APIError(400, 'invalid_fields', 'job_id 必须为文本')
        with self.lock:
            required = {'awaiting_login'} if finish else ACTIVE
            if body['job_id'] != self.job['id'] or self.job['status'] not in required:
                raise APIError(409, 'stale_job', '任务已结束或不处于可操作状态')
            (self.finish_event if finish else self.cancel_event).set()
        return {'accepted': True}

    def request_shutdown(self, body):
        _fields(body, set())
        with self.lock:
            self._idle()
            self.closing = True
        return {'closed': True}

    def close(self):
        with self.lock:
            self.closing = True
            self.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=False)


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, controller):
        self.controller = controller
        super().__init__(address, ConsoleHandler)
        controller.server = self

    def shutdown_request(self, request):
        # Immediate close with an unread rejected body can discard the HTTP error
        # on Windows. Half-close first, then boundedly drain until the peer closes.
        try:
            request.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.2
            while (remaining := deadline - time.monotonic()) > 0:
                request.settimeout(remaining)
                if not request.recv(65536):
                    break
        except OSError:
            pass
        finally:
            self.close_request(request)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = 'NoticeRadar'
    sys_version = ''

    def log_message(self, *args):
        pass  # Never log a token, request body, query, or private path.

    def send_error(self, code, message=None, explain=None):
        if code != 501:
            return super().send_error(code, message, explain)
        # BaseHTTPRequestHandler's unknown-method path must not bypass API auth.
        try:
            if urlsplit(self.path).path.startswith('/api/'):
                self._authorize(writing=True)
            error = APIError(404, 'not_found', '接口不存在')
        except APIError as denied:
            error = denied
        self.close_connection = True
        self._send(error.status, {'error': {'code': error.code, 'message': error.message}})

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _send(self, status, body=None, content_type='application/json; charset=utf-8'):
        data = (json.dumps(body, ensure_ascii=False).encode('utf-8') if content_type.startswith('application/json')
                else body or b'')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _authorize(self, writing):
        controller = self.server.controller
        hosts, tokens = self.headers.get_all('Host', []), self.headers.get_all('X-Radar-Token', [])
        expected = f'127.0.0.1:{self.server.server_port}'
        if hosts != [expected] or len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode(), controller.token.encode()):
            raise APIError(403, 'forbidden', '控制台授权已失效，请重新双击 run.bat 打开控制台')
        if writing:
            if self.headers.get_all('Origin', []) != ['http://' + expected]:
                raise APIError(403, 'forbidden', '仅允许本控制台的同源请求')

    def _body(self):
        if self.headers.get('Transfer-Encoding') is not None:
            raise APIError(400, 'invalid_json', '不支持分块请求正文')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise APIError(400, 'invalid_json', '需要明确的 JSON 正文长度')
        length = int(lengths[0])
        if length > 65536:
            raise APIError(413, 'body_too_large', 'JSON 正文不能超过 64KiB')
        content_types = self.headers.get_all('Content-Type', [])
        if len(content_types) != 1 or content_types[0].split(';')[0].strip().lower() != 'application/json':
            raise APIError(400, 'invalid_json', '请求必须使用 application/json')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError('invalid constant')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete body')
            body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, OSError, RecursionError):
            raise APIError(400, 'invalid_json', 'JSON 正文无效') from None
        if not isinstance(body, dict):
            raise APIError(400, 'invalid_json', 'JSON 正文必须为对象')
        return body

    def _dispatch(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        controller = self.server.controller
        api = path.startswith('/api/')
        if api:
            self._authorize(self.command != 'GET')
            if self.command == 'GET':
                if path == '/api/state' and not parsed.query:
                    self._send(200, controller.state())
                elif path == '/api/sources' and not parsed.query:
                    self._send(200, controller.sources()[0])
                elif path == '/api/notices':
                    self._send(200, controller.notices(parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)))
                else:
                    raise APIError(404, 'not_found', '接口不存在')
                return
            if parsed.query:
                raise APIError(400, 'invalid_parameter', '写请求不接受查询参数')
            body = self._body()
            route = (self.command, path)
            if route == ('PUT', '/api/sources'):
                self._send(200, controller.mutate_sources(body))
            elif route == ('POST', '/api/sources'):
                self._send(201, controller.mutate_sources(body, adding=True))
            elif route in {('POST', '/api/collect'), ('POST', '/api/login')}:
                self._send(202, controller.start(path.rsplit('/', 1)[1], body))
            elif route == ('POST', '/api/login/finish'):
                self._send(202, controller.signal(body, finish=True))
            elif route == ('POST', '/api/cancel'):
                self._send(202, controller.signal(body))
            elif route == ('PUT', '/api/notice'):
                self._send(200, controller.set_notice_flags(body))
            elif route == ('POST', '/api/notices/cleanup'):
                self._send(200, controller.cleanup_notices(body))
            elif route == ('POST', '/api/shutdown'):
                self._send(200, controller.request_shutdown(body))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                raise APIError(404, 'not_found', '接口不存在')
        elif self.command == 'GET' and path in {'/', '/console.js', '/console.css', '/favicon.ico'} and not parsed.query:
            if path == '/favicon.ico':
                self._send(204, b'', 'image/x-icon')
            else:
                name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                              '/console.js': ('console.js', 'text/javascript; charset=utf-8'),
                              '/console.css': ('console.css', 'text/css; charset=utf-8')}[path]
                try:
                    data = (ROOT / name).read_bytes()
                except OSError:
                    raise APIError(503, 'static_unavailable', '控制台页面文件不可用') from None
                self._send(200, data, mime)
        else:
            raise APIError(404, 'not_found', '路径不存在')

    def _handle(self):
        try:
            self._dispatch()
        except APIError as error:
            self.close_connection = True
            self._send(error.status, {'error': {'code': error.code, 'message': error.message}})
        except (OSError, sqlite3.Error):
            self.close_connection = True
            self._send(503, {'error': {'code': 'local_io_failed', 'message': '本地文件操作失败，请检查配置与数据目录'}})
        except (ValueError, TypeError, OverflowError):
            self.close_connection = True
            self._send(400, {'error': {'code': 'invalid_parameter', 'message': '请求参数无效'}})
        except Exception:
            self.close_connection = True
            self._send(503, {'error': {'code': 'service_unavailable', 'message': '控制台暂时无法处理请求'}})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle


def create_server(controller: ConsoleController, *, port=0):
    return ConsoleHTTPServer(('127.0.0.1', port), controller)


def _existing_connection(path, config_path, output):
    try:
        metadata = json_read(path)
        if (metadata['config_path'] != str(config_path) or metadata['output_path'] != str(output) or
                type(metadata['port']) is not int or not 1 <= metadata['port'] <= 65535 or
                not isinstance(metadata['token'], str) or not isinstance(metadata['instance_id'], str)):
            return None
        connection = http.client.HTTPConnection('127.0.0.1', metadata['port'], timeout=2)
        try:
            connection.request('GET', '/api/state', headers={'X-Radar-Token': metadata['token']})
            response = connection.getresponse()
            payload = response.read(2 * 1024 * 1024)
            if response.status != 200 or json.loads(payload)['instance_id'] != metadata['instance_id']:
                return None
        finally:
            connection.close()
        return metadata
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return None


def _open_console(metadata, open_browser):
    address = f"http://127.0.0.1:{metadata['port']}/"
    print('通知雷达控制台：' + address, flush=True)
    if open_browser:
        webbrowser.open(address + '#token=' + metadata['token'])


def serve_console(config_path: Path, data_dir: Path, output: Path, *, port=8765,
                  open_browser=True, direct=True, timeout=15, delay=1) -> int:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('端口必须为 0 至 65535')
    config_path, data_dir, output = (Path(path).resolve() for path in (config_path, data_dir, output))
    connection_path = data_dir / 'console' / 'connection.json'
    existing = _existing_connection(connection_path, config_path, output)
    if existing:
        _open_console(existing, open_browser)
        return 0
    lock = app_lock(data_dir / 'console')
    try:
        lock.__enter__()
    except RuntimeError:
        existing = _existing_connection(connection_path, config_path, output)
        if existing:
            _open_console(existing, open_browser)
            return 0
        raise RuntimeError('已有控制台尚未就绪或使用不同配置/输出；请在原控制台关闭后重试') from None
    controller = server = None
    try:
        controller = ConsoleController(config_path, data_dir, output, direct=direct, timeout=timeout, delay=delay)
        controller.sources()
        with controller.store():
            pass
        try:
            server = create_server(controller, port=port)
        except OSError as error:
            if port != 8765 or (error.errno != errno.EADDRINUSE and getattr(error, 'winerror', None) != 10048):
                raise
            server = create_server(controller, port=0)
        metadata = {'instance_id': controller.instance_id, 'port': server.server_port, 'token': controller.token,
                    'config_path': str(config_path), 'output_path': str(output)}
        atomic_json(connection_path, metadata)
        if os.name != 'nt':
            connection_path.chmod(0o600)
        _open_console(metadata, open_browser)
        print('关闭标签不会停止服务；请用页面“关闭控制台”或终端 Ctrl+C。', flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            controller.cancel_event.set()
        return 0
    finally:
        if controller is not None:
            controller.close()
        if server is not None:
            server.server_close()
        if controller is not None:
            try:
                metadata = json_read(connection_path)
                if metadata.get('instance_id') == controller.instance_id:
                    connection_path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                pass
        lock.__exit__(None, None, None)
