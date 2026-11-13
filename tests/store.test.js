'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'store.js'), 'utf8');
const markup = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const initialTime = 1800000000000;

test('adjusted payable amount is used in checkout and lookup QR metadata', async () => {
  const adjusted = order('ADJUSTED', 'pending', { quantity: 2, unit_price_cents: 1000, base_price_cents: 2000, price_cents: 2007, payment_adjustment_cents: 7 });
  const ui = await harness(url => url === '/api/orders/lookup' ? { orders: [adjusted], total: 1, has_more: false } : { order: adjusted, token: 'token' });
  await ui.select(1); await ui.checkout();
  assert.equal(ui.node('pay-amount').textContent, '2007');
  assert.equal(ui.node('enlarge-payment-qr').dataset.qrAmount, '2007');
  assert.match(ui.node('payment-breakdown').textContent, /2000.*7/);
  await ui.lookup('buyer@example.com');
  const result = ui.node('lookup-results').querySelector('[data-order]');
  assert.equal(result.querySelector('strong').textContent, '2007');
  assert.equal(result.querySelector('[data-qr-amount]').dataset['qr-amount'], '2007');
});

test('full payment amount slots show retry message without activating an order', async () => {
  const ui = await harness(() => { throw Object.assign(new Error('当前金额的待付款订单较多，请稍后重新下单'), { code: 'payment_amount_busy', status: 409 }); });
  await ui.select(1); await ui.checkout();
  assert.match(ui.node('checkout-message').textContent, /请稍后重新下单/);
  assert.equal(ui.node('create-order').disabled, false);
  assert.equal(ui.node('order-view').hidden, true);
});
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function order(id, status = 'pending', extra = {}) {
  return { id, status, product_name: '测试商品', channel: 'alipay', price_cents: 1000, contact: 'buyer@example.com',
    created_at: initialTime / 1000, expires_at: initialTime / 1000 + 1800,
    card_code: status === 'paid' ? 'CARD-' + id : undefined, ...extra };
}
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = new Map(); this.dataset = {};
    this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.checked = false; this.open = false; this.attributes = {};
  }
  append(...children) { this.children.push(...children.flat().filter(child => child != null)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5)] = String(value);
    if (['name', 'type', 'value', 'src'].includes(name)) this[name] = String(value);
    if (['disabled', 'hidden'].includes(name)) this[name] = true;
  }
  querySelectorAll(selector) {
    const result = [];
    const matches = node => selector.startsWith('[data-') ? selector.slice(6, -1) in node.dataset
      : selector === 'input[name="channel"]:checked' ? node.tag === 'input' && node.name === 'channel' && node.checked
      : node.tag === selector;
    const walk = node => { for (const child of node.children) if (child instanceof Element) { if (matches(child)) result.push(child); walk(child); } };
    walk(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  dispatch(name, fields = {}) { return Promise.all((this.listeners.get(name) || []).map(callback => callback({ preventDefault() {}, ...fields }))); }
  showModal() { assert.equal(this.open, false, 'Cannot open an already open dialog'); this.open = true; }
  close() { this.open = false; this.dispatch('close'); }
}
async function harness(respond, options = {}) {
  const nodes = new Map([...markup.matchAll(/<([a-z0-9]+)[^>]*id="([^"]+)"[^>]*>/g)].map(match => {
    const node = new Element(match[1]); node.hidden = /\bhidden\b/.test(match[0]); return [match[2], node];
  }));
  const node = id => { assert.ok(nodes.has(id), 'Unknown HTML id ' + id); return nodes.get(id); };
  const calls = [], copied = [], storage = new Map(), timers = new Map(), documentEvents = new Map();
  let timerId = 0, clock = initialTime;
  if (options.saved) storage.set('cardshop.current-order', JSON.stringify(options.saved));
  const catalog = { shop_name: '测试商店', products: [
    { id: 1, name: '商品 A', stock: 3, price_cents: 1000, active: true },
    { id: 2, name: '商品 B', stock: 2, price_cents: 2000, active: true }
  ], channels: [{ id: 'alipay', enabled: true, label: '支付宝' }] };
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
  const api = (url, request = {}) => {
    calls.push({ url, options: request });
    if (url === '/api/products') return Promise.resolve(catalog);
    return Promise.resolve().then(() => respond(url, request));
  };
  const setBusy = (button, busy, text) => {
    if (busy) { button.dataset.label = button.textContent; button.textContent = text || '处理中…'; button.disabled = true; }
    else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
  };
  const message = (target, text, type = 'info') => { target.textContent = text || ''; target.messageType = type; target.hidden = !text; };
  const schedule = (kind, callback, delay) => { const id = ++timerId; timers.set(id, { kind, callback, delay }); return id; };
  vm.runInNewContext(source, {
    Sub2api: options.bridge,
    App: { el, api, money: String, date: String, channel: String, message, copy: async value => { copied.push(value); }, badge: (value, label) => el('span', { text: label || value }), setBusy },
    document: { getElementById: node, querySelector: selector => node('channels').querySelector(selector), hidden: false, title: '', addEventListener: (name, fn) => documentEvents.set(name, fn) },
    Date: { now: () => clock },
    sessionStorage: { setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key), removeItem: key => storage.delete(key) },
    confirm: () => true,
    setTimeout: (callback, delay) => schedule('timeout', callback, delay), clearTimeout: id => timers.delete(id),
    setInterval: (callback, delay) => schedule('interval', callback, delay), clearInterval: id => timers.delete(id)
  }, { filename: 'static/store.js' });
  await flush();
  const click = id => node(id).dispatch('click');
  const checkout = (contact = 'buyer@example.com') => { node('buyer-contact').value = contact; return node('checkout-form').dispatch('submit', { submitter: node('create-order') }); };
  const lookup = async contact => {
    if (!node('lookup-dialog').open) await click('open-lookup');
    node('lookup-contact').value = contact;
    return node('lookup-form').dispatch('submit', { submitter: node('lookup-submit') });
  };
  const select = id => {
    const button = node('products').querySelectorAll('[data-product]').find(item => item.dataset.product === String(id));
    assert.ok(button); return button.dispatch('click');
  };
  const polls = delay => [...timers.entries()].filter(([, timer]) => timer.kind === 'timeout' && (!delay || timer.delay === delay));
  return { node, calls, copied, storage, timers, catalog, click, checkout, lookup, select, polls,
    stored: () => JSON.parse(storage.get('cardshop.current-order') || 'null'),
    orderIds: () => node('lookup-results').querySelectorAll('[data-order]').map(item => item.dataset.order),
    advance: async seconds => { clock += seconds * 1000; for (const timer of [...timers.values()]) if (timer.kind === 'interval') await timer.callback(); },
    runPoll: async delay => { const [id, timer] = polls(delay)[0] || []; assert.ok(timer, 'Expected a scheduled poll'); timers.delete(id); await timer.callback(); await flush(); }
  };
}

