/* A93 isolated browser acceptance: the personal research loop done ENTIRELY
 * through the real board page (login, import, create, follow-up, sign-off,
 * reject, re-research, cancel). Environment init (server, replay worker) and
 * read-only persistence checks run through tests/personal_board_entry.py
 * under the isolation guard; USER actions are real page interactions only —
 * no direct HTTP/CLI/DB writes substitute for them.
 *
 * Requires: PLAYWRIGHT_MODULE (playwright or playwright-core), a Chromium
 * executable (PLAYWRIGHT_CHROMIUM_EXECUTABLE or the playwright cache), and a
 * Python 3.12 interpreter (STOCK_AGENT_PYTHON or python3.12 on PATH). */
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');
const net = require('net');
const assert = require('assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const OWNER = 'a93-browser-user';

function resolvePython() {
  const candidates = [
    process.env.STOCK_AGENT_PYTHON,
    path.join(root, '.venv312', 'bin', 'python'),
    path.join(root, '.venv312', 'Scripts', 'python.exe'),
    'python3.12',
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (!candidate.includes(path.sep)) return candidate; // bare name: let PATH resolve it
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error('no locked Python 3.12 interpreter found');
}

function resolveChromium() {
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) return process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  const cache = path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright');
  if (fs.existsSync(cache)) {
    const builds = fs.readdirSync(cache).filter(name => /^chromium-\d+$/.test(name)).sort();
    const newest = builds[builds.length - 1];
    if (newest) {
      const app = path.join(cache, newest, 'chrome-mac-arm64',
        'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing');
      if (fs.existsSync(app)) return app;
    }
  }
  return undefined; // let playwright use its bundled browser
}

async function freePort() {
  return new Promise(resolve => {
    const socket = net.createServer();
    socket.listen(0, '127.0.0.1', () => {
      const port = socket.address().port;
      socket.close(() => resolve(port));
    });
  });
}

async function main() {
  const python = resolvePython();
  const tempReal = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'stock-a93-')));
  const shots = path.join(root, 'output', 'playwright', 'personal-flow');
  fs.mkdirSync(shots, { recursive: true });

  const config = {
    storage: { sqlite_path: path.join(tempReal, 'data', 'stock_agent.sqlite3') },
    official_evidence_vault: {
      enabled: true,
      events_path: path.join(tempReal, 'vault', 'events.jsonl'),
      index_path: path.join(tempReal, 'vault', 'index.json'),
      objects_dir: path.join(tempReal, 'vault', 'objects'),
      quarantine_dir: path.join(tempReal, 'vault', 'quarantine'),
      report_dir: path.join(tempReal, 'vault', 'reports'),
      allow_network_fetch: false,
      automatic_approval: false,
    },
    research_operations: {
      enforce_roster: false,
      roster_path: path.join(tempReal, 'vault', 'roster.json'),
      restrictions_path: path.join(tempReal, 'vault', 'restrictions.json'),
    },
    llm_governance: { enabled: true, enforce: true },
    market_data_governance: {
      approved_internal_research_providers: ['eastmoney'],
      use_production_pilot_provider_registry: false,
      require_consensus_for_llm: false,
      allow_degraded_offhours_llm: true,
    },
    task_runtime: { db_path: path.join(tempReal, 'tasks.sqlite3') },
    personal_evidence: { root: path.join(tempReal, 'personal_evidence') },
  };
  fs.mkdirSync(path.join(tempReal, 'vault'), { recursive: true });
  fs.mkdirSync(path.join(tempReal, 'data'), { recursive: true });
  fs.writeFileSync(config.research_operations.restrictions_path, JSON.stringify({ entries: [] }));
  const configPath = path.join(tempReal, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify(config));

  // Explicitly-labelled SAMPLE announcement (not a real filing).
  const announcementPath = path.join(tempReal, 'a93-sample-announcement.html');
  fs.writeFileSync(announcementPath, `<!DOCTYPE html><html><head><title>A93样本公告</title></head>
<body><h1>样本公司 2026 年第三季度样本报告</h1>
<p>本公告为 A93 个人工作台浏览器验收的明示样本文本，不是任何真实公司的披露文件。</p>
<p>样本营业收入为 1,234.56 万元，同比 -7.89%。</p>
</body></html>`);

  const entry = path.join(root, 'tests', 'personal_board_entry.py');
  const common = ['--root', root, '--temp', tempReal, '--config', configPath];
  const work = (taskId, extra = []) => JSON.parse(cp.execFileSync(python,
    ['-B', entry, 'work', ...common, ...(taskId ? ['--task-id', taskId] : []), ...extra],
    { cwd: root, encoding: 'utf8', timeout: 120000 }));
  const state = taskId => JSON.parse(cp.execFileSync(python,
    ['-B', entry, 'state', ...common, '--task-id', taskId],
    { cwd: root, encoding: 'utf8', timeout: 60000 }));

  const port = await freePort();
  const server = cp.spawn(python, ['-B', entry, 'serve', ...common, '--port', String(port)],
    { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverError = '';
  server.stderr.on('data', chunk => { serverError += chunk.toString(); });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server not ready: ' + serverError)), 60000);
    server.stdout.on('data', chunk => {
      if (chunk.toString().startsWith('READY')) { clearTimeout(timer); resolve(); }
    });
    server.on('exit', code => reject(new Error('server exited early: ' + code + ' ' + serverError)));
  });

  let browser;
  const url = `http://127.0.0.1:${port}/`;
  try {
    browser = await chromium.launch({ headless: true, ...(resolveChromium() ? { executablePath: resolveChromium() } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(30000);

    // ---- 1) login through the page form ----
    await page.goto(url);
    await page.locator('#login-form [name=actor]').fill(OWNER);
    await page.locator('#login-form [name=symbols]').fill('600519');
    await page.locator('#login-form button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#personal .who') &&
      document.querySelector('#personal .who').textContent.includes('已登录'));
    const session = await page.evaluate(async () => (await (await fetch('/api/v1/personal/session')).json()));
    assert.equal(session.actor, OWNER);
    assert.equal(session.logged_in, true);
    console.log('PASS login through the real page form; session resolves server-side');
    await page.screenshot({ path: path.join(shots, '01-logged-in.png'), fullPage: true });

    // ---- 2) import the labelled sample announcement through the page ----
    await page.locator('#personal details summary', { hasText: '导入样本原件' }).first().click();
    await page.locator('#import-form [name=source_url]')
      .fill('https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&fixture=a93-browser');
    await page.locator('#import-form [name=title]').fill('A93样本公告（非真实披露）');
    await page.locator('#import-form [name=published_at]').fill('2026-11-22T18:00:00+08:00');
    await page.locator('#import-form input[type=file]').setInputFiles(announcementPath);
    await page.locator('#import-form button[type=submit]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已导入'));
    await page.waitForFunction(() => document.querySelectorAll('#doc-list .doc').length === 1);
    const docCheckbox = page.locator('#attach-docs input[name=document_id]');
    await docCheckbox.waitFor();
    assert.equal(await docCheckbox.count(), 1, 'imported source becomes selectable at create time');
    console.log('PASS sample announcement imported via the page into the personal namespace');

    // ---- 3) create the research with the source selected ----
    await page.locator('#create-form [name=question]')
      .fill('A93浏览器验收：样本公司三季度业绩变动的驱动因素？');
    await page.locator('#create-form [name=symbols]').fill('600519');
    await docCheckbox.check();
    await page.locator('#create-form button[type=submit]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已创建'));
    const createdId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    assert.ok(createdId, 'page reports the created task id');
    await page.locator(`tr[data-task="${createdId}"] td.status-queued`).waitFor();
    const createdState = state(createdId);
    assert.equal(createdState.input_version, 1,
      'the task is BORN claimable with its complete v1 input (documents included)');
    assert.equal(createdState.snapshot_count, 1, 'the R01 snapshot chain starts at complete v1');
    assert.equal(createdState.evidence_documents.length, 1);
    console.log(`PASS research created from the page (${createdId}); complete v1 with the selected source (snapshots=1)`);

    // ---- 4) replay worker pass; view the result on the page ----
    const worked = work(createdId);
    assert.equal(worked.status, 'awaiting_human');
    // The worker READ the selected source in the owner's namespace: the drop
    // reason is the missing signal binding, never "not verified in vault".
    const workedState = state(createdId);
    assert.ok(workedState.evidence_documents.length === 1,
      'the claimed task carries the selected source');
    assert.ok(!workedState.dropped_evidence.some(reason => reason.includes('not_verified_in_vault')),
      `worker verified the source in the personal namespace (drops: ${workedState.dropped_evidence})`);
    await page.locator(`tr[data-task="${createdId}"] td.status-awaiting_human`).waitFor();
    await page.locator(`form[data-task="${createdId}"].ws-feedback textarea[name=note]`).waitFor();
    await page.locator(`tr[data-task="${createdId}"] td:first-child`).click();
    await page.waitForFunction(id => {
      const detail = document.querySelector('#detail');
      return detail && !detail.hidden && detail.textContent.includes(id) &&
        detail.textContent.includes('REPLAY 草稿') && detail.textContent.includes('输入版本');
    }, createdId);
    const detailText = await page.locator('#detail').innerText();
    assert.ok(detailText.includes('所选原件：document:sha256:'),
      'the v1 version history already names the selected source document');
    await page.screenshot({ path: path.join(shots, '02-replay-awaiting-detail.png'), fullPage: true });
    await page.keyboard.press('Escape');
    console.log('PASS replay result, pending question and input version history visible in the page');

    // ---- 5) follow up through the page (A64) ----
    const note = '追问：剔除样本研发费用后的口径如何？请补充说明。';
    await page.locator(`form[data-task="${createdId}"].ws-feedback textarea[name=note]`).fill(note);
    await page.locator(`form[data-task="${createdId}"] button[data-ws="followup"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已记录追问'));
    await page.locator(`tr[data-task="${createdId}"] td.status-queued`).waitFor();
    await page.waitForFunction(id => {
      const cell = document.querySelector(`tr[data-task="${id}"] td:nth-child(5)`);
      return cell && cell.textContent.trim() === 'v2';
    }, createdId);
    const followedState = state(createdId);
    assert.equal(followedState.snapshot_count, 2, 'follow-up preserved v1 and added v2');
    assert.equal(followedState.extra_note, note, 'the typed note is the persisted extra_note');
    console.log('PASS follow-up from the page: input v2, snapshot chain 1→2, note persisted verbatim');

    // ---- 6) worker pass again, then personal sign-off through the page ----
    assert.equal(work(createdId).status, 'awaiting_human');
    await page.locator(`tr[data-task="${createdId}"] td.status-awaiting_human`).waitFor();
    await page.locator(`form[data-task="${createdId}"] button[data-ws="acknowledge"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已签收'));
    await page.locator(`tr[data-task="${createdId}"] td.status-succeeded`).waitFor();
    const signedState = state(createdId);
    assert.equal(signedState.status, 'succeeded');
    assert.equal(signedState.acknowledgement.actor, OWNER);
    assert.equal(signedState.acknowledgement.scope,
      'personal_research_acknowledgement_not_institutional_approval');
    assert.ok(signedState.output_summary.includes('REPLAY'), 'original output preserved after sign-off');
    await page.locator(`tr[data-task="${createdId}"] td:first-child`).click();
    await page.waitForFunction(() => {
      const detail = document.querySelector('#detail');
      return detail && !detail.hidden && detail.textContent.includes('个人签收');
    });
    await page.screenshot({ path: path.join(shots, '03-signed-off-detail.png'), fullPage: true });
    await page.keyboard.press('Escape');
    console.log('PASS personal sign-off from the page: scoped acknowledgement persisted, output untouched');

    // ---- 7) explicit re-research; linked case readable from the page ----
    await page.locator(`tr[data-task="${createdId}"] button[data-ws="rerun"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已创建重新研究'));
    const rerunId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    assert.ok(rerunId && rerunId !== createdId);
    await page.locator(`tr[data-task="${rerunId}"] td.status-queued`).waitFor();
    const rerunState = state(rerunId);
    assert.equal(rerunState.origin.prior_task_id, createdId, 'rerun links the prior case');
    assert.equal(rerunState.origin.research_occasion, 'explicit_new_research');
    const priorState = state(createdId);
    assert.equal(priorState.status, 'succeeded', 'old terminal result not reopened');
    assert.ok(priorState.acknowledgement, 'old acknowledgement survives the rerun');
    await page.locator(`tr[data-task="${rerunId}"] td:first-child`).click();
    await page.waitForFunction(prior => {
      const link = document.querySelector(`#detail [data-tasklink="${prior}"]`);
      return link && link.textContent.trim() === prior;
    }, createdId);
    await page.locator(`#detail [data-tasklink="${createdId}"]`).click();
    await page.waitForFunction(prior => {
      const detail = document.querySelector('#detail');
      return detail && detail.textContent.includes(prior) && detail.textContent.includes('个人签收');
    }, createdId);
    await page.screenshot({ path: path.join(shots, '04-linked-prior-case.png'), fullPage: true });
    await page.keyboard.press('Escape');
    console.log('PASS re-research from the page: new task links the prior case; prior result and acknowledgement intact');

    // ---- 8) worker pass, then REJECT through the page (否定) ----
    assert.equal(work(rerunId).status, 'awaiting_human');
    await page.locator(`tr[data-task="${rerunId}"] td.status-awaiting_human`).waitFor();
    const rejectReason = '否定理由：样本口径不支持该结论（页面验收）。';
    await page.locator(`form[data-task="${rerunId}"].ws-feedback textarea[name=note]`).fill(rejectReason);
    await page.locator(`form[data-task="${rerunId}"] button[data-ws="reject"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已否定'));
    await page.locator(`tr[data-task="${rerunId}"] td.status-blocked`).waitFor();
    assert.ok(state(rerunId).last_error.includes('样本口径'), 'reject reason persisted as last_error');
    // blocked IS terminal: the row offers re-research, never a cancel.
    assert.equal(await page.locator(`tr[data-task="${rerunId}"] button[data-ws="cancel"]`).count(), 0,
      'a rejected (blocked) row shows no cancel button');
    assert.ok((await page.locator(`tr[data-task="${rerunId}"] button[data-ws="rerun"]`).count()) === 1,
      'a rejected row still offers explicit re-research');
    console.log('PASS rejection from the page: task blocked with the typed reason persisted; blocked row offers rerun, not cancel');

    // ---- 9) create a second research and CANCEL it from the page ----
    await page.locator('#create-form [name=question]').fill('A93浏览器验收：用于取消验证的第二个问题？');
    await page.locator('#create-form [name=symbols]').fill('600519');
    await page.locator('#create-form button[type=submit]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已创建'));
    const cancelId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    await page.locator(`tr[data-task="${cancelId}"] td.status-queued`).waitFor();
    await page.locator(`tr[data-task="${cancelId}"] button[data-ws="cancel"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已取消'));
    await page.locator(`tr[data-task="${cancelId}"] td.status-cancelled`).waitFor();
    assert.equal(state(cancelId).status, 'cancelled');
    console.log('PASS cancellation from the page while queued lands immediately');

    // ---- 10) A90: a no-question completion still allows 追问 and 签收 ----
    await page.locator('#create-form [name=question]').fill('A90浏览器验收：模型没有待答问题的样本研究？');
    await page.locator('#create-form [name=symbols]').fill('600519');
    await page.locator('#create-form button[type=submit]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已创建'));
    const noQuestionId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    await page.locator(`tr[data-task="${noQuestionId}"] td.status-queued`).waitFor();
    // Fixture (governed input bump): the recorded replay answer carries no
    // question, so the run completes without any human decision point.
    assert.equal(work(noQuestionId, ['--replay-no-questions']).status, 'succeeded');
    await page.locator(`tr[data-task="${noQuestionId}"] td.status-succeeded`).waitFor();
    const succeededForm = page.locator(`form[data-task="${noQuestionId}"].ws-feedback`);
    await succeededForm.waitFor();
    assert.ok((await page.locator(`form[data-task="${noQuestionId}"] button[data-ws="signoff"]`).count()) === 1,
      'an unacknowledged no-question result offers sign-off');
    assert.ok((await page.locator(`form[data-task="${noQuestionId}"] button[data-ws="followup"]`).count()) === 1,
      'a no-question result still offers a proactive follow-up');
    const outputBefore = state(noQuestionId).output_summary;
    // 10a) proactive follow-up: the note becomes a NEW linked question (A90).
    const a90Note = '主动追问：样本结论对下游口径是否稳健？';
    await succeededForm.locator('textarea[name=note]').fill(a90Note);
    await succeededForm.locator('button[data-ws="followup"]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已记录追问'));
    const followupTaskId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    assert.ok(followupTaskId && followupTaskId !== noQuestionId);
    await page.locator(`tr[data-task="${followupTaskId}"] td.status-queued`).waitFor();
    const followupState = state(followupTaskId);
    assert.equal(followupState.origin.prior_task_id, noQuestionId, 'A90 follow-up links the finished case');
    assert.equal(followupState.origin.research_occasion, 'user_followup');
    assert.equal(followupState.question, a90Note, 'the follow-up text is the new question');
    assert.equal(state(noQuestionId).output_summary, outputBefore,
      'the finished output is preserved across the follow-up');
    console.log('PASS A90 proactive follow-up on a no-question result: new linked task, original output intact');
    // 10a-2) lost-response retry: the first submission REACHES the server and
    // commits, but its response is lost (fixture 503). Resending the SAME
    // note must reuse the pending request id — had the page minted a fresh
    // id, the retry would queue a SECOND task.
    const retryNote = '主动追问：响应丢失后重发应复用同一任务。';
    let interceptOnce = true;
    await page.route('**/api/v1/workspace/followup', async route => {
      if (interceptOnce) {
        interceptOnce = false;
        await route.fetch(); // the server commits the follow-up task…
        await route.fulfill({status: 503, contentType: 'application/json',
          body: JSON.stringify({error: 'fixture: response lost after commit'})}); // …response lost
        return;
      }
      await route.continue();
    });
    const rowsBeforeRetry = await page.locator('tr[data-task]').count();
    await page.locator(`form[data-task="${noQuestionId}"].ws-feedback textarea[name=note]`).fill(retryNote);
    await page.locator(`form[data-task="${noQuestionId}"] button[data-ws="followup"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('操作被拒绝'));
    await page.locator(`form[data-task="${noQuestionId}"] button[data-ws="followup"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已记录追问'));
    await page.unroute('**/api/v1/workspace/followup');
    const rowsAfterRetry = await page.locator('tr[data-task]').count();
    assert.equal(rowsAfterRetry, rowsBeforeRetry + 1,
      'the lost-response retry reused the request id and queued exactly ONE task');
    const retriedTaskId = await page.evaluate(() => {
      const match = document.querySelector('#msg').textContent.match(/task_[0-9a-f]+/);
      return match ? match[0] : '';
    });
    assert.equal(state(retriedTaskId).question, retryNote);
    console.log('PASS lost-response follow-up retry: same request id resent, one task (server-side dedup)');
    // 10b) personal sign-off: acknowledgement ADDED to the preserved output.
    await page.locator(`form[data-task="${noQuestionId}"] button[data-ws="signoff"]`).click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('已签收'));
    const signedNoQuestion = state(noQuestionId);
    assert.equal(signedNoQuestion.status, 'succeeded');
    assert.equal(signedNoQuestion.acknowledgement.actor, OWNER);
    assert.equal(signedNoQuestion.acknowledgement.scope,
      'personal_research_acknowledgement_not_institutional_approval');
    assert.equal(signedNoQuestion.output_summary, outputBefore, 'sign-off rewrote no output byte');
    await page.locator(`tr[data-task="${noQuestionId}"] td:first-child`).click();
    await page.waitForFunction(() => {
      const detail = document.querySelector('#detail');
      return detail && !detail.hidden && detail.textContent.includes('个人签收');
    });
    await page.screenshot({ path: path.join(shots, '06-a90-signed-no-question.png'), fullPage: true });
    await page.keyboard.press('Escape');
    console.log('PASS A90 personal sign-off on a no-question result: scoped acknowledgement, output untouched');
    // 10c) resubmitting the SAME successful create reuses the task (DUPLICATE).
    await page.locator('#create-form [name=question]').fill('A90浏览器验收：模型没有待答问题的样本研究？');
    await page.locator('#create-form [name=symbols]').fill('600519');
    await page.locator('#create-form button[type=submit]').click();
    await page.waitForFunction(() => (document.querySelector('#msg') || {}).textContent &&
      document.querySelector('#msg').textContent.includes('DUPLICATE'));
    assert.equal(await page.locator(`tr[data-task="${noQuestionId}"]`).count(), 1,
      'a duplicate submission creates no second row');
    console.log('PASS duplicate resubmission reuses the existing task (DUPLICATE), queue stays single-task');

    // ---- 10d) A67: the follow-up text reads back at its version ----
    await page.locator(`tr[data-task="${createdId}"] td:first-child`).click();
    await page.waitForFunction(id => {
      const detail = document.querySelector('#detail');
      return detail && !detail.hidden && detail.textContent.includes(id);
    }, createdId);
    const detailAll = await page.locator('#detail').innerText();
    assert.ok(detailAll.includes('追问原文：' + note),
      'the typed follow-up is readable at its version in the case detail');
    assert.ok(detailAll.includes('所选原件：document:sha256:'),
      'the version names the selected source document');
    await page.keyboard.press('Escape');
    console.log('PASS A67 case detail reads back the follow-up text and the selected document per version');

    // ---- 11) page-vs-persistence consistency sweep ----
    for (const taskId of [createdId, rerunId, cancelId, noQuestionId, followupTaskId]) {
      const persisted = state(taskId);
      const shown = await page.locator(`tr[data-task="${taskId}"] td:nth-child(3)`).innerText();
      assert.equal(shown.trim(), persisted.status, `page status matches the store for ${taskId}`);
      const versionShown = await page.locator(`tr[data-task="${taskId}"] td:nth-child(5)`).innerText();
      assert.equal(versionShown.trim(), 'v' + persisted.input_version,
        `page input version matches the store for ${taskId}`);
    }
    console.log('PASS page-displayed status/input_version equal the persisted records for every task');

    // ---- 12) logout through the page ----
    await page.locator('#logout-btn').click();
    await page.waitForFunction(() => document.querySelector('#login-form'));
    const loggedOut = await page.evaluate(async () => (await (await fetch('/api/v1/personal/session')).json()));
    assert.equal(loggedOut.logged_in, false);
    assert.equal(await page.locator('button[data-ws]').count(), 0,
      'logged-out pages show no session-gated action buttons');
    assert.equal(await page.locator('form.ws-feedback').count(), 0,
      'logged-out pages show no feedback forms');
    console.log('PASS logout through the page; no session-gated buttons or forms remain');
    await page.screenshot({ path: path.join(shots, '05-logged-out.png'), fullPage: true });
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      await new Promise(resolve => { server.once('exit', resolve); server.kill(); });
    }
    const resolved = path.resolve(tempReal);
    if (path.dirname(resolved) !== path.resolve(fs.realpathSync(os.tmpdir())) ||
        !path.basename(resolved).startsWith('stock-a93-')) {
      throw new Error('Unsafe cleanup path: ' + resolved);
    }
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
