// Inspect the real Ghost draft without approving or publishing it.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch();
  const page=await browser.newPage();
  for(const width of [320,390,1440]) {
    await page.setViewportSize({width,height:844});
    await page.goto('http://100.64.0.1:5001/content/23/preview');
    await page.getByRole('button',{name:'Approve & Publish',exact:true}).click();
    const dialog=page.getByRole('dialog');
    assert(await dialog.isVisible());
    assert((await dialog.innerText()).includes('Ghost'));
    assert.equal(await dialog.locator('input,select,textarea').count(),0);
    assert.equal(await dialog.locator('#publishConfirm').count(),1);
    assert(await dialog.locator('#publishConfirm').isEnabled());
    assert((await dialog.innerText()).includes('https://trueapply.in/blog/'));
    const box=await dialog.boundingBox();
    assert(Math.abs(box.x+box.width/2-width/2)<2,'Dialog is horizontally centred');
    assert(Math.abs(box.y+box.height/2-844/2)<2,'Dialog is vertically centred');
    assert(box.width<=width-20);
    if(width===390) await dialog.screenshot({path:'/tmp/publish-dialog-mobile.png'});
    await page.keyboard.press('Escape');
    assert(!(await dialog.isVisible()));
  }
  await browser.close();
  console.log('Publishing dialog passed at 320/390/1440px: centred, connected Ghost destination, no setup fields. Confirmation was NOT clicked.');
})().catch(e=>{console.error(e.stack);process.exit(1)});
