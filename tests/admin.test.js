'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'admin.js'), 'utf8');
const markup = fs.readFileSync(path.join(__dirname, '..', 'static', 'admin.html'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const camel = value => value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function order(id, status = 'pending', extra = {}) {
  return { id, status, product_name: '商品 A', contact: 'buyer@example.com', price_cents: 1000, unit_price_cents: 1000, quantity: 1,
    channel: 'alipay', created_at: 1800000000, expires_at: 1800001800,
    ...(status === 'paid' ? { card_code: 'CARD-' + id } : {}), ...extra };
}

// Minimal DOM adapter for the actual HTML and script; business behavior stays in admin.js.
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = new Map(); this.attributes = {};
    this.dataset = {}; this.className = ''; this._value = ''; this.hidden = false;
    this.disabled = false; this.checked = false; this.open = false;
    this.classList = { toggle: (name, active) => {
      const names = new Set(this.className.split(/\s+/).filter(Boolean));
      if (active) names.add(name); else names.delete(name); this.className = [...names].join(' ');
    } };
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get textContent() { return this.children.map(child => child instanceof Element ? child.textContent : String(child)).join(''); }
  set textContent(value) { this.children = [String(value)]; }
  get childElementCount() { return this.children.filter(child => child instanceof Element).length; }
  append(...children) { this.children.push(...children.flat().filter(child => child !== null && child !== undefined)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[camel(name.slice(5))] = String(value);
    if (name === 'class') this.className = String(value);
    if (['id', 'type', 'name', 'src', 'value'].includes(name)) this[name] = String(value);
    if (['hidden', 'disabled', 'checked', 'open'].includes(name)) this[name] = true;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (['hidden', 'disabled', 'checked', 'open'].includes(name)) this[name] = false;
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  dispatch(name, fields = {}) {
    return Promise.all((this.listeners.get(name) || []).map(callback => callback({ preventDefault() {}, ...fields })));
  }
  matches(selector) {
    if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
    const match = /^(\w+)?(?:\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\])?$/.exec(selector);
    if (!match) throw new Error('Unsupported selector: ' + selector);
    if (match[1] && this.tag !== match[1]) return false;
    if (!match[2]) return true;
    if (match[2] === 'open') return this.open;
    if (!(match[2] in this.attributes)) return false;
    return match[3] === undefined || this.attributes[match[2]] === match[3];
  }
  querySelectorAll(selector) {
    const result = [];
    const visit = node => { for (const child of node.children) if (child instanceof Element) {
      if (child.matches(selector)) result.push(child); visit(child);
    } };
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  reset() {
    for (const tag of ['input', 'textarea', 'select']) for (const input of this.querySelectorAll(tag)) {
      input.value = input.attributes.value || ''; input.checked = 'checked' in input.attributes;
    }
  }
  showModal() { assert.equal(this.open, false, 'Dialog already open'); this.open = true; }
  close() { if (!this.open) return; this.open = false; this.dispatch('close'); }
}
function parseMarkup() {
  const root = new Element('document'), nodes = new Map(), stack = [root];
  const voidTags = new Set(['meta', 'link', 'input', 'img', 'br', 'hr']);
  for (const token of markup.match(/<[^>]+>|[^<]+/g)) {
    if (token.startsWith('<!')) continue;
    if (token.startsWith('</')) { stack.pop(); continue; }
    if (!token.startsWith('<')) { stack.at(-1).append(token); continue; }
    const tag = /^<([a-z0-9]+)/i.exec(token)[1], node = new Element(tag);
    const attributes = token.slice(tag.length + 1, -1);
    for (const match of attributes.matchAll(/([^\s=/>]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g)) {
      node.setAttribute(match[1], match[2] ?? match[3] ?? match[4] ?? '');
    }
    stack.at(-1).append(node); if (node.id) nodes.set(node.id, node);
    if (!voidTags.has(tag)) stack.push(node);
  }
  return { root, nodes };
}
async function harness(respond = () => undefined, options = {}) {
  const { root, nodes } = parseMarkup();
  const node = id => { assert.ok(nodes.has(id), 'Unknown HTML id: ' + id); return nodes.get(id); };
  const calls = [], copied = [], confirmations = [], timers = new Map(); let nextTimer = 0;
  const server = {
    orders: options.orders || [order('A')],
    products: [{ id: 1, name: '商品 A', description: '', price_cents: 1000, stock: 2, active: true },
      { id: 2, name: '商品 B', description: '', price_cents: 2000, stock: 2, active: true }],
    cards: [{ id: 1, product_id: 1, code: 'CARD-A', status: 'available' },
      { id: 2, product_id: 1, code: 'CARD-B', status: 'available' }],
    settings: { shop_name: 'Saved name', support_notice: '' },
    sub2api: { base_url: 'https://sub2api.example.com', enabled: false, relay_confirmed: false,
      revision: 1, key_configured: true, worker_seen: Date.now() / 1000, worker_revision: 1, paused: false, message: '' }
  };
  const el = (tag, attributes = {}, ...children) => {
    const element = new Element(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (key === 'text') element.textContent = value;
      else if (key === 'className') element.className = value;
      else if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2), value);
      else if (value !== undefined && value !== null && value !== false) element.setAttribute(key, value === true ? '' : value);
    }
    element.append(...children); return element;
  };
  const api = async (url, request = {}) => {
    calls.push({ url, request });
    const result = await respond(url, request, server);
    if (result !== undefined) return result;
    if (url === '/api/admin/session') return { authenticated: true, csrf_token: 'session-A' };
    if (url === '/api/admin/login') return { csrf_token: 'session-B' };
    if (url === '/api/admin/logout') return { ok: true };
    if (url.startsWith('/api/admin/orders?')) return { orders: server.orders, total: server.orders.length, has_more: false };
    if (url === '/api/admin/products') return { products: server.products };
    if (url === '/api/admin/overview') return { settings: server.settings, channels: [], sub2api: server.sub2api };
    if (url === '/api/admin/sub2api' && !request.method) return { sub2api: { ...server.sub2api } };
    if (/\/api\/admin\/products\/\d+\/cards\?/.test(url)) return { cards: server.cards, total: server.cards.length, has_more: false };
    throw new Error('Unexpected API request: ' + url);
  };
  const setBusy = (button, busy, text) => {
    assert.ok(button, 'Button must exist in real markup');
    if (busy) { button.dataset.label = button.textContent; button.textContent = text || '处理中…'; button.disabled = true; }
    else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
  };
  const message = (target, text, type = 'info') => { target.textContent = text || ''; target.messageType = type; target.hidden = !text; };
  vm.runInNewContext(source, {
    App: { el, api, money: String, date: String, channel: String, message, copy: async value => copied.push(value),
      badge: (value, label) => el('span', { text: label || value }), setBusy },
    document: { getElementById: node, querySelectorAll: selector => root.querySelectorAll(selector), hidden: false, addEventListener() {} },
    URLSearchParams, FormData, Date, confirm: text => { confirmations.push(text); return options.confirm !== false; },
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id)
  }, { filename: 'static/admin.js' });
  await flush();
  const button = (parent, text) => { const result = parent.querySelectorAll('button').find(item => item.textContent === text); assert.ok(result, 'Missing button: ' + text); return result; };
  const product = id => node('admin-products').querySelectorAll('article').find(item => item.querySelector('h3').textContent === server.products.find(p => p.id === id).name);
  const card = id => node('inventory-cards').querySelectorAll('[data-card-id]').find(item => item.dataset.cardId === String(id));
  const tab = name => root.querySelectorAll('[data-tab]').find(item => item.dataset.tab === name).dispatch('click');
  const submit = id => node(id).dispatch('submit', { submitter: node(id).querySelector('[type=submit]') });
  return { node, server, calls, timers, copied, confirmations, button, product, card, tab, submit,
    editProduct: id => button(product(id), '编辑').dispatch('click'),
    openInventory: id => button(product(id), '库存').dispatch('click'),
    editCard: id => button(card(id), '修改').dispatch('click'),
    runRefresh: async () => { const entry = [...timers].find(([, item]) => item.delay === 8000); assert.ok(entry, 'Order refresh must remain scheduled'); timers.delete(entry[0]); await entry[1].callback(); await flush(); }
  };
}

test('settings entered during save survive the response and a later settings refresh', async () => {
  const pending = deferred();
  const ui = await harness((url, request) => url === '/api/admin/settings' ? pending.promise : undefined);
  await ui.tab('settings'); ui.node('shop-name').value = 'Submitted name'; await ui.node('settings-form').dispatch('input');
  const saving = ui.submit('settings-form');
  ui.node('shop-name').value = 'New unsaved draft'; await ui.node('settings-form').dispatch('input');
  ui.server.settings.shop_name = 'Submitted name'; pending.resolve({ settings: ui.server.settings }); await saving;
  await ui.tab('orders'); await ui.tab('settings');
  assert.equal(ui.node('shop-name').value, 'New unsaved draft');
  assert.match(ui.node('admin-message').textContent, /未保存/);
  assert.equal(ui.calls.find(call => call.url === '/api/admin/settings').request.body.shop_name, 'Submitted name');
});

test('late save for product A neither closes nor unlocks a newer product B editor', async () => {
  const a = deferred(), b = deferred();
  const ui = await harness((url, request) => request.method === 'PATCH' ? (url.endsWith('/1') ? a.promise : b.promise) : undefined);
  await ui.editProduct(1); ui.node('product-name').value = 'A saved'; const saveA = ui.submit('product-form');
  ui.node('product-dialog').close(); await ui.editProduct(2); ui.node('product-name').value = 'B draft'; const saveB = ui.submit('product-form');
  a.resolve({ product: { id: 1 } }); await saveA;
  assert.equal(ui.node('product-dialog').open, true);
  assert.equal(ui.node('product-id').value, '2'); assert.equal(ui.node('product-name').value, 'B draft');
  assert.equal(ui.node('product-form').querySelector('[type=submit]').disabled, true);
  b.resolve({ product: { id: 2 } }); await saveB; assert.equal(ui.node('product-dialog').open, false);
});

test('late save for card A neither closes nor unlocks a newer card B editor', async () => {
  const a = deferred(), b = deferred();
  const ui = await harness((url, request) => request.method === 'PATCH' ? (url.endsWith('/1') ? a.promise : b.promise) : undefined);
  await ui.openInventory(1); await ui.editCard(1); ui.node('edit-card-code').value = 'A saved'; const saveA = ui.submit('edit-card-form');
  ui.node('edit-card-dialog').close(); await ui.editCard(2); ui.node('edit-card-code').value = 'B draft'; const saveB = ui.submit('edit-card-form');
  a.resolve({ card: { id: 1 } }); await saveA;
  assert.equal(ui.node('edit-card-dialog').open, true);
  assert.equal(ui.node('edit-card-id').value, '2'); assert.equal(ui.node('edit-card-code').value, 'B draft');
  assert.equal(ui.node('edit-card-form').querySelector('[type=submit]').disabled, true);
  b.resolve({ card: { id: 2 } }); await saveB; assert.equal(ui.node('edit-card-dialog').open, false);
});

for (const failed of ['orders', 'products', 'overview']) test('initial ' + failed + ' failure retains login and automatic order refresh', async () => {
  let failedOnce = false;
  const ui = await harness(url => {
    if (!failedOnce && (failed === 'orders' ? url.startsWith('/api/admin/orders?') : url === '/api/admin/' + failed)) {
      failedOnce = true; throw Object.assign(new Error('temporary unavailable'), { status: 503 });
    }
  });
  assert.equal(failedOnce, true); assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  await ui.runRefresh();
  assert.equal(ui.node('all-orders').querySelectorAll('[data-order-id]').length, 1);
  const last = ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).at(-1);
  assert.equal(last.request.headers['X-CSRF-Token'], 'session-A'); assert.ok(ui.timers.size > 0);
});