test('storefront uses two dialogs and only contact recovery', () => {
  assert.equal((markup.match(/<dialog/g) || []).length, 2);
  assert.ok(markup.includes('QQ、手机号或邮箱'));
  assert.ok(!markup.includes('type="password"'));
  assert.ok(!markup.includes('查询口令'));
  assert.ok(!markup.includes('class="hero"'));
  assert.ok(!markup.includes('href="/admin"'));
});

test('verified account headers bind creation and identity errors require explicit guest choice', async () => {
  const listeners = [];
  const bridge = { status: 'ready', user: { id: 8, email: 'linked@example.com' }, error: '',
    headers: () => bridge.status === 'ready' ? { 'X-Sub2api-Session': 'local-session-handle' } : {},
    onChange: fn => listeners.push(fn), render: () => null,
    guest: () => { bridge.status = 'guest'; listeners.forEach(fn => fn()); } };
  const h = await harness((url, request) => {
    if (url === '/api/orders') return { order: order('bound'), token: 'order-token' };
    return { order: order('bound') };
  }, { bridge });
  h.catalog.products[0].sub2api_redeem = true;
  await h.select(1);
  assert.equal(h.node('buyer-contact').value, 'linked@example.com');
  assert.ok(h.node('purchase-redemption').textContent.includes('ID 8'));
  await h.checkout();
  const creation = h.calls.find(call => call.url === '/api/orders');
  assert.equal(creation.options.headers['X-Sub2api-Session'], 'local-session-handle');
  bridge.status = 'error'; bridge.error = '重新授权'; listeners.forEach(fn => fn());
  assert.equal(h.node('create-order').disabled, true);
  const before = h.calls.filter(call => call.url === '/api/orders').length;
  await h.checkout();
  assert.equal(h.calls.filter(call => call.url === '/api/orders').length, before);
  await h.click('sub2api-guest');
  await h.checkout();
  const calls = h.calls.filter(call => call.url === '/api/orders');
  assert.equal(calls.at(-1).options.headers['X-Sub2api-Session'], undefined);
});

