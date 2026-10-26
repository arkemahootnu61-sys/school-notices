"""Read-only transports; strict robots gates with per-source network-failure consent."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit

import requests
from protego import Protego

from .parsing import AccessIssue, allowed_url, access_check

USER_AGENT = 'XidianNoticeRadar/1.0 (personal notice reader; manual runs only)'
MAX_BYTES = 3_000_000


class FetchError(RuntimeError):
    def __init__(self, message, *, code='fetch_failed'):
        super().__init__(message)
        self.code = code


@dataclass
class Page:
    url: str
    text: str
    status: int
    content_type: str = ''


class Throttle:
    def __init__(self, delay: float):
        self.delay = max(.2, delay)
        self.last = {}
        self.crawl_delays = {}

    def wait(self, url):
        host = urlsplit(url).netloc
        delay = max(self.delay, self.crawl_delays.get(host, 0))
        wait = delay - (time.monotonic() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last[host] = time.monotonic()


def chrome_launch_options(direct=True):
    """Use installed Chrome only; never alter the user's proxy or everyday profile."""
    return {'channel': 'chrome', 'headless': False, 'accept_downloads': False,
            'viewport': {'width': 1280, 'height': 850},
            'args': ['--no-proxy-server'] if direct else []}


def restore_browser_state(context, state_file: Path):
    """The persistent profile owns Web Storage; JSON supplements session cookies."""
    if not state_file.exists():
        return
    try:
        state = json.loads(state_file.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or not isinstance(state.get('cookies'), list):
            raise ValueError('invalid state')
        cookies = state['cookies']
        for cookie in cookies:
            if (not isinstance(cookie, dict)
                    or not isinstance(cookie.get('name'), str)
                    or not isinstance(cookie.get('value'), str)
                    or not isinstance(cookie.get('domain'), str)
                    or not isinstance(cookie.get('path'), str)):
                raise ValueError('invalid cookie')
            domain = cookie['domain'].lstrip('.').lower()
            if domain != 'xidian.edu.cn' and not domain.endswith('.xidian.edu.cn'):
                raise ValueError('invalid cookie domain')
        if not isinstance(state.get('origins', []), list):
            raise ValueError('invalid origins')
        for origin in state.get('origins', []):
            if (not isinstance(origin, dict) or not isinstance(origin.get('origin'), str)
                    or not isinstance(origin.get('localStorage'), list)):
                raise ValueError('invalid origin')
            for entry in origin['localStorage']:
                if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str)
                        or not isinstance(entry.get('value'), str)):
                    raise ValueError('invalid storage entry')
        context.add_cookies(cookies)
    except Exception:
        raise FetchError('专用会话文件无效；请本人重新登录，原文件已保留', code='session_invalid') from None


def _authentication_url(url):
    parts = urlsplit(url)
    return bool(re.search(r'authserver|(?:^|[./_-])(?:login|signin|sso)(?:[./_?;-]|$)',
                          parts.netloc + parts.path, re.I))


def _redirect(current, next_url, hosts, *, raw=False):
    if _authentication_url(next_url):
        raise AccessIssue('跳转本人认证入口；请先在专用窗口手动登录', code='login_required')
    if urlsplit(current).scheme == 'https' and urlsplit(next_url).scheme != 'https':
        raise FetchError('拒绝 HTTPS 降级跳转', code='unsafe_redirect')
    if raw and urlsplit(current).path == '/robots.txt':
        before, after = urlsplit(current), urlsplit(next_url)
        if ((before.scheme, before.netloc) != (after.scheme, after.netloc)
                or after.path != '/robots.txt' or after.query):
            raise FetchError('规则文件跳转不在原站原路径，已停止', code='unsafe_redirect')
    try:
        return allowed_url(next_url, hosts)
    except ValueError:
        raise FetchError('跳转超出安全读取范围，已停止', code='unsafe_redirect') from None


