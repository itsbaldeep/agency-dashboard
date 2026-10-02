const { chromium } = require('/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const script = fs.readFileSync('/static/content-assets.js', 'utf8');
const css = fs.readFileSync('/templates/content_assets.html', 'utf8').match(/<style>([\s\S]*?)<\/style>/)[1];
const fixture = `<!doctype html><html><head><style>${css}</style></head><body>
<details id="asset-studio"><summary>Assets</summary>
<section><button id="asset-suggestions-refresh" type="button">Refresh suggestions</button>
<p id="asset-suggestions-status"></p><p id="asset-suggestions-task"></p><div id="asset-suggestions-results"></div></section>
<div id="asset-results"></div><p id="asset-status"></p><form id="asset-search"><input id="asset-search-q" name="q"><button>Search</button></form>
<button id="asset-library" type="button">Library</button><form id="asset-upload"><input name="file"><input name="description"><input name="creator"><select name="rights"><option>owned</option></select><input name="source_url"><input name="public_asset" type="checkbox"></form>
<form id="asset-review"><img id="asset-preview"><p id="asset-provenance"></p><input name="alt"><input name="caption"><select name="index"><option value="0">Insert before prose</option><option value="2">Replace planned image</option><option value="3">End</option></select><input name="reviewed" type="checkbox"><button>Attach</button></form>
</details><script id="asset-context" type="application/json">{"id":22,"title":"Draft","revision":"rev-1","defaultIndex":2}</script>
</body></html>`;

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 320, height: 900}});
  let navigationCount = 0;
  let suggestionGets = 0;
  let refreshTask = false;
  const imports = [];
  const attaches = [];
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/content/22/assets/suggestions') {
      if (request.method() === 'POST') {
        refreshTask = true;
        return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, task_id: 10, task_status: 'queued'})});
      }
      suggestionGets += 1;
      const slot = {index: 2, label: 'Planned image', queries: ['resume workflow diagram'], reason: 'Supports the section.', candidates: [{kind: 'stock', provider_id: 'pex-1', url: 'https://images.example/one.jpg', thumbnail_url: 'https://images.example/thumb.jpg', alt: 'A resume workflow diagram', caption: 'A concise workflow.', source_url: 'https://pexels.example/photo/1', provenance: {provider: 'Pexels', creator: 'Photographer', rights: 'Pexels license'}}]};
      if (refreshTask) return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(suggestionGets % 2 ? {ok: true, task_status: 'failed', error: 'provider unavailable', slots: []} : {ok: true, task_status: 'failed', error: 'provider unavailable', slots: []})});
      if (suggestionGets === 1 || suggestionGets === 2) return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, task_id: 9, task_status: 'queued', slots: []})});
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, task_status: 'done', slots: [slot]})});
    }
    if (url.pathname === '/content/22/assets/import') {
      imports.push(JSON.parse(request.postData() || '{}'));
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, asset: {id: 77, url: 'https://cdn.example/imported.jpg', alt: '', caption: '', provenance: {provider: 'Pexels', creator: 'Photographer', rights: 'Pexels license'}}})});
    }
    if (url.pathname === '/content/22/assets/attach') {
      attaches.push(JSON.parse(request.postData() || '{}'));
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
    }
    return route.fulfill({status: 200, contentType: 'text/html', body: fixture});
  });
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigationCount += 1; });
  await page.goto('http://fixture.test/');
  await page.addScriptTag({content: script});
  await page.locator('#asset-studio summary').click();
  await page.waitForNavigation({waitUntil: 'load'});
  await page.addScriptTag({content: script});
  await page.waitForFunction(() => document.querySelector('#asset-suggestions-status').textContent.includes('Suggestions ready'));
  assert.ok(navigationCount >= 2, 'completed task should reload the page');
  assert.equal(await page.locator('.asset-query-chip').count(), 1);
  assert.equal(await page.locator('#asset-search-q').inputValue(), 'resume workflow diagram');
  assert.equal(await page.locator('#asset-review select[name="index"]').inputValue(), '2');
  assert.ok(await page.locator('.asset-suggestion-card').textContent().then(text => text.includes('View source and license')));
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));

  await page.getByRole('button', {name: 'Edit details'}).click();
  await page.waitForFunction(() => document.querySelector('#asset-review input[name="alt"]').value === 'A resume workflow diagram');
  assert.equal(await page.locator('#asset-review input[name="caption"]').inputValue(), 'A concise workflow.');
  assert.equal(imports.length, 1);
  const attachResponse = page.waitForResponse(response => response.url().endsWith('/content/22/assets/attach'));
  const secondReload = page.waitForNavigation({waitUntil: 'load'});
  await page.getByRole('button', {name: 'Use this image'}).click();
  await attachResponse;
  await secondReload;
  await page.addScriptTag({content: script});
  await page.waitForFunction(() => document.querySelector('#asset-suggestions-status').textContent.includes('Suggestions ready'));
  assert.equal(attaches[0].asset_id, 77);
  assert.equal(attaches[0].index, 2);
  assert.equal(attaches[0].reviewed, true);

  await page.locator('#asset-suggestions-refresh').click();
  await page.waitForFunction(() => document.querySelector('#asset-suggestions-status').textContent.includes('provider unavailable'));
  assert.equal(await page.locator('#asset-suggestions-status').textContent(), 'provider unavailable');
  await browser.close();
  console.log(JSON.stringify({queued_empty_poll: true, completed_reload: true, failed_stops: true, stock_edit_import: true, reviewed_attach: true, mobile_overflow: false}));
})().catch(error => { console.error(error); process.exitCode = 1; });
