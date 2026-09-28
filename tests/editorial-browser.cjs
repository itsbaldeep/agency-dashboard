const {chromium, webkit} = require('/node_modules/playwright');
const assert = require('node:assert/strict');
(async () => {
 for (const engine of [chromium, webkit]) {
  const browser = await engine.launch({headless:true});
  for (const width of [320,390,768,1440]) {
   const page = await browser.newPage({viewport:{width,height:900}});
   await page.route(/google-analytics|googletagmanager/, r => r.abort());
   const errors=[]; page.on('pageerror',e=>errors.push(e.message));
   const response=await page.goto('http://100.64.0.1:5001/content/22/preview');
   assert.equal(response.status(),200);
   await page.locator('.pipeline-article img').waitFor();
   await page.locator('.pipeline-article img').scrollIntoViewIfNeeded();
   await page.waitForFunction(()=>[...document.querySelectorAll('.pipeline-article img')].every(i=>i.complete && i.naturalWidth>0));
   assert.equal(await page.locator('.pipeline-article .imgph').count(),0);
   assert.equal(await page.locator('.pipeline-article details.faq').count(),2);
   assert.ok(await page.locator('.pipeline-article a[href="https://trueapply.in/blog/content-23/"]').count());
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1));
   await page.locator('.block-citations a').first().click();
   assert.ok(await page.locator('.external-link-dialog').evaluate(e=>e.open));
   await page.getByRole('button',{name:'Stay here'}).click();
   await page.locator('#asset-studio > summary').click();
   await page.locator('#asset-library').click();
   await page.locator('.asset-card').first().waitFor();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1));
   if (engine===chromium && width===390) {
    await page.locator('#asset-studio > summary').click();
    await page.locator('.pipeline-article figure').scrollIntoViewIfNeeded();
    await page.screenshot({path:'/tmp/draft22-mobile-image.png'});
   }
   assert.deepEqual(errors,[]);
   console.log(JSON.stringify({engine:engine.name(),width,images:'loaded',faq:2,overflow:false,externalDialog:'passed',library:'passed'}));
   await page.close();
  }
  await browser.close();
 }
})();
