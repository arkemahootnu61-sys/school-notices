"""Validated public source configuration shared by the CLI and local console."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import soupsieve

from .parsing import SELECTOR_FIELDS, allowed_url, normal_url
from .storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
# Social feeds are never notices; further exclusions belong to the configuration,
# because which sites count as campus news differs by school.
BUILTIN_EXCLUDED_HOSTS = {'weibo.com', 'www.weibo.com', 'weibo.cn'}
# Optional public-suffix data; the built-in set below is used when it is absent.
try:
    from publicsuffix2 import get_sld as _public_suffix_sld
except ImportError:  # pragma: no cover - exercised by the fallback test
    _public_suffix_sld = None
# Fallback second-level public suffixes; never granted as a session domain alone.
PUBLIC_SUFFIXES = frozenset({'edu.cn', 'gov.cn', 'com.cn', 'net.cn', 'org.cn', 'ac.cn',
                             'edu.hk', 'edu.tw', 'edu.mo', 'ac.jp', 'co.jp', 'ac.uk', 'co.uk',
                             'edu.au', 'com.au', 'edu.sg', 'com.sg', 'edu.my', 'ac.in', 'co.in'})


def json_read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validate_config(config):
    if not isinstance(config, dict) or not isinstance(config.get('sources'), list):
        raise ValueError('配置需要包含 sources 数组')
    allowed_root = {'schema_version','purpose','content_scope','excluded_content','excluded_hosts','session_domains','sources'}
    if set(config) - allowed_root:
        raise ValueError('来源配置含不支持的字段；Cookie、密码、请求头或登录状态不能放在来源清单中')
    excluded = config.get('excluded_hosts', [])
    if not isinstance(excluded, list) or any(not isinstance(h, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', h) for h in excluded):
        raise ValueError('excluded_hosts 必须是主机名列表，不使用通配符')
    excluded_hosts = BUILTIN_EXCLUDED_HOSTS | {h.lower().lstrip('.') for h in excluded}
    config['excluded_hosts'] = sorted(excluded_hosts - BUILTIN_EXCLUDED_HOSTS)
    if not config['excluded_hosts']:
        config.pop('excluded_hosts')
    declared = config.get('session_domains', [])
    if not isinstance(declared, list) or any(not isinstance(d, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', d) for d in declared):
        raise ValueError('session_domains 必须是域名列表，不使用通配符')
    config['session_domains'] = [d.lower().lstrip('.') for d in declared]
    if not config['session_domains']:
        config.pop('session_domains')
    source_fields = {'id','name','group','url','allowed_hosts','seed_urls','enabled','access_status','requires_confirmation',
                     'note','allow_robots_unavailable','allow_http_detail', *SELECTOR_FIELDS}
    ids = set()
    for source in config['sources']:
        if not isinstance(source, dict) or set(source) - source_fields:
            raise ValueError('来源条目格式错误或包含不支持的字段；请勿加入登录凭据')
        if not isinstance(source.get('url'), str): raise ValueError('来源 url 必须为网址文本')
        sid = source.get('id', '')
        if not isinstance(sid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', sid) or sid in ids:
            raise ValueError('来源编号必须唯一，只含英文、数字、下划线或短横线')
        ids.add(sid)
        source['name'] = str(source.get('name') or sid)[:150]
        source['url'] = normal_url(source['url'])
        if (urlsplit(source['url']).hostname or '').lower() in excluded_hosts:
            raise ValueError('该网址在排除范围内（新闻、微博等不作为通知来源），请删除相应来源')
        hosts = source.setdefault('allowed_hosts', [urlsplit(source['url']).hostname])
        if not isinstance(hosts, list) or not hosts or any(not isinstance(h,str) or not re.fullmatch(r'[a-zA-Z0-9.-]+',h) for h in hosts):
            raise ValueError('allowed_hosts 必须是明确的主机名列表，不使用通配符')
        source['allowed_hosts'] = [h.lower() for h in hosts]
        allowed_url(source['url'], set(source['allowed_hosts']))
        seeds = source.setdefault('seed_urls', [source['url']])
        if not isinstance(seeds, list) or not seeds or any(not isinstance(u,str) for u in seeds):
            raise ValueError('每个来源至少需要一个 seed_urls 文本入口')
        source['seed_urls'] = [allowed_url(u, set(source['allowed_hosts'])) for u in seeds]
        if not isinstance(source.get('enabled', True), bool): raise ValueError('enabled 需要为 true 或 false')
        if not isinstance(source.get('allow_robots_unavailable', False), bool):
            raise ValueError('allow_robots_unavailable 需要为 true 或 false，默认关闭')
        if not isinstance(source.get('allow_http_detail', False), bool):
            raise ValueError('allow_http_detail 需要为 true 或 false，默认关闭；仅允许详情页 HTTPS 降级到同域 HTTP')
        for key in SELECTOR_FIELDS:
            if key not in source:
                continue
            if not isinstance(source[key], str) or len(source[key]) > 1000:
                raise ValueError('CSS 选择器必须是最多 1000 字的文本')
            value = source[key].strip()
            if not value:
                source.pop(key)
                continue
            source[key] = value
            try:
                soupsieve.compile(value)
            except soupsieve.SelectorSyntaxError:
                raise ValueError(f'{key} 不是有效的 CSS 选择器') from None
    return config


def _ip_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def registrable_domain(host):
    """Registrable domain of a host: `notice.ftc.school.edu.cn` -> `school.edu.cn`.

    Uses the public suffix list when `publicsuffix2` is installed and falls back to
    the built-in suffix set otherwise, so a missing optional dependency never
    broadens or narrows the session scope by accident.
    """
    if _public_suffix_sld is not None:
        try:
            value = _public_suffix_sld(host)
        except Exception:
            value = None
        if value:
            return value
    labels = host.split('.')
    for index in range(1, len(labels)):
        if '.'.join(labels[index:]) in PUBLIC_SUFFIXES:
            return '.'.join(labels[index - 1:])
    return '.'.join(labels[-2:]) if len(labels) > 1 else host


def session_domains(config):
    """Institution domains whose session may be saved.

    Sources contribute their own hosts plus their registrable domain, so a campus
    SSO host (`ids.school.edu.cn`) is covered by a source on any sibling subdomain,
    while public suffixes such as `edu.cn` and parts of IP addresses never are.
    `session_domains` can still declare extra domains explicitly.
    """
    domains = {str(domain).lower().lstrip('.') for domain in (config.get('session_domains') or [])}
    domains.discard('')
    for source in config['sources']:
        for host in source.get('allowed_hosts', []):
            host = host.lower().lstrip('.')
            if not host:
                continue
            domains.add(host)
            if not _ip_host(host):
                domains.add(registrable_domain(host))
    return sorted(domains)


def make_default(path):
    """Create a first configuration from the shipped template; never replace one."""
    if path.exists(): return
    template = ROOT / 'sources.example.json'
    if not template.exists():
        raise ValueError('缺少 sources.example.json，无法生成默认来源配置')
    atomic_json(path, validate_config(json_read(template)))


def save_config(path, config):
    config = validate_config(config)
    if path.exists():
        backup = path.with_name(path.stem+'.backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+path.suffix)
        backup.write_bytes(path.read_bytes())
    atomic_json(path, config)


def add_source(config: dict, name: str, url: str) -> dict:
    if not isinstance(name, str) or not name.strip():
        raise ValueError('来源名称不能为空')
    if not isinstance(url, str):
        raise ValueError('来源 url 必须为网址文本')
    url = normal_url(url)
    if any(source['url'] == url for source in config['sources']):
        raise ValueError('该入口已存在')
    updated = deepcopy(config)
    used = {source['id'] for source in updated['sources']}
    number = 1
    while f'M{number:02}' in used:
        number += 1
    updated['sources'].append({'id':f'M{number:02}', 'name':name.strip(), 'group':'手动添加', 'url':url,
                               'allowed_hosts':[urlsplit(url).hostname], 'seed_urls':[url], 'enabled':True})
    return validate_config(updated)


def update_source_urls(config: dict, source_urls: dict) -> dict:
    """A changed entry replaces obsolete seeds, never successful notice history."""
    if not isinstance(source_urls, dict):
        raise ValueError('source_urls 必须为来源编号到普通网址的映射')
    known = {source['id'] for source in config['sources']}
    if set(source_urls) - known:
        raise ValueError('来源编号不存在')
    updated = deepcopy(config)
    for source in updated['sources']:
        if source['id'] not in source_urls:
            continue
        value = source_urls[source['id']]
        if not isinstance(value, str):
            raise ValueError('来源 url 必须为网址文本')
        url = normal_url(value)
        if url != source['url']:
            source['url'] = url
            source['allowed_hosts'] = [urlsplit(url).hostname]
            source['seed_urls'] = [url]
            source['allow_robots_unavailable'] = False
            source['allow_http_detail'] = False
            for key in SELECTOR_FIELDS:
                source.pop(key, None)
    return validate_config(updated)


def update_source_rules(config: dict, source_rules: dict) -> dict:
    """Replace public same-host entry points and declarative extraction rules."""
    known = {source['id'] for source in config['sources']}
    if not isinstance(source_rules, dict) or set(source_rules) - known:
        raise ValueError('采集规则必须为已知来源编号到规则的映射')
    updated = deepcopy(config)
    for source in updated['sources']:
        if source['id'] not in source_rules:
            continue
        rules = source_rules[source['id']]
        if not isinstance(rules, dict) or set(rules) - {'seed_urls', *SELECTOR_FIELDS}:
            raise ValueError('采集规则仅接受入口网址与 CSS 选择器，不接受脚本、凭据或请求头')
        for key, value in rules.items():
            if key in SELECTOR_FIELDS and isinstance(value, str) and not value.strip():
                source.pop(key, None)
            else:
                source[key] = value
    return validate_config(updated)
