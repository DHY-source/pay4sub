'use strict';
// Exercises the real shared helper and OS clipboard with cross-origin iframe policy.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/common.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/styles.css'), 'utf8');
const code = 'TEST-CARD-ONE\nTEST-CARD-TWO';
const html = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/style.css">
<button id="open">Open order</button><dialog id="order" class="modal"><pre id="code"></pre>
<button id="copy" type="button">复制卡密</button><p id="result"></p></dialog>
<script src="/common.js"></script><script>
document.querySelector('#code').textContent=${JSON.stringify(code)};
document.querySelector('#open').onclick=()=>document.querySelector('#order').showModal();
document.querySelector('#copy').onclick=async()=>{
  document.querySelector('#result').textContent='';
  try { await App.copy(document.querySelector('#code').textContent); document.querySelector('#result').textContent='已复制'; }
  catch(e) { document.querySelector('#result').textContent=e.message; }
};
</script>`;
let browser;
async function main() {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE_PATH ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH } : {}) });
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  const errors = [], checks = [];
  await context.route('https://clipboard-shop.test/**', route => {
    const name = new URL(route.request().url()).pathname;
    return route.fulfill({ contentType: name === '/common.js' ? 'text/javascript' : name === '/style.css' ? 'text/css' : 'text/html',
      body: name === '/common.js' ? source : name === '/style.css' ? css : html });
  });
  await context.route('https://clipboard-parent.test/**', route => route.fulfill({ contentType: 'text/html',
    body: '<!doctype html><iframe title="shop" src="https://clipboard-shop.test/" style="width:100%;height:700px"></iframe>' }));
  const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  await page.goto('https://clipboard-shop.test/');
  await page.locator('#open').click(); await page.locator('#copy').click();
  await page.locator('#result').filter({ hasText: '已复制' }).waitFor();
  assert.equal((await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n'), code);
  checks.push('normal secure-context copy preserves all multiline codes');

  // No allow=clipboard-write on the iframe: reproduce the actual menu restriction.
  const parent = await context.newPage(); parent.on('pageerror', e => errors.push(e.message));
  await parent.goto('https://clipboard-parent.test/');
  const frame = parent.frameLocator('iframe');
  await frame.locator('#open').click();
  assert.equal(await frame.locator('body').evaluate(() => document.featurePolicy.allowsFeature('clipboard-write')), false);
  await frame.locator('#copy').click();
  await frame.locator('#result').filter({ hasText: '已复制' }).waitFor();
  assert.equal((await parent.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n'), code);
  assert.equal(await frame.locator('.copy-buffer').count(), 0);
  assert.equal(await frame.locator('#copy').evaluate(node => document.activeElement === node), true);
  checks.push('cross-origin iframe without clipboard delegation copies inside a modal and restores focus');

  await page.bringToFront();
  await page.evaluate(() => {
    window.savedClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async () => { throw new DOMException('PRIVATE browser error', 'NotAllowedError'); }
    } });
    document.querySelector('#code').textContent = 'DENIED-API-FALLBACK\nSECOND-CARD';
  });
  await page.locator('#copy').click(); await page.locator('#result').filter({ hasText: '已复制' }).waitFor();
  assert.equal((await page.evaluate(() => savedClipboard.readText())).replace(/\r\n/g, '\n'), 'DENIED-API-FALLBACK\nSECOND-CARD');
  checks.push('rejected async clipboard falls back to real clipboard copy');

  await page.evaluate(() => { Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }); document.querySelector('#code').textContent = 'NO-ASYNC-API'; });
  await page.locator('#copy').click(); await page.locator('#result').filter({ hasText: '已复制' }).waitFor();
  assert.equal(await page.evaluate(() => savedClipboard.readText()), 'NO-ASYNC-API');
  checks.push('missing clipboard API still copies successfully');

  for (const mode of ['false', 'throw']) {
    await page.evaluate(mode => { document.execCommand = () => { if (mode === 'throw') throw new Error('PRIVATE command failure'); return false; }; }, mode);
    await page.locator('#copy').click();
    await page.locator('#result').filter({ hasText: '请长按或选中上方卡密复制' }).waitFor();
    assert.doesNotMatch(await page.locator('#result').textContent(), /PRIVATE|已复制/);
    assert.equal(await page.locator('.copy-buffer').count(), 0);
    assert.equal(await page.locator('#copy').evaluate(node => document.activeElement === node), true);
    assert.equal(await page.locator('#code').textContent(), 'NO-ASYNC-API');
  }
  checks.push('both copy failures show manual instructions, remove buffers, retain codes and never claim success');
  assert.deepEqual(errors, []);
  const output = path.join(root, '.deployment', 'clipboard-browser-check'); fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed: checks.length, checks, browser_errors: errors }, null, 2));
  console.log(JSON.stringify({ passed: checks.length, checks, browser_errors: errors }));
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); });
