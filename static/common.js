'use strict';
(() => {
  const money = value => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(Number(value || 0) / 100);
  const date = value => value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
  const channel = value => ({ alipay: '支付宝', wechat: '微信支付' }[value] || value || '—');
  const statuses = { pending: '等待付款', paid: '已发卡', expired: '已过期', cancelled: '已取消', review: '待人工核对', delivered: '已自动发卡', resolved: '已处理', ignored: '已忽略', duplicate: '重复通知' };
  const status = value => statuses[value] || value || '—';
  function el(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (key === 'className') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat()) if (child !== undefined && child !== null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return node;
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
    try {
      const response = await fetch(path, { ...options, body, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.error?.message || '请求失败（' + response.status + '），请稍后重试。');
        error.status = response.status; error.code = data.error?.code; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时。下单后请先查询订单，避免重复操作。');
      if (error instanceof TypeError) throw new Error('连接失败，请检查网络后重试。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function message(target, text, type = 'info') { target.textContent = text || ''; target.className = 'notice ' + type; target.hidden = !text; }
  async function copy(value) {
    // Cross-origin menus may not delegate clipboard-write to this frame.
    try {
      const policy = document.permissionsPolicy || document.featurePolicy;
      if (window.isSecureContext && navigator.clipboard?.writeText && (!policy?.allowsFeature || policy.allowsFeature('clipboard-write'))) {
        await navigator.clipboard.writeText(value); return;
      }
    } catch (_) { /* Permission denial still allows a user-initiated legacy copy. */ }
    const previous = document.activeElement;
    const selection = window.getSelection();
    const ranges = Array.from({ length: selection?.rangeCount || 0 }, (_, index) => selection.getRangeAt(index).cloneRange());
    const field = el('textarea', { className: 'copy-buffer', 'aria-label': '待复制文本', readonly: true });
    let ok = false;
    try {
      // A modal makes the rest of the document inert; keep the buffer inside it.
      const host = previous?.closest('dialog[open]') || document.querySelector('dialog[open]') || document.body;
      field.value = value; host.append(field); field.focus({ preventScroll: true });
      field.select(); field.setSelectionRange(0, field.value.length);
      ok = document.execCommand('copy');
    } catch (_) { /* Never expose browser permission errors or report false success. */ }
    finally {
      field.remove();
      if (previous?.isConnected) previous.focus({ preventScroll: true });
      if (selection) { selection.removeAllRanges(); ranges.forEach(range => selection.addRange(range)); }
    }
    if (!ok) throw new Error('浏览器限制了自动复制，请长按或选中上方卡密复制，也可在新窗口打开后重试。');
  }
  function badge(value, label) { return el('span', { className: 'badge ' + (['paid', 'delivered', 'resolved'].includes(value) ? 'success' : ['expired', 'cancelled', 'review'].includes(value) ? 'warning' : ''), text: label || status(value) }); }
  function setBusy(button, busy, text) {
    if (busy) { button.dataset.label = button.textContent; button.textContent = text || '处理中…'; button.disabled = true; }
    else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
  }
  // Shared by checkout, contact lookup, and the dynamically rendered admin previews.
  let qrViewer = null, qrKey = '', qrTimer = null, qrObserver = null;
  function liveQrSource() {
    const source = [...document.querySelectorAll('[data-qr-preview]')].find(node => node.dataset.qrPreview === qrKey);
    if (!source || source.closest('[hidden]') || source.closest('details:not([open])')) return null;
    const parent = source.closest('dialog');
    if (parent && !parent.open) return null;
    if (source.dataset.qrExpires && Number(source.dataset.qrExpires) * 1000 <= Date.now()) return null;
    return source;
  }
  function showQrPreview(source) {
    qrKey = source.dataset.qrPreview;
    if (!liveQrSource()) return;
    const thumbnail = source.querySelector('img');
    if (!thumbnail || !thumbnail.getAttribute('src')) return;
    if (!qrViewer) {
      const title = el('h2', { id: 'qr-viewer-title', text: '收款二维码' });
      const close = el('button', { type: 'button', className: 'close-button', 'aria-label': '关闭大图', text: '×', onclick: () => qrViewer.close() });
      const picture = el('img', { id: 'qr-viewer-image', alt: '收款二维码', draggable: 'false' });
      const stage = el('div', { className: 'qr-viewer-stage', tabindex: '0', 'aria-label': '收款码大图，放大后可滚动查看' }, picture);
      const zoom = el('button', { id: 'qr-viewer-zoom', type: 'button', className: 'button secondary', text: '继续放大', 'aria-pressed': 'false', onclick: () => {
        const enlarged = stage.classList.toggle('is-zoomed');
        zoom.textContent = enlarged ? '适应屏幕' : '继续放大';
        zoom.setAttribute('aria-pressed', String(enlarged));
        stage.scrollLeft = (stage.scrollWidth - stage.clientWidth) / 2;
        stage.scrollTop = (stage.scrollHeight - stage.clientHeight) / 2;
      } });
      const hint = el('p', { id: 'qr-viewer-hint', className: 'small muted', role: 'status', text: '可长按图片保存，放大后可滑动查看' });
      picture.addEventListener('error', () => { hint.textContent = '图片加载失败，请关闭后重试'; });
      qrViewer = el('dialog', { id: 'qr-viewer', className: 'qr-viewer', 'aria-labelledby': 'qr-viewer-title' },
        el('div', { className: 'section-heading' }, title, close),
        el('div', { id: 'qr-viewer-payment', className: 'payment-amount', hidden: true },
          el('span', { text: '应付金额（请按此金额付款，勿抹零）' }),
          el('strong', { id: 'qr-viewer-amount' })), stage,
        el('div', { className: 'qr-viewer-toolbar' }, hint, zoom));
      qrViewer.addEventListener('click', event => {
        if (event.target !== qrViewer) return;
        const box = qrViewer.getBoundingClientRect();
        if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) qrViewer.close();
      });
      qrViewer.addEventListener('close', () => {
        clearInterval(qrTimer); qrTimer = null;
        qrObserver?.disconnect(); qrObserver = null;
        picture.removeAttribute('src');
        const currentSource = liveQrSource();
        if (currentSource) currentSource.focus();
        qrKey = '';
      });
      document.body.append(qrViewer);
    }
    qrViewer.querySelector('#qr-viewer-image').src = thumbnail.currentSrc || thumbnail.src;
    qrViewer.querySelector('#qr-viewer-image').alt = thumbnail.alt;
    qrViewer.querySelector('#qr-viewer-title').textContent = thumbnail.alt || '收款二维码';
    const amountText = source.dataset.qrAmount;
    const amount = Number(amountText);
    qrViewer.querySelector('#qr-viewer-payment').hidden = !Number.isInteger(amount) || amount <= 0;
    qrViewer.querySelector('#qr-viewer-amount').textContent = Number.isInteger(amount) && amount > 0 ? money(amount) : '';
    qrViewer.querySelector('#qr-viewer-hint').textContent = '可长按图片保存，放大后可滑动查看';
    qrViewer.querySelector('.qr-viewer-stage').classList.remove('is-zoomed');
    qrViewer.querySelector('#qr-viewer-zoom').textContent = '继续放大';
    qrViewer.querySelector('#qr-viewer-zoom').setAttribute('aria-pressed', 'false');
    qrViewer.showModal();
    qrViewer.querySelector('.qr-viewer-stage').scrollTo(0, 0);
    // Match a replacement thumbnail after polling; close when its order is no longer payable.
    const checkSource = () => {
      if (!qrViewer.open) return;
      const currentSource = liveQrSource();
      if (!currentSource || currentSource.dataset.qrAmount !== amountText || currentSource.querySelector('img')?.src !== qrViewer.querySelector('#qr-viewer-image').src) qrViewer.close();
    };
    qrObserver = new MutationObserver(checkSource);
    qrObserver.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['hidden', 'open', 'src', 'data-qr-preview', 'data-qr-expires', 'data-qr-amount'] });
    qrTimer = setInterval(checkSource, 1000);
  }
  document.addEventListener('click', event => {
    const source = event.target.closest?.('button[data-qr-preview]');
    if (source) showQrPreview(source);
  });
  window.App = { money, date, channel, status, el, api, message, copy, badge, setBusy };
})();