test('repeated confirmation clicks submit once and replace the delivered order', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/orders/A/confirm' ? pending.promise : undefined);
  const firstButton = ui.button(ui.node('all-orders'), '发卡'); const first = firstButton.dispatch('click');
  const currentButton = ui.button(ui.node('all-orders'), '发卡'); assert.equal(currentButton.disabled, true);
  await firstButton.dispatch('click'); await currentButton.dispatch('click'); await flush();
  assert.equal(ui.calls.filter(call => call.url === '/api/admin/orders/A/confirm').length, 1);
  ui.server.orders = [order('A', 'paid')]; pending.resolve({ order: ui.server.orders[0] }); await first;
  assert.equal(ui.node('all-orders').querySelectorAll('button').filter(item => item.textContent === '发卡').length, 0);
  assert.match(ui.node('all-orders').textContent, /CARD-A/);
});

test('a stale initial session response cannot clear a successful explicit login', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/session' ? pending.promise : undefined);
  ui.node('admin-password').value = 'temporary-password'; await ui.submit('login-form');
  assert.equal(ui.node('admin-view').hidden, false);
  pending.resolve({ authenticated: false }); await flush();
  assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  await ui.runRefresh();
  assert.equal(ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).at(-1).request.headers['X-CSRF-Token'], 'session-B');
});