test('paid orders continue polling until background redemption finishes', async () => {
  let completed = false;
  const h = await harness(url => {
    if (url === '/api/orders') return { order: order('redeem', 'paid', { redemption: { active: true } }), token: 'order-token' };
    return { order: order('redeem', 'paid', { redemption: { active: !completed } }) };
  });
  await h.select(1); await h.checkout(); await h.click('close-checkout');
  assert.equal(h.polls(4000).length, 1);
  await h.runPoll(4000);
  assert.equal(h.polls(4000).length, 1);
  completed = true; await h.runPoll(4000);
  assert.equal(h.polls(4000).length, 0);
  assert.equal(h.node('card-code').textContent, 'CARD-redeem');
});

test('my orders uses account session and contact search remains separate', async () => {
  const bridge = { status: 'ready', user: { id: 8, email: 'linked@example.com' },
    headers: () => ({ 'X-Sub2api-Session': 'local-session-handle' }), onChange: () => {}, render: () => null };
  const h = await harness(url => {
    if (url.startsWith('/api/sub2api/orders')) return { orders: [order('mine', 'paid')], total: 1, has_more: false };
    if (url === '/api/orders/lookup') return { orders: [order('contact', 'paid')], total: 1, has_more: false };
    throw new Error(url);
  }, { bridge });
  await h.click('my-orders');
  assert.deepEqual(h.orderIds(), ['mine']);
  assert.equal(h.node('lookup-form').hidden, true);
  assert.equal(h.calls.at(-1).options.headers['X-Sub2api-Session'], 'local-session-handle');
  await h.click('close-lookup'); await h.lookup('ordinary@example.com');
  assert.deepEqual(h.orderIds(), ['contact']);
  assert.equal(h.node('lookup-form').hidden, false);
});

test('creation trims contact, submits once and ignores product switching until complete', async () => {
  const creation = deferred();
  const ui = await harness(url => { assert.equal(url, '/api/orders'); return creation.promise; });
  await ui.select(1); const request = ui.checkout('  buyer@example.com  ');
  await ui.select(2); await ui.checkout();
  const requests = ui.calls.filter(call => call.url === '/api/orders');
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].options.body)), { product_id: 1, channel: 'alipay', contact: 'buyer@example.com', quantity: 1 });
  assert.equal(ui.node('create-order').disabled, true);
  creation.resolve({ order: order('A'), token: 'token-A' }); await request;
  assert.deepEqual(ui.stored(), { id: 'A', token: 'token-A' });
  assert.equal(ui.node('order-id').textContent, 'A');
  assert.equal(ui.node('order-view').hidden, false);
  assert.equal(ui.polls(4000).length, 1);
});

test('missing contact cannot create an order', async () => {
  const ui = await harness(() => assert.fail('No order should be submitted'));
  await ui.select(1); await ui.checkout('  ');
  assert.equal(ui.calls.filter(call => call.url === '/api/orders').length, 0);
  assert.equal(ui.node('checkout-message').textContent, '请填写联系方式');
});

