"""Single-task execution and same-thread dedicated manual login lifecycle."""
from __future__ import annotations

from concurrent.futures import CancelledError
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .config import json_read, session_domains, validate_config
from .engine import Collector, Options
from .parsing import normal_url
from .storage import Store, app_lock, save_browser_state, session_domain_allowed
from .transport import AutoTransport, FetchError, chrome_launch_options, restore_browser_state


def run_collection(config_path: Path, data_dir: Path, output: Path, options: Options, *,
                   source_ids=None, all_sources=False, direct=True, timeout=15, delay=1,
                   log=print, progress=None, cancel_event=None) -> tuple[dict, tuple[Path, Path]]:
    if source_ids and all_sources:
        raise ValueError('--only 与 --all 不能同时使用')
    with app_lock(data_dir):
        config = validate_config(json_read(config_path))
        wanted = set(source_ids or [])
        if wanted - {source['id'] for source in config['sources']}:
            raise ValueError('有来源编号不存在，请先 sources list')
        sources = ([source for source in config['sources'] if source['id'] in wanted] if wanted else
                   [source for source in config['sources'] if all_sources or source.get('enabled', True)])
        if not sources:
            raise ValueError('没有启用的来源，请启用来源或导入来源清单')
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        log(f'本次检查 {len(sources)} 个来源。请先连接学校允许的校园网络；本程序不会配置 VPN 或绕过权限。')
        log('网络方式：'+('直连（不使用系统代理或 7890）' if direct else '使用系统代理（显式选择）'))
        log('只读访问，受分页与数量上限限制。可以停止；没有定时任务。')
        transport = AutoTransport(data_dir, timeout, delay, direct=direct, mode=options.transport_mode,
                                  session_domains=session_domains(config))
        store = None
        try:
            store = Store(data_dir / 'notices.sqlite3')
            return Collector(transport, store, options, log=log, progress=progress,
                             cancel_event=cancel_event).collect(sources, output.resolve())
        except BaseException:
            if store is not None:
                store.rollback()
            raise
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                transport.close()


def _login_host_allowed(hostname, domains):
    """Login stays inside the institution's own domains from this configuration."""
    return bool(hostname) and session_domain_allowed(hostname, domains)


class ManualLoginSession:
    """Own the profile lock and Playwright objects on one task thread.

    The caller pumps events while awaiting an explicit finish/cancel signal. A
    persistent Chrome profile owns Web Storage; the JSON snapshot supplements
    session cookies and is only written after an explicit successful finish.
    """

    def __init__(self, data_dir: Path, *, direct=True, domains=()):
        self.data_dir = data_dir
        self.direct = direct
        self.domains = tuple(domains)
        self._owner = None
        self._lock = None
        self._playwright = None
        self._context = None

    def _check_thread(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise RuntimeError('专用登录会话必须在同一任务线程操作')

    def open(self, url: str):
        self._check_thread()
        if self._context is not None:
            raise RuntimeError('登录窗口已打开')
        url = normal_url(url)
        parts = urlsplit(url)
        hostname = parts.hostname or ''
        if parts.scheme != 'https' or not _login_host_allowed(hostname, self.domains):
            raise ValueError('登录入口只允许本工具来源配置中的学校 HTTPS 域名')
        self._owner = threading.get_ident()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise FetchError('请先运行 setup.bat 安装浏览器依赖', code='browser_unavailable') from None
        lock = app_lock(self.data_dir)
        lock.__enter__()
        self._lock = lock
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.data_dir / 'browser-profile'), **chrome_launch_options(self.direct))
            restore_browser_state(self._context, self.data_dir / 'browser-state.json', self.domains)
            pages = self._context.pages
            page = pages[0] if pages else self._context.new_page()
            # No collection route guard: the person may authenticate in this window.
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
            except Exception:
                # The window remains available for the person to navigate manually.
                if not self._live_page():
                    raise CancelledError() from None
        except BaseException as error:
            self.cancel()
            if isinstance(error, (FetchError, CancelledError, KeyboardInterrupt)):
                raise
            raise FetchError('Chrome 无法启动，可能是专用窗口仍占用登录目录；请关闭后重试',
                             code='browser_unavailable') from None

    def _live_page(self):
        if self._context is None:
            return None
        try:
            return next((page for page in self._context.pages if not page.is_closed()), None)
        except Exception:
            return None

    def pump(self, timeout_ms=200) -> bool:
        """Dispatch browser events for at most 200 ms; False means the window closed."""
        self._check_thread()
        page = self._live_page()
        if page is None:
            return False
        try:
            page.wait_for_timeout(min(200, max(0, timeout_ms)))
        except Exception:
            if self._live_page() is None:
                return False
            raise FetchError('登录窗口事件处理失败，请关闭窗口后重试', code='session_invalid') from None
        return self._live_page() is not None

    def finish(self):
        self._check_thread()
        try:
            if not self.pump(0):
                raise CancelledError('登录窗口已关闭，会话未保存')
            try:
                save_browser_state(self._context, self.data_dir / 'browser-state.json', self.domains)
            except Exception:
                raise FetchError('未能保存会话；旧会话未被替换，请在窗口仍打开时重新完成登录',
                                 code='session_invalid') from None
        finally:
            self.cancel()

    def cancel(self):
        """Close and release without replacing the saved session snapshot."""
        self._check_thread()
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
                self._context = None
        finally:
            try:
                if self._playwright is not None:
                    try:
                        self._playwright.stop()
                    except Exception:
                        pass
                    finally:
                        self._playwright = None
            finally:
                if self._lock is not None:
                    self._lock.__exit__(None, None, None)
                    self._lock = None