test('automatic refresh keeps an expanded delivered card visible', async () => {
  const ui = await harness(() => undefined, { orders: [order('A', 'paid')] });
  ui.node('all-orders').querySelector('details').open = true; await ui.runRefresh();
  assert.equal(ui.node('all-orders').querySelector('details').open, true);
  assert.match(ui.node('all-orders').querySelector('pre').textContent, /CARD-A/);
});

test('late import response cannot erase a draft after reopening the same inventory', async () => {
  const pending = deferred();
  const ui = await harness((url, request) => url === '/api/admin/products/1/cards' && request.method === 'POST' ? pending.promise : undefined);
  await ui.openInventory(1); ui.node('card-codes').value = 'IMPORT-A'; const importing = ui.submit('cards-form');
  ui.node('inventory-dialog').close(); await ui.openInventory(1); ui.node('card-codes').value = 'NEW-DRAFT';
  pending.resolve({ inserted: 1, duplicates: 0 }); await importing;
  assert.equal(ui.node('card-codes').value, 'NEW-DRAFT'); assert.equal(ui.node('inventory-dialog').open, true);
});


test('expired multi-card orders can be cancelled once and report released inventory', async () => {
  const pending = deferred(), expired = order('EXPIRED', 'expired', { quantity: 3, price_cents: 3000 });
  const ui = await harness(url => url === '/api/admin/orders/EXPIRED/cancel' ? pending.promise : undefined, { orders: [expired] });
  const original = ui.button(ui.node('all-orders'), '取消订单'), cancelling = original.dispatch('click');
  assert.match(ui.confirmations[0], /未收到付款/); assert.match(ui.confirmations[0], /全部预留库存/);
  assert.equal(ui.button(ui.node('all-orders'), '取消订单').disabled, true);
  assert.equal(ui.button(ui.node('all-orders'), '发卡').disabled, true);
  await original.dispatch('click'); await flush();
  assert.equal(ui.calls.filter(call => call.url === '/api/admin/orders/EXPIRED/cancel').length, 1);
  ui.server.orders = [{ ...expired, status: 'cancelled' }]; ui.server.products[0].stock = 3;
  pending.resolve({ order: ui.server.orders[0] }); await cancelling;
  assert.equal(ui.node('all-orders').querySelectorAll('button').filter(item => ['发卡', '取消订单'].includes(item.textContent)).length, 0);
  assert.equal(ui.node('admin-message').textContent, '订单已取消，库存已释放');
  assert.match(ui.product(1).textContent, /可用 3 张/);
});