test('failed creation restores submission and keeps a concise error', async () => {
  let attempts = 0;
  const ui = await harness(() => { if (++attempts === 1) throw new Error('暂时无法下单'); return { order: order('A'), token: 'token-A' }; });
  await ui.select(1); await ui.checkout();
  assert.equal(ui.node('create-order').disabled, false);
  assert.equal(ui.node('checkout-message').textContent, '暂时无法下单');
  await ui.checkout(); assert.equal(attempts, 2); assert.equal(ui.stored().id, 'A');
});

test('closing payment dialog neither cancels nor stops automatic delivery', async () => {
  const ui = await harness(url => url === '/api/orders' ? { order: order('A'), token: 'token-A' } : { order: order('A', 'paid') });
  await ui.select(1); await ui.checkout(); await ui.click('close-checkout');
  assert.equal(ui.node('checkout-dialog').open, false);
  assert.equal(ui.node('continue-order').hidden, false);
  await ui.runPoll(4000);
  assert.equal(ui.node('card-code').textContent, 'CARD-A');
  assert.equal(ui.node('continue-order').textContent, '查看卡密');
  assert.equal(ui.calls.filter(call => call.url.endsWith('/cancel')).length, 0);
  assert.equal(ui.polls(4000).length, 0);
  await ui.click('copy-card'); assert.deepEqual(ui.copied, ['CARD-A']);
});

test('closing during creation keeps the result recoverable without reopening dialog', async () => {
  const creation = deferred(); const ui = await harness(() => creation.promise);
  await ui.select(1); const pending = ui.checkout(); await ui.click('close-checkout');
  creation.resolve({ order: order('A'), token: 'token-A' }); await pending;
  assert.equal(ui.node('checkout-dialog').open, false);
  assert.equal(ui.node('continue-order').hidden, false);
  assert.equal(ui.stored().id, 'A');
});

test('saved order restores quietly and resume opens its order view', async () => {
  const ui = await harness((url, request) => { assert.equal(url, '/api/orders/A'); assert.equal(request.headers.Authorization, 'Bearer token-A'); return { order: order('A') }; }, { saved: { id: 'A', token: 'token-A' } });
  assert.equal(ui.node('checkout-dialog').open, false);
  assert.equal(ui.node('continue-order').hidden, false);
  await ui.click('continue-order'); await flush();
  assert.equal(ui.node('checkout-dialog').open, true);
  assert.equal(ui.node('order-view').hidden, false);
});

test('stale restore response cannot overwrite a newly created order', async () => {
  const restored = deferred();
  const ui = await harness(url => url === '/api/orders/A' ? restored.promise : { order: order('B'), token: 'token-B' }, { saved: { id: 'A', token: 'token-A' } });
  await ui.select(1); await ui.checkout();
  restored.resolve({ order: order('A', 'paid') }); await flush();
  assert.equal(ui.node('order-id').textContent, 'B');
  assert.deepEqual(ui.stored(), { id: 'B', token: 'token-B' });
});

test('failed initial restore does not show an incomplete order and retries quietly', async () => {
  let attempts = 0;
  const ui = await harness(() => {
    if (++attempts === 1) throw new Error('网络不可用');
    return { order: order('A') };
  }, { saved: { id: 'A', token: 'token-A' } });
  assert.equal(ui.node('continue-order').hidden, true);
  assert.equal(ui.node('checkout-dialog').open, false);
  await ui.runPoll(4000);
  assert.equal(ui.node('continue-order').hidden, false);
  assert.equal(ui.node('order-product').textContent, '测试商品 × 1');
});

test('expired local deadline hides payment before the API responds', async () => {
  const ui = await harness(() => ({ order: order('A', 'pending', { expires_at: initialTime / 1000 + 2 }), token: 'token-A' }));
  await ui.select(1); await ui.checkout();
  assert.equal(ui.node('payment-box').hidden, false);
  await ui.advance(3);
  assert.equal(ui.node('payment-box').hidden, true);
  assert.equal(ui.node('payment-qr').hidden, true);
  assert.equal(ui.node('cancel-order').hidden, true);
  assert.equal(ui.node('closed-order-message').hidden, false);
});

