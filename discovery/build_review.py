from __future__ import annotations
import collections
import html
import json
from pathlib import Path
import re
import urllib.parse
from discover_xidian import OUT, run

ROOT = OUT.parent
candidates = json.loads((OUT / 'candidates.json').read_text(encoding='utf-8'))
columns = json.loads((OUT / 'columns.json').read_text(encoding='utf-8'))
# User explicitly excludes news, newspapers, microblogs and retrospective reports.
excluded_source_ids = {'S39'}  # University news portal; keep IDs stable for later review.
candidates = [s for s in candidates if s['id'] not in excluded_source_ids]
excluded_column_urls = {
    'https://ie.xidian.edu.cn/sjjiao_xue1/jsjd.htm',  # Base introductions, not current notices.
    'https://sfl.xidian.edu.cn/dqgz/xytw_xsh.htm',   # Organization introduction, not notices.
}
columns = [c for c in columns if c['url'] not in excluded_column_urls]


def is_notice_title(text):
    if re.search('通知|公告|公示', text):
        return True
    if re.search('喜报|获佳绩|斩获|圆满|成功举办|顺利举行|获奖报道|新闻|荣获|获.*等奖|经验分享会', text):
        return False
    return bool(re.search('报名|招募|征集|选拔|申报|赛程|考试安排|培训安排|讲座预告|活动预告|招生简章|招聘', text))
pages = []
for name in ['initial-pages.json', 'candidate-homepages.json', 'column-pages.json']:
    pages.extend(json.loads((OUT / name).read_text(encoding='utf-8')))

refine_urls = [
 'https://notice.xidian.edu.cn/2016lanmuye.jsp?urltype=tree.TreeTempUrl&wbtreeid=1227',
 'https://notice.xidian.edu.cn/2016lanmuye.jsp?urltype=tree.TreeTempUrl&wbtreeid=1235',
 'https://bksy.xidian.edu.cn/tzgg.htm',
 'https://xgxt.xidian.edu.cn/',
 'https://ehall.xidian.edu.cn/',
]
# Cache the final checks to keep rebuilding the review page offline and non-repetitive.
refine_file = OUT / 'final-checks.json'
if not refine_file.exists():
    run(refine_urls, refine_file.name)
pages.extend(json.loads(refine_file.read_text(encoding='utf-8')))
by_url = {p['requested_url']:p for p in pages}
by_host = collections.defaultdict(list)
for p in pages:
    by_host[urllib.parse.urlsplit(p['requested_url']).hostname].append(p)

names = {
 'S01':'机电工程学院', 'S02':'数学与统计学院', 'S03':'教务处', 'S04':'党委学生工作部 / 学生工作处',
 'S12':'图书馆', 'S18':'国际合作与交流处 / 港澳台事务办公室',
 'S22':'电子工程学院', 'S23':'计算机科学与技术学院', 'S26':'经济与管理学院',
 'S27':'人文学院', 'S28':'语言智能学部', 'S29':'集成电路学部 / 微电子学院',
 'S30':'生命科学技术学院', 'S31':'空间科学与技术学院', 'S32':'先进材料与纳米科技学院',
 'S33':'网络与信息安全学院（目录标为网络安全与密码学部）',
}
notes = {
 'S01':'重点：学院通知、教育管理、学生活动、本科教学、研究生培养、学术报告。部分子栏目未识别出文章链接，需后续适配，不能当作没有更新。',
 'S02':'重点：学院通知、公示、学工动态、教学动态和学术报告。主页的“教学竞赛”偏教师教学，不作为学生竞赛首选。',
 'S03':'选课、考试、学籍、转专业、毕业设计等；部分通知面向教师，可后续加筛选。',
 'S04':'奖助学金、评优、学生教育管理；这不是学生会的网站。',
 'S05':'官网机构目录确认存在；本次 HTTPS 连接被重置、HTTP 返回 502，暂未核实栏目。不能据此断定网站失效。',
 'S06':'竞赛、创新创业项目的重要来源；“竞赛基地”部分为介绍页，不等于最新赛事列表。',
 'S07':'本科生相关综合通知。',
 'S08':'工程训练、创新实践、竞赛类通知。',
 'S09':'研究生通知、公示、活动预告与创新实践系列大赛；本科生可只保留竞赛部分。',
 'S10':'研究生培养、学位、招生等；不需要时可以删除。',
 'S16':'官方目录已确认，但本次 HTTPS 连接被重置、HTTP 返回 502，待复核。',
 'S28':'主页已变为语言智能学部；“学院团委&学生会”是该学部栏目，不是校学生会。',
 'S38':'学校通知门户本次明确提示校外访问受限，要求学校 aTrust 零信任系统。仅保存登录 Cookie 不保证能访问；需用户在学校允许的网络环境下运行。',
 'S39':'校园新闻和赛事获奖报道较多，噪声可能较大；不是所有内容都是报名通知。',
 'S40':'官方创新创业学院链接到此平台，本次返回 502；未确认当前登录或通知能力。',
 'S41':'已跳转到 HTTPS 在线评测平台，偏程序设计比赛与作业，不是全校竞赛通知中心。',
 'S42':'由数学与统计学院官网友情链接发现；校外赛事官网，不代表学校报名安排。',
 'S43':'由数学与统计学院官网友情链接发现；校外研究生赛事平台，不代表学校报名安排。',
 'S44':'综合信息网首页本次可公开读取，聚合多个部门通知。详情可能另需登录或校园网络，不能假定全部公开。',
 'S45':'官方入口已发现，本次 TLS 握手超时；尚未确认登录后有哪些可用通知。',
 'S46':'可打开办事大厅外壳页面，未登录、未验证业务或通知内容；不采集成绩或个人资料。',
 'S47':'已跳转统一身份认证平台，未登录；不采集个人资料、奖助申请或其他业务记录。',
}

