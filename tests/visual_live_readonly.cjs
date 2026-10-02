// Read-only acceptance checks for the authorized TrueApply visual pilot.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
(async()=>{
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://100.64.0.1:5001/content/23/preview',{waitUntil:'networkidle'});
  assert.equal(await page.locator('.content-preview-body .editorial-visual').count(),3);
  assert.equal(await page.locator('.content-preview-body .editorial-visual table').count(),1);
  await page.locator('.annotated_example').screenshot({path:'/tmp/visual-example-desktop.png'});
  await page.locator('.comparison').screenshot({path:'/tmp/visual-table-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  for (const visual of await page.locator('.content-preview-body .editorial-visual').all()) {
    assert(await visual.evaluate(el=>el.scrollWidth<=el.clientWidth+1));
    assert(await visual.evaluate(el=>el.getBoundingClientRect().right <= innerWidth));
  }
  await page.locator('.annotated_example').screenshot({path:'/tmp/visual-example-mobile.png'});
  const response = await page.request.get('http://100.64.0.1:5001/content/23/download-html');
  assert.equal(response.status(),200);
  const html=await response.text();
  assert.equal((html.match(/class="editorial-visual /g)||[]).length,3);
  assert(html.includes('Hypothetical example'));
  await page.goto('http://100.64.0.1:5001/content/21/preview');
  assert(await page.getByText('This is an outline for review, not a completed article.').isVisible());
  assert(await page.locator('.content-preview-body ol li').count()>0);
  assert.deepEqual(errors,[]);
  await browser.close();
  console.log('Live read-only checks passed: three visuals, desktop/mobile layout, portable HTML export, nonempty outline preview.');
})().catch(error=>{console.error(error.message);process.exit(1)});
