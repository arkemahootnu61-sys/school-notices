from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .parsing import AccessIssue, Candidate, allowed_url, extract_detail, extract_listing, identity
from .report import write_report
from .storage import Store, merge_notices, utc_now
from .transport import FetchError, Robots


@dataclass
class Options:
    days: int = 30
    pages_per_seed: int = 2
    max_list_pages_per_source: int = 16
    max_details_per_source: int = 25
    max_pages: int = 200
    max_details: int = 150
    include_seen: bool = False
    transport_mode: str = 'auto'
    keywords: tuple[str, ...] = ()


STATUSES = {'checked': '已检查', 'partial': '部分覆盖', 'unrecognized': '未识别列表',
            'failed': '失败或受限', 'not_attempted_budget': '本轮未尝试（预算不足）'}


class Collector:
    def __init__(self, transport, store: Store, options: Options, log=print, *, progress=None, cancel_event=None):
        self.transport, self.store, self.options, self.log = transport, store, options, log
        self.progress, self.cancel_event = progress, cancel_event
        self.robots = Robots(transport)
        self.pages = 0
        self.detail_attempts = 0

    def _check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CancelledError()

    def _progress(self, phase, source_id, completed, total):
        if self.progress:
            self.progress({'phase': phase, 'source_id': source_id, 'completed_sources': completed,
                           'total_sources': total, 'page_reads': self.pages,
                           'detail_attempts': self.detail_attempts})
        self._check_cancel()

    def collect(self, sources: list[dict], output: Path):
        try:
            return self._collect(sources, output)
        except BaseException:
            self.store.rollback()
            raise

    def _collect(self, sources, output):
        started = utc_now()
        items, coverage = [], []
        cutoff = (date.today() - timedelta(days=self.options.days)).isoformat() if self.options.days else None
        sources = sorted(sources, key=lambda source: source['id'])
        if sources:
            start = self.store.completed_run_count() % len(sources)
            sources = sources[start:] + sources[:start]
        for completed, source in enumerate(sources):
            self._check_cancel()
            self.log(f'[{source["id"]}] {source["name"]}')
            hosts = set(source['allowed_hosts'])
            seed_urls = source.get('seed_urls') or [source['url']]
            scope_key = source['id'] + ':' + ','.join(sorted(seed_urls))
            initialized = self.store.initialized(scope_key)
            checkpoints = self.store.list_checkpoints(scope_key)
            queue = deque((url, url, 0, False) for url in dict.fromkeys(seed_urls))
            seen_pages, page_counts, candidates = set(), Counter(), {}
            attempted = set()
            list_attempts = 0
            stat = {'id': source['id'], 'name': source['name'], 'status': '', 'outcome': '',
                    'list_pages': 0, 'recognized_list_pages': 0, 'details': 0, 'detail_attempts': 0,
                    'detail_failures': 0, 'candidate_count': 0, 'messages': [], 'limited': False,
                    'issues': [], 'limit_reasons': [], 'pending_unattempted': 0, 'pending_rechecks': 0,
                    'matched_details': 0, 'outside_range_candidates': 0, 'unknown_date_details': 0}
            blocked_before = getattr(self.transport, 'blocked_methods', 0)

            def message(text):
                if text not in stat['messages']: stat['messages'].append(text)

            def issue(error, stage, code=None):
                entry = {'code': code or getattr(error, 'code', 'parse_error'), 'stage': stage, 'message': str(error)}
                if entry not in stat['issues']: stat['issues'].append(entry)
                message(entry['message'])

            def check_rules(url):
                self.robots.allowed(url, hosts, allow_unavailable=source.get('allow_robots_unavailable', False))
                warning = self.robots.unverified_warning(url)
                if warning:
                    issue(FetchError(warning, code='robots_unverified'), 'robots')

            def limit(reason):
                stat['limited'] = True
                if reason not in stat['limit_reasons']: stat['limit_reasons'].append(reason)
                labels = {'pages_per_seed': '单栏目分页', 'source_list_pages': '本来源列表页',
                          'source_details': '本来源详情', 'global_pages': '本轮网页', 'global_details': '本轮详情'}
                message('达到' + labels[reason] + '上限，仍有已发现内容未检查')

            global_skip = self.pages >= self.options.max_pages or self.detail_attempts >= self.options.max_details
            self._progress('lists', source['id'], completed, len(sources))
            if global_skip:
                limit('global_pages' if self.pages >= self.options.max_pages else 'global_details')
                queue.clear()
            while queue:
                self._check_cancel()
                url, section, depth, trusted_section = queue.popleft()
                if (self.options.pages_per_seed == 1 and not page_counts[section] and
                        url == section and section in checkpoints):
                    url, trusted_section = checkpoints.pop(section)
                    depth = max(depth, 1)
                try:
                    url = allowed_url(url, hosts)
                except ValueError:
                    issue(ValueError('跳过不在允许范围内或有风险的栏目网址'), 'list', 'unsafe_redirect')
                    continue
                if url in seen_pages: continue
                if page_counts[section] >= self.options.pages_per_seed:
                    limit('pages_per_seed')
                    continue
                if list_attempts >= self.options.max_list_pages_per_source:
                    limit('source_list_pages')
                    break
                if self.pages >= self.options.max_pages:
                    limit('global_pages')
                    break
                seen_pages.add(url)
                try:
                    self._check_cancel()
                    check_rules(url)
                except (FetchError, AccessIssue, ValueError) as error:
                    issue(error, 'robots')
                    continue
                self._check_cancel()
                page_counts[section] += 1
                list_attempts += 1
                try:
                    self.pages += 1
                    page = self.transport.fetch(url, hosts)
                    self._check_cancel()
                    stat['list_pages'] += 1
                    listing = extract_listing(page.text, page.url, hosts, source, trusted_section=trusted_section)
                    if listing.candidates or listing.explicitly_empty:
                        stat['recognized_list_pages'] += 1
                    for candidate in listing.candidates:
                        candidates.setdefault(identity(candidate.url), candidate)
                    if depth == 0:
                        for child in listing.sections:
                            if child not in seen_pages and child not in seed_urls:
                                queue.append((child, child, 1, False))
                    next_pages = [(child, listing.trusted) for child in listing.next_pages if child not in seen_pages]
                    if url == section and section in checkpoints and checkpoints[section][0] not in seen_pages:
                        # Refresh the head, then resume the saved historical frontier.
                        next_pages = [checkpoints.pop(section)]
                    else:
                        self.store.save_list_checkpoint(scope_key, section,
                            next_pages[0][0] if next_pages else None, listing.trusted)
                    for child, trusted in next_pages:
                        queue.append((child, section, depth + 1, trusted))
                    if not listing.candidates and not listing.explicitly_empty and not listing.sections:
                        issue(ValueError('页面未识别出可靠通知列表；可在来源的采集适配中指定栏目与 CSS 规则，不视为没有更新'), 'list', 'unrecognized_list')
                except (AccessIssue, FetchError) as error:
                    issue(error, 'list')
                except Exception:
                    issue(ValueError('栏目解析失败，需适配该网站'), 'list', 'parse_error')
                self._progress('lists', source['id'], completed, len(sources))

            self.store.discover_candidates(source, scope_key, list(candidates.values()), initialized=initialized)
            stat['candidate_count'] = len(candidates)
            known_dates = [candidate.date for candidate in candidates.values() if candidate.date]
            stat['listing_date_range'] = {'oldest': min(known_dates), 'newest': max(known_dates)} if known_dates else None
            stat['outside_range_candidates'] = sum(bool(cutoff and candidate.date and candidate.date < cutoff)
                                                   for candidate in candidates.values())
            pending = self.store.pending_candidates(scope_key, cutoff=cutoff, keywords=self.options.keywords)
            for row in pending:
                self._check_cancel()
                if global_skip: break
                if stat['detail_attempts'] >= self.options.max_details_per_source:
                    limit('source_details'); break
                if self.detail_attempts >= self.options.max_details:
                    limit('global_details'); break
                if self.pages >= self.options.max_pages:
                    limit('global_pages'); break
                key = row['notice_key']
                candidate = Candidate(row['url'], row['title'], row['listed_date'], row['listed_on'], bool(row['trusted']))
                try:
                    candidate.url = allowed_url(candidate.url, hosts)
                except ValueError:
                    issue(ValueError('积压通知已不在当前允许范围内，未访问'), 'detail', 'unsafe_redirect')
                    continue
                try:
                    self._check_cancel()
                    check_rules(candidate.url)
                except (FetchError, AccessIssue, ValueError) as error:
                    issue(error, 'robots')
                    continue
                self._check_cancel()
                stat['detail_attempts'] += 1
                attempted.add(key)
                outcome, stored_key = 'parse_error', None
                try:
                    self.detail_attempts += 1
                    self.pages += 1
                    page = self.transport.fetch(candidate.url, hosts)
                    self._check_cancel()
                    notice = extract_detail(page.text, page.url, candidate, source)
                    state = self.store.remember(notice, not bool(row['baseline_candidate']))
                    stored_key, outcome = notice['key'], 'success'
                    stat['details'] += 1
                    if not notice['published_date']:
                        stat['unknown_date_details'] += 1
                    notice.update(state=state, sources=[{'id': source['id'], 'name': source['name']}],
                                  source_attribution='known', alternative_urls=[])
                    if not notice['detail_parsed']:
                        issue(ValueError('详情未识别正文容器，已保存标题和日期，请打开原文确认'), 'detail', 'unrecognized_detail')
                    # Persist every accepted read before applying this run's display filters.
                    if (not cutoff or not notice['published_date'] or notice['published_date'] >= cutoff) and (
                        not self.options.keywords or any(k.casefold() in (notice['title']+' '+notice['summary']).casefold()
                                                        for k in self.options.keywords)):
                        items.append(notice)
                        stat['matched_details'] += 1
                except AccessIssue as error:
                    outcome = 'access_issue'; stat['detail_failures'] += 1; issue(error, 'detail')
                except FetchError as error:
                    outcome = 'fetch_error'; stat['detail_failures'] += 1; issue(error, 'detail')
                except ValueError:
                    outcome = 'excluded'; stat['detail_failures'] += 1
                    issue(ValueError('详情已排除：不是可接受的通知内容'), 'detail', 'excluded')
                except Exception:
                    stat['detail_failures'] += 1
                    issue(ValueError('详情解析失败，需适配该网站'), 'detail', 'parse_error')
                self.store.record_attempt(scope_key, key, outcome, stored_notice_key=stored_key)
                self._progress('details', source['id'], completed, len(sources))
            remaining = [row for row in pending if row['notice_key'] not in attempted]
            stat['pending_unattempted'] = sum(row['last_attempt_seq'] is None for row in remaining)
            stat['pending_rechecks'] = len(remaining) - stat['pending_unattempted']
            stat['pending_list_pages'] = len(self.store.list_checkpoints(scope_key))
            reliable = bool(stat['recognized_list_pages'] or stat['details'])
            if reliable:
                self.store.initialize(scope_key)
                if not initialized:
                    message('本来源首次成功识别通知，本轮建立基线；首次收录不代表刚发布')
            if getattr(self.transport, 'blocked_methods', 0) > blocked_before:
                issue(ValueError('浏览器只读采集拦截了非 GET/HEAD 请求，依赖提交查询的栏目可能不完整'), 'list', 'read_only_blocked')
            if global_skip:
                outcome = 'not_attempted_budget'
            elif reliable:
                outcome = 'partial' if stat['issues'] or stat['limited'] else 'checked'
            elif not stat['list_pages'] and stat['issues']:
                outcome = 'failed'
            else:
                outcome = 'unrecognized'
            stat.update(outcome=outcome, status=STATUSES[outcome])
            coverage.append(stat)
            self.log(f'  列表 {stat["list_pages"]} 页，详情 {stat["details"]} 条；{stat["status"]}')
            if stat['messages']: self.log('  说明：'+'；'.join(stat['messages']))
            self._progress('source_complete', source['id'], completed + 1, len(sources))
        merged = merge_notices(items)
        omitted = sum(item['state'] == 'seen' for item in merged) if not self.options.include_seen else 0
        visible = [item for item in merged if self.options.include_seen or item['state'] != 'seen']
        visible.sort(key=lambda item: (item['published_date'] or '', item['title']), reverse=True)
        source_counts = Counter(row['outcome'] for row in coverage)
        outcome = ('checked' if coverage and all(row['outcome']=='checked' for row in coverage) else
                   'failed' if not any(row['recognized_list_pages'] or row['details'] for row in coverage) else 'partial')
        result = {'schema_version': 2, 'started_at': started, 'finished_at': utc_now(),
                  'transport_mode': self.options.transport_mode, 'days': self.options.days,
                  'outcome': outcome, 'seen_omitted': omitted, 'items': visible, 'coverage': coverage,
                  'stats': {'displayed': len(visible), 'page_reads': self.pages, 'transport_calls': self.transport.calls,
                            'detail_attempts': self.detail_attempts, 'source_counts': dict(source_counts),
                            'states': dict(Counter(item['state'] for item in visible))}}
        self._check_cancel()
        paths = write_report(output, result)
        self._check_cancel()
        self.store.finish(started, result['stats'], result=result)
        return result, paths
