/* Standalone browser driver. All project imports run under the locked Python. */
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');
const net = require('net');
const assert = require('assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv312', 'Scripts', 'python.exe');
const bootstrap = "import sys,runpy; from pathlib import Path; sys.path.insert(0,sys.argv.pop(1)); from scripts.run_isolated_tests import install_guard; import stock_agent.config as c; install_guard(c.PROJECT_ROOT); c.PROJECT_ROOT=Path(sys.argv.pop(1)); runpy.run_module('stock_agent',run_name='__main__')";

async function interruptibleProxy(port) {
  let enabled = true;
  const sockets = new Set();
  const eventCursors = [];
  const proxy = net.createServer(client => {
    if (!enabled) { client.destroy(); return; }
    const upstream = net.connect(port, '127.0.0.1');
    let header = '';
    function inspectHeader(chunk) {
      header += chunk.toString('latin1');
      if (header.includes('\r\n\r\n') || header.length > 16384) {
        if (header.startsWith('GET /api/v1/events ')) {
          const cursor = header.match(/\r\nlast-event-id:\s*([^\r\n]+)/i);
          eventCursors.push(cursor ? cursor[1].trim() : '');
        }
        client.removeListener('data', inspectHeader);
      }
    }
    client.on('data', inspectHeader);
    for (const socket of [client, upstream]) {
      sockets.add(socket);
      socket.on('close', () => sockets.delete(socket));
      socket.on('error', () => { client.destroy(); upstream.destroy(); });
    }
    client.on('close', () => upstream.destroy());
    upstream.on('close', () => client.destroy());
    client.pipe(upstream); upstream.pipe(client);
  });
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
  return {
    url: `http://127.0.0.1:${proxy.address().port}/`,
    eventCursors,
    pause() { enabled = false; for (const socket of sockets) socket.destroy(); },
    resume() { enabled = true; },
    async close() { this.pause(); await new Promise(resolve => proxy.close(resolve)); }
  };
}

