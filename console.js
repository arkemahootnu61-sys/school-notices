'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const TOKEN_KEY = 'radar-console-token';
  const ACTIVE = new Set(['running', 'awaiting_login']);
  const FOCUS_SOURCES = new Set(['S01', 'S02', 'S03', 'S04', 'S05', 'S06', 'S07', 'S08', 'S09', 'S38', 'S44']);
  const STATE_LABELS = {seen: '已收录', baseline: '首次收录', new: '新发现', updated: '内容变化'};
  const COVERAGE_LABELS = {checked: '已检查', partial: '部分覆盖', unrecognized: '未识别列表', failed: '失败或受限', not_attempted_budget: '本轮未尝试（预算不足）'};
  const LIMIT_LABELS = {pages_per_seed: '单入口分页上限', source_list_pages: '来源列表页上限', source_details: '来源详情上限', global_pages: '本轮总页数上限', global_details: '本轮总详情上限'};
  const RULE_FIELDS = {list_selector: '列表容器', detail_link_selector: '详情链接',
    list_date_selector: '条目发布日期（在单条内匹配）', next_page_selector: '下一页链接',
    title_selector: '详情标题', date_selector: '详情发布日期', content_selector: '详情正文'};
  let token = '', connected = false, closed = false, pending = false;
  let sources = [], revision = '', selected = new Set(), sourceUrls = new Map(), dirty = false;
  let robotsExceptions = new Set();
  let sourceRules = new Map();
  let job = {id: null, type: null, status: 'idle'}, latest = null;
  let offset = 0, total = 0, loading = false, noticeSequence = 0, stateSequence = 0, sourceSequence = 0;
  let pollTimer = null, searchTimer = null;
  const limit = 50;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }
  function message(id, text = '', kind = 'neutral') {
    const element = $(id);
    element.textContent = text;
    element.className = `notice ${kind}`;
    element.hidden = !text;
  }
  function externalLink(url, text) {
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return node('span', '', text);
      const link = node('a', '', text);
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    } catch (_) {
      return node('span', '', text);
    }
  }
  function timeText(value) {
    if (!value) return '未记录';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {hour12: false});
  }
  function active() { return ACTIVE.has(job.status); }
  function rulesForSource(source) {
    return {seed_urls: source.seed_urls || [source.url],
      ...Object.fromEntries(Object.keys(RULE_FIELDS).map(key => [key, source[key] || '']))};
  }
  function enabledCount() { return sources.filter(source => source.enabled).length; }
  function updateSourceDraft() {
    dirty = sources.some(source => Boolean(source.enabled) !== selected.has(source.id) ||
      sourceUrls.get(source.id).trim() !== source.url ||
      Boolean(source.allow_robots_unavailable) !== robotsExceptions.has(source.id) ||
      JSON.stringify(rulesForSource(source)) !== JSON.stringify(sourceRules.get(source.id)));
    message('sources-message', dirty ? '来源选择、网址、采集适配或规则许可尚未保存。保存后才用于采集；修改入口会清除原适配与许可，需重新确认。' : '', 'warning');
    updateControls();
  }
  function updateControls() {
    const unavailable = !connected || closed || pending;
    $('collect').disabled = unavailable || active() || dirty || !enabledCount();
    $('login').disabled = unavailable || active();
    $('mode').disabled = unavailable || active();
    $('days').disabled = unavailable || active();
    $('login-source').disabled = unavailable || active();
    $('cancel').disabled = unavailable || !active();
    $('shutdown').disabled = unavailable || active();
    $('refresh').disabled = unavailable;
    $('cleanup-read').disabled = unavailable || active();
    $('save-sources').disabled = unavailable || active() || !dirty || selected.size === 0;
    $('reload-sources').disabled = unavailable || active();
    $('add-source').disabled = unavailable || active() || dirty || !revision;
    $('add-name').disabled = unavailable || active();
    $('add-url').disabled = unavailable || active();
    $('login-finish').disabled = unavailable || job.type !== 'login' || job.status !== 'awaiting_login';
    for (const input of $('sources').querySelectorAll('input, textarea')) input.disabled = unavailable || active();
    for (const button of $('coverage').querySelectorAll('[data-login-source]')) {
      const selectable = [...$('login-source').options].some(option => option.value && option.value === button.dataset.loginSource);
      button.disabled = unavailable || active() || !selectable;
    }
    for (const id of ['view', 'search', 'source-filter']) $(id).disabled = !connected || closed;
    $('previous').disabled = !connected || closed || loading || offset === 0;
    $('next').disabled = !connected || closed || loading || offset + limit >= total;
    $('selection-count').textContent = `${selected.size} / ${sources.length} 个来源已勾选${dirty ? ' · 有未保存更改，采集已禁用' : ' · 已保存'}`;
  }
  function disconnect(text) {
    connected = false;
    clearTimeout(pollTimer);
    message('connection-message', text, 'error');
    updateControls();
  }
  async function api(path, options = {}) {
    const headers = {'X-Radar-Token': token};
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(path, {method: options.method || 'GET', headers, cache: 'no-store', credentials: 'omit', ...(options.body === undefined ? {} : {body: JSON.stringify(options.body)})});
    } catch (_) {
      throw new Error('无法连接本地服务。已有通知保留在页面中；请检查终端，必要时重新双击 run.bat。');
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('本地服务返回了无法识别的数据，请检查终端。'); }
    if (!response.ok) {
      if (response.status === 403) disconnect('本标签的访问凭据已失效或不被接受。请重新双击 run.bat 打开控制台；已有通知不会清空。');
      const error = new Error(data.error?.message || `操作失败（HTTP ${response.status}）`);
      error.status = response.status;
      error.code = data.error?.code;
      throw error;
    }
    return data;
  }

  function renderSources(data) {
    sources = data.sources;
    revision = data.revision;
    selected = new Set(sources.filter(source => source.enabled).map(source => source.id));
    sourceUrls = new Map(sources.map(source => [source.id, source.url]));
    robotsExceptions = new Set(sources.filter(source => source.allow_robots_unavailable).map(source => source.id));
    sourceRules = new Map(sources.map(source => [source.id, rulesForSource(source)]));
    dirty = false;
    const cards = document.createDocumentFragment();
    const sourceFilter = $('source-filter').value;
    const loginSource = $('login-source').value;
    $('source-filter').replaceChildren(node('option', '', '全部已存来源'));
    $('source-filter').firstChild.value = '';
    $('login-source').replaceChildren(node('option', '', '统一身份认证入口'));
    $('login-source').firstChild.value = '';
    for (const source of sources) {
      const card = node('article', 'source-card');
      card.dataset.sourceId = source.id;
      const label = node('label', 'source-choice');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.id = `source-${source.id}`;
      checkbox.value = source.id;
      checkbox.checked = Boolean(source.enabled);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(source.id); else selected.delete(source.id);
        updateSourceDraft();
      });
      label.append(checkbox, node('span', '', `${source.id} · ${source.name}`));
      const urlLabel = node('label', 'source-url', '学校网站或通知栏目网址');
      const urlInput = document.createElement('input');
      const permissionInput = document.createElement('input');
      urlInput.id = `source-url-${source.id}`;
      urlInput.type = 'url';
      urlInput.required = true;
      urlInput.autocomplete = 'off';
      urlInput.value = source.url;
      urlInput.addEventListener('input', () => {
        sourceUrls.set(source.id, urlInput.value);
        const savedEntry = urlInput.value.trim() === source.url;
        if (!savedEntry) {
          robotsExceptions.delete(source.id);
          permissionInput.checked = false;
        }
        const reset = rulesForSource(savedEntry ? source : {url: urlInput.value.trim()});
        sourceRules.set(source.id, reset);
        for (const input of card.querySelectorAll('[data-rule]')) {
          input.value = input.dataset.rule === 'seed_urls' ? reset.seed_urls.join('\n') : reset[input.dataset.rule];
        }
        updateSourceDraft();
      });
      urlLabel.append(urlInput);
      card.append(label, urlLabel, externalLink(source.url, '打开已保存入口'));
      const permissionLabel = node('label', 'source-permission');
      permissionInput.type = 'checkbox';
      permissionInput.id = `source-robots-${source.id}`;
      permissionInput.checked = robotsExceptions.has(source.id);
      permissionInput.addEventListener('change', () => {
        if (permissionInput.checked) robotsExceptions.add(source.id); else robotsExceptions.delete(source.id);
        updateSourceDraft();
      });
      permissionLabel.append(permissionInput, node('span', '', '本人许可：仅规则连接失败 / 超时 / 空响应时继续公开采集（规则未核实，仍遵守明确禁止）'));
      card.append(permissionLabel);
      const adaptation = node('details', 'source-rules');
      adaptation.id = `source-rules-${source.id}`;
      adaptation.append(node('summary', '', '采集适配（可选；新增网站自动识别不足时填写）'));
      adaptation.append(node('p', 'help', '入口每行一个，必须属于本站允许域名。CSS 选择器留空使用通用识别；不填写脚本、账号或请求头。日期需是发布日期，不是活动日期。'));
      const fields = {seed_urls: '采集栏目入口（每行一个）', ...RULE_FIELDS};
      for (const [key, text] of Object.entries(fields)) {
        const ruleLabel = node('label', 'source-rule', text);
        const input = document.createElement(key === 'seed_urls' ? 'textarea' : 'input');
        input.id = `source-rule-${key}-${source.id}`;
        input.dataset.rule = key;
        input.autocomplete = 'off';
        if (key === 'seed_urls') input.rows = 3;
        else { input.type = 'text'; input.maxLength = 1000; input.placeholder = '自动识别'; }
        input.value = key === 'seed_urls' ? sourceRules.get(source.id).seed_urls.join('\n') : sourceRules.get(source.id)[key];
        input.addEventListener('input', () => {
          const rules = sourceRules.get(source.id);
          rules[key] = key === 'seed_urls' ? input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean) : input.value.trim();
          updateSourceDraft();
        });
        ruleLabel.append(input);
        adaptation.append(ruleLabel);
      }
      card.append(adaptation);
      if (source.group) card.append(node('p', 'help', source.group));
      if (!FOCUS_SOURCES.has(source.id)) card.append(node('p', 'help', '非本次重点验收'));
      cards.append(card);
      const option = node('option', '', `${source.id} · ${source.name}`);
      option.value = source.id;
      $('source-filter').append(option);
      try {
        const url = new URL(source.url);
        if (url.protocol === 'https:' && (url.hostname === 'xidian.edu.cn' || url.hostname.endsWith('.xidian.edu.cn'))) {
          const loginOption = option.cloneNode(true);
          $('login-source').append(loginOption);
        }
      } catch (_) { /* Invalid configured entries are reported by the server. */ }
    }
    $('sources').replaceChildren(cards);
    if ([...$('source-filter').options].some(option => option.value === sourceFilter)) $('source-filter').value = sourceFilter;
    if ([...$('login-source').options].some(option => option.value === loginSource)) $('login-source').value = loginSource;
    updateControls();
  }
  async function loadSources() {
    const sequence = ++sourceSequence;
    const data = await api('/api/sources');
    if (sequence !== sourceSequence || closed) return;
    // A response begun before a user's checkbox change must not replace that change.
    if (dirty) return;
    renderSources(data);
  }
  function noticeFlagButton(card, item, kind) {
    const button = node('button', 'button small notice-flag');
    button.type = 'button';
    button.dataset.kind = kind;
    const labels = kind === 'favorite'
      ? {on: '★ 已收藏', off: '☆ 收藏'}
      : {on: '✓ 已读', off: '标记已读'};
    const render = () => {
      const active = Boolean(item[kind]);
      button.textContent = active ? labels.on : labels.off;
      button.dataset.active = String(active);
      button.setAttribute('aria-pressed', String(active));
    };
    render();
    button.addEventListener('click', async () => {
      if (!connected || closed) return;
      button.disabled = true;
      try {
        const body = {key: item.key};
        body[kind] = !item[kind];
        const data = await api('/api/notice', {method: 'PUT', body});
        item[kind] = data[kind];
        if (kind === 'favorite') card.dataset.favorite = String(Boolean(data.favorite));
        render();
      } catch (error) {
        message('notices-message', `${error.message}`, 'error');
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }
  function noticeCard(item) {
    const card = node('article', 'notice-card');
    card.dataset.key = item.key || '';
    card.dataset.state = item.state || 'seen';
    card.dataset.favorite = String(Boolean(item.favorite));
    const meta = node('div', 'notice-meta');
    const tag = node('span', 'tag', STATE_LABELS[item.state] || '已收录');
    tag.dataset.state = item.state || 'seen';
    meta.append(tag, node('span', '', item.published_date || '发布日期未知 · 请核对原文'));
    const title = node('h3');
    title.append(externalLink(item.url, item.title || '无标题通知'));
    card.append(meta, title, node('p', 'notice-excerpt', item.summary || '未提取到摘要，请打开原文确认。'));
    const attribution = item.sources?.length ? item.sources.map(source => source.name).join(' / ') : '来源归属未知（历史条目）';
    card.append(node('p', 'help', attribution));
    if (item.detail_parsed === false) card.append(node('p', 'help', '正文未完整识别，已保留标题与日期。'));
    const footer = node('div', 'notice-footer');
    const actions = node('div', 'notice-actions');
    actions.append(noticeFlagButton(card, item, 'favorite'), noticeFlagButton(card, item, 'read'));
    footer.append(actions, node('p', '', `上次成功读取：${timeText(item.last_checked_at || item.last_seen)}`));
    const alternatives = (item.alternative_urls || []).filter(url => url !== item.url);
    if (alternatives.length) {
      const links = node('div');
      links.append(node('span', '', '其他来源原文：'));
      alternatives.forEach((url, index) => {
        if (index) links.append(document.createTextNode(' · '));
        links.append(externalLink(url, `链接 ${index + 1}`));
      });
      footer.append(links);
    }
    card.append(footer);
    return card;
  }
  async function loadNotices() {
    const sequence = ++noticeSequence;
    const query = new URLSearchParams({view: $('view').value, days: $('days').value, q: $('search').value, source_id: $('source-filter').value, offset: String(offset), limit: String(limit)});
    loading = true;
    $('notices').dataset.loading = 'true';
    $('notices').setAttribute('aria-busy', 'true');
    updateControls();
    try {
      const data = await api(`/api/notices?${query}`);
      if (sequence !== noticeSequence || closed) return;
      if (offset > 0 && offset >= data.total) {
        offset = data.total ? Math.floor((data.total - 1) / limit) * limit : 0;
        await loadNotices();
        return;
      }
      total = data.total;
      const cards = document.createDocumentFragment();
      data.items.forEach(item => cards.append(noticeCard(item)));
      $('notices').replaceChildren(cards);
      $('notices').dataset.total = String(total);
      $('notices').dataset.offset = String(offset);
      $('notices').dataset.view = $('view').value;
      $('empty-notices').hidden = data.items.length > 0;
      const filtered = $('search').value.trim() || $('source-filter').value;
      $('empty-notices').textContent = data.recorded === false ? data.message || '最近一次运行未记录变化明细，不能判断为没有更新。请切换“近期通知”查看已存通知。' : filtered ? '没有符合当前筛选的通知，可调整关键词、来源或日期范围。' : $('view').value === 'changes' ? '最近一次已提交采集没有发现 / 变化条目。请结合上方覆盖结论判断，不代表所有来源都没有更新。' : latest ? '当前时间范围没有已收录通知。可扩大日期范围；失败或受限不代表没有通知。' : '尚未采集通知。选择来源后点击“开始采集”。';
      $('notice-count').textContent = `共 ${total} 条 · ${$('view').value === 'recent' ? '读取本地已存通知' : '最近一次已提交采集的变化'}`;
      const rangeText = Number($('days').value) ? `最近 ${$('days').value} 天` : '不限日期';
      $('notice-count').textContent += ` · ${rangeText}（按发布日期，含日期未知项）`;
      $('page-label').textContent = total ? `${offset + 1}–${Math.min(offset + limit, total)} / ${total} · 每页 50 条` : '每页 50 条';
      message('notices-message', data.recorded === false ? data.message || '本次变化明细未记录；这不代表没有更新。' :
        latest && latest.days !== Number($('days').value) ?
          `当前浏览：${rangeText}；上次采集：${latest.days ? `最近 ${latest.days} 天` : '不限日期'}。此处切换立即筛选已存通知，扩大范围后请开始采集以补充尚未读取的历史。` : '', 'warning');
    } catch (error) {
      if (sequence === noticeSequence && !closed) message('notices-message', `${error.message} 未替换上次成功显示的通知。`, 'error');
    } finally {
      if (sequence === noticeSequence) {
        loading = false;
        $('notices').dataset.loading = 'false';
        $('notices').setAttribute('aria-busy', 'false');
        updateControls();
      }
    }
  }
  function resetNotices() { offset = 0; loadNotices(); }

  function renderCoverage(rows) {
    const cards = document.createDocumentFragment();
    for (const row of rows || []) {
      const card = node('article', 'coverage-card');
      card.dataset.outcome = row.outcome || '';
      card.append(node('h3', '', `${row.source_id || row.id || ''} · ${row.source_name || row.name || ''}`));
      card.append(node('p', '', COVERAGE_LABELS[row.outcome] || row.status || '未记录覆盖结论'));
      card.append(node('p', 'help', `识别列表 ${row.recognized_list_pages || 0} 页 · 成功详情 ${row.details || 0} 条 · 详情失败 ${row.detail_failures || 0} 条`));
      card.append(node('p', 'help', `本轮范围内成功详情 ${row.matched_details ?? '未记录'} 条 · 列表日期过早 ${row.outside_range_candidates ?? '未记录'} 条 · 已读详情日期未知 ${row.unknown_date_details ?? '未记录'} 条`));
      if (row.listing_date_range) card.append(node('p', 'help', `本轮列表识别日期：${row.listing_date_range.oldest} 至 ${row.listing_date_range.newest}`));
      const reasons = node('ul');
      for (const issue of row.issues || []) {
        reasons.append(node('li', '', `${issue.message}（${issue.code} / ${issue.stage}）`));
        if (issue.code === 'login_required') {
          const button = node('button', 'button small', '本人登录此站');
          button.type = 'button';
          button.dataset.loginSource = row.source_id || row.id || '';
          const selectable = [...$('login-source').options].some(option => option.value && option.value === button.dataset.loginSource);
          button.disabled = !selectable || active() || !connected || pending;
          button.addEventListener('click', () => startLogin(button.dataset.loginSource));
          card.append(button);
        }
        if (issue.code === 'campus_network_required') reasons.append(node('li', '', '请本人连接学校授权校园网 / aTrust；登录无法替代网络准入。'));
      }
      for (const reason of row.limit_reasons || []) reasons.append(node('li', '', LIMIT_LABELS[reason] || reason));
      for (const text of row.messages || []) reasons.append(node('li', '', text));
      if (reasons.childElementCount) card.append(reasons);
      card.append(node('p', 'help', `符合本轮条件的已发现候选：待补采 ${row.pending_unattempted || 0} · 待重检 ${row.pending_rechecks || 0}（不是全站剩余条数）`));
      if (row.pending_list_pages) card.append(node('p', 'help', `有 ${row.pending_list_pages} 个栏目保存了历史翻页进度；下轮继续，不是从头重复同两页。`));
      cards.append(card);
    }
    if (!cards.childNodes.length) cards.append(node('p', 'help', '尚无已提交的来源覆盖记录。'));
    $('coverage').replaceChildren(cards);
  }
  function renderSummary() {
    if (!latest) {
      message('run-summary', '尚无已提交的采集记录。页面打开只读取本地数据库，不访问学校网站。');
      $('run-summary').dataset.outcome = 'unknown';
      renderCoverage([]);
      return;
    }
    const counts = latest.stats?.source_counts || {};
    const status = latest.outcome;
    const states = latest.stats?.states || {};
    const found = (states.baseline || 0) + (states.new || 0) + (states.updated || 0);
    const conclusion = status === 'failed' ? '本轮采集失败，未成功读取通知来源。旧通知仍保留，可继续搜索。' : status === 'partial' ? '本轮仅部分覆盖，请查看失败、受限或预算不足的来源，不能据此认定没有更新。' : status === 'checked' ? found === 0 ? '本轮已检查，无新发现。近期已收录通知仍然保留。' : `本轮已检查，首次收录 ${states.baseline || 0}、新发现 ${states.new || 0}、内容变化 ${states.updated || 0} 条。` : '该历史运行未记录完整覆盖结论，请核对下方来源信息。';
    const countText = `已检查 ${counts.checked || 0} · 部分 ${counts.partial || 0} · 失败 / 未识别 ${(counts.failed || 0) + (counts.unrecognized || 0)} · 未尝试 ${counts.not_attempted_budget || 0}`;
    const mode = {auto: '自动模式', http: '仅 HTTP', browser: '仅专用 Chrome'}[latest.transport_mode] || '历史模式未记录';
    message('run-summary', `最近一次已提交采集：${conclusion} ${countText}。采集范围：${latest.days ? `最近 ${latest.days} 天` : '不限日期'}（按发布日期）。完成于 ${timeText(latest.finished_at)} · ${mode}`, status === 'failed' ? 'error' : status === 'partial' ? 'warning' : status === 'checked' ? 'success' : 'neutral');
    $('run-summary').dataset.outcome = status || 'unknown';
    renderCoverage(latest.coverage);
  }
  function renderJob() {
    $('job').dataset.status = job.status;
    $('job').dataset.type = job.type || '';
    $('job').dataset.id = job.id || '';
    const labels = {idle: '当前无任务', running: job.type === 'login' ? '正在打开专用登录窗口…' : '正在采集…', awaiting_login: '等待本人登录并确认保存', completed: job.type === 'login' ? '会话已保存。请自行点击“开始采集”。' : '采集任务已结束，请查看覆盖结论', failed: '任务失败', cancelled: '任务已停止；未提交的本轮数据不会保存'};
    $('job-status').textContent = `${labels[job.status] || job.status}${job.error?.message ? `：${job.error.message}` : ''}`;
    $('job-status').className = job.status === 'failed' ? 'notice error' : '';
    const progress = job.progress || {};
    const completed = progress.completed_sources || 0;
    const sourceTotal = progress.total_sources || 0;
    $('job-progress').max = sourceTotal || 1;
    $('job-progress').value = completed;
    $('job-progress').hidden = job.type !== 'collect';
    const source = sources.find(item => item.id === progress.source_id);
    const phase = {lists: '读取列表', details: '读取详情', source_complete: '来源完成'}[progress.phase] || '准备中';
    $('progress-text').textContent = job.type === 'collect' ? `${phase} · 已完成来源 ${completed} / ${sourceTotal} · 当前 ${source ? `${source.id} ${source.name}` : progress.source_id || '—'} · 列表 / 页面读取 ${progress.page_reads || 0} · 详情尝试 ${progress.detail_attempts || 0}` : '';
    $('login-panel').hidden = job.type !== 'login' || job.status !== 'awaiting_login';
    const logs = document.createDocumentFragment();
    for (const line of (job.logs || []).slice(-100)) logs.append(node('li', '', typeof line === 'string' ? line : line.message || ''));
    $('job-logs').replaceChildren(logs);
    updateControls();
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (active() && connected && !closed) pollTimer = setTimeout(() => loadState().catch(error => message('action-message', `${error.message} 点击“刷新本地数据”重新查看任务。`, 'error')), 1000);
  }
  async function loadState() {
    const sequence = ++stateSequence;
    const previousJob = job;
    const data = await api('/api/state');
    if (sequence !== stateSequence || closed) return;
    job = data.job;
    latest = data.latest;
    $('session-status').textContent = data.session_available ? '检测到专用会话资料；不代表当前登录仍有效。' : '尚无专用会话资料；公开来源仍可直接尝试采集。';
    renderJob();
    renderSummary();
    if (ACTIVE.has(previousJob.status) && !active()) await loadNotices();
    schedulePoll();
  }
  async function action(operation, errorTarget = 'action-message') {
    if (pending || closed) return;
    pending = true;
    updateControls();
    message(errorTarget);
    try { await operation(); } catch (error) {
      const conflict = error.status === 409 && errorTarget === 'sources-message' ? ' 你的表单和选择已保留。配置冲突时请先记录选择，再重新载入磁盘配置。' : '';
      message(errorTarget, `${error.message}${conflict}`, 'error');
    } finally { pending = false; updateControls(); }
  }
  async function startJob(path, body) {
    await action(async () => {
      const data = await api(path, {method: 'POST', body});
      job = {id: data.job_id, type: path === '/api/login' ? 'login' : 'collect', status: 'running', progress: {}, logs: []};
      renderJob();
      await loadState();
    });
  }
  function startLogin(sourceId = $('login-source').value) {
    if (active() || !connected || pending) return;
    startJob('/api/login', sourceId ? {source_id: sourceId} : {});
  }
  $('collect').addEventListener('click', () => startJob('/api/collect', {mode: $('mode').value, days: Number($('days').value)}));
  $('login').addEventListener('click', () => startLogin());
  $('login-finish').addEventListener('click', () => action(async () => {
    await api('/api/login/finish', {method: 'POST', body: {job_id: job.id}});
    message('action-message', '已发送保存请求，请等待专用窗口关闭。');
    await loadState();
  }));
  $('cancel').addEventListener('click', () => action(async () => {
    await api('/api/cancel', {method: 'POST', body: {job_id: job.id}});
    message('action-message', '已请求停止。正在进行的有限超时请求结束后回滚并释放会话，请稍候。', 'warning');
    await loadState();
  }));
  $('shutdown').addEventListener('click', () => action(async () => {
    await api('/api/shutdown', {method: 'POST', body: {}});
    closed = true;
    connected = false;
    clearTimeout(pollTimer);
    message('connection-message', '控制台服务已关闭。通知仍保存在本地，下次请双击 run.bat。', 'success');
    $('job').dataset.status = 'closed';
  }));
  $('refresh').addEventListener('click', () => action(async () => {
    await loadState();
    await loadSources();
    await loadNotices();
  }));
  $('reload-sources').addEventListener('click', () => action(async () => {
    // Explicit reload is the only operation allowed to discard unsaved selections.
    const data = await api('/api/sources');
    ++sourceSequence;
    renderSources(data);
    message('sources-message', '已重新载入磁盘配置，未保存选择已放弃。');
  }, 'sources-message'));
  $('save-sources').addEventListener('click', () => {
    for (const input of $('sources').querySelectorAll('input[type="url"]')) {
      if (!input.reportValidity()) return;
    }
    action(async () => {
      const source_urls = Object.fromEntries(sources.filter(source => sourceUrls.get(source.id).trim() !== source.url)
        .map(source => [source.id, sourceUrls.get(source.id).trim()]));
      const source_rules = Object.fromEntries(sources.filter(source =>
        JSON.stringify(rulesForSource(source)) !== JSON.stringify(sourceRules.get(source.id)))
        .map(source => [source.id, sourceRules.get(source.id)]));
      const data = await api('/api/sources', {method: 'PUT', body: {revision, enabled_ids: [...selected],
        source_urls, source_rules, robots_unavailable_ids: [...robotsExceptions]}});
      ++sourceSequence;
      renderSources(data);
      message('sources-message', '来源与规则许可已保存。修改入口会重新发现通知；未核实规则的采集标为部分覆盖，历史通知不受影响。', 'success');
    }, 'sources-message');
  });
  $('add-form').addEventListener('submit', event => {
    event.preventDefault();
    if (dirty) return;
    action(async () => {
      const data = await api('/api/sources', {method: 'POST', body: {revision, name: $('add-name').value.trim(), url: $('add-url').value.trim()}});
      ++sourceSequence;
      renderSources(data);
      $('add-form').reset();
      message('sources-message', '普通入口已添加并保存，尚未请求该网站。', 'success');
    }, 'sources-message');
  });
  $('search').addEventListener('input', () => {
    // Invalidate an already-running request immediately, not after the debounce.
    ++noticeSequence;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(resetNotices, 250);
  });
  for (const id of ['view', 'source-filter', 'days']) $(id).addEventListener('change', () => {
    clearTimeout(searchTimer);
    resetNotices();
  });
  function goToPage(nextOffset) {
    offset = nextOffset;
    loadNotices().then(() => $('notices-heading').scrollIntoView({block: 'start'}));
  }
  $('previous').addEventListener('click', () => goToPage(Math.max(0, offset - limit)));
  $('next').addEventListener('click', () => goToPage(offset + limit));
  $('cleanup-read').addEventListener('click', () => {
    if (!window.confirm('将删除所有“已读且未收藏”的通知，收藏的通知会保留。此操作不可撤销，确定继续？')) return;
    action(async () => {
      const data = await api('/api/notices/cleanup', {method: 'POST', body: {}});
      message('notices-message', data.removed ? `已清理 ${data.removed} 条已读且未收藏的通知。` : '没有可清理的已读通知；已收藏的通知会保留。', 'success');
      await loadNotices();
    }, 'notices-message');
  });

  async function initialize() {
    updateControls();
    if (location.protocol === 'file:') {
      for (const element of document.querySelectorAll('header, footer, main > :not(#connection-message)')) element.hidden = true;
      disconnect('请双击run.bat启动本地控制台');
      return;
    }
    try {
      const fragment = new URLSearchParams(location.hash.slice(1));
      const incoming = fragment.get('token');
      // Remove the secret before any API call or user navigation.
      if (location.hash) history.replaceState(null, '', `${location.pathname}${location.search}`);
      if (incoming) sessionStorage.setItem(TOKEN_KEY, incoming);
      token = incoming || sessionStorage.getItem(TOKEN_KEY) || '';
    } catch (_) {
      disconnect('浏览器无法保存本标签访问凭据。请允许本地站点的会话存储，并重新双击 run.bat。');
      return;
    }
    if (!token) {
      disconnect('缺少本标签访问凭据，请双击 run.bat 重新打开控制台。');
      return;
    }
    try {
      await loadSources();
      await loadState();
      connected = true;
      message('connection-message');
      renderJob();
      renderSummary();
      schedulePoll();
      await loadNotices();
    } catch (error) {
      disconnect(`${error.message} 请重新双击 run.bat；不会自动触发采集。`);
    }
  }
  initialize();
})();