test('late pending refresh cannot undo cancellation and repeated cancel clicks submit once', async () => {
  const refresh = deferred(), cancellation = deferred();
  const ui = await harness(url => url === '/api/orders' ? { order: order('A'), token: 'token-A' } : url.endsWith('/cancel') ? cancellation.promise : refresh.promise);
  await ui.select(1); await ui.checkout();
  const pendingRefresh = ui.click('refresh-order'); const cancel = ui.click('cancel-order'); await ui.click('cancel-order');
  cancellation.resolve({ order: order('A', 'cancelled') }); await cancel;
  refresh.resolve({ order: order('A') }); await pendingRefresh;
  assert.equal(ui.node('order-status').children[0].textContent, 'cancelled');
  assert.equal(ui.node('refresh-order').disabled, false);
  assert.equal(ui.calls.filter(call => call.url.endsWith('/cancel')).length, 1);
  assert.equal(ui.polls(4000).length, 0);
});

test('failed cancellation restores button and automatic refresh', async () => {
  const ui = await harness(url => { if (url === '/api/orders') return { order: order('A'), token: 'token-A' }; throw new Error('取消失败'); });
  await ui.select(1); await ui.checkout(); await ui.click('cancel-order');
  assert.equal(ui.node('cancel-order').disabled, false);
  assert.equal(ui.node('order-message').textContent, '取消失败');
  assert.equal(ui.polls(4000).length, 1);
});

test('contact lookup is blocked during creation and cannot replace current credentials', async () => {
  const creation = deferred(); const ui = await harness(() => creation.promise);
  await ui.select(1); const pending = ui.checkout(); await ui.lookup('other@example.com');
  assert.equal(ui.calls.filter(call => call.url === '/api/orders/lookup').length, 0);
  creation.resolve({ order: order('A'), token: 'token-A' }); await pending;
  assert.equal(ui.stored().id, 'A');
});

test('in-flight lookup blocks creation until it completes', async () => {
  const query = deferred(); const ui = await harness(() => query.promise);
  await ui.select(1); const pending = ui.lookup('buyer@example.com'); await flush(); await ui.checkout();
  assert.equal(ui.calls.filter(call => call.url === '/api/orders').length, 0);
  query.resolve({ orders: [order('A', 'paid')], total: 1, has_more: false }); await pending;
  assert.equal(ui.node('create-order').disabled, false);
});

test('lookup sends contact only in POST body and shows multiple orders without exposing unissued codes', async () => {
  const ui = await harness((url, request) => {
    assert.equal(url, '/api/orders/lookup'); assert.equal(request.method, 'POST'); assert.equal(request.body.contact, 'buyer@example.com');
    return { orders: [order('A', 'paid'), order('B', 'pending', { card_code: 'MUST-NOT-RENDER' })], total: 2, has_more: false };
  });
  await ui.lookup(' buyer@example.com ');
  assert.deepEqual(ui.orderIds(), ['A', 'B']);
  const codes = ui.node('lookup-results').querySelectorAll('pre');
  assert.deepEqual(codes.map(node => node.textContent), ['CARD-A']);
  await ui.node('lookup-results').querySelector('button').dispatch('click'); assert.deepEqual(ui.copied, ['CARD-A']);
  assert.equal(ui.polls(5000).length, 1);
});

test('older lookup response cannot replace a newer contact result', async () => {
  const first = deferred();
  const ui = await harness((url, request) => request.body.contact === 'first@example.com' ? first.promise : { orders: [order('B', 'paid')], total: 1, has_more: false });
  const pending = ui.lookup('first@example.com'); await flush();
  await ui.lookup('second@example.com');
  first.resolve({ orders: [order('A', 'paid')], total: 1, has_more: false }); await pending;
  assert.deepEqual(ui.orderIds(), ['B']);
});