test('multi-card orders show quantity, unit price and total and copy every card from card_codes', async () => {
  const codes = ['CARD-ALPHA', 'CARD-BETA', 'CARD-GAMMA'];
  const paid = order('MULTI', 'paid', { quantity: 3, unit_price_cents: 350, price_cents: 1050,
    card_codes: codes, card_code: 'STALE-LEGACY-FIELD' });
  const ui = await harness(() => undefined, { orders: [paid] });
  const view = ui.node('all-orders').querySelector('[data-order-id]');
  const fields = new Map(view.querySelector('dl').children.filter(item => item instanceof Element)
    .map(item => [item.querySelector('dt').textContent, item.querySelector('dd').textContent]));
  assert.equal(fields.get('数量 / 单价'), '3 张 · 350');
  assert.equal(fields.get('应付金额 / 支付'), '1050 · alipay');
  assert.equal(view.querySelector('summary').textContent, '查看卡密（3 张）');
  assert.equal(view.querySelector('pre').textContent, codes.join('\n'));
  assert.equal(view.textContent.includes('STALE-LEGACY-FIELD'), false);
  await ui.button(view, '复制全部').dispatch('click'); assert.deepEqual(ui.copied, [codes.join('\n')]);
  assert.equal(view.querySelectorAll('button').some(item => item.textContent === '取消订单'), false);
});

