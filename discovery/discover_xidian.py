"""Public-page reconnaissance only: no login, no credentials, no periodic task."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
OUT = Path(__file__).resolve().parent


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.text = []
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            self.current = attrs.get('href', '')
            self.text = [attrs.get('title', '')]
        if tag == 'img' and self.current is not None:
            self.text.append(attrs.get('alt', ''))

    def handle_data(self, data):
        if self.current is not None:
            self.text.append(data)
        if self.in_title:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.current is not None:
            text = ' '.join(''.join(self.text).split())
            self.links.append({'text': text, 'href': self.current})
            self.current = None


def fetch(url):
    result = {'requested_url': url, 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        request = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; CampusNoticeSourceDiscovery/0.1)',
            'Accept': 'text/html,application/xhtml+xml',
        })
        with urllib.request.urlopen(request, timeout=15) as response:
            result.update(status=response.status, final_url=response.url)
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                result['truncated'] = True
            raw = raw[:2_000_000]
            match = re.search(br'charset\s*=\s*[\"\x27]?\s*([a-zA-Z0-9_-]+)', raw[:12000], re.I)
            charset = response.headers.get_content_charset() or (match.group(1).decode('ascii') if match else 'utf-8')
        try:
            content = raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError:
                content = raw.decode('gb18030', errors='replace')
        parser = Links()
        parser.feed(content)
        result['title'] = html.unescape(' '.join(''.join(parser.title).split()))
        body_text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', content, flags=re.I | re.S)
        body_text = html.unescape(re.sub(r'<[^>]+>', ' ', body_text))
        body_text = ' '.join(body_text.split())
        if '校外访问' in body_text and ('aTrust' in body_text or '零信任' in body_text):
            result['access_hint'] = '校外访问受限：页面要求通过学校 aTrust 零信任系统访问'
        elif re.search('统一身份认证|用户登录|用户登陆', result['title']) or 'authserver/login' in result['final_url']:
            result['access_hint'] = '已到达登录入口；未登录，尚未验证通知内容'
        elif result['title'] == '系统提示':
            result['access_hint'] = '系统提示页，不能当作已成功访问通知列表'
        else:
            result['access_hint'] = '公开页面可读取；通知详情与附件权限需分别确认'
        result['links'] = []
        seen = set()
        for link in parser.links:
            absolute = urllib.parse.urljoin(result['final_url'], link['href'])
            parts = urllib.parse.urlsplit(absolute)
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                continue
            absolute = urllib.parse.urlunsplit(parts._replace(fragment=''))
            key = (absolute, link['text'])
            if key not in seen:
                seen.add(key)
                result['links'].append({'text': link['text'], 'url': absolute})
        result['notice_links'] = [link for link in result['links'] if re.search('通知|公告|竞赛|大赛|创新创业|学生工作|教务|团委|学生会|学科竞赛', link['text'])]
    except urllib.error.HTTPError as error:
        result.update(status=error.code, error=str(error), final_url=error.url)
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    return result


def run(urls, filename):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        pages = list(executor.map(fetch, urls))
    (OUT / filename).write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding='utf-8')
    return pages


if __name__ == '__main__':
    urls = sys.argv[1:] or [
        'https://www.xidian.edu.cn/',
        'https://www.xidian.edu.cn/jgsz.htm',
        'https://eme.xidian.edu.cn/',
        'https://math.xidian.edu.cn/',
        'https://jwc.xidian.edu.cn/',
        'https://xgc.xidian.edu.cn/',
    ]
    for page in run(urls, 'initial-pages.json'):
        print(json.dumps({k: v for k, v in page.items() if k not in ('links',)}, ensure_ascii=False))