test('closing lookup ignores late results and stops query timers', async () => {
  const query = deferred(); const ui = await harness(() => query.promise);
  const pending = ui.lookup('buyer@example.com'); await flush(); await ui.click('close-lookup');
  query.resolve({ orders: [order('A')], total: 1, has_more: false }); await pending;
  assert.deepEqual(ui.orderIds(), []);
  assert.equal(ui.polls(5000).length, 0);
  assert.equal(ui.node('lookup-submit').disabled, false);
});

test('loaded older pages and expanded QR survive polling with a newly inserted order', async () => {
  let records = Array.from({ length: 101 }, (_, i) => order('ORDER-' + i));
  const ui = await harness((url, request) => ({ orders: records.slice(request.body.offset, request.body.offset + 50), total: records.length, has_more: request.body.offset + 50 < records.length }));
  await ui.lookup('buyer@example.com'); assert.equal(ui.orderIds().length, 50);
  await ui.click('lookup-more'); assert.equal(ui.orderIds().length, 100);
  const open = ui.node('lookup-results').querySelectorAll('details')[4]; open.open = true;
  records = [order('NEW'), ...records]; await ui.runPoll(10000);
  assert.equal(ui.orderIds()[0], 'NEW');
  assert.ok(ui.orderIds().includes('ORDER-99'));
  const oldCard = ui.node('lookup-results').querySelectorAll('[data-order]').find(card => card.dataset.order === 'ORDER-4');
  assert.equal(oldCard.querySelector('details').open, true);
});

test('lookup payment QR is hidden at expiry without waiting for a network response', async () => {
  const ui = await harness(() => ({ orders: [order('A', 'pending', { expires_at: initialTime / 1000 + 2 })], total: 1, has_more: false }));
  await ui.lookup('buyer@example.com'); const details = ui.node('lookup-results').querySelector('details');
  assert.equal(details.hidden, false); await ui.advance(3); assert.equal(details.hidden, true);
});

test('lookup polling replaces pending details with a delivered card', async () => {
  let paid = false;
  const ui = await harness(() => ({ orders: [order('A', paid ? 'paid' : 'pending')], total: 1, has_more: false }));
  await ui.lookup('buyer@example.com'); paid = true; await ui.runPoll(5000);
  assert.equal(ui.node('lookup-results').querySelector('details'), null);
  assert.equal(ui.node('lookup-results').querySelector('pre').textContent, 'CARD-A');
  assert.equal(ui.polls(5000).length, 0);
});

test('expired lookup remains manually refreshable for later fulfillment', async () => {
  let paid = false;
  const ui = await harness(() => ({ orders: [order('A', paid ? 'paid' : 'expired')], total: 1, has_more: false }));
  await ui.lookup('buyer@example.com'); assert.equal(ui.node('lookup-refresh').hidden, false);
  paid = true; await ui.click('lookup-refresh');
  assert.equal(ui.node('lookup-results').querySelector('pre').textContent, 'CARD-A');
});

test('lookup rate limit backs off for thirty seconds, including manual retries', async () => {
  let calls = 0;
  const ui = await harness(() => {
    if (++calls === 1) throw Object.assign(new Error('查询过于频繁'), { status: 429 });
    return { orders: [order('A', 'paid')], total: 1, has_more: false };
  });
  await ui.lookup('buyer@example.com');
  assert.equal(ui.polls(30000).length, 1);
  await ui.click('lookup-refresh'); assert.equal(calls, 1);
  await ui.advance(30); await ui.runPoll(30000);
  assert.equal(calls, 2); assert.deepEqual(ui.orderIds(), ['A']);
  assert.equal(ui.polls().length, 0);
});

test('contact length matches the backend limit and rejects oversized input', async () => {
  const ui = await harness(() => assert.fail('Oversized contact should not be submitted'));
  await ui.select(1); await ui.checkout('x'.repeat(129));
  await ui.lookup('x'.repeat(129));
  assert.equal(ui.calls.filter(call => call.url.startsWith('/api/orders')).length, 0);
  assert.equal((markup.match(/maxlength="128"/g) || []).length, 2);
});