class HttpTransport:
    def __init__(self, timeout=15, delay=1, retries=1, direct=True, *, throttle=None):
        self.timeout, self.retries = timeout, retries
        self.throttle = throttle if throttle is not None else Throttle(delay)
        self.session = requests.Session()
        if direct:
            self.session.trust_env = False
        self.session.headers.update({'User-Agent': USER_AGENT,
                                     'Accept': 'text/html,application/xhtml+xml,text/plain'})
        self.calls = 0
        self.blocked_methods = 0

    def fetch(self, url, hosts, *, raw=False):
        current = allowed_url(url, hosts)
        redirects = 0
        while True:
            response = None
            for attempt in range(self.retries + 1):
                self.throttle.wait(current)
                self.calls += 1
                try:
                    response = self.session.get(current, timeout=self.timeout,
                                                allow_redirects=False, stream=True)
                except requests.exceptions.ProxyError:
                    raise FetchError('所选系统代理连接失败；请使用默认直连后重试', code='proxy_failed') from None
                except requests.exceptions.SSLError:
                    raise FetchError('站点 TLS 安全连接失败；未降低安全要求', code='tls_failed') from None
                except requests.exceptions.Timeout:
                    if attempt < self.retries:
                        time.sleep(1 + attempt)
                        continue
                    raise FetchError('连接站点超时，请稍后重试', code='timeout') from None
                except requests.exceptions.ConnectionError:
                    if attempt < self.retries:
                        time.sleep(1 + attempt)
                        continue
                    raise FetchError('无法连接站点，请检查站点或授权网络可达性', code='connection_failed') from None
                except requests.RequestException:
                    raise FetchError('无法完成网页请求', code='http_error') from None
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    pause = response.headers.get('Retry-After', '')
                    response.close()
                    if pause.isdigit() and int(pause) > 30:
                        raise FetchError('站点要求较长时间后重试，本轮跳过', code='http_error')
                    time.sleep(max(2, int(pause) if pause.isdigit() else 2 + attempt))
                    continue
                break
            assert response is not None
            status = response.status_code
            mime = response.headers.get('Content-Type', '')
            if status in (301, 302, 303, 307, 308):
                next_url = urljoin(current, response.headers.get('Location', ''))
                response.close()
                current = _redirect(current, next_url, hosts, raw=raw)
                redirects += 1
                if redirects > 5:
                    raise FetchError('跳转次数超过安全上限', code='unsafe_redirect')
                continue
            if raw and status in (401, 403, 404, 410):
                response.close()
                return Page(current, '', status, mime)
            if status in (401, 403):
                response.close()
                raise AccessIssue('站点要求本人登录或访问授权；未绕过访问控制',
                                  code='login_required' if status == 401 else 'access_denied')
            if status >= 400:
                response.close()
                raise FetchError(f'站点返回 HTTP {status}', code='http_error')
            if mime and not any(kind in mime.lower() for kind in ('text/', 'html', 'xml', 'json')):
                response.close()
                if raw:
                    return Page(current, '', status, mime)
                raise FetchError('跳过非网页资源，不自动下载附件', code='resource_limit')
            data, size = [], 0
            try:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError('网页超过 3 MB 安全读取上限', code='resource_limit')
                    data.append(chunk)
            except requests.exceptions.ProxyError:
                raise FetchError('代理读取中断', code='proxy_failed') from None
            except requests.exceptions.SSLError:
                raise FetchError('TLS 读取中断', code='tls_failed') from None
            except requests.RequestException:
                raise FetchError('读取网页中断', code='read_interrupted') from None
            finally:
                response.close()
            payload = b''.join(data)
            match = re.search(br'charset\s*=\s*["\x27]?\s*([\w-]+)', payload[:12000], re.I)
            encoding = match[1].decode('ascii') if match else response.encoding
            if not encoding or encoding.lower() in ('iso-8859-1', 'ascii'):
                encoding = 'utf-8'
            try:
                text = payload.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                try:
                    text = payload.decode('utf-8')
                except UnicodeDecodeError:
                    text = payload.decode('gb18030', errors='replace')
            if not raw:
                access_check(text, current)
            return Page(current, text, status, mime)

    def close(self):
        self.session.close()


