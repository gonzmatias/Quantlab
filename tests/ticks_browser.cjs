// Run with Playwright on NODE_PATH; uses installed Edge unless BROWSER_CHANNEL is set.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const python = process.env.TEST_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe');
  const server = spawn(python, [path.join(__dirname, 'tick_browser_fixture.py')], { cwd: root });
  let browser;
  server.stderr.pipe(process.stderr);
  try {
    const fixture = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timeout')), 30000);
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited: ${code}`)); });
      createInterface({ input: server.stdout }).once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
    });
    browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.request().url().startsWith(fixture.url)
      ? route.continue() : route.abort());
    await page.goto(fixture.url);
    assert.equal(await page.locator('#apiKeyInput').inputValue(), '');
    assert.equal(await page.locator('a[href="/api/gherkin/example-ticks"]').count(), 1);
    const template = await page.request.get(fixture.url + '/api/gherkin/example-ticks');
    assert.match(await template.text(), /tick_bid_ask_v1/);
    await page.locator('#gherkinArtifact').setInputFiles(fixture.artifact);
    await page.waitForFunction(() => document.getElementById('gherkinResult').textContent.includes('Artefacto cargado'));
    await page.locator('#gherkinManifest').fill(fixture.manifest);
    const requestPromise = page.waitForRequest(request => request.url().endsWith('/api/gherkin/start'));
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/gherkin/start'));
    await page.locator('#gherkinEvaluate').click();
    const payload = (await requestPromise).postDataJSON();
    assert.equal(payload.action, 'evaluate');
    assert.ok(!('model' in payload) && !('api_key' in payload));
    const response = await responsePromise;
    assert.equal(response.status(), 202, await response.text());
    try {
      await page.waitForFunction(() => document.getElementById('gherkinResult').textContent.includes('INSUFFICIENT_EVIDENCE'), null, { timeout: 30000 });
    } catch (error) {
      console.error('Panel:', await page.locator('#gherkinResult').textContent());
      console.error('Status:', await (await page.request.get(fixture.url + '/api/status')).text());
      console.error('Browser errors:', errors);
      throw error;
    }
    const result = JSON.parse(await page.locator('#gherkinResult').textContent());
    assert.equal(result.ai_calls_during_evaluation, 0);
    assert.equal(result.execution_model, 'tick_bid_ask_v1');
    assert.equal(result.holdout.status, 'NOT_CONSUMED');
    for (const name of ['validation_trades.json', 'validation_orders.json', 'validation_equity.json', 'research_audit.json']) {
      assert.equal(await page.locator(`#gherkinDownloads a[href$="/${name}"]`).count(), 1);
      assert.equal((await page.request.get(fixture.url + '/api/gherkin/download/' + name)).status(), 200);
    }
    assert.deepEqual(errors, []);
    console.log('PASS: browser artifact upload → offline tick evaluation → downloadable traces; no model/key in request.');
  } finally {
    if (browser) await browser.close();
    server.stdin.end();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