test('legacy paid orders without quantity or card_codes still show and copy one card', async () => {
  const paid = order('LEGACY', 'paid'); delete paid.quantity; delete paid.unit_price_cents;
  const ui = await harness(() => undefined, { orders: [paid] });
  const view = ui.node('all-orders').querySelector('[data-order-id]');
  assert.match(view.textContent, /1 张 · 1000/); assert.equal(view.querySelector('summary').textContent, '查看卡密');
  await ui.button(view, '复制').dispatch('click'); assert.deepEqual(ui.copied, ['CARD-LEGACY']);
});

function fillPassword(ui, current = 'old-test-password', replacement = 'new-test-password', confirmation = replacement) {
  ui.node('current-password').value = current;
  ui.node('new-password').value = replacement;
  ui.node('confirm-password').value = confirmation;
}
function assertPasswordsCleared(ui) {
  for (const id of ['current-password', 'new-password', 'confirm-password']) assert.equal(ui.node(id).value, '', id);
}

test('password settings use masked fields and standard password autocomplete', async () => {
  const ui = await harness();
  for (const id of ['current-password', 'new-password', 'confirm-password']) {
    assert.equal(ui.node(id).type, 'password');
    assert.equal(ui.node(id).attributes.maxlength, '256');
  }
  assert.equal(ui.node('current-password').attributes.autocomplete, 'current-password');
  for (const id of ['new-password', 'confirm-password']) {
    assert.equal(ui.node(id).attributes.autocomplete, 'new-password');
    assert.equal(ui.node(id).attributes.minlength, '12');
  }
});