class BrowserTransport:
    """Dedicated read-only Chrome, with authentication redirects never followed."""
    def __init__(self, data_dir: Path, timeout=15, delay=1, direct=True, *, throttle=None):
        self.context = None
        self.playwright = None
        self.data_dir = Path(data_dir)
        self.timeout = timeout * 1000
        self.throttle = throttle if throttle is not None else Throttle(delay)
        self.hosts = set()
        self.blocked_methods = 0
        self.calls = 0
        self._navigation_issue = None
        self._raw_target = None
        self._page_origin = None
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.data_dir / 'browser-profile'), **chrome_launch_options(direct),
                service_workers='block', user_agent=USER_AGENT)
            restore_browser_state(self.context, self.data_dir / 'browser-state.json')
            self.page = self.context.new_page()
            self.context.route('**/*', self._guard)
        except Exception as error:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            if isinstance(error, FetchError):
                raise
            raise FetchError('专用 Chrome 不可用；请确认已安装 Chrome 且资料目录未被占用',
                             code='browser_unavailable') from None

    def _guard(self, route):
        request = route.request
        main_document = request.resource_type == 'document'
        if hasattr(request, 'frame'):
            main_document = main_document and request.frame.parent_frame is None
        active_document = main_document
        if hasattr(request, 'frame'):
            active_document = main_document and request.frame is self.page.main_frame
        if request.method not in ('GET', 'HEAD'):
            self.blocked_methods += 1
            if active_document and _authentication_url(request.url):
                self._navigation_issue = AccessIssue('认证导航已阻止；请先本人手动登录', code='login_required')
            route.abort()
            return
        if request.resource_type in ('image', 'media', 'font'):
            route.abort()
            return
        try:
            if main_document and _authentication_url(request.url):
                raise AccessIssue('浏览器转向本人登录入口；请先手动登录', code='login_required')
            if main_document and urlsplit(request.url).scheme != 'https':
                raise FetchError('拒绝 HTTPS 降级跳转', code='unsafe_redirect')
            allowed_url(request.url, self.hosts)
            if main_document and getattr(self, '_raw_target', None):
                target = urlsplit(self._raw_target)
                actual = urlsplit(request.url)
                if (target.scheme, target.netloc, target.path, target.query) != (
                        actual.scheme, actual.netloc, actual.path, actual.query):
                    raise FetchError('规则文件跳转不在原站原路径', code='unsafe_redirect')
        except (ValueError, AccessIssue, FetchError) as error:
            if active_document:
                self._navigation_issue = (error if isinstance(error, (AccessIssue, FetchError)) else
                                          FetchError('浏览器跳转超出安全读取范围', code='unsafe_redirect'))
            route.abort()
            return
        # Continue through any test fixture or other read-only route, without bypassing this guard.
        route.fallback()

    def fetch(self, url, hosts, *, raw=False):
        target = allowed_url(url, hosts)
        if urlsplit(target).scheme != 'https':
            raise FetchError('专用浏览器只允许已确认的 HTTPS 入口', code='unsafe_redirect')
        self.hosts = set(hosts)
        self._navigation_issue = None
        self._raw_target = target if raw and urlsplit(target).path == '/robots.txt' else None
        self.throttle.wait(target)
        self.calls += 1
        try:
            origin = (urlsplit(target).scheme, urlsplit(target).netloc)
            if self._page_origin is not None and self._page_origin != origin:
                # Failed Chrome pages can retry their old origin during a later goto.
                # Close that document; its late guarded requests must not taint this read.
                previous = self.page
                self.page = self.context.new_page()
                previous.close()
                self._navigation_issue = None
            self._page_origin = origin
            response = self.page.goto(target, wait_until='domcontentloaded', timeout=self.timeout)
            self.page.wait_for_timeout(500)
            if self._navigation_issue:
                raise self._navigation_issue
            final = _redirect(target, self.page.url, self.hosts, raw=raw)
            status = response.status if response else 200
            mime = (response.header_value('content-type') or '') if response else ''
            if raw and status in (401, 403, 404, 410):
                return Page(final, '', status, mime)
            if status in (401, 403):
                raise AccessIssue('站点要求本人登录或访问授权；未绕过访问控制',
                                  code='login_required' if status == 401 else 'access_denied')
            if status >= 400:
                raise FetchError(f'站点返回 HTTP {status}', code='http_error')
            if mime and not any(kind in mime.lower() for kind in ('text/', 'html', 'xml', 'json')):
                if raw:
                    return Page(final, '', status, mime)
                raise FetchError('跳过非网页资源，不自动下载附件', code='resource_limit')
            if raw and mime.split(';', 1)[0].strip().lower() == 'text/plain':
                pre = self.page.locator('body > pre')
                text = pre.first.inner_text() if pre.count() else self.page.content()
            else:
                text = self.page.content()
            if len(text.encode('utf-8')) > MAX_BYTES:
                raise FetchError('网页超过 3 MB 安全读取上限', code='resource_limit')
            if not raw:
                access_check(text, final)
            return Page(final, text, status, mime)
        except (AccessIssue, FetchError):
            raise
        except Exception as error:
            if self._navigation_issue:
                raise self._navigation_issue from None
            name = type(error).__name__
            message = str(error).upper()
            if name == 'TimeoutError' or 'ERR_TIMED_OUT' in message:
                code, text = 'timeout', '浏览器读取超时'
            elif 'ERR_CERT_' in message or 'ERR_SSL_' in message:
                code, text = 'tls_failed', '浏览器 TLS 安全连接失败'
            elif 'ERR_PROXY_' in message or 'ERR_TUNNEL_' in message:
                code, text = 'proxy_failed', '浏览器代理连接失败'
            elif 'ERR_CONNECTION_' in message or 'ERR_NAME_NOT_RESOLVED' in message:
                code, text = 'connection_failed', '浏览器无法连接站点'
            else:
                code, text = 'read_interrupted', '浏览器读取中断；请检查专用窗口和站点可达性'
            raise FetchError(text, code=code) from None
        finally:
            self._raw_target = None

    def close(self):
        from .storage import save_browser_state
        try:
            save_browser_state(self.context, self.data_dir / 'browser-state.json')
        except Exception:
            print('提示：浏览器会话未能更新保存，原会话文件已保留。')
        finally:
            try:
                self.context.close()
            finally:
                self.playwright.stop()


