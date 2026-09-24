// Read-only checks against the draft and public theme, without publishing content.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage();
  const root = 'http://100.64.0.1:5001';
  async function checkVisuals(surface) {
    for (const width of [320,390,640,1440]) {
      await page.setViewportSize({width,height:900});
      const table = page.locator('.comparison-table').first();
      assert.equal(await table.count(),1);
      const cell = table.locator('td').first();
      if (width <= 600) {
        assert.equal(await cell.evaluate(el=>getComputedStyle(el).display),'grid',surface);
        assert(await cell.evaluate(el=>el.clientWidth>=220),`${surface}: readable cell width at ${width}`);
        assert(await cell.evaluate(el=>getComputedStyle(el,':before').content.includes('Technique')));
        assert(await page.locator('.comparison').first().evaluate(el=>parseFloat(getComputedStyle(el).paddingLeft)<=12));
      } else {
        assert.equal(await cell.evaluate(el=>getComputedStyle(el).display),'table-cell',surface);
      }
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${surface}: page overflow at ${width}`);
      assert(await table.evaluate(el=>el.scrollWidth<=el.clientWidth+1),`${surface}: comparison requires sideways scrolling`);
      if(width===390) await page.locator('.comparison').first().screenshot({path:`/tmp/visual-mobile-${surface}.png`});
    }
  }
  await page.goto(`${root}/content/23/preview`,{waitUntil:'networkidle'});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('.content-actionbar').evaluate(el=>getComputedStyle(el).position),'static');
  assert.equal(await page.locator('.content-preview-body').evaluate(el=>getComputedStyle(el).paddingLeft),'12px');
  await checkVisuals('dashboard');
  const exported = await page.request.get(`${root}/content/23/download-html`);
  assert.equal(exported.status(),200);
  const html = await exported.text();
  await page.setContent(html);
  await checkVisuals('export');
  // Use the actual deployed blog stylesheet, not a simplified test theme.
  const blog = await page.request.get('https://trueapply.in/blog/');
  const blogHtml = await blog.text();
  const href = blogHtml.match(/href="([^"]*screen\.css[^"]*)"/);
  assert(href,'Public blog stylesheet must be discoverable');
  const cssResponse = await page.request.get(new URL(href[1],'https://trueapply.in/blog/').href);
  assert.equal(cssResponse.status(),200);
  const css = await cssResponse.text();
  const body = html.match(/<body>([\s\S]*)<\/body>/)[1];
  await page.setContent(`<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body><main class="wrapper"><article class="article-body">${body}</article></main></body></html>`);
  await checkVisuals('blog-theme');
  await browser.close();
  console.log('Passed: compact dashboard, export and deployed blog CSS at 320/390/640/1440px. No publication.');
})().catch(error=>{console.error(error.stack);process.exit(1)});