test('password change sends exact fields with CSRF and logs out with all passwords cleared', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/password' ? pending.promise : undefined);
  fillPassword(ui, ' old-test-password ', ' new-test-password ');
  ui.node('admin-password').value = 'unused-login-draft';
  const change = ui.submit('password-form'); await flush();
  const request = ui.calls.find(call => call.url === '/api/admin/password').request;
  assert.equal(request.method, 'POST');
  assert.equal(request.headers['X-CSRF-Token'], 'session-A');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), { current_password: ' old-test-password ', new_password: ' new-test-password ', confirm_password: ' new-test-password ' });
  assert.equal(ui.node('change-password').disabled, true);
  pending.resolve({ ok: true }); await change;
  assertPasswordsCleared(ui); assert.equal(ui.node('admin-password').value, '');
  assert.equal(ui.node('admin-view').hidden, true); assert.equal(ui.node('login-view').hidden, false);
  assert.equal(ui.node('login-message').textContent, '密码已更新，请重新登录');
  assert.equal(ui.node('login-message').messageType, 'success');
  assert.equal(ui.node('change-password').disabled, false); assert.equal(ui.timers.size, 0);
  assert.deepEqual(ui.copied, []);
});

test('invalid password lengths and mismatched confirmation never submit', async () => {
  const ui = await harness(url => { if (url === '/api/admin/password') assert.fail('Invalid password form reached server'); });
  for (const [current, replacement, confirmation] of [
    ['', 'new-test-password', 'new-test-password'],
    ['x'.repeat(257), 'new-test-password', 'new-test-password'],
    ['old-test-password', 'short', 'short'],
    ['old-test-password', 'x'.repeat(257), 'x'.repeat(257)],
    ['old-test-password', ' '.repeat(12), ' '.repeat(12)],
    ['old-test-password', 'new-test-password', 'different-test-password']
  ]) {
    fillPassword(ui, current, replacement, confirmation); await ui.submit('password-form');
    assert.equal(ui.node('password-message').hidden, false);
    assert.equal(ui.node('change-password').disabled, false);
  }
  assert.equal(ui.calls.filter(call => call.url === '/api/admin/password').length, 0);
  assert.equal(ui.node('password-message').textContent, '两次密码不一致');
});

test('a rejected password retains the session and never echoes error content containing secrets', async () => {
  const ui = await harness(url => { if (url === '/api/admin/password') throw Object.assign(new Error('old-test-password new-test-password'), { status: 400 }); });
  fillPassword(ui); await ui.submit('password-form');
  assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  assert.equal(ui.node('change-password').disabled, false);
  assert.equal(ui.node('password-message').textContent, '修改失败，请核对当前密码后重试');
  assert.equal(ui.node('password-message').textContent.includes('test-password'), false);
  assert.equal(ui.node('admin-message').textContent.includes('test-password'), false);
  await ui.runRefresh();
  assert.equal(ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).at(-1).request.headers['X-CSRF-Token'], 'session-A');
});

test('pending password change blocks duplicate submissions and briefly pauses automatic orders refresh', async () => {
  const pending = deferred(); const ui = await harness(url => url === '/api/admin/password' ? pending.promise : undefined);
  fillPassword(ui); const change = ui.submit('password-form'); await ui.submit('password-form');
  const before = ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).length;
  await ui.runRefresh();
  assert.equal(ui.calls.filter(call => call.url === '/api/admin/password').length, 1);
  assert.equal(ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).length, before);
  pending.reject(Object.assign(new Error('temporary unavailable'), { status: 503 })); await change;
  assert.equal(ui.node('change-password').disabled, false);
  await ui.runRefresh(); assert.equal(ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).length, before + 1);
});

test('logout immediately clears password drafts before the server responds', async () => {
  const pending = deferred(); const ui = await harness(url => url === '/api/admin/logout' ? pending.promise : undefined);
  fillPassword(ui); const logout = ui.node('logout').dispatch('click');
  assertPasswordsCleared(ui);
  pending.resolve({ ok: true }); await logout;
  assert.equal(ui.node('login-view').hidden, false);
});

test('session expiry clears password drafts and prevents an unauthenticated change', async () => {
  let expire = false;
  const ui = await harness(url => { if (expire && url.startsWith('/api/admin/orders?')) throw Object.assign(new Error('Session expired'), { status: 401 }); });
  fillPassword(ui); expire = true; await ui.node('refresh-admin').dispatch('click');
  assertPasswordsCleared(ui); assert.equal(ui.node('login-view').hidden, false);
  fillPassword(ui); await ui.submit('password-form');
  assert.equal(ui.calls.filter(call => call.url === '/api/admin/password').length, 0);
});