class AutoTransport:
    """HTTP first, with one Chrome attempt only for the explicit eligible failures."""
    def __init__(self, data_dir, timeout=15, delay=1, direct=True, *, mode='auto'):
        if mode not in ('auto', 'http', 'browser'):
            raise ValueError('读取模式必须是 auto/http/browser')
        self.data_dir, self.timeout, self.delay, self.direct = Path(data_dir), timeout, delay, direct
        self.mode = mode
        self.throttle = Throttle(delay)
        self.http = None if mode == 'browser' else HttpTransport(timeout, delay, direct=direct,
                                                                throttle=self.throttle)
        self.browser = None
        self._browser_unavailable = False

    @property
    def calls(self):
        return sum(transport.calls for transport in (self.http, self.browser) if transport is not None)

    @property
    def blocked_methods(self):
        return sum(transport.blocked_methods for transport in (self.http, self.browser)
                   if transport is not None)

    def _chrome(self):
        if self._browser_unavailable:
            raise FetchError('本轮专用 Chrome 不可用', code='browser_unavailable')
        if self.browser is None:
            try:
                self.browser = BrowserTransport(self.data_dir, self.timeout, self.delay, self.direct,
                                                throttle=self.throttle)
            except FetchError as error:
                if error.code == 'browser_unavailable':
                    self._browser_unavailable = True
                raise
        return self.browser

    def fetch(self, url, hosts, *, raw=False):
        allowed_url(url, hosts)
        if self.mode == 'browser':
            return self._chrome().fetch(url, hosts, raw=raw)
        try:
            return self.http.fetch(url, hosts, raw=raw)
        except (FetchError, AccessIssue) as error:
            if self.mode != 'auto':
                raise
            eligible = isinstance(error, FetchError) and error.code in (
                'connection_failed', 'timeout', 'read_interrupted')
            session_available = ((self.data_dir / 'browser-profile').exists()
                                 or (self.data_dir / 'browser-state.json').exists())
            if not raw and isinstance(error, AccessIssue) and error.code == 'login_required':
                eligible = session_available
            if not eligible:
                raise
            try:
                return self._chrome().fetch(url, hosts, raw=raw)
            except (FetchError, AccessIssue) as browser_error:
                error_type = AccessIssue if isinstance(browser_error, AccessIssue) else FetchError
                raise error_type(
                    f'HTTP[{error.code}]；Chrome[{browser_error.code}]：{browser_error}',
                    code=browser_error.code) from None

    def close(self):
        try:
            if self.http is not None:
                self.http.close()
        finally:
            if self.browser is not None:
                self.browser.close()


