import json
import re
import urllib.parse
from discover_xidian import OUT, run

candidates = json.loads((OUT / 'candidates.json').read_text(encoding='utf-8'))
homes = json.loads((OUT / 'candidate-homepages.json').read_text(encoding='utf-8'))
initial = json.loads((OUT / 'initial-pages.json').read_text(encoding='utf-8'))
directory = next(p for p in initial if '/jgsz.htm' in p['requested_url'])
for candidate in candidates:
    host = urllib.parse.urlsplit(candidate['url']).hostname
    if not candidate['name']:
        candidate['name'] = next((l['text'] for l in directory['links'] if l['text'] and urllib.parse.urlsplit(l['url']).hostname == host), host)
    if candidate['id'] == 'S18': candidate['name'] = '国际合作与交流处 / 港澳台事务办公室'
    if candidate['id'] == 'S12': candidate['name'] = '图书馆'

# All paths below were extracted from links in fetched official pages, including generic “更多” links.
preferred = {
 'eme': ['/tz.htm', '/xsgz/jygl.htm', '/xsgz/xshd.htm', '/rcpy/bkjx.htm', '/rcpy/yjspy.htm', '/xsbg.htm'],
 'math': ['/xwgg/tzgg.htm', '/xwgg/gsgg.htm', '/xsgz/xgdt.htm', '/bkjx/jxdt.htm', '/xsbg.htm'],
 'jwc': ['/tzgg.htm'], 'xgc': ['/tzgg1.htm'],
 'ie': ['/sczx/ggtz.htm', '/sjjiao_xue1/jsjd.htm', '/cgzs/xmzm.htm'],
 'bksy': ['/tzgg.htm'], 'etc': ['/index/tzgg.htm', '/kjcx/jsl.htm'],
 'ygb': ['/tzgg.htm', '/gsxx.htm', '/hdyg.htm', '/ytzm1/cxsjxlds.htm'],
 'gr': ['/xwsy/tzgg.htm'], 'job': ['/news/index/tag/tzgg'],
 'tyb': ['/ydjs/xnjs.htm'], 'xl': ['/tzgg.htm'], 'hqbzb': ['/tzgg.htm'],
 'xxzx': ['/zxdt/xwgg.htm'], 'oice': ['/gjjyyfwzx/tzggykszx.htm'], 'xyy': ['/ywgk/tzgg.htm'],
 'see': ['/tzgg.htm', '/bks.htm'],
 'cs': ['/xsgz1/tzgg.htm', '/bkjy/kjjs.htm', '/bkjy/jxtz.htm'],
 'phy': ['/tzgg.htm'], 'sem': ['/tzgg.htm', '/xxgs.htm'],
 'sfl': ['/dqgz/xytw_xsh.htm'],
 'sme': ['/plus/list.php?tid=6'], 'life': ['/tzgg.htm'],
 'sast': ['/xszj/tzgg.htm', '/bkjy/xsjs.htm'],
 'ce': ['/sy/xsgz/tzgg.htm', '/sy/bkjy/jxtz.htm'],
 'sai': ['/tzgg.htm', '/bkjx1/xkjs.htm'],
 'ncee': ['/xwdt/tzgg.htm'], 'imis': ['/index/tzgg.htm', '/xshd1/xlbg.htm'],
}
columns = []
for candidate, home in zip(candidates, homes):
    prefix = urllib.parse.urlsplit(candidate['url']).hostname.split('.')[0]
    for path in preferred.get(prefix, []):
        found = next((l for l in home.get('links', []) if urllib.parse.urlsplit(l['url']).path + ('?' + urllib.parse.urlsplit(l['url']).query if urllib.parse.urlsplit(l['url']).query else '') == path), None)
        if not found: raise RuntimeError(f'Path not actually discovered: {prefix} {path}')
        text = found['text']
        if text in ('更多', '更多+', 'More+', '中心动态 更多'):
            text = {'/tz.htm':'学院通知', '/xsbg.htm':'学术报告', '/tzgg.htm':'通知公告', '/gsxx.htm':'公示信息', '/hdyg.htm':'活动预告', '/bks.htm':'本科生通知', '/xxgs.htm':'信息公示', '/index/tzgg.htm':'通知公告'}.get(path, '栏目入口')
        if len(text) % 2 == 0 and text[:len(text)//2] == text[len(text)//2:]: text = text[:len(text)//2]
        columns.append({'source_id': candidate['id'], 'name': text, 'url': found['url'], 'discovered_from': home['final_url']})

homepage = initial[0]
for label, treeid in [('学校通知', '1227'), ('学校公告', '1235')]:
    link = next(l for l in homepage['links'] if '2016lanmuye.jsp' in l['url'] and 'wbtreeid='+treeid in l['url'])
    columns.append({'source_id':'S38','name':label,'url':link['url'].replace('http:','https:',1),'discovered_from':homepage['final_url']})

extras = [
 ('创新创业训练计划平台', '竞赛与项目补充', 'http://cxcycs.xidian.edu.cn/', 'https://ie.xidian.edu.cn/'),
 ('西电 ACM 竞赛网', '竞赛与项目补充', 'http://acm.xidian.edu.cn/', 'https://cs.xidian.edu.cn/'),
 ('全国大学生数学建模竞赛（校外官网）', '校外赛事补充', 'https://www.mcm.edu.cn/', 'https://math.xidian.edu.cn/'),
 ('中国研究生数学建模竞赛（校外官网）', '校外赛事补充', 'https://cpipc.acge.org.cn/cw/hp/4', 'https://math.xidian.edu.cn/'),
 ('综合信息网', '需另行确认的登录入口', 'http://info.xidian.edu.cn/', 'https://www.xidian.edu.cn/'),
 ('一网通办', '需另行确认的登录入口', 'https://ywtb.xidian.edu.cn/', 'https://www.xidian.edu.cn/'),
 ('教务系统', '需另行确认的登录入口', 'https://ehall.xidian.edu.cn/', 'https://www.xidian.edu.cn/'),
 ('学工系统', '需另行确认的登录入口', 'https://xgxt.xidian.edu.cn/', 'https://www.xidian.edu.cn/'),
]
for name, group, url, evidence in extras:
    evidence_page = next((p for p in homes + initial if p['requested_url'] == evidence), None)
    if not evidence_page or not any(l['url'].rstrip('/') == url.rstrip('/') for l in evidence_page.get('links', [])):
        raise RuntimeError(f'Extra URL not discovered: {url}')
    candidates.append({'id':f'S{len(candidates)+1:02}', 'name':name, 'group':group, 'url':url, 'directory_url':url, 'discovered_from':evidence})

# Retry failures without changing protocol silently; HTTP is only used when linked by the official directory.
retry_urls = ['http://tw.xidian.edu.cn/', 'http://bwc.xidian.edu.cn/', 'https://ste.xidian.edu.cn/']
urls = list(dict.fromkeys([c['url'] for c in columns] + [c['url'] for c in candidates[39:]] + retry_urls))
print('Checking', len(urls), 'additional public URLs')
pages = run(urls, 'column-pages.json')
(OUT / 'candidates.json').write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding='utf-8')
(OUT / 'columns.json').write_text(json.dumps(columns, ensure_ascii=False, indent=2), encoding='utf-8')
for page in pages:
    samples = [l for l in page.get('links', []) if re.search(r'/info/\d+/\d+|wbnewsid=|/news/view/', l['url']) and l['text']]
    print(page.get('status', 'ERROR'), page['requested_url'], page.get('title', ''), (page.get('error') or '')[:70], 'articles:', len(samples))
