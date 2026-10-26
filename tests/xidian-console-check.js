async page => {
  // Run only on the isolated T01/T02 fixture console created by the smoke driver.
  // The driver owns /_fixture; this helper never visits a fixed port or real source.
  const checks = [], errors = [];
  const check = (condition, label) => {
    if (!condition) throw new Error(label);
    checks.push(label);
  };
  const onError = error => errors.push(String(error));
  const originalViewport = page.viewportSize();
  const base = new URL(page.url()).origin;
  const token = await page.evaluate(() => sessionStorage.getItem('radar-console-token'));
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || !token) throw new Error('Open the authenticated throwaway console first');
  const headers = {'X-Radar-Token': token, Origin: base, 'Content-Type': 'application/json'};
  const api = async (path, method = 'GET', body, expected = 200) => {
    const response = await page.request.fetch(`${base}${path}`, {method, headers, ...(body === undefined ? {} : {data: body})});
    if (response.status() !== expected) throw new Error(`${method} ${path}: expected ${expected}, got ${response.status()}`);
    return response.json();
  };
  const sources = await api('/api/sources');
  check(sources.sources.length === 2 && sources.sources.some(source => source.id === 'T01') && sources.sources.some(source => source.id === 'T02'), 'isolated two-source fixture only');
  const fixture = new URL('/_fixture', sources.sources.find(source => source.id === 'T01').url).href;
  const fixtureGet = async () => {
    const response = await page.request.get(fixture);
    if (!response.ok()) throw new Error('Fixture driver is unavailable');
    return response.json();
  };
  const fixtureSet = async body => {
    const response = await page.request.post(fixture, {data: body});
    if (!response.ok()) throw new Error('Fixture control failed');
  };
  const waitNotices = async (count, view = 'recent') => {
    await page.waitForFunction(({count, view}) => {
      const element = document.getElementById('notices');
      return element.dataset.loading === 'false' && element.dataset.view === view && Number(element.dataset.total) === count;
    }, {count, view});
  };
  const changeView = async (view, count) => {
    await page.locator('#view').selectOption(view);
    await waitNotices(count, view);
  };
  const collect = async () => {
    const prior = await page.locator('#job').getAttribute('data-id');
    await page.locator('#collect').click();
    await page.waitForFunction(prior => {
      const job = document.getElementById('job');
      return job.dataset.id && job.dataset.id !== prior && ['completed', 'failed', 'cancelled'].includes(job.dataset.status);
    }, prior, {timeout: 90000});
    check(await page.locator('#job').getAttribute('data-status') === 'completed', 'collection creates a committed result');
    await page.waitForFunction(() => document.getElementById('notices').dataset.loading === 'false');
  };
  const save = async () => {
    const response = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'PUT');
    await page.locator('#save-sources').click();
    check((await response).status() === 200, 'source selection saves through the API');
    await page.waitForFunction(() => document.getElementById('save-sources').disabled && document.getElementById('selection-count').textContent.includes('已保存'));
  };
  try {
    page.on('pageerror', onError);
    await page.waitForFunction(() => !document.getElementById('collect').disabled);
    check(!new URL(page.url()).hash, 'token fragment removed from address');
    const initial = await fixtureGet();
    check(initial.reads === 0 && initial.version === 1 && initial.third === false, 'opening console performs no collection');
    check(!await page.locator('#source-robots-T01').isChecked() && !await page.locator('#source-robots-T02').isChecked(), 'rules-unavailable permission is off by default');
    const updatedEntry = new URL('/tzgg.htm', fixture).href;
    await page.locator('#source-url-T01').fill(updatedEntry);
    check(await page.locator('#collect').isDisabled(), 'unsaved website edit disables collection');
    await save();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('collect').disabled);
    check(await page.locator('#source-url-T01').inputValue() === updatedEntry, 'edited existing source URL survives reload');
    const edited = (await api('/api/sources')).sources.find(source => source.id === 'T01');
    check(edited.seed_urls.length === 1 && edited.seed_urls[0] === updatedEntry, 'collection starts at edited website instead of stale seeds');
    await page.locator('#source-url-T01').fill('https://example.edu.cn/?ticket=do-not-save');
    const badEdit = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'PUT');
    await page.locator('#save-sources').click();
    check((await badEdit).status() === 400, 'unsafe existing-source URL edit is rejected');
    await page.waitForFunction(() => !document.getElementById('save-sources').disabled);
    check((await page.locator('#source-url-T01').inputValue()).includes('ticket='), 'failed edit preserves typed website');
    await page.locator('#source-url-T01').fill(updatedEntry);
    await page.locator('#days').selectOption('15');
    await collect();
    await waitNotices(2);
    check(await page.locator('.notice-card[data-state="baseline"]').count() === 2, 'first run retains two baseline notices');
    check(await page.locator('#run-summary').getAttribute('data-outcome') === 'checked', 'successful coverage explicitly checked');
    check((await api('/api/state')).latest.days === 15, 'half-month selection controls real collection options');
    await page.locator('#days').selectOption('7');
    await collect();
    await waitNotices(2);
    check((await api('/api/state')).latest.days === 7, 'week selection controls real collection options');
    await changeView('changes', 0);
    check((await page.locator('#run-summary').textContent()).includes('无新发现'), 'second run reports no changes, not lost history');
    await changeView('recent', 2);
    await fixtureSet({version: 2, third: true});
    await collect();
    await waitNotices(3);
    await changeView('changes', 2);
    check(await page.locator('.notice-card[data-state="updated"]').count() === 1 && await page.locator('.notice-card[data-state="new"]').count() === 1, 'third run distinguishes updated and new notices');
    await changeView('recent', 3);
    await page.locator('#source-robots-T01').check();
    check(await page.locator('#collect').isDisabled(), 'unsaved rules permission cannot affect collection');
    await save();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('collect').disabled);
    await waitNotices(3);
    check(await page.locator('#source-robots-T01').isChecked() && !await page.locator('#source-robots-T02').isChecked(), 'saved rules permission survives reload without enabling another source');

    await page.locator('#search').fill('第三场');
    await waitNotices(1);
    check((await page.locator('.notice-card').textContent()).includes('第三场'), 'search matches the complete saved collection');
    await page.locator('#search').fill('');
    await waitNotices(3);
    await page.locator('#source-filter').selectOption('T02');
    await waitNotices(0);
    await page.locator('#source-filter').selectOption('T01');
    await waitNotices(3);
    await page.locator('#source-filter').selectOption('');
    await waitNotices(3);
    const firstPage = await api('/api/notices?days=0&limit=2&offset=0');
    const secondPage = await api('/api/notices?days=0&limit=2&offset=2');
    check(firstPage.total === 3 && secondPage.total === 3 && firstPage.items.length === 2 && secondPage.items.length === 1 && !firstPage.items.some(first => first.key === secondPage.items[0].key), 'saved collection pagination has stable totals and no overlap');
    check(await page.locator('#previous').isDisabled() && await page.locator('#next').isDisabled(), 'single 50-item UI page has bounded navigation');

    await page.locator('#source-T01').uncheck();
    check(await page.locator('#collect').isDisabled() && await page.locator('#save-sources').isDisabled(), 'empty unsaved source selection cannot save or collect');
    await page.locator('#source-T02').check();
    await save();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('collect').disabled);
    check(!await page.locator('#source-T01').isChecked() && await page.locator('#source-T02').isChecked(), 'saved source selection survives reload');
    await waitNotices(3);
    check((await api('/api/sources')).sources.find(source => source.id === 'T02').enabled, 'saved source selection is read from disk');

    await page.locator('#source-T01').check();
    await page.locator('#source-T02').uncheck();
    await page.locator('#source-url-T01').fill(new URL('/tzgg/2.htm', fixture).href);
    check(!await page.locator('#source-robots-T01').isChecked(), 'changing entry clears old rules permission for reconfirmation');
    const external = await api('/api/sources');
    await api('/api/sources', 'PUT', {revision: external.revision, enabled_ids: ['T01', 'T02']});
    const conflict = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'PUT');
    await page.locator('#save-sources').click();
    check((await conflict).status() === 409, 'stale revision rejects source save');
    await page.waitForFunction(() => document.getElementById('sources-message').classList.contains('error'));
    check(await page.locator('#source-T01').isChecked() && !await page.locator('#source-T02').isChecked() && await page.locator('#collect').isDisabled(), 'revision conflict retains unsaved selections');
    check((await page.locator('#source-url-T01').inputValue()).endsWith('/tzgg/2.htm'), 'revision conflict preserves edited website');
    await page.locator('#reload-sources').click();
    await page.waitForFunction(() => document.getElementById('source-T02').checked && document.getElementById('save-sources').disabled);
    await page.locator('#source-T02').uncheck();
    await save();
    check((await api('/api/sources')).sources.filter(source => source.enabled).map(source => source.id).join() === 'T01', 'fixture source selection restored');

    await page.locator('#add-name').fill('不应保存的票据入口');
    await page.locator('#add-url').fill('https://example.com/?ticket=do-not-save');
    const rejected = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'POST');
    await page.locator('#add-source').click();
    check((await rejected).status() === 400, 'credential-like manual entry rejected');
    await page.waitForFunction(() => !document.getElementById('add-source').disabled);
    check(await page.locator('#add-name').inputValue() === '不应保存的票据入口', 'failed add preserves form');
    await page.locator('#add-name').fill('');
    await page.locator('#add-url').fill('');
    const privatePath = await page.request.get(`${base}/.private/browser-state.json`);
    check([403, 404].includes(privatePath.status()), 'private paths never served');
    const traversal = await page.request.get(`${base}/%2e%2e/sources.json`);
    check([403, 404].includes(traversal.status()), 'path traversal never served');

    await fixtureSet({slow: true});
    await page.locator('#collect').click();
    await page.waitForFunction(() => document.getElementById('job').dataset.status === 'running' && !document.getElementById('cancel').disabled);
    check(await page.locator('#collect').isDisabled() && await page.locator('#login').isDisabled() && await page.locator('#shutdown').isDisabled() && await page.locator('#source-T01').isDisabled(), 'active task locks conflicting controls');
    check(await page.locator('#source-url-T01').isDisabled() && await page.locator('#days').isDisabled(), 'active task locks website and collection-range edits');
    check(await page.locator('#source-robots-T01').isDisabled(), 'active task locks explicit rules permission');
    check(await page.locator('#source-rule-seed_urls-T01').isDisabled(), 'active task locks declarative site adaptation');
    check(await page.locator('#job-progress').isVisible() && (await page.locator('#progress-text').textContent()).includes('已完成来源'), 'active collection exposes source and request progress');
    await api('/api/collect', 'POST', {mode: 'http', days: 30}, 409);
    check(true, 'second collection cannot queue behind active task');
    await page.locator('#cancel').click();
    await page.waitForFunction(() => document.getElementById('job').dataset.status === 'cancelled', null, {timeout: 90000});
    await fixtureSet({slow: false});
    await waitNotices(3);
    check(await page.locator('#collect').isEnabled(), 'cancelled task releases controls and retains committed notices');
    await page.locator('#mode').selectOption('http');
    await fixtureSet({robots_disconnect: true});
    await collect();
    await waitNotices(3);
    const permittedRun = await api('/api/state');
    check(permittedRun.latest.outcome === 'partial' && permittedRun.latest.coverage.some(row => row.issues.some(issue => issue.code === 'robots_unverified')), 'explicit permission reads public notices but always reports rules unverified');
    check(await page.locator('#run-summary').evaluate(element => element.classList.contains('warning')), 'unverified rules never produce green full-coverage success');
    await page.locator('#source-robots-T01').uncheck();
    await save();
    await collect();
    await waitNotices(3);
    check((await api('/api/state')).latest.outcome === 'failed', 'revoking permission restores strict stop on unreadable rules');
    await fixtureSet({robots_disconnect: false});
    await page.locator('#source-robots-T01').check();
    await save();

    await fixtureSet({robots_fail: true});
    await collect();
    await waitNotices(3);
    check(await page.locator('#run-summary').getAttribute('data-outcome') === 'failed' && await page.locator('#run-summary').evaluate(element => element.classList.contains('error')), 'all-source failure is red and not no-updates success');
    await page.locator('#search').fill('第一场');
    await waitNotices(1);
    check((await page.locator('.notice-card').textContent()).includes('第一场'), 'failed run leaves old data searchable');
    await page.locator('#search').fill('');
    await waitNotices(3);
    await fixtureSet({robots_fail: false});

    await page.locator('#add-name').fill('通用自定义通知站');
    await page.locator('#add-url').fill(new URL('/board', fixture).href);
    const addedResponse = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'POST');
    await page.locator('#add-source').click();
    check((await addedResponse).status() === 201, 'ordinary future website can be added in the console');
    await page.waitForFunction(() => document.querySelectorAll('.source-card').length === 3);
    const added = (await api('/api/sources')).sources.find(source => source.name === '通用自定义通知站');
    check(added && !added.allow_robots_unavailable, 'new website does not inherit another source permission');
    await page.locator('#source-T01').uncheck();
    await page.locator(`#source-rules-${added.id} > summary`).click();
    const selectors = {list_selector: '.bulletins', detail_link_selector: 'a.entry',
      list_date_selector: '.pub', next_page_selector: 'a.forward', title_selector: '.doc-title',
      date_selector: '.doc-date', content_selector: '.detailtext'};
    for (const [key, value] of Object.entries(selectors)) await page.locator(`#source-rule-${key}-${added.id}`).fill(value);
    check(await page.locator('#collect').isDisabled(), 'unsaved extraction rules cannot be used by collection');
    await save();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('collect').disabled);
    await page.locator(`#source-rules-${added.id} > summary`).click();
    check(await page.locator(`#source-rule-next_page_selector-${added.id}`).inputValue() === 'a.forward', 'site adaptation survives save and reload');
    await page.locator(`#source-rule-list_selector-${added.id}`).fill('a[');
    const badSelector = page.waitForResponse(response => response.url() === `${base}/api/sources` && response.request().method() === 'PUT');
    await page.locator('#save-sources').click();
    check((await badSelector).status() === 400, 'invalid CSS is rejected before collection');
    await page.waitForFunction(() => !document.getElementById('save-sources').disabled);
    check(await page.locator(`#source-rule-list_selector-${added.id}`).inputValue() === 'a[', 'invalid adaptation is retained for correction');
    await page.locator(`#source-rule-list_selector-${added.id}`).fill('.bulletins');
    await page.locator('#days').selectOption('15');
    await collect();
    await waitNotices(4);
    check((await page.locator('#notices').textContent()).includes('第1场材料提交安排'), 'new website uses saved link title body and publication-date selectors');
    await page.locator('#days').selectOption('7');
    await waitNotices(3);
    check(await page.locator('#notices-message').isVisible(), 'different browse and last-collection windows are explicit');
    await page.locator('#days').selectOption('0');
    await collect();
    await waitNotices(5);
    check((await page.locator('#notices').textContent()).includes('第2场材料提交安排'), 'unlimited-date collection follows custom next link and reads older notice');
    await page.locator(`#source-url-${added.id}`).fill(new URL('/different-board', fixture).href);
    check(await page.locator(`#source-rule-detail_link_selector-${added.id}`).inputValue() === '', 'changing website clears obsolete extraction selectors');
    await page.locator('#reload-sources').click();
    await page.waitForFunction(() => document.getElementById('save-sources').disabled);
    await page.locator(`#source-rules-${added.id} > summary`).click();

    for (const viewport of [{width: 1440, height: 900}, {width: 390, height: 844}]) {
      await page.setViewportSize(viewport);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `layout has no horizontal overflow at ${viewport.width}px`);
      check(await page.locator('#collect').isEnabled() && await page.locator('#shutdown').isEnabled(), `controls available at ${viewport.width}px`);
      await page.screenshot({path: viewport.width === 1440 ? 'artifacts/xidian-console-desktop.png' : 'artifacts/xidian-console-mobile.png', fullPage: true});
    }
    check(errors.length === 0, 'no uncaught page errors');
    await page.locator('#shutdown').click();
    await page.waitForFunction(() => document.getElementById('job').dataset.status === 'closed');
    check(await page.locator('#collect').isDisabled() && (await page.locator('#connection-message').textContent()).includes('已关闭'), 'explicit shutdown disables controls and confirms service closed');
    return {passed: checks.length, checks, errors};
  } finally {
    page.off('pageerror', onError);
    if (originalViewport) await page.setViewportSize(originalViewport);
    // Do not reload: success intentionally leaves the service shut down.
  }
}