test('late password success cannot clear or unlock a newer password request after relogin', async () => {
  const old = deferred(), latest = deferred(); let count = 0;
  const ui = await harness(url => url === '/api/admin/password' ? (++count === 1 ? old.promise : latest.promise) : undefined);
  fillPassword(ui); const first = ui.submit('password-form');
  await ui.node('logout').dispatch('click'); ui.node('admin-password').value = 'login-test-password'; await ui.submit('login-form');
  fillPassword(ui, 'different-current-password', 'different-new-password'); const second = ui.submit('password-form');
  old.resolve({ ok: true }); await first;
  assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  assert.equal(ui.node('new-password').value, 'different-new-password');
  assert.equal(ui.node('change-password').disabled, true);
  latest.resolve({ ok: true }); await second;
  assertPasswordsCleared(ui); assert.equal(ui.node('login-view').hidden, false);
});

for (const status of [400, 401]) test('late password ' + status + ' response cannot log out a newer authentication generation', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/password' ? pending.promise : url === '/api/admin/login' ? { csrf_token: 'session-A' } : undefined);
  fillPassword(ui); const change = ui.submit('password-form');
  await ui.node('logout').dispatch('click'); ui.node('admin-password').value = 'login-test-password'; await ui.submit('login-form');
  fillPassword(ui, 'fresh-current-password', 'fresh-new-password');
  pending.reject(Object.assign(new Error('old-test-password new-test-password'), { status })); await change;
  assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  assert.equal(ui.node('new-password').value, 'fresh-new-password');
  assert.equal(ui.node('password-message').textContent, '');
  assert.equal(ui.node('change-password').disabled, false);
  await ui.runRefresh();
  assert.equal(ui.calls.filter(call => call.url.startsWith('/api/admin/orders?')).at(-1).request.headers['X-CSRF-Token'], 'session-A');
});

test('ordinary settings saves cannot include values from the separate password form', async () => {
  const ui = await harness(url => url === '/api/admin/settings' ? { ok: true } : undefined);
  fillPassword(ui); ui.node('shop-name').value = 'Another shop'; ui.node('support-notice').value = 'Open';
  await ui.submit('settings-form');
  const request = ui.calls.find(call => call.url === '/api/admin/settings').request;
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), { shop_name: 'Another shop', support_notice: 'Open' });
  assert.equal(JSON.stringify(request.body).includes('password'), false);
  assert.equal(ui.node('new-password').value, 'new-test-password');
});

test('a late logout response does not clear a newer login or its password drafts', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/logout' ? pending.promise : undefined);
  const logout = ui.node('logout').dispatch('click');
  ui.node('admin-password').value = 'login-test-password'; await ui.submit('login-form');
  fillPassword(ui, 'fresh-current-password', 'fresh-new-password');
  pending.resolve({ ok: true }); await logout;
  assert.equal(ui.node('admin-view').hidden, false); assert.equal(ui.node('login-view').hidden, true);
  assert.equal(ui.node('new-password').value, 'fresh-new-password');
  assert.equal(ui.node('logout').disabled, false);
});




