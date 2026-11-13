'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const output = process.env.BROWSER_TEST_OUTPUT || path.join(root, '.deployment', 'sub2api-browser-check');
const python = process.env.PYTHON_EXE || (process.platform === 'win32' ? path.join(root, '.venv', 'Scripts', 'python.exe') : 'python3');
const fixture = spawn(python, [path.join(__dirname, 'sub2api_browser_fixture.py')], { cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
let browser, stderr = '';
fixture.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-3000); });
const configPromise = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Fixture startup timed out')), 20000);
  readline.createInterface({ input: fixture.stdout }).once('line', line => { clearTimeout(timer); try { resolve(JSON.parse(line)); } catch (e) { reject(e); } });
  fixture.once('error', reject);
  fixture.once('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited: ' + code)); });
});
async function text(page, selector, needle) {
  await page.waitForFunction(([selector, needle]) => document.querySelector(selector)?.textContent.includes(needle), [selector, needle], { timeout: 15000 });
}
async function main() {
  const config = await configPromise;
  fs.mkdirSync(output, { recursive: true });
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE_PATH ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const adminContext = await browser.newContext({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  const attach = page => page.on('pageerror', error => errors.push(error.message));
  let buyer = await context.newPage(); attach(buyer);
  const admin = await adminContext.newPage(); attach(admin);
  await admin.goto(config.url + '/admin');
  await admin.locator('#admin-password').fill(config.password);
  await admin.locator('#login-form button').click();
  await admin.locator('#admin-view').waitFor({ state: 'visible' });
  async function confirm(id) {
    await admin.locator('[data-tab="orders"]').click();
    await admin.locator('#order-query').fill(id);
    await admin.locator('#order-search button').click();
    const card = admin.locator('[data-order-id="' + id + '"]');
    await card.getByRole('button', { name: '发卡', exact: true }).click();
    await card.getByText('已发卡', { exact: true }).waitFor();
  }
  async function buy(name, quantity, contact) {
    await buyer.locator('.product-card').filter({ has: buyer.getByRole('heading', { name, exact: true }) }).getByRole('button', { name: '购买', exact: true }).click();
    await buyer.locator('#buy-quantity').fill(String(quantity));
    if (contact) await buyer.locator('#buyer-contact').fill(contact);
    await buyer.locator('#create-order').click();
    await buyer.locator('#payment-box').waitFor({ state: 'visible' });
    return buyer.locator('#order-id').textContent();
  }
  await buyer.goto(config.url + '/?user_id=1&token=token-user-one&theme=dark');
  await text(buyer, '#sub2api-message', 'one@example.com');
  assert.equal(new URL(buyer.url()).searchParams.has('token'), false);
  const stored = await buyer.evaluate(() => JSON.stringify({ ...sessionStorage }));
  assert.ok(!stored.includes('token-user-one'));
  const id = await buy('余额充值卡', 2);
  await buyer.close();
  await admin.locator('[data-tab="settings"]').click();
  await admin.locator('#sub2api-enabled').uncheck();
  await admin.locator('#sub2api-save').click();
  await text(admin, '#sub2api-message', '配置已保存');
  assert.equal((await (await admin.request.get(config.url + '/api/products')).json()).sub2api_enabled, false);
  await confirm(id);
  const pausedOrder = (await (await admin.request.get(config.url + '/api/admin/orders?q=' + id)).json()).orders.find(o => o.id === id);
  assert.equal(pausedOrder.redemption.status, 'pending');
  await admin.locator('[data-tab="settings"]').click();
  await admin.locator('#sub2api-enabled').check();
  await admin.locator('#sub2api-save').click();
  await text(admin, '#sub2api-message', '配置已保存');
  console.log('PASS admin disable preserves delivered tasks and enabling resumes worker without restart');
  buyer = await context.newPage(); attach(buyer);
  await buyer.goto(config.url + '/?user_id=1&token=token-user-one');
  await text(buyer, '#sub2api-message', 'one@example.com');
  await buyer.locator('#my-orders').click();
  await text(buyer, '#lookup-results', '已兑换到账');
  assert.ok((await buyer.locator('#lookup-results').textContent()).includes('CODE-1\nCODE-2'));
  assert.ok((await buyer.locator('#lookup-results').textContent()).includes('ID 1'));
  assert.equal(await buyer.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await buyer.screenshot({ path: path.join(output, 'redeemed-mobile.png'), fullPage: true });
  console.log('PASS close buyer page, background multi-card redemption, token cleanup, mine and mobile layout');

  await buyer.goto(config.url + '/?user_id=2&token=token-user-two');
  await text(buyer, '#sub2api-message', 'two@example.com');
  await buyer.locator('#my-orders').click();
  await text(buyer, '#lookup-results', '没有找到订单');
  assert.ok(!(await buyer.locator('#lookup-results').textContent()).includes('CODE-1'));
  console.log('PASS switching accounts cannot see earlier account orders');

  await buyer.goto(config.url + '/?user_id=999&token=token-user-one');
  await text(buyer, '#sub2api-message', '不一致');
  await buyer.locator('#sub2api-guest').click();
  const guest = await buy('余额充值卡', 1, 'guest@example.com');
  await confirm(guest);
  await text(buyer, '#card-code', 'CODE-3');
  assert.equal(await buyer.locator('#order-redemption').textContent(), '');
  await buyer.locator('#close-checkout').click();
  await buyer.locator('#open-lookup').click();
  await buyer.locator('#lookup-contact').fill('guest@example.com');
  await buyer.locator('#lookup-submit').click();
  await text(buyer, '#lookup-results', 'CODE-3');
  console.log('PASS invalid identity explicit guest fallback and unchanged contact recovery');

  await buyer.goto(config.url + '/?user_id=1&token=token-user-one');
  await text(buyer, '#sub2api-message', 'one@example.com');
  const partial = await buy('异常测试卡', 2);
  await confirm(partial);
  await text(buyer, '#order-redemption', '部分已兑换');
  await text(buyer, '#order-redemption', '兑换失败');
  assert.equal(await buyer.locator('#card-code').textContent(), 'FAIL-ONE\nFAIL-TWO');
  assert.equal(await buyer.locator('#order-redemption button').count(), 1);
  await buyer.screenshot({ path: path.join(output, 'partial-mobile.png'), fullPage: true });
  await admin.locator('#refresh-admin').click();
  await text(admin, '#all-orders', '部分已兑换');
  await admin.screenshot({ path: path.join(output, 'admin-redemption-desktop.png'), fullPage: true });
  console.log('PASS partial redemption keeps codes, individual status and retry actions');

  await admin.locator('[data-tab="settings"]').click();
  await text(admin, '#sub2api-diagnostics', '自动兑换已启用');
  await text(admin, '#sub2api-diagnostics', '后台已应用当前配置');
  await admin.screenshot({ path: path.join(output, 'admin-settings-desktop.png'), fullPage: true });
  await admin.setViewportSize({ width: 390, height: 844 });
  assert.equal(await admin.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await admin.screenshot({ path: path.join(output, 'admin-settings-mobile.png'), fullPage: true });
  await admin.reload();
  await admin.locator('[data-tab="settings"]').click();
  await text(admin, '#sub2api-diagnostics', '自动兑换已启用');
  assert.equal(await admin.locator('#sub2api-url').inputValue(), 'https://sub2api.example.com');
  // Route a synthetic HTTPS shop to the local fixture to avoid mixed-content /
  // private-network restrictions unrelated to the production HTTPS embed.
  await context.route('https://pay.example.com/**', async route => {
    const url = new URL(route.request().url());
    const response = await route.fetch({ url: config.url + url.pathname + url.search });
    await route.fulfill({ response });
  });
  await context.route('https://sub2api.example.com/**', route => route.fulfill({
    contentType: 'text/html', body: '<!doctype html><iframe title="shop" style="width:100%;height:800px" src="https://pay.example.com/?user_id=1&token=token-user-one"></iframe>'
  }));
  const embedded = await context.newPage(); attach(embedded);
  await embedded.goto('https://sub2api.example.com/custom-page');
  const frame = embedded.frameLocator('iframe');
  await frame.locator('#sub2api-message').filter({ hasText: 'one@example.com' }).waitFor();
  await frame.locator('#my-orders').click();
  await frame.locator('#lookup-results').filter({ hasText: 'CODE-1' }).waitFor();
  console.log('PASS allowed sub2api iframe works without third-party login cookies');
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed: true, browser_errors: errors, scenarios: 8 }, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  fixture.stdin.end();
  const timer = setTimeout(() => fixture.kill(), 5000);
  fixture.once('exit', () => clearTimeout(timer));
});
