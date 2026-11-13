'use strict';
(() => {
  const { api, el, date } = App;
  const key = 'cardshop.sub2api-session';
  const labels = { not_requested: '仅发卡', awaiting_delivery: '发卡后自动兑换', cancelled: '订单已取消', pending: '等待自动兑换', processing: '正在兑换', retry: '等待重试', succeeded: '已兑换到账', partial: '部分已兑换', reauth: '待重新授权', failed: '兑换失败', incompatible: '对接需要检查' };
  const listeners = new Set();
  let handle = '', user = null, status = 'guest', error = '', rawToken = '';
  const notify = () => { for (const fn of listeners) fn(); };
  function clearStored() { try { sessionStorage.removeItem(key); } catch (_) { /* optional storage */ } }
  function guest() { handle = ''; user = null; status = 'guest'; error = ''; clearStored(); notify(); }
  function invalidate(message) { handle = ''; status = 'error'; error = message; clearStored(); notify(); }
  const headers = () => status === 'ready' && handle ? { 'X-Sub2api-Session': handle } : {};
  function render(order, retry) {
    const r = order.redemption;
    if (!r?.auto) return null;
    const target = r.target_user || {};
    const box = el('section', { className: 'redemption-box', 'aria-label': '自动兑换状态' },
      el('strong', { text: labels[r.status] || '兑换状态待核实' }),
      el('p', { className: 'small break-all', text: '目标账户：' + (target.email || '') + '（ID ' + target.id + '）' }));
    for (const item of r.items || []) {
      box.append(el('p', { className: 'small', text: '第 ' + (item.position + 1) + ' 张：' + (labels[item.status] || '待核实') + (item.redeemed_at ? ' · ' + date(item.redeemed_at) : '') + (item.message ? ' · ' + item.message : '') }));
    }
    if (r.status === 'succeeded') box.append(el('p', { className: 'small muted', text: '下方兑换码已使用，保留供查询核对，无需再次兑换。' }));
    if (retry && r.can_retry) {
      const feedback = el('div', { role: 'status', hidden: true });
      const button = el('button', { type: 'button', className: 'button secondary tiny', text: '重试未成功项' });
      button.addEventListener('click', async () => {
        if (button.disabled) return;
        button.disabled = true;
        try { await retry(order); }
        catch (e) { App.message(feedback, e.message, 'error'); }
        finally { button.disabled = false; }
      });
      box.append(button, feedback);
    }
    return box;
  }
  async function initialize() {
    if (!document.getElementById('sub2api-context')) return;
    let hasIncoming = false, expected;
    try {
      const url = new URL(window.location.href);
      hasIncoming = url.searchParams.has('token') || url.searchParams.has('user_id');
      rawToken = url.searchParams.get('token') || '';
      if (url.searchParams.has('user_id')) expected = Number(url.searchParams.get('user_id'));
      const sensitive = ['token', 'user_id', 'src_url', 'src_host'];
      if (sensitive.some(k => url.searchParams.has(k))) {
        sensitive.forEach(k => url.searchParams.delete(k));
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
      if (hasIncoming) {
        clearStored();
        // A fresh menu launch may belong to a different account in this tab.
        try { sessionStorage.removeItem('cardshop.current-order'); } catch (_) { /* optional */ }
        if (!rawToken || (expected !== undefined && (!Number.isSafeInteger(expected) || expected <= 0))) throw new Error('登录信息不完整，请从 sub2api 重新进入，或选择普通购买');
        status = 'checking'; notify();
        const data = await api('/api/sub2api/session', { method: 'POST', body: { token: rawToken, ...(expected !== undefined ? { user_id: expected } : {}) } });
        if (!data.session_token || !data.user?.id) throw new Error('身份验证响应异常，请重新进入');
        handle = data.session_token; user = data.user; status = 'ready';
        try { sessionStorage.setItem(key, JSON.stringify({ handle, expires_at: data.expires_at })); } catch (_) { /* continue in memory */ }
      } else {
        let saved;
        try { saved = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { clearStored(); }
        if (saved?.handle && saved.expires_at > Date.now() / 1000) {
          status = 'checking'; handle = saved.handle; notify();
          const data = await api('/api/sub2api/session', { headers: { 'X-Sub2api-Session': handle } });
          if (data.needs_reauth || !data.user?.id) throw new Error('授权已失效，请从 sub2api 重新进入，或选择普通购买');
          user = data.user; status = 'ready';
        } else { clearStored(); }
      }
    } catch (e) { invalidate(e.message || '身份验证失败，请从 sub2api 重新进入'); }
    finally { rawToken = ''; notify(); }
  }
  const bridge = {
    get status() { return status; }, get user() { return user; }, get error() { return error; },
    headers, guest, invalidate, render, onChange: fn => listeners.add(fn),
    async retry(order) {
      if (status !== 'ready' || user?.id !== order.redemption?.target_user?.id) throw new Error('请由原账户从 sub2api 重新进入');
      return api('/api/sub2api/orders/' + encodeURIComponent(order.id) + '/retry', { method: 'POST', headers: headers(), body: {} });
    }
  };
  window.Sub2api = bridge;
  bridge.ready = initialize();
})();
