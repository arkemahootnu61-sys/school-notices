from __future__ import annotations

from collections import Counter
from datetime import datetime
import html
from pathlib import Path

from .storage import atomic_json, atomic_text

LABELS = {'baseline':'首次收录', 'new':'新发现', 'updated':'内容变化', 'seen':'已收录'}


def write_report(output: Path, result: dict):
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    counts = Counter(item['state'] for item in result['items'])
    demo_label = '（模拟演示，非真实通知）' if result.get('demo') else ''
    e = lambda text: html.escape(str(text or ''), quote=True)
    outcome = result.get('outcome')
    source_counts = result.get('stats', {}).get('source_counts', {})
    source_summary = (f"来源：已检查 {source_counts.get('checked', 0)} · 部分 {source_counts.get('partial', 0)} · "
                      f"失败 / 未识别 {source_counts.get('failed', 0) + source_counts.get('unrecognized', 0)} · "
                      f"未尝试 {source_counts.get('not_attempted_budget', 0)}")
    conclusion = {'checked': '本轮已按设置检查。',
                  'partial': '本轮仅部分覆盖，不能据此判断所有来源均无更新。',
                  'failed': '本轮采集失败，未成功读取通知来源。'}.get(outcome, '本轮覆盖结论未记录。')
    empty = ('本轮采集失败，未成功读取通知来源。' if outcome == 'failed' else
             '本轮无新发现；近期已收录通知请在本地控制台查看。' if outcome == 'checked' else
             '本轮未展示发现 / 变化条目；存在未检查或无法识别的范围，请查看覆盖说明。')
    cards = []
    for item in result['items']:
        sources = ' / '.join(s['name'] for s in item['sources'])
        extra = ''.join(f'<a href="{e(u)}" target="_blank" rel="noopener noreferrer">转载 / 其他入口</a> ' for u in item.get('alternative_urls', []))
        cards.append(f'''<article class="notice" data-search="{e(item['title']+' '+sources+' '+item['summary'])}">
          <div class="meta"><span class="badge {e(item['state'])}">{e(LABELS[item['state']])}</span><span>{e(item['published_date'] or '发布日期未识别')}</span><span>{e(sources)}</span></div>
          <h2><a href="{e(item['url'])}" target="_blank" rel="noopener noreferrer">{e(item['title'])}</a></h2>
          <p>{e(item['summary'] or '未识别出正文容器，请打开原文确认内容。')}</p><div class="extra">{extra}</div></article>''')
    rows = []
    for source in result['coverage']:
        messages = '；'.join(source['messages']) or '本轮已按设置检查；不代表覆盖站内所有页面'
        codes = '；'.join(f'{issue["code"]} / {issue["stage"]}' for issue in source.get('issues', []))
        if codes: messages += '（' + codes + '）'
        rows.append(f'<tr><td>{e(source["name"])}</td><td>{e(source["status"])}</td><td>{source["list_pages"]} / {source["details"]}</td><td>{e(messages)}</td></tr>')
    stats_html = ''.join(f'<div><b>{counts[k]}</b><span>{v}</span></div>' for k,v in LABELS.items())
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>西电通知 · 本次采集{demo_label}</title>
    <link rel="icon" href="data:,">
    <style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f6f8;color:#203e50;font-family:"Segoe UI","Microsoft YaHei",sans-serif}}header{{background:#153a50;color:#fff;padding:35px max(20px,6vw)}}h1{{font-size:28px;margin:0 0 12px}}header p{{font-size:12px;color:#bdd0d9;line-height:1.9;margin:5px 0}}.stats{{display:flex;gap:35px;margin-top:25px;flex-wrap:wrap}}.stats b{{font-size:27px;display:block}}.stats span{{font-size:11px;color:#bbd2d9}}main{{max-width:1060px;margin:auto;padding:24px 20px 70px}}.info{{font-size:12px;color:#6b7e8a;line-height:1.9}}input{{width:100%;padding:13px 15px;border:1px solid #cedae1;border-radius:7px;margin:15px 0 22px;font:inherit}}.notice{{background:white;border:1px solid #dce5ea;padding:22px;border-radius:9px;margin-bottom:13px}}.meta{{font-size:10px;display:flex;gap:12px;color:#6f8793;flex-wrap:wrap;align-items:center}}.badge{{padding:3px 7px;background:#e4eef2;color:#32657c;border-radius:4px}}.new{{background:#dff1e9;color:#277551}}.updated{{background:#fff1d8;color:#996722}}h2{{font-size:17px;line-height:1.8;margin:13px 0 9px}}a{{color:#25627e;text-decoration:none;overflow-wrap:anywhere}}a:hover{{text-decoration:underline}}.notice p{{font-size:12px;color:#6c7f8c;line-height:1.95;margin:0}}.extra{{font-size:10px;margin-top:10px}}details{{margin-top:30px}}summary{{cursor:pointer;font-size:15px;padding:13px 0}}.table-wrap{{overflow-x:auto}}table{{width:100%;font-size:11px;border-collapse:collapse;background:#fff}}th,td{{padding:12px;text-align:left;border:1px solid #dce5ea;line-height:1.8;min-width:110px}}td:last-child{{min-width:280px}}.empty{{padding:30px;background:#fff;border-radius:9px;line-height:2;font-size:14px}}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{{outline:3px solid #bc9245;outline-offset:3px}}[hidden]{{display:none!important}}</style></head>
    <body><header><h1>西电通知 · 本次采集{demo_label}</h1><p>{e(result['started_at'])} · 手动运行一次 · {e({'auto':'自动模式（HTTP优先，受限Chrome后备）','http':'公开HTTP模式','browser':'专用Chrome模式'}[result['transport_mode']])}</p><p>只收通知、公告、公示与报名 / 选拔 / 赛程信息，不收新闻、微博、校报或获奖喜报。</p><div class="stats">{stats_html}</div></header>
    <main><p class="info"><strong>{e(conclusion)} {e(source_summary)}</strong></p>
    <p class="info">此页是本轮增量报告，不是全部已存通知库；近期通知在本地控制台持续浏览。首次收录用于建立基线，不代表刚发布。新发现是已有基线之后脚本首次看到的通知；可能是补采的旧信息，请结合发布日期判断。默认仅显示最近 {result['days'] or '不限'} 天及日期未识别的通知。<br>本轮受页面数、分页、详情数量及权限限制；失败 / 跳过项目见下方覆盖情况。没有显示不等于没有新通知。已收录且未变化的通知：{result['seen_omitted']} 条未重复展示。</p>
    <input type="search" id="search" aria-label="筛选通知" placeholder="搜索标题、来源或摘要…"><p id="count" class="info" role="status" aria-live="polite"></p>
    <section id="notices">{''.join(cards) if cards else '<div class="empty">'+e(empty)+'</div>'}</section>
    <details open><summary>来源覆盖与失败说明（{len(rows)} 个来源）</summary><div class="table-wrap"><table><thead><tr><th>来源</th><th>状态</th><th>列表 / 详情</th><th>说明</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
    <p class="info">附件没有批量下载；不保存网页原始 HTML 或个人业务记录。详情链接仍可能要求校园网络或本人登录。仅比较本次成功读取范围内的通知，无法证明穷尽学校全部通知。</p></main>
    <script>const field=document.getElementById('search'),cards=[...document.querySelectorAll('.notice')];function filter(){{const q=field.value.trim().toLowerCase();let n=0;for(const card of cards){{card.hidden=!card.dataset.search.toLowerCase().includes(q);if(!card.hidden)n++;}}document.getElementById('count').textContent='当前显示 '+n+' 条通知';}}field.addEventListener('input',filter);filter();</script></body></html>'''
    json_path = output / f'notices-{stamp}.json'
    html_path = output / f'notices-{stamp}.html'
    atomic_json(json_path, result)
    atomic_text(html_path, page)
    # Public metadata only. The private browser profile and DB live outside this directory.
    atomic_text(output / 'latest.html', page)
    atomic_json(output / 'latest.json', result)
    return html_path, json_path