def _rules_text(page):
    access_check(page.text, page.url)
    mime = page.content_type.split(';', 1)[0].strip().lower()
    text = page.text
    html = re.search(r'<\s*(?:!doctype\b|/?[a-z][a-z0-9]*\b[^>]*>)', text, re.I)
    if mime and mime != 'text/plain':
        raise FetchError('规则文件不是纯文本，本轮不采集', code='robots_invalid')
    if html:
        # BrowserTransport extracts Chrome's generated text/plain <pre> before this gate.
        # A transport payload containing HTML is never itself accepted as a rules file.
        raise FetchError('规则文件包含 HTML 内容，本轮不采集', code='robots_invalid')
    return text


class Robots:
    def __init__(self, transport):
        self.transport = transport
        self.cache = {}
        self._unverified = {}

    def allowed(self, url, hosts, *, allow_unavailable=False):
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.cache:
            try:
                page = self.transport.fetch(origin + '/robots.txt', hosts, raw=True)
                final = urlsplit(page.url)
                if (final.scheme, final.netloc, final.path, final.query) != (
                        parts.scheme, parts.netloc, '/robots.txt', ''):
                    raise FetchError('无法核实原站规则文件', code='robots_invalid')
                if page.status in (401, 403):
                    raise FetchError('站点拒绝读取规则文件，本轮不采集', code='robots_denied')
                if page.status in (404, 410):
                    self.cache[origin] = (None, None, False)
                elif page.status >= 400 or page.status < 200 or page.status >= 300:
                    raise FetchError('规则文件暂时不可用，本轮不采集', code='robots_unavailable')
                else:
                    parser = Protego.parse(_rules_text(page).lstrip('\ufeff'))
                    crawl_delay = parser.crawl_delay(USER_AGENT) or 0
                    self.transport.throttle.crawl_delays[parts.netloc] = crawl_delay
                    self.cache[origin] = (parser, None, False)
            except AccessIssue:
                self.cache[origin] = (None, FetchError('规则文件要求认证或本人验证，本轮不采集',
                                                      code='robots_invalid'), False)
            except (FetchError, ValueError) as error:
                cause_code = getattr(error, 'code', '')
                network_failure = cause_code in {'connection_failed', 'timeout', 'read_interrupted'}
                code = cause_code
                if code not in ('robots_denied', 'robots_invalid', 'robots_unavailable'):
                    code = 'robots_unavailable'
                reason = str(error) if isinstance(error, FetchError) else '规则文件格式无效'
                self.cache[origin] = (None, FetchError('无法核实站点规则：' + reason, code=code), network_failure)
        parser, error, network_failure = self.cache[origin]
        if error:
            if allow_unavailable is True and network_failure:
                self._unverified[origin] = '规则未核实；本人已明确许可继续读取本来源公开通知。' + str(error)
                return True
            raise error
        if parser and not parser.can_fetch(url, USER_AGENT):
            raise FetchError('站点规则不允许读取此路径，已跳过', code='robots_denied')
        return True

    def unverified_warning(self, url):
        parts = urlsplit(url)
        return self._unverified.get(f'{parts.scheme}://{parts.netloc}')
