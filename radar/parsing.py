"""Notice-only HTML extraction. No scripts, forms or attachment downloads."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

NOTICE = re.compile(r'通知|公告|公示|报名|招募|征集|选拔|申报|赛程|考试安排|培训安排|活动预告|讲座预告|招生简章|招聘|补选|补考|选课|竞赛安排')
EXPLICIT = re.compile(r'通知|公告|公示')
NEWS = re.compile(r'喜报|获佳绩|斩获|圆满|成功举办|顺利举行|荣获|获.{0,18}[一二三特]等奖|获奖喜报|新闻报道|纪实|侧记')
# 教学培训/公开课/教学示范/教学研讨等教师教研活动不是“通知”，即使出现在通知公告栏目也排除。
TEACHING_ACTIVITY = re.compile(r'^【\s*(?:教学培训|教学示范|公开课|人工智能赋能|教学研讨|教学沙龙)\s*】')
LIST_PATH = re.compile(r'tzgg|ggtz|/tz\.(?:htm|html)|jxtz|xsgz.*/tz|/jsl\.|/kjjs\.|/xkjs\.|/xsjs\.|cxsjxlds|gsxx|gsgg|hdyg|/tzgg\d*\.')
ARTICLE_PATH = re.compile(r'/info/\d+/\d+\.(?:htm|html)|/news/view/|/\d{4}/\d{2}/\d{2}/|/\d{5,}\.(?:htm|html|shtml)')
ACTION_PATH = re.compile(r'(?:^|[/_.?=&-])(logout|signout|delete|remove|unsubscribe|setlang|caslogout|submit|save|apply)(?:$|[/_.?=&-])', re.I)
SENSITIVE_KEY = re.compile(r'^(?:ticket|token|access_token|id_token|password|passwd|sessionid|jsessionid|samlresponse|authorization|secret|code|auth|cookie|sid)$', re.I)
DATE = re.compile(r'(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?:日)?(?!\d)')
ARTICLE_SELECTORS = ('.v_news_content', '#vsb_content', '#vsb_content_2', '.vsb_content', '.TRS_Editor', '.article-content', '.news-content', '#article_content', '#content', 'article')
# Campus templates put the title in varied headings: `<h2 class="title">`, `.title h4`, etc.
TITLE_SELECTORS = ('meta[name="ArticleTitle"]', '.arti-title', '.article-title', '.news-title',
                   'h1', 'h2.title', '.title h1', '.title h2', '.title h3', '.title h4', '.title h5')
INLINE_TAGS = {'a', 'b', 'em', 'font', 'i', 'small', 'span', 'strong', 'sub', 'sup'}
# `<article>` and `#content` usually wrap the heading as well as the text, so a
# heading inside them is the article title; other body regions hold text only.
CHROME_TAGS = {'header', 'footer', 'nav', 'aside'}
CHROME_MARKS = {'logo', 'banner', 'nav', 'navbar', 'subnav', 'menu', 'top', 'topbar',
                'header', 'footer', 'sidebar', 'side-bar', 'side', 'related', 'related-news'}
SELECTOR_FIELDS = ('list_selector', 'detail_link_selector', 'list_date_selector',
                   'next_page_selector', 'title_selector', 'date_selector', 'content_selector')
SECTION_LABEL = re.compile(r'通知公告|公告通知|通知|公告|公示公告|教学通知|竞赛通知|学生竞赛|科技竞赛|学科竞赛|创新实践系列大赛')
BINARY_PATH = re.compile(r'\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|jpe?g|png|gif|mp4|mp3)$', re.I)
NEXT_LABEL = re.compile(r'下一页|下页|下一頁|Next(?:\s*page)?|[>›»]', re.I)


def _navigation_link(anchor):
    for parent in anchor.parents:
        if parent.name in ('nav', 'header', 'footer', 'form'):
            return True
        names = [parent.get('id', ''), *parent.get('class', [])]
        if any(name.lower() in {'nav', 'navigation', 'menu', 'subnav', 'navbar', 'nav-menu',
                                'sub-nav', 'dropdown-menu', 'header', 'footer', 'top', 'topbar'} for name in names):
            return True
    return False


def _date_node(node):
    for attribute in ('datetime', 'content'):
        if node.get(attribute):
            return parse_date(node[attribute])
    text = node.get_text(' ', strip=True)
    parsed = parse_date(text)
    if parsed:
        return parsed
    # Calendar tiles commonly render day and year-month in separate child nodes.
    pieces = [text.strip() for text in node.stripped_strings]
    if len(pieces) == 2:
        for month, day in (pieces, pieces[::-1]):
            if re.fullmatch(r'20\d{2}[-/.年]\d{1,2}月?', month) and re.fullmatch(r'\d{1,2}日?', day):
                return parse_date(month.rstrip('月') + '-' + day.rstrip('日'))
    return None


class AccessIssue(RuntimeError):
    def __init__(self, message: str, *, code: str = 'access_denied'):
        super().__init__(message)
        self.code = code


def normal_url(value: str, base: str = '') -> str:
    parts = urlsplit(urljoin(base, value.strip()))
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('只接受不含账号密码的 HTTP(S) 网址')
    if any(SENSITIVE_KEY.search(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError('网址包含登录票据或敏感参数，请改用普通栏目网址')
    if ACTION_PATH.search(parts.path + '?' + parts.query):
        raise ValueError('跳过可能改变账号或业务状态的链接')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('spm', 'from', 'fbclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(query), ''))


def allowed_url(value: str, hosts: set[str], base: str = '') -> str:
    url = normal_url(value, base)
    if urlsplit(url).hostname not in hosts:
        raise ValueError('链接不在本来源明确允许的域名范围内')
    return url


def clean_title(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    # Broken heading markup (e.g. <h2>text</h1>) lets the metadata paragraph become
    # part of the title; cut it at the first labelled metadata field.
    text = re.split(r'\s*(?:文章来源|信息来源|发布日期|发布时间|发表时间|浏览次数|点击次数|点击数|访问量)\s*[:：]',
                    text, maxsplit=1)[0].strip()
    text = re.sub(r'\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\s*$', '', text).strip()
    # Campus templates often concatenate title attributes, image alt text and visible text.
    for _ in range(3):
        compact = text.strip()
        if len(compact) % 2 == 0 and compact[:len(compact)//2] == compact[len(compact)//2:]:
            text = compact[:len(compact)//2].strip()
        else:
            match = re.fullmatch(r'(.{6,}?)\s+\1', compact)
            if match: text = match[1].strip()
            else: break
    return text[:400]


def parse_date(text: str) -> str | None:
    match = DATE.search(text or '')
    if match:
        try: return date(*map(int, match.groups())).isoformat()
        except ValueError: pass
    return None


def notice_title(title: str, trusted_list: bool = False) -> bool:
    # An award-result announcement is a notice; a celebratory award report is not.
    if TEACHING_ACTIVITY.search(title): return False
    if EXPLICIT.search(title): return True
    if NEWS.search(title): return False
    return bool(NOTICE.search(title) or trusted_list)


def identity(url: str) -> str:
    parts = urlsplit(normal_url(url))
    query = parse_qs(parts.query)
    article = re.search(r'/info/\d+/(\d+)\.(?:htm|html)', parts.path)
    aid = article.group(1) if article else next(iter(query.get('wbnewsid', [])), None)
    raw = f'{parts.hostname}:article:{aid}' if aid else normal_url(url)
    return hashlib.sha256(raw.encode()).hexdigest()


def soup_page(content: str) -> BeautifulSoup:
    return BeautifulSoup(content, 'html.parser')


def access_check(content: str, url: str) -> None:
    soup = soup_page(content)
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    text = soup.get_text(' ', strip=True)[:16000]
    if '校外访问' in text and re.search('aTrust|零信任|校园网', text, re.I):
        raise AccessIssue('校园网络受限：请先连接学校允许的校园网 / aTrust，再重新运行',
                          code='campus_network_required')
    if re.search('统一身份认证|用户登录|用户登陆|登录平台', title) or '/authserver/login' in url:
        raise AccessIssue('需要本人登录：请打开学校登录入口手动登录', code='login_required')
    if soup.select_one('input[type="password"]'):
        raise AccessIssue('检测到登录表单；不会自动填写或提交，请先手动登录', code='login_required')
    if re.search('Access Denied|访问拒绝|拒绝访问|禁止访问', title, re.I):
        raise AccessIssue('站点拒绝访问，不绕过访问限制', code='access_denied')
    if re.search('安全验证|人机验证|访问验证|验证码|请稍候|Just a moment', title, re.I):
        raise AccessIssue('需要本人完成访问验证，不绕过验证', code='verification_required')
    if title.strip() == '系统提示':
        raise AccessIssue('站点返回系统提示，不把它当作成功读取的通知页面', code='access_denied')


@dataclass
class Candidate:
    url: str
    title: str
    date: str | None
    listed_on: str
    trusted: bool = False


@dataclass
class Listing:
    title: str
    candidates: list[Candidate]
    sections: list[str]
    next_pages: list[str]
    explicitly_empty: bool = False
    trusted: bool = False


def article_url(url: str) -> bool:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    return bool(ARTICLE_PATH.search(parts.path) or 'wbnewsid' in query or
                (parts.path.endswith('view.php') and ('aid' in query or 'id' in query)))


def _entry_date(anchor, target: str, root, custom_anchors: set[int], base: str,
                selector: str | None = None) -> str | None:
    """Only independent date nodes inside an unambiguous article entry count."""
    key = identity(target)
    entry = anchor
    for parent in anchor.parents:
        if parent.name in ('nav', 'header', 'footer', 'form'):
            break
        other_article = False
        for link in parent.select('a[href]'):
            try:
                linked = normal_url(link['href'], base)
            except ValueError:
                continue
            if (article_url(linked) or id(link) in custom_anchors) and identity(linked) != key:
                other_article = True
                break
        if other_article or len(parent.get_text(' ', strip=True)) > 700:
            break
        entry = parent
        if parent is root or parent.name in ('li', 'tr'):
            break
    dates = set()
    metadata = entry.select(selector) if selector else [
        node for node in entry.find_all(True) if node.name == 'time' or
        any(re.search(r'(?:^|[-_])(?:date|time)\d*(?:$|[-_])', name, re.I) for name in node.get('class', []))]
    for node in metadata:
        value = _date_node(node)
        if value:
            dates.add(value)
    for text in entry.find_all(string=True):
        if text.find_parent(['script', 'style', 'noscript']):
            continue
        if text.find_parent('a'):
            if text.parent.name not in ('span', 'b', 'em', 'time', 'small', 'i'):
                continue
            if any(parent.name in ('h1', 'h2', 'h3') or
                   any('title' in name.lower() for name in parent.get('class', []))
                   for parent in text.parents if parent is not entry):
                continue
        time = text.find_parent('time')
        if time and parse_date(time.get('datetime', '')):
            continue
        value = text.strip()
        if DATE.fullmatch(value):
            parsed = parse_date(value)
            if parsed:
                dates.add(parsed)
    return next(iter(dates)) if len(dates) == 1 else None


def extract_listing(content: str, url: str, hosts: set[str], source: dict, *,
                    trusted_section: bool = False) -> Listing:
    soup = soup_page(content)
    page_title = soup.title.get_text(' ', strip=True) if soup.title else ''
    trusted = bool(trusted_section or source.get('detail_link_selector') or
                   LIST_PATH.search(urlsplit(url).path) or EXPLICIT.search(page_title))
    root = soup.select_one(source['list_selector']) if source.get('list_selector') else soup
    if root is None: return Listing(page_title, [], [], [], trusted=trusted)
    candidates, sections, pagination = {}, [], []
    custom = source.get('detail_link_selector')
    anchors = root.select(custom) if custom else root.select('a[href]')
    custom_anchors = {id(anchor) for anchor in anchors}
    for anchor in anchors:
        href = anchor.get('href', '')
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')): continue
        try: target = allowed_url(href, hosts, url)
        except ValueError: continue
        label = anchor.get('title') or anchor.get_text(' ', strip=True)
        title = clean_title(label)
        if not title or title in ('首页', '返回', '更多', '更多+', '点击查看', '详细', '详情'): continue
        if _navigation_link(anchor) or BINARY_PATH.search(urlsplit(target).path): continue
        if SECTION_LABEL.fullmatch(title) or NEXT_LABEL.fullmatch(title): continue
        listed_date = _entry_date(anchor, target, root, custom_anchors, url, source.get('list_date_selector'))
        generic_entry = not LIST_PATH.search(urlsplit(target).path) and (
            bool(listed_date) or len(title) >= 6 and notice_title(title))
        if (article_url(target) or custom or generic_entry) and len(title) >= 4 and notice_title(title, trusted):
            candidates.setdefault(identity(target), Candidate(target, title, listed_date, url, trusted))
    # Discover only notice-related sections, never arbitrary site links.
    explicit_next = {id(anchor) for anchor in soup.select(source['next_page_selector'])} if source.get('next_page_selector') else set()
    for anchor in soup.select('a[href]'):
        label = clean_title(anchor.get('aria-label') or anchor.get('title') or anchor.get_text(' ', strip=True))
        try: target = allowed_url(anchor['href'], hosts, url)
        except ValueError: continue
        if target == normal_url(url) or article_url(target): continue
        if (id(anchor) in explicit_next or 'next' in anchor.get('rel', []) or NEXT_LABEL.fullmatch(label)):
            pagination.append(target)
        elif SECTION_LABEL.fullmatch(label) or LIST_PATH.search(urlsplit(target).path):
            # Do not treat historical numbered pagination as new sections.
            if not re.search(r'/\d+\.(?:htm|html)$', urlsplit(target).path):
                sections.append(target)
    return Listing(page_title, list(candidates.values()), list(dict.fromkeys(sections)),
                   list(dict.fromkeys(pagination))[:1],
                   bool(re.search('暂无相关|暂无信息|暂无内容|没有相关', soup.get_text(' ', strip=True))), trusted)


def _site_chrome(node, body=None):
    """True when a heading belongs to the site frame, not to one article.

    A heading that shares the body's own container is article content even when it
    sits in `<article><header>` or a `class="header"` title bar.
    """
    if body is not None and body.parent is not None and body.parent.name not in ('[document]', 'html', 'body'):
        if any(parent is body.parent for parent in node.parents):
            return False
    for parent in node.parents:
        if getattr(parent, 'name', None) in CHROME_TAGS:
            return True
        marks = {parent.get('id') or '', *(parent.get('class') or [])}
        if any(mark.lower() in CHROME_MARKS for mark in marks if mark):
            return True
    return False


def _enclosing_body(body):
    """A body region that also wraps the article heading instead of only its text."""
    return body.name == 'article'


def _find_title(soup, body=None):
    """Pick the formal heading; headings inside a text-only body are subheadings."""
    candidates, seen = [], set()
    for selector in TITLE_SELECTORS:
        for node in soup.select(selector):
            if id(node) in seen:
                continue
            seen.add(id(node))
            text = node.get_text(' ', strip=True)
            if node.name != 'meta' and text and not SECTION_LABEL.fullmatch(text):
                candidates.append(node)
    if not candidates:
        return None
    if body is None:
        return candidates[0]
    # Site headers, logos and related blocks never title an article.
    formal = [node for node in candidates if not _site_chrome(node, body)] or candidates
    ancestors, node, depth = {}, body, 0
    while node is not None:
        ancestors[id(node)] = depth
        node, depth = node.parent, depth + 1
    inside = [node for node in formal if any(parent is body for parent in node.parents)]
    outside = [node for node in formal if not any(parent is body for parent in node.parents)]
    pool = (inside or outside) if _enclosing_body(body) else (outside or inside)
    def distance(candidate):
        node, steps = candidate, 0
        while node is not None:
            if id(node) in ancestors:
                return (ancestors[id(node)], steps)
            node, steps = node.parent, steps + 1
        return (len(ancestors), steps)
    return min(pool, key=distance)


def _detail_date(soup, title_node, body) -> str | None:
    if body is None:
        return None
    if title_node is None or title_node.name == 'meta':
        title_node = _find_title(soup, body)
    # A document-wide common ancestor is not evidence of a single article.
    if title_node is None:
        return None
    region = body if any(node is title_node for node in body.descendants) else next(
        (parent for parent in body.parents
         if any(node is title_node for node in parent.descendants)), None)
    if region is None or region.name in ('[document]', 'html', 'body'):
        return None
    # Scripts are common inside the metadata paragraph (click counters) and their
    # text would otherwise poison `get_text`; strip them before scanning.
    for tag in region.select('script, style, noscript'):
        tag.decompose()
    excluded = set()
    if region is not body:
        excluded.add(id(body))
    excluded.update(id(node) for node in region.select(
        'aside, nav, footer, form, .sidebar, .side-bar, .related, .related-news'))
    labelled = re.compile(
        r'(?<![\u3400-\u9fffA-Za-z])(?:发布时间|发布日期|发表时间|日期)\s*[:：]?\s*'
        r'(20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}(?:日)?)(?!\d)')
    dates = set()
    title_text = title_node.get_text(' ', strip=True) if title_node is not None else ''
    # Wrappers around the heading would otherwise re-join its text, including an
    # event date, into the metadata scan.
    title_ancestors = {id(parent) for parent in title_node.parents} if title_node is not None else set()
    for node in region.descendants:
        if id(node) in excluded or any(id(parent) in excluded for parent in node.parents):
            continue
        # The title's own text may mention an event date; metadata blocks nested
        # inside broken heading markup still count.
        in_title = title_node is not None and node is not title_node and any(
            parent is title_node for parent in node.parents)
        if node is title_node or (in_title and (not getattr(node, 'name', None)
                                               or node.name in INLINE_TAGS)):
            continue
        if getattr(node, 'name', None):
            # Do not join the article title/body or unrelated subtrees to metadata.
            if any(id(child) in excluded for child in node.descendants):
                continue
            text = node.get_text(' ', strip=True)
        else:
            text = str(node).strip()
        if len(text) > 250:
            continue
        if title_text and id(node) in title_ancestors:
            text = text.replace(title_text, ' ')
        for match in labelled.finditer(text):
            parsed = parse_date(match[1])
            if parsed:
                dates.add(parsed)
    return next(iter(dates)) if len(dates) == 1 else None


def extract_detail(content: str, url: str, candidate: Candidate, source: dict) -> dict:
    soup = soup_page(content)
    body = None
    selectors = [source['content_selector']] if source.get('content_selector') else ARTICLE_SELECTORS
    for selector in selectors:
        body = soup.select_one(selector)
        if body: break
    title_node = soup.select_one(source['title_selector']) if source.get('title_selector') else None
    if title_node is None:
        # Visible heading first; the metadata title is a text-only fallback.
        title_node = _find_title(soup, body) or soup.select_one('meta[name="ArticleTitle"]')
    raw_title = (title_node.get('content') or title_node.get_text(' ', strip=True)) if title_node else candidate.title
    title = clean_title(raw_title)
    if NEWS.search(title) and not EXPLICIT.search(title):
        raise ValueError('详情标题判定为新闻或非通知，已排除')
    if len(title) < 4 or not (notice_title(title) or title[:12] in candidate.title or candidate.title[:12] in title):
        title = candidate.title
    if not notice_title(title, candidate.trusted): raise ValueError('详情标题判定为新闻或非通知，已排除')
    published = None
    if source.get('date_selector'):
        dates = {value for tag in soup.select(source['date_selector']) if (value := _date_node(tag))}
        if len(dates) == 1:
            published = dates.pop()
    for selector in (() if published else ('meta[name="PubDate"]', 'meta[name="publishdate"]', 'meta[property="article:published_time"]', 'meta[name="publishDate"]')):
        tag = soup.select_one(selector)
        if tag: published = parse_date(tag.get('content', ''))
        if published: break
    if not published:
        published = _detail_date(soup, title_node, body)
    published = published or candidate.date
    text = ''
    if body:
        for tag in body.select('script,style,nav,header,footer,form,button,input,select,iframe,noscript'):
            tag.decompose()
        text = ' '.join(body.get_text(' ', strip=True).split())
        text = re.sub(r'(?:点击次数|访问量|浏览次数)\s*[:：]?\s*\d*', '', text)
        # Keep a bounded excerpt, not a raw HTML snapshot or all attachments.
        text = text[:30000]
    signature = hashlib.sha256((title + '\n' + (published or '') + '\n' + text).encode()).hexdigest()
    return {'key':identity(url), 'url':normal_url(url), 'title':title, 'published_date':published,
            'summary':text[:320], 'content_hash':signature, 'detail_parsed':bool(body),
            'listed_on':candidate.listed_on}
