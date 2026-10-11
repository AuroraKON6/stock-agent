/* Real Chromium + locked guarded Python. Only synthetic temporary identities. */
const cp = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');
const assert = require('assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');

async function fixture(lifetime = 300) {
  const child = cp.spawn(path.join(root, '.venv312', 'Scripts', 'python.exe'),
    ['-B', path.join(__dirname, 'summary_viewer_fixture.py'), String(lifetime)],
    {cwd:root, windowsHide:true, stdio:['pipe','pipe','pipe']});
  const lines = readline.createInterface({input:child.stdout})[Symbol.asyncIterator]();
  let stderr = '';
  child.stderr.on('data', data => {stderr += data.toString();});
  let waitTimer;
  try {
    const next = await Promise.race([lines.next(), new Promise((_,reject) => {
      waitTimer = setTimeout(() => reject(new Error('Fixture startup timeout')), 20000);
    })]);
    clearTimeout(waitTimer);
    if(next.done) throw new Error('Fixture exited: ' + stderr);
    const {url} = JSON.parse(next.value);
    return {url, origin:new URL(url).origin, child,
      async command(value){child.stdin.write(JSON.stringify(value) + '\n'); const reply=await lines.next(); assert.equal(JSON.parse(reply.value).done,value);},
      async close(){
        if(child.exitCode === null){
          child.stdin.end('"stop"\n');
          const exited = new Promise(resolve => child.once('exit', resolve));
          const timeout = setTimeout(() => child.kill(), 7000);
          await exited; clearTimeout(timeout);
        }
        assert.equal(child.exitCode, 0, 'fixture must shut down and clean only its own temporary data: ' + stderr);
      }
    };
  } catch(error){clearTimeout(waitTimer); child.kill(); throw error;}
}

async function main(){
  const browser = await chromium.launch({headless:true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
  const active = [];
  async function open(lifetime){
    const app = await fixture(lifetime); active.push(app);
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    await page.goto(app.url);
    await page.locator('#state[data-state=active]').waitFor();
    return {app,page};
  }
  try{
    const {app,page} = await open();
    assert.equal(new URL(page.url()).hash,'','one-time fragment removed before reading content');
    assert.ok((await page.locator('#summary').innerText()).includes('<img src=x'));
    assert.equal(await page.locator('#summary img').count(),0,'source/summary markup must be text, never executable HTML');
    assert.equal(await page.evaluate(()=>window.__injected),undefined);
    assert.equal(await page.locator('.source-text').innerText(),'Revenue decreased.');
    assert.ok((await page.locator('#provenance').innerText()).includes('Model called: false'));
    assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length,document.cookie]),[0,0,'']);
    const screenshots = path.join(root,'output','playwright');
    fs.mkdirSync(screenshots,{recursive:true});
    await page.screenshot({path:path.join(screenshots,'summary-viewer-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:path.join(screenshots,'summary-viewer-mobile.png'),fullPage:true});
    console.log('PASS exact excerpt, replay provenance, literal markup, removed fragment, no browser storage, desktop/mobile');

    const anonymous = await browser.newPage();
    await anonymous.goto(app.origin);
    await anonymous.locator('#state[data-state=locked]').waitFor();
    assert.equal(await anonymous.locator('#content').innerText(),'');
    assert.equal(await anonymous.evaluate(async()=> (await fetch('/api/detail')).status),403);
    const replay = await fetch(app.origin+'/session',{method:'POST',headers:{Origin:app.origin,'X-Review-Request':'1','Content-Type':'application/json'},
      body:JSON.stringify({bootstrap:new URL(app.url).hash.slice(1)})});
    assert.equal(replay.status,403);
    await page.locator('#close').click();
    await page.locator('#state[data-state=locked]').waitFor();
    assert.equal(await page.locator('#content').innerText(),'');
    await page.reload();
    await page.locator('#state[data-state=locked]').waitFor();
    console.log('PASS anonymous denial, single-use entry, explicit close clears data, reload cannot restore access');

    for(const command of ['change','revoke']){
      const run = await open();
      await run.app.command(command);
      await run.page.locator('#state[data-state=locked]').waitFor({timeout:10000});
      assert.equal(await run.page.locator('#content').innerText(),'');
      console.log('PASS '+command+' invalidates and clears the open browser view');
    }

    const expiring = await open(5);
    await expiring.page.locator('#state[data-state=locked]').waitFor({timeout:10000});
    assert.equal(await expiring.page.locator('#content').innerText(),'');
    console.log('PASS absolute expiry removes displayed research content');

    const delayed = await open();
    let release, ready;
    const held = new Promise(resolve=>release=resolve), captured = new Promise(resolve=>ready=resolve);
    await delayed.page.route('**/api/detail', async route=>{
      const response = await route.fetch();
      ready(); await held; await route.fulfill({response});
    });
    await captured;
    await delayed.page.locator('#close').click();
    release();
    await delayed.page.waitForTimeout(500);
    assert.equal(await delayed.page.locator('#content').innerText(),'','late valid response cannot repopulate a closed view');
    console.log('PASS late valid response cannot restore content after close');
  } finally{
    await browser.close();
    for(const app of active) await app.close();
  }
}
main().catch(error=>{console.error(String(error.stack || error).replace(/#[A-Za-z0-9_-]{40,}/g,'#[redacted]')); process.exitCode=1;});