test('integration saves dedicated configuration with CSRF and preserves edits during save and refresh', async () => {
  const pending = deferred();
  const ui = await harness((url, request) => url === '/api/admin/sub2api' && request.method === 'POST' ? pending.promise : undefined);
  await ui.tab('settings');
  ui.node('sub2api-enabled').checked = true; ui.node('sub2api-relay').checked = true;
  await ui.node('sub2api-form').dispatch('input');
  const saving = ui.submit('sub2api-form'); await ui.submit('sub2api-form');
  const requests = ui.calls.filter(c => c.url === '/api/admin/sub2api' && c.request.method === 'POST');
  assert.equal(requests.length, 1); assert.equal(requests[0].request.headers['X-CSRF-Token'], 'session-A');
  assert.equal(requests[0].request.body.revision, 1); assert.equal(requests[0].request.body.enabled, true);
  assert.equal(Object.keys(requests[0].request.body).sort().join(','), 'base_url,enabled,relay_confirmed,revision');
  ui.node('sub2api-url').value = 'https://draft.example.com';
  await ui.node('sub2api-form').dispatch('input', { target: ui.node('sub2api-url') });
  assert.equal(ui.node('sub2api-relay').checked, false);
  ui.server.sub2api = { ...ui.server.sub2api, enabled: true, relay_confirmed: true, revision: 2 };
  pending.resolve({ sub2api: ui.server.sub2api }); await saving; await ui.runRefresh();
  assert.equal(ui.node('sub2api-url').value, 'https://draft.example.com');
  assert.match(ui.node('sub2api-message').textContent, /未保存/);
  await ui.node('sub2api-reload').dispatch('click');
  assert.equal(ui.node('sub2api-url').value, 'https://sub2api.example.com');
  assert.equal(ui.node('sub2api-relay').checked, true);
});

test('integration conflict preserves original revision until explicit reload', async () => {
  const ui = await harness((url, request) => {
    if (url === '/api/admin/sub2api' && request.method === 'POST') throw Object.assign(new Error('配置已被其他页面修改，请重新加载'), { status: 409 });
  });
  ui.node('sub2api-enabled').checked = true; await ui.node('sub2api-form').dispatch('input');
  ui.server.sub2api.revision = 2;
  await ui.submit('sub2api-form'); await ui.tab('settings');
  assert.equal(ui.node('sub2api-enabled').checked, true);
  assert.match(ui.node('sub2api-message').textContent, /重新加载/);
  await ui.submit('sub2api-form');
  assert.equal(ui.calls.filter(c => c.url === '/api/admin/sub2api' && c.request.method === 'POST').at(-1).request.body.revision, 1);
  await ui.node('sub2api-reload').dispatch('click'); await ui.submit('sub2api-form');
  assert.equal(ui.calls.filter(c => c.url === '/api/admin/sub2api' && c.request.method === 'POST').at(-1).request.body.revision, 2);
});

test('connection check ignores late results for an edited address and never saves', async () => {
  const pending = deferred();
  const ui = await harness(url => url === '/api/admin/sub2api/check' ? pending.promise : undefined);
  const checking = ui.node('sub2api-check').dispatch('click');
  ui.node('sub2api-url').value = 'https://changed.example.com';
  await ui.node('sub2api-form').dispatch('input', { target: ui.node('sub2api-url') });
  pending.resolve({ site_name: 'Old site', message: 'Connection succeeded' }); await checking;
  assert.doesNotMatch(ui.node('sub2api-message').textContent, /Old site/);
  assert.equal(ui.calls.filter(c => c.url === '/api/admin/sub2api' && c.request.method === 'POST').length, 0);
});

test('late integration save cannot overwrite a new login or draft', async () => {
  const pending = deferred();
  const ui = await harness((url, request) => url === '/api/admin/sub2api' && request.method === 'POST' ? pending.promise : undefined);
  const saving = ui.submit('sub2api-form');
  await ui.node('logout').dispatch('click'); await ui.submit('login-form');
  ui.node('sub2api-url').value = 'https://new-login.example.com'; await ui.node('sub2api-form').dispatch('input');
  pending.resolve({ sub2api: { ...ui.server.sub2api, base_url: 'https://old.example.com', revision: 2 } }); await saving;
  assert.equal(ui.node('sub2api-url').value, 'https://new-login.example.com');
  assert.equal(ui.node('admin-view').hidden, false);
  assert.match(ui.node('sub2api-message').textContent, /未保存/);
});
