"""Build a review-only inventory from the university's official directory."""
import json
from pathlib import Path
import urllib.parse
from discover_xidian import OUT, run

initial = json.loads((OUT / 'initial-pages.json').read_text(encoding='utf-8'))
directory = next(p for p in initial if '/jgsz.htm' in p['requested_url'])
by_host = {}
for link in directory['links']:
    host = urllib.parse.urlsplit(link['url']).hostname
    if host not in by_host or (not by_host[host]['text'] and link['text']):
        by_host[host] = link

# Keys are matched against links actually present in the official directory.
groups = {
    '重点关注': ['eme', 'math', 'jwc', 'xgc', 'tw', 'ie', 'bksy', 'etc'],
    '学业与校园服务': ['ygb', 'gr', 'job', 'lib', 'tyb', 'xl', 'hqbzb', 'bwc', 'xxzx', 'oice', 'zsb', 'xyy'],
    '其他学院与竞赛线索': ['ste', 'see', 'cs', 'soe', 'phy', 'sem', 'rwxy', 'sfl', 'sme', 'life', 'sast', 'amn', 'ce', 'sai', 'imse', 'ncee', 'imis'],
}
candidates = []
for group, prefixes in groups.items():
    for prefix in prefixes:
        host = prefix + '.xidian.edu.cn'
        link = by_host.get(host)
        if not link:
            raise RuntimeError(f'Not found in directory: {host}')
        candidates.append({'id': f'S{len(candidates)+1:02}', 'name': link['text'], 'group': group,
                           'url': f'https://{host}/', 'directory_url': link['url'],
                           'discovered_from': directory['final_url']})
        if prefix == 'oice':
            candidates[-1]['name'] = '国际合作与交流处 / 港澳台事务办公室'
        elif prefix == 'lib':
            candidates[-1]['name'] = '图书馆'
candidates += [
    {'id': f'S{len(candidates)+1:02}', 'name': '学校通知公告门户', 'group': '重点关注',
     'url': 'https://notice.xidian.edu.cn/', 'directory_url': 'http://notice.xidian.edu.cn/2016lanmuye.jsp?urltype=tree.TreeTempUrl&wbtreeid=1227',
     'discovered_from': 'https://www.xidian.edu.cn/'},
    {'id': f'S{len(candidates)+2:02}', 'name': '西电新闻网', 'group': '学业与校园服务',
     'url': 'https://news.xidian.edu.cn/', 'directory_url': 'https://news.xidian.edu.cn/',
     'discovered_from': 'https://www.xidian.edu.cn/'},
]
(OUT / 'candidates.json').write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding='utf-8')
pages = run([c['url'] for c in candidates], 'candidate-homepages.json')
for candidate, page in zip(candidates, pages):
    print(candidate['id'], candidate['name'], page.get('status', 'ERROR'), page.get('title', page.get('error', '')), candidate['url'])