def readable(page):
    return bool(page and not page.get('error') and page.get('title') and page.get('status') == 200)

report = []
for source in candidates:
    source = dict(source)
    source['name'] = names.get(source['id'], source['name'])
    if source['id'] == 'S44': source['group'] = '重点关注'
    host = urllib.parse.urlsplit(source['url']).hostname
    attempts = by_host[host]
    home = by_url.get(source['url'], {})
    if home.get('error') or not home.get('title'):
        home = next((p for p in reversed(attempts) if readable(p) and urllib.parse.urlsplit(p['requested_url']).path in ('', '/')), home)
    source['homepage_check'] = {k:home[k] for k in ['status','final_url','title','error','access_hint','checked_at'] if k in home}
    if source['id'] == 'S38':
        source['access'] = '校园网络受限'
    elif source['id'] in ('S46','S47'):
        source['access'] = '待本人登录确认'
    elif not readable(home):
        source['access'] = '连接异常，待复核'
    elif not columns or not any(c['source_id'] == source['id'] for c in columns):
        source['access'] = '已读取首页'
    else:
        source['access'] = '已读取公开栏目'
    source['note'] = notes.get(source['id'], '官方目录发现，本次已检查首页；后续仅取通知公告，不采集新闻、宣传或活动回顾。是否保留由用户按需要决定。')
    if source['id'] == 'S06': source['note'] = '竞赛与创新创业通知的重要来源；仅保留公告通知和项目招募，不采集基地介绍、获奖喜报或活动报道。'
    if source['id'] == 'S28': source['note'] = '主页已变为语言智能学部；尚需定位与核实其通知栏目，不使用学院团委/学生会介绍页充当通知来源。'
    source['content_scope'] = '只收通知、公告、公示与可行动的报名/选拔/赛程信息；排除新闻、宣传、微博、校报及获奖报道' 
    source['recommended'] = source['id'] in {'S01','S02','S03','S04','S05','S06','S07','S08','S09','S44'}
    source['columns'] = []
    for column in columns:
        if column['source_id'] != source['id']: continue
        page = by_url.get(column['url'], {})
        source['columns'].append({**column, 'verified':readable(page) and page.get('title') != '系统提示',
            'title':page.get('title',''), 'status':page.get('status'), 'error':page.get('error'),
            'access_hint':page.get('access_hint',''), 'checked_at':page.get('checked_at')})
    sample_pages = [by_url.get(c['url'], {}) for c in source['columns']] + [home]
    samples, seen = [], set()
    for page in sample_pages:
        for link in page.get('links', []):
            u, text = link['url'], link['text']
            if urllib.parse.urlsplit(u).hostname != host: continue
            if not re.search(r'/info/\d+/\d+|wbnewsid=|/news/view/', u) or not text or u in seen: continue
            if text in ('《点击此处》', '办事指南') or not is_notice_title(text): continue
            # Keep the displayed excerpt short and collapse exact duplicate anchor text.
            if len(text)%2==0 and text[:len(text)//2]==text[len(text)//2:]:text=text[:len(text)//2]
            seen.add(u)
            samples.append({'title':text[:180], 'url':u, 'listed_on':page.get('requested_url')})
            if len(samples) >= 5: break
        if len(samples) >= 5: break
    source['samples'] = samples
    report.append(source)

stats = {
 'sources':len(report), 'school_sources':sum(urllib.parse.urlsplit(s['url']).hostname.endswith('.xidian.edu.cn') for s in report),
 'columns':len(columns), 'verified_columns':sum(c['verified'] for s in report for c in s['columns']),
 'unique_pages_checked':len(by_url), 'sample_links':sum(len(s['samples']) for s in report),
 'recommended_sources':sum(s['recommended'] for s in report),
 'excluded_sources':['S39 西电新闻网'],
 'content_scope':'通知、公告、公示、报名/选拔/赛程信息；不收新闻、校报、微博或获奖报道',
 'checked_at_utc':max(p['checked_at'] for p in pages),
}
(ROOT / 'sources.review.json').write_text(json.dumps({'stats':stats,'sources':report}, ensure_ascii=False, indent=2), encoding='utf-8')
md = ['# 西安电子科技大学通知来源候选清单', '',
 f"候选入口 {stats['sources']} 个（校内 {stats['school_sources']} 个、校外赛事补充 2 个）；发现具体栏目 {stats['columns']} 个，其中本次可读取并核实标题 {stats['verified_columns']} 个；请求检查 {stats['unique_pages_checked']} 个不同 URL。", '',
 '本轮是来源侦察，不是正式全量采集；只访问公开页面，没有使用凭据或进入登录后系统。HTTP 200 不等于内容可访问，系统提示页单独标记。',
 '按用户要求，已移除新闻网；不加入校报、微博或宣传来源，排除基地介绍等非通知栏目。混合栏目只提取通知候选，不整页收录。',
 '样例已按通知标题初筛，未逐篇核实正文与发布日期，可能含置顶、旧通知；不能直接作为“全部最新通知”。获奖结果公示属于通知，获奖喜报属于新闻，两者区分。', '',
 '核查 URL 总数包含本轮发现后被排除的新闻网等入口，不等于有效通知源数量。',
 '官方发现依据：[学校主页](https://www.xidian.edu.cn/) · [机构设置](https://www.xidian.edu.cn/jgsz.htm)', '',
 '建议先保留重点来源，再按你的年级、本科/研究生身份删减。登录文件不属于此清单，不应发送到聊天或提交到 Git。', '']
for s in report:
    md += [f"## {s['id']} {s['name']}", f"- 分组：{s['group']}；状态：{s['access']}；建议：{'优先保留' if s['recommended'] else '按需选择'}", f"- 首页：{s['url']}", f"- 发现依据：{s['discovered_from']}", f"- 说明：{s['note']}"]
    for c in s['columns']:
        md.append(f"- 栏目：[{c['name']}]({c['url']}) — {'已读取核实' if c['verified'] else '受限或读取失败，需确认'}")
    if s['samples']:
        md.append('- 列表样例（不是新增判定）：')
        md.extend(f"  - [{sample['title'].replace('[','（').replace(']','）')}]({sample['url']})" for sample in s['samples'][:3])
    md.append('')
(ROOT / '候选网址清单.md').write_text('\n'.join(md), encoding='utf-8')
template = (OUT / 'review-template.html').read_text(encoding='utf-8')
embedded = json.dumps({'stats':stats,'sources':report}, ensure_ascii=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
(OUT / 'review.html').write_text(template.replace('__REPORT_DATA__', embedded), encoding='utf-8')
print(json.dumps(stats, ensure_ascii=False, indent=2))