test('quantity controls calculate the total and send the requested integer quantity', async () => {
  const ui = await harness((url, request) => {
    assert.equal(request.body.quantity, 3);
    return { order: order('BULK', 'pending', { quantity: 3, unit_price_cents: 1000, price_cents: 3000 }), token: 'bulk-token' };
  });
  await ui.select(1);
  assert.equal(ui.node('buy-quantity').value, '1');
  assert.equal(ui.node('buy-quantity').max, '3');
  assert.equal(ui.node('quantity-decrease').disabled, true);
  await ui.click('quantity-increase'); await ui.click('quantity-increase');
  assert.equal(ui.node('buy-quantity').value, '3');
  assert.equal(ui.node('selected-unit-price').textContent, '1000 × 3');
  assert.equal(ui.node('selected-price').textContent, '3000');
  assert.equal(ui.node('quantity-increase').disabled, true);
  await ui.checkout();
  assert.equal(ui.node('order-product').textContent, '测试商品 × 3');
  assert.equal(ui.node('pay-amount').textContent, '3000');
});

test('invalid or insufficient quantities disable submission even when forced', async () => {
  const ui = await harness(() => assert.fail('Invalid quantity must not reach the API'));
  await ui.select(1);
  for (const value of ['', '0', '-1', '1.5', '4', '101', 'abc']) {
    ui.node('buy-quantity').value = value; await ui.node('buy-quantity').dispatch('input');
    assert.equal(ui.node('create-order').disabled, true, value);
    assert.equal(ui.node('selected-price').textContent, '—', value);
    assert.equal(ui.node('quantity-message').hidden, false, value);
    await ui.checkout();
  }
  assert.equal(ui.calls.filter(call => call.url === '/api/orders').length, 0);
  ui.node('buy-quantity').value = '3'; await ui.node('buy-quantity').dispatch('input');
  assert.equal(ui.node('create-order').disabled, false);
});

test('quantity is capped at one hundred even with larger inventory', async () => {
  const ui = await harness(() => assert.fail('No purchase needed'));
  ui.catalog.products[0].stock = 200; await ui.select(1);
  assert.equal(ui.node('buy-quantity').max, '100');
  ui.node('buy-quantity').value = '100'; await ui.node('buy-quantity').dispatch('input');
  assert.equal(ui.node('create-order').disabled, false);
  assert.equal(ui.node('quantity-increase').disabled, true);
  assert.equal(ui.node('selected-price').textContent, '100000');
  ui.node('buy-quantity').value = '101'; await ui.node('buy-quantity').dispatch('input');
  assert.equal(ui.node('create-order').disabled, true);
});

test('switching products resets quantity and updates unit price and stock limit', async () => {
  const ui = await harness(() => assert.fail('No purchase needed'));
  await ui.select(1); await ui.click('quantity-increase');
  assert.equal(ui.node('buy-quantity').value, '2');
  await ui.select(2);
  assert.equal(ui.node('buy-quantity').value, '1');
  assert.equal(ui.node('buy-quantity').max, '2');
  assert.equal(ui.node('selected-unit-price').textContent, '2000 × 1');
  assert.equal(ui.node('selected-price').textContent, '2000');
});

test('stock refresh preserves typed quantity and flags the changed available stock', async () => {
  let delivered = false;
  const ui = await harness(() => ({ order: order('OLD', delivered ? 'paid' : 'pending') }), { saved: { id: 'OLD', token: 'old-token' } });
  await ui.select(1); ui.node('buy-quantity').value = '3'; await ui.node('buy-quantity').dispatch('input');
  ui.catalog.products[0].stock = 2; delivered = true; await ui.runPoll(4000);
  assert.equal(ui.node('buy-quantity').value, '3');
  assert.equal(ui.node('buy-quantity').max, '2');
  assert.equal(ui.node('create-order').disabled, true);
  assert.equal(ui.node('quantity-message').textContent, '库存仅剩 2 张');
});

