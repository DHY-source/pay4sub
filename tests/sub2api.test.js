'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'sub2api.js'), 'utf8');

async function initialize(url, respond, stored = {}) {
  const storage = new Map(Object.entries(stored)), calls = [], history = [];
  const window = { location: { href: url }, history: { replaceState: (_, __, value) => history.push(value) } };
  vm.runInNewContext(source, {
    App: { api: async (url, options) => { calls.push({ url, options }); return respond(url, options); } },
    window, URL, Date, document: { getElementById: () => ({}) },
    sessionStorage: { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) }
  });
  await window.Sub2api.ready;
  return { bridge: window.Sub2api, storage, calls, history };
}
test('menu token is exchanged, removed from URL, never persisted; new account clears old order', async () => {
  const h = await initialize('https://pay.example.com/?token=raw-secret-token&user_id=12&src_url=private&theme=dark', async (url, options) => {
    assert.equal(url, '/api/sub2api/session');
    assert.equal(options.body.token, 'raw-secret-token');
    assert.equal(options.body.user_id, 12);
    return { session_token: 'opaque-local-session', expires_at: Date.now() / 1000 + 600, user: { id: 12, email: 'user@example.com' } };
  }, { 'cardshop.current-order': '{"id":"old","token":"old-order-secret"}' });
  assert.equal(h.bridge.status, 'ready');
  assert.equal(h.bridge.user.id, 12);
  assert.equal(h.history[0], '/?theme=dark');
  assert.equal(h.storage.has('cardshop.current-order'), false);
  assert.ok(!JSON.stringify([...h.storage]).includes('raw-secret-token'));
  assert.equal(h.bridge.headers()['X-Sub2api-Session'], 'opaque-local-session');
});
test('invalid incoming identity cannot fall back to a previously saved account', async () => {
  const h = await initialize('https://pay.example.com/?user_id=55', () => { throw new Error('must not call'); }, {
    'cardshop.sub2api-session': JSON.stringify({ handle: 'old-session', expires_at: Date.now() / 1000 + 600 })
  });
  assert.equal(h.calls.length, 0);
  assert.equal(h.bridge.status, 'error');
  assert.equal(h.storage.size, 0);
  assert.deepEqual(Object.keys(h.bridge.headers()), []);
  h.bridge.guest();
  assert.equal(h.bridge.status, 'guest');
});
test('restored short session is verified and never sent in a URL', async () => {
  const h = await initialize('https://pay.example.com/', (url, options) => {
    assert.equal(url, '/api/sub2api/session');
    assert.equal(options.headers['X-Sub2api-Session'], 'local-handle');
    return { user: { id: 1, email: 'user@example.com' }, needs_reauth: false };
  }, { 'cardshop.sub2api-session': JSON.stringify({ handle: 'local-handle', expires_at: Date.now() / 1000 + 600 }) });
  assert.equal(h.bridge.status, 'ready');
  assert.equal(h.history.length, 0);
});
test('expired upstream authorization blocks shopping until reentry or explicit guest mode', async () => {
  const h = await initialize('https://pay.example.com/', () => ({ user: { id: 1 }, needs_reauth: true }), {
    'cardshop.sub2api-session': JSON.stringify({ handle: 'local-handle', expires_at: Date.now() / 1000 + 600 })
  });
  assert.equal(h.bridge.status, 'error');
  assert.equal(h.storage.size, 0);
});
test('retries cannot target another account even with a visible order', async () => {
  const h = await initialize('https://pay.example.com/?token=raw-secret-token&user_id=12', () => ({
    session_token: 'local', expires_at: Date.now() / 1000 + 600, user: { id: 12 }
  }));
  await assert.rejects(h.bridge.retry({ id: 'other', redemption: { target_user: { id: 13 } } }), /原账户/);
  assert.equal(h.calls.length, 1);
});
