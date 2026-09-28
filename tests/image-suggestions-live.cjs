// Read-only acceptance against the deployed draft. Never attach or publish.
const {chromium, webkit} = require('/node_modules/playwright');
const assert = require('node:assert/strict');
(async () => {
  for (const engine of [chromium, webkit]) {
    const browser = await engine.launch({headless:true});
    for (const width of [320,390,768,1440]) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors=[];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/content/22/assets/**', async route => {
        assert.equal(route.request().method(), 'GET', 'Live acceptance must stay read-only');
        await route.continue();
      });
      await page.goto('http://100.64.0.1:5001/content/22/preview');
      await page.locator('#asset-studio > summary').click();
      await page.locator('.asset-suggestion-card').first().waitFor();
      assert.ok(await page.locator('.asset-suggestion-card').count() >= 2);
      assert.ok(await page.locator('#asset-search-q').inputValue());
      assert.ok(await page.locator('.asset-suggestion-card').first().textContent().then(t=>t.includes('Currently in this draft')));
      await page.locator('.asset-suggestion-card img').evaluateAll(async imgs => {
        await Promise.all(imgs.map(i=>i.decode()));
      });
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1));
      await page.locator('.asset-suggestion-card').first().getByRole('button',{name:'Edit details'}).click();
      assert.equal(await page.locator('#asset-review select[name=index]').inputValue(),'15');
      assert.ok((await page.locator('#asset-review input[name=alt]').inputValue()).length > 10);
      assert.ok(await page.locator('#asset-review input[name=caption]').inputValue());
      assert.equal(await page.locator('#asset-review input[name=reviewed]').isChecked(),false);
      await page.waitForFunction(()=>document.documentElement.scrollWidth <= innerWidth+1, null, {timeout:3000}).catch(()=>{});
      if (!await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1)) {
        console.log(await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,x:scrollX,elements:[...document.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right+scrollX>innerWidth).map(e=>({tag:e.tagName,id:e.id,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).slice(0,12)})));
        await page.screenshot({path:'/tmp/image-suggestions-overflow.png'});
      }
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1));
      if (engine===chromium && width===390) {
        await page.locator('#asset-suggestions-heading').scrollIntoViewIfNeeded();
        await page.screenshot({path:'/tmp/image-suggestions-mobile.png'});
      }
      assert.deepEqual(errors,[]);
      console.log(JSON.stringify({engine:engine.name(),width,candidates:'loaded',prefill:'passed',overflow:false}));
      await page.close();
    }
    await browser.close();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