test('current bulk delivery renders the array in order and copies every code', async () => {
  const ui = await harness(() => ({ order: order('BULK', 'paid', { quantity: 3, unit_price_cents: 1000, price_cents: 3000, card_codes: ['FIRST', 'SECOND', 'THIRD'], card_code: 'IGNORE-LEGACY' }), token: 'bulk-token' }));
  await ui.select(1); ui.node('buy-quantity').value = '3'; await ui.node('buy-quantity').dispatch('input'); await ui.checkout();
  assert.equal(ui.node('order-product').textContent, '测试商品 × 3');
  assert.equal(ui.node('pay-amount').textContent, '3000');
  assert.equal(ui.node('card-code').textContent, 'FIRST\nSECOND\nTHIRD');
  assert.equal(ui.node('copy-card').textContent, '复制全部');
  await ui.click('copy-card'); assert.deepEqual(ui.copied, ['FIRST\nSECOND\nTHIRD']);
});

test('bulk lookup displays all cards and total while pending cards remain hidden', async () => {
  const ui = await harness(() => ({ orders: [
    order('PAID', 'paid', { quantity: 2, unit_price_cents: 1000, price_cents: 2000, card_codes: ['ONE', 'TWO'], card_code: 'IGNORE-LEGACY' }),
    order('WAIT', 'pending', { quantity: 3, unit_price_cents: 1000, price_cents: 3000, card_codes: ['SECRET-ONE', 'SECRET-TWO', 'SECRET-THREE'] })
  ], total: 2, has_more: false }));
  await ui.lookup('buyer@example.com');
  const results = ui.node('lookup-results').querySelectorAll('[data-order]');
  assert.equal(results[0].querySelector('h3').textContent, '测试商品 × 2');
  assert.ok(results[0].querySelectorAll('p').some(item => item.textContent.startsWith('2000 ·')));
  assert.equal(results[0].querySelector('pre').textContent, 'ONE\nTWO');
  assert.equal(results[0].querySelector('button').textContent, '复制全部');
  assert.equal(results[1].querySelector('h3').textContent, '测试商品 × 3');
  assert.equal(results[1].querySelector('pre'), null);
  await results[0].querySelector('button').dispatch('click'); assert.deepEqual(ui.copied, ['ONE\nTWO']);
});

test('legacy single-card orders default to quantity one and retain single copy text', async () => {
  const ui = await harness(url => url === '/api/orders/lookup' ? { orders: [order('LEGACY', 'paid')], total: 1, has_more: false } : { order: order('LEGACY', 'paid') }, { saved: { id: 'LEGACY', token: 'legacy-token' } });
  assert.equal(ui.node('order-product').textContent, '测试商品 × 1');
  assert.equal(ui.node('card-code').textContent, 'CARD-LEGACY');
  assert.equal(ui.node('copy-card').textContent, '复制卡密');
  await ui.lookup('buyer@example.com');
  const result = ui.node('lookup-results').querySelector('[data-order]');
  assert.equal(result.querySelector('h3').textContent, '测试商品 × 1');
  assert.equal(result.querySelector('button').textContent, '复制卡密');
  assert.equal(result.querySelector('pre').textContent, 'CARD-LEGACY');
});

test('unpaid current bulk orders never expose card arrays or allow copying', async () => {
  const ui = await harness(() => ({ order: order('WAIT', 'pending', { quantity: 2, price_cents: 2000, card_codes: ['SECRET-1', 'SECRET-2'], card_code: 'SECRET-3' }), token: 'wait-token' }));
  await ui.select(1); await ui.checkout();
  assert.equal(ui.node('delivered-box').hidden, true);
  assert.equal(ui.node('card-code').textContent, '');
  assert.equal(ui.node('copy-card').disabled, true);
  await ui.click('copy-card'); assert.deepEqual(ui.copied, []);
});