async function main() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'stock-board-'));
  let server, browser, proxy;
  try {
    const config = JSON.parse(fs.readFileSync(path.join(root, 'config/watchlist.example.json'), 'utf8'));
    config.research_operations = {...config.research_operations, enforce_roster:false};
    config.llm_governance = {...config.llm_governance, enabled:true, enforce:true};
    config.research_operations.restrictions_path = path.join(temp,'restrictions.json');
    fs.writeFileSync(config.research_operations.restrictions_path, JSON.stringify({entries:[]}));
    config.task_runtime = {db_path:path.join(temp, 'tasks.sqlite3')};
    const configPath = path.join(temp, 'config.json');
    fs.writeFileSync(configPath, JSON.stringify(config));
    const args = ['-B', '-c', bootstrap, root, temp];
    const cli = (...cmd) => cp.execFileSync(python, [...args, ...cmd, '--config', configPath], {cwd:root, encoding:'utf8', timeout:30000, windowsHide:true});
    const specPath = path.join(temp, 'spec.json');
    fs.writeFileSync(specPath, JSON.stringify({
      research_question:'Public research browser replay', data_classification:'public', public_context:{},
      replay_response:{schema_version:'1.0',task_id:'placeholder',summary:'Replay draft',key_findings:[],unknowns:[],evidence_refs:[],
        human_questions:[{question:'Accept draft?',why_needed:'Fixture only',options:[]}],model_self_assessed_confidence:0.2}
    }));
    const created = cli('task-create','--title','Browser replay','--spec',specPath,'--case-id','isolated-browser','--execution-mode','replay');
    const taskId = created.match(/task_[0-9a-f]+/)[0];
    assert.match(cli('task-run-next','--worker-id','test-browser'), /awaiting_human/);
    const port = await new Promise(resolve => {
      const socket = net.createServer();
      socket.listen(0,'127.0.0.1', () => {const port=socket.address().port; socket.close(()=>resolve(port));});
    });
    server = cp.spawn(python,[...args,'task-board','--config',configPath,'--port',String(port)],{cwd:root,stdio:'ignore',windowsHide:true});
    let ready=false;
    for(let i=0;i<100;i++) {
      if(server.exitCode !== null) throw new Error('Board exited before ready');
      try { const r=await fetch(`http://127.0.0.1:${port}/`); if(r.ok){ready=true;break;} } catch {}
      await new Promise(r=>setTimeout(r,100));
    }
    assert.ok(ready,'board ready');
    browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
    const page = await browser.newPage();
    const observer = await browser.newPage();
    await observer.addInitScript(() => {
      window.__sseMessages = [];
      const NativeEventSource = window.EventSource;
      window.EventSource = class extends NativeEventSource {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', ev => window.__sseMessages.push({id:ev.lastEventId, data:JSON.parse(ev.data)}));
        }
      };
    });
    const url=`http://127.0.0.1:${port}/`;
    proxy = await interruptibleProxy(port);
    await page.goto(url); await observer.goto(proxy.url);
    const form = page.locator(`form[data-task="${taskId}"]`);
    await form.locator('[name=notes]').fill('Isolated replay acceptance');
    const summaryBadge = await page.evaluate(async id => {
      const state = await (await fetch('/api/v1/tasks')).json();
      const task = state.tasks.find(t => t.task_id === id);
      const input = document.querySelector(`form[data-task="${id}"] [name=notes]`);
      input.focus();
      task.summary_review = {status:'pending'};
      window.__taskBoardRender(state);
      task.summary_review = {status:'invalidated'};
      window.__taskBoardRender(state);
      return document.querySelector(`tr[data-task="${id}"] .summary-state`).textContent === 'Summary: invalidated'
        && document.activeElement === input && input.value === 'Isolated replay acceptance';
    }, taskId);
    assert.ok(summaryBadge, 'summary expiry/invalidation updates even when the same-version feedback form is dirty');
    const screenshots = path.join(root, 'output', 'playwright');
    fs.mkdirSync(screenshots, {recursive:true});
    await page.setViewportSize({width:1440,height:900});
    await page.screenshot({path:path.join(screenshots,'summary-board-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile body has no horizontal overflow');
    await page.screenshot({path:path.join(screenshots,'summary-board-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:900});
    console.log('PASS read-only summary badge, dirty input retention, desktop/mobile capture');
    const focusResult = await page.evaluate(async id => {
      const state = await (await fetch('/api/v1/tasks')).json();
      const input = document.querySelector(`form[data-task="${id}"] [name=notes]`);
      input.focus(); input.setSelectionRange(3, 8);
      const original = state.tasks.find(task => task.task_id === id);
      state.tasks = [{...original, task_id:'task_browser_other', status:'queued', human_request:{}}, original];
      window.__taskBoardRender(state);
      return document.activeElement === input && input.selectionStart === 3 && input.selectionEnd === 8
        && input.value === 'Isolated replay acceptance';
    }, taskId);
    assert.ok(focusResult, 'reorder preserves the actual input node, value and selection');
    await form.locator('[name=actor]').fill('browser-fixture-user');
    await form.locator('button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#msg').textContent.includes('recorded'));
    await page.locator(`tr[data-task="${taskId}"] td.status-succeeded`).waitFor();
    // A second tab never submits or manually refreshes: it must consume SSE.
    await observer.locator(`tr[data-task="${taskId}"] td.status-succeeded`).waitFor();
    assert.equal(await observer.locator('#conn').innerText(),'LIVE');
    assert.equal(await page.locator(`form[data-task="${taskId}"][data-dirty="1"]`).count(),0);
    console.log('PASS isolated CLI replay, focus/selection retention, real POST, dirty cleared, observer SSE update');

    proxy.pause();
    await observer.waitForFunction(() => document.querySelector('#conn').textContent === 'RECONNECTING');
    const lastId = await observer.evaluate(() => window.__sseMessages.at(-1).id);
    const missedId = cli('task-create','--title','Created during disconnect','--spec',specPath,
      '--case-id','offline-event','--execution-mode','replay').match(/task_[0-9a-f]+/)[0];
    assert.equal(await observer.locator(`tr[data-task="${missedId}"]`).count(),0);
    proxy.resume();
    await observer.locator(`tr[data-task="${missedId}"]`).waitFor();
    await observer.waitForFunction(() => document.querySelector('#conn').textContent === 'LIVE');
    assert.ok(proxy.eventCursors.includes(lastId), `native EventSource resumes with ${lastId}; wire cursors=${proxy.eventCursors}`);
    const received = await observer.evaluate(() => window.__sseMessages);
    assert.equal(new Set(received.map(item => item.id)).size, received.length, 'no duplicate replay on reconnect');
    assert.ok(received.some(item => item.data.task_id === missedId));
    console.log('PASS real TCP disconnect, RECONNECTING, Last-Event-ID resume and missed event delivery');

    let releaseOld, signalOldCaptured, delayedRequest;
    const held = new Promise(resolve => { releaseOld = resolve; });
    const captured = new Promise(resolve => { signalOldCaptured = resolve; });
    let first = true;
    await page.route('**/api/v1/tasks', async route => {
      if (!first) { await route.continue(); return; }
      first = false;
      delayedRequest = route.request();
      const response = await route.fetch();
      const body = await response.text();
      signalOldCaptured();
      await held;
      await route.fulfill({status:200, contentType:'application/json', body});
    });
    cli('task-create','--title','First snapshot','--spec',specPath,'--case-id','snapshot-a','--execution-mode','replay');
    await Promise.race([captured, new Promise((_,reject)=>setTimeout(()=>reject(new Error('no delayed snapshot')),10000))]);
    const newestId = cli('task-create','--title','Newer snapshot','--spec',specPath,
      '--case-id','snapshot-b','--execution-mode','replay').match(/task_[0-9a-f]+/)[0];
    await page.locator(`tr[data-task="${newestId}"]`).waitFor();
    const finished = page.waitForEvent('requestfinished', request => request === delayedRequest);
    releaseOld(); await finished;
    await page.evaluate(() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.locator(`tr[data-task="${newestId}"]`).count(),1);
    await page.unroute('**/api/v1/tasks');
    console.log('PASS delayed older snapshot cannot replace newer state');

    await page.route('**/api/v1/tasks', route => route.fulfill({status:503,contentType:'application/json',body:'{"error":"fixture unavailable"}'}));
    cli('task-create','--title','Snapshot failure','--spec',specPath,'--case-id','snapshot-error','--execution-mode','replay');
    await page.waitForFunction(() => document.querySelector('#msg').textContent.includes('refresh failed'));
    assert.equal(await page.locator(`tr[data-task="${newestId}"]`).count(),1, 'failed snapshot preserves last good table');
    await page.unroute('**/api/v1/tasks');
    console.log('PASS HTTP snapshot failure preserves last good state');

    await observer.route('**/api/v1/tasks', route => route.fulfill({status:200,contentType:'application/json',body:'{"tasks":"malformed"}'}));
    const retryId = cli('task-create','--title','Retry after invalid snapshot','--spec',specPath,
      '--case-id','invalid-snapshot','--execution-mode','replay').match(/task_[0-9a-f]+/)[0];
    await observer.waitForFunction(id => window.__sseMessages.some(item=>item.data.task_id===id), retryId);
    await observer.waitForFunction(() => document.querySelector('#msg').textContent.includes('refresh failed'));
    assert.equal(await observer.locator(`tr[data-task="${retryId}"]`).count(),0);
    assert.equal(await observer.locator(`tr[data-task="${taskId}"]`).count(),1);
    await observer.unroute('**/api/v1/tasks');
    proxy.pause();
    await observer.waitForFunction(() => document.querySelector('#conn').textContent === 'RECONNECTING');
    const messageCount = await observer.evaluate(() => window.__sseMessages.length);
    proxy.resume();
    await observer.locator(`tr[data-task="${retryId}"]`).waitFor();
    assert.equal(await observer.evaluate(() => window.__sseMessages.length),messageCount,
      'reconnect recovers failed snapshot even when there are no unseen events');
    console.log('PASS malformed snapshot preserved, reconnect refresh recovers without new events');
  } finally {
    if(browser) await browser.close();
    if(proxy) await proxy.close();
    if(server && server.exitCode === null) {
      await new Promise(resolve=>{server.once('exit',resolve); server.kill();});
    }
    // Only this driver-created OS temporary directory can be removed.
    const resolved=path.resolve(temp);
    if(path.dirname(resolved)!==path.resolve(os.tmpdir()) || !path.basename(resolved).startsWith('stock-board-')) throw new Error('Unsafe cleanup path');
    fs.rmSync(resolved,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
