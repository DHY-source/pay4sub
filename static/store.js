'use strict';
(() => {
  const { el, api, money, date, channel, message, copy, badge, setBusy } = App;
  const $ = id => document.getElementById(id);
  const bridge = typeof Sub2api === 'undefined' ? null : Sub2api;
  const storageKey = 'cardshop.current-order';
  let catalog = null, selected = null, catalogGeneration = 0;
  let current = null, token = '', orderGeneration = 0, orderRequest = null;
  let creating = false, cancelling = false, restoring = false, orderTimer = null, countdownTimer = null;
  let lookupContact = '', lookupOrders = [], lookupPages = 1, lookupHasMore = false;
  let lookupGeneration = 0, lookupTask = null, lookupTimer = null, lookupCountdown = null;
  let lookupRetryAfter = 0;
  let lookupDetails = new Map();
  let lookupMine = false;
  const redemptionActive = order => Boolean(order?.redemption?.active);
  const accountHeaders = () => bridge?.headers() || {};
  function syncIdentity() {
    const account = bridge?.user;
    $('sub2api-context').hidden = !bridge || bridge.status === 'guest';
    $('sub2api-message').textContent = bridge?.status === 'checking' ? '正在验证 sub2api 账户…' : bridge?.status === 'error' ? bridge.error : account ? '已连接：' + (account.email || '') + '（ID ' + account.id + '）' : '';
    $('sub2api-guest').hidden = bridge?.status === 'checking';
    $('sub2api-guest').disabled = creating;
    $('my-orders').hidden = bridge?.status !== 'ready';
    $('my-orders').disabled = creating;
    const auto = selected?.sub2api_redeem && bridge?.status === 'ready';
    $('purchase-redemption').hidden = !selected?.sub2api_redeem;
    $('purchase-redemption').textContent = auto ? '发卡后自动兑换至 ' + (account.email || '') + '（ID ' + account.id + '）；关闭页面也会继续处理。' : '普通购买：发卡后显示兑换码，由你自行兑换。';
  }
  async function retryRedemption(order) {
    const result = await bridge.retry(order);
    if (current?.id === order.id) { current = result.order; renderOrder(); queueOrderPoll(orderGeneration); }
    lookupOrders = lookupOrders.map(item => item.id === order.id ? result.order : item);
    if ($('lookup-dialog').open) { renderLookup(); queueLookup(lookupGeneration); }
  }
  const redemptionView = order => bridge?.render(order, bridge?.status === 'ready' && bridge.user?.id === order.redemption?.target_user?.id ? retryRedemption : null);
  const orderQuantity = order => Number.isInteger(order?.quantity) && order.quantity > 0 ? order.quantity : 1;
  const orderName = order => (order.product_name || '') + ' × ' + orderQuantity(order);
  const issuedCodes = order => order?.status !== 'paid' ? '' : Array.isArray(order.card_codes) ? order.card_codes.join('\n') : order.card_code || '';
  const copyLabel = order => orderQuantity(order) > 1 ? '复制全部' : '复制卡密';
  const purchaseQuantity = () => Number($('buy-quantity').value);
  const quantityLimit = () => Math.max(0, Math.min(100, selected?.stock || 0));
  function quantityError() {
    const quantity = purchaseQuantity();
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 100) return '数量须为 1–100 的整数';
    if (quantity > quantityLimit()) return selected?.stock > 0 ? '库存仅剩 ' + selected.stock + ' 张' : '库存不足';
    return '';
  }
  function renderPurchase(blocked) {
    const quantity = purchaseQuantity(), error = selected ? quantityError() : '';
    $('buy-quantity').max = String(quantityLimit());
    $('buy-quantity').disabled = blocked;
    $('quantity-decrease').disabled = blocked || !selected || quantity <= 1;
    $('quantity-increase').disabled = blocked || !selected || quantity >= quantityLimit();
    $('selected-unit-price').textContent = selected ? money(selected.price_cents) + ' × ' + (Number.isInteger(quantity) && quantity >= 1 ? quantity : '—') : '';
    $('selected-price').textContent = selected && !error ? money(selected.price_cents * quantity) : '—';
    message($('quantity-message'), error, 'error');
    return error;
  }
  const secondsLeft = order => Math.max(0, Math.ceil(order.expires_at - Date.now() / 1000));
  const statusLabel = order => order.status === 'pending' ? (secondsLeft(order) ? '待发卡' : '已超时') : undefined;
  const paymentTime = order => {
    const seconds = secondsLeft(order);
    return seconds ? '付款有效期 ' + String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0') : '已超时，请勿付款；已付款请联系店主';
  };
  function showDialog(id) {
    for (const other of ['checkout-dialog', 'lookup-dialog']) if (other !== id && $(other).open) $(other).close();
    if (!$(id).open) $(id).showModal();
  }
  function syncButtons() {
    const blocked = creating || cancelling || lookupTask !== null || ['checking', 'error'].includes(bridge?.status);
    const error = renderPurchase(blocked);
    $('create-order').disabled = blocked || !selected || Boolean(error) || !document.querySelector('input[name="channel"]:checked');
    $('open-lookup').disabled = creating;
    for (const button of $('products').querySelectorAll('[data-product]')) button.disabled = blocked || button.dataset.stock === '0';
    $('continue-order').hidden = !current || restoring;
    $('continue-order').textContent = current?.status === 'paid' ? '查看卡密' : current?.status === 'pending' && secondsLeft(current) ? '继续付款' : '查看订单';
    syncIdentity();
  }
  function selectProduct(product) {
    if (creating || cancelling || lookupTask !== null || product.stock <= 0) return;
    selected = product;
    $('buy-quantity').value = '1';
    $('selected-name').textContent = product.name;
    $('checkout-view').hidden = false; $('order-view').hidden = true;
    $('checkout-title').textContent = '购买';
    if (!$('buyer-contact').value && current?.contact) $('buyer-contact').value = current.contact;
    if (!$('buyer-contact').value && bridge?.status === 'ready' && bridge.user?.email) $('buyer-contact').value = bridge.user.email;
    message($('checkout-message'), ''); syncButtons(); showDialog('checkout-dialog');
  }
  async function loadProducts() {
    const generation = ++catalogGeneration;
    try {
      const data = await api('/api/products');
      if (generation !== catalogGeneration) return;
      catalog = data;
      $('brand-name').textContent = catalog.shop_name || '自助发卡';
      document.title = catalog.shop_name || '自助发卡';
      message($('shop-notice'), catalog.support_notice);
      message($('store-message'), '');
      const products = (catalog.products || []).filter(product => product.active !== false);
      $('products').replaceChildren();
      if (!products.length) $('products').append(el('div', { className: 'empty-state', text: '暂无商品' }));
      for (const product of products) {
        const buy = el('button', { type: 'button', className: 'button primary', 'data-product': product.id, 'data-stock': product.stock > 0 ? '1' : '0', disabled: product.stock <= 0, text: product.stock > 0 ? '购买' : '售罄', onclick: () => selectProduct(product) });
        $('products').append(el('article', { className: 'product-card' },
          el('div', { className: 'section-heading compact' }, el('h2', { text: product.name }), el('span', { className: 'stock-pill', text: '库存 ' + product.stock })),
          product.description ? el('p', { className: 'product-description', text: product.description }) : null,
          el('div', { className: 'product-bottom' }, el('strong', { text: money(product.price_cents) }), buy)));
      }
      if (selected) {
        selected = products.find(product => product.id === selected.id) || null;
        if (selected) $('selected-name').textContent = selected.name;
      }
      const previous = document.querySelector('input[name="channel"]:checked')?.value;
      const channels = (catalog.channels || []).filter(item => item.enabled);
      $('channels').replaceChildren();
      channels.forEach((item, index) => {
        const input = el('input', { type: 'radio', name: 'channel', value: item.id, required: true });
        input.checked = previous ? item.id === previous : index === 0;
        input.addEventListener('change', syncButtons);
        $('channels').append(el('label', { className: 'channel-option' }, input, el('span', { text: item.label || channel(item.id) })));
      });
      if (channels.length && !document.querySelector('input[name="channel"]:checked')) $('channels').querySelector('input').checked = true;
      if (!channels.length) $('channels').append(el('p', { className: 'muted small', text: '暂未开放收款' }));
      syncButtons();
    } catch (error) {
      if (generation !== catalogGeneration) return;
      message($('store-message'), error.message, 'error');
      if (!catalog) $('products').replaceChildren(el('div', { className: 'empty-state' }, el('button', { type: 'button', className: 'button secondary', text: '重新加载', onclick: loadProducts })));
    }
  }
  function stopOrderTimers() { clearTimeout(orderTimer); clearInterval(countdownTimer); orderTimer = null; countdownTimer = null; }
  function saveOrder() { try { sessionStorage.setItem(storageKey, JSON.stringify({ id: current.id, token })); } catch (_) { /* Contact lookup is always available. */ } }
  function tickOrder() {
    if (!current) return;
    const payable = current.status === 'pending' && secondsLeft(current) > 0;
    $('payment-box').hidden = !payable;
    $('payment-qr').hidden = !payable;
    $('enlarge-payment-qr').dataset.qrPreview = 'checkout:' + current.id;
    $('enlarge-payment-qr').dataset.qrExpires = String(current.expires_at);
    $('cancel-order').hidden = !payable || !token;
    $('countdown').textContent = paymentTime(current);
    $('closed-order-message').hidden = !(['expired', 'cancelled'].includes(current.status) || (current.status === 'pending' && !payable));
    $('closed-order-message').textContent = current.status === 'cancelled' ? '订单已取消，请勿付款' : '已超时，请勿付款；已付款请联系店主';
    $('order-status').replaceChildren(badge(current.status, statusLabel(current)));
    syncButtons();
  }
  function renderOrder() {
    if (!current) return;
    $('order-product').textContent = orderName(current);
    $('order-id').textContent = current.id;
    $('order-channel').textContent = channel(current.channel);
    $('order-contact').textContent = current.contact || '';
    $('pay-amount').textContent = money(current.price_cents);
    const base = current.base_price_cents ?? (current.unit_price_cents ?? current.price_cents) * orderQuantity(current);
    $('payment-breakdown').textContent = '商品合计 ' + money(base) + ' ＋ 金额区分加价 ' + money(current.price_cents - base);
    $('enlarge-payment-qr').dataset.qrAmount = String(current.price_cents);
    $('delivered-box').hidden = current.status !== 'paid';
    $('card-code').textContent = current.status === 'paid' ? issuedCodes(current) || '卡密暂未显示，请刷新' : '';
    $('copy-card').textContent = copyLabel(current);
    $('copy-card').disabled = !issuedCodes(current);
    $('order-redemption').replaceChildren(...[redemptionView(current)].filter(Boolean));
    if (current.status === 'pending' && secondsLeft(current)) $('payment-qr').src = '/qr/' + encodeURIComponent(current.channel);
    clearInterval(countdownTimer); countdownTimer = null;
    tickOrder();
    if (current.status === 'pending') countdownTimer = setInterval(tickOrder, 1000);
  }
  function queueOrderPoll(generation) {
    clearTimeout(orderTimer); orderTimer = null;
    if ((current?.status === 'pending' || redemptionActive(current)) && !creating && !cancelling) orderTimer = setTimeout(() => refreshOrder(false, generation), document.hidden ? 15000 : 4000);
  }
  function activateOrder(order, accessToken, open = true) {
    stopOrderTimers(); orderGeneration += 1; orderRequest = null;
    setBusy($('refresh-order'), false);
    current = order; token = accessToken; restoring = false; saveOrder(); renderOrder();
    message($('order-message'), '');
    if (open) { $('checkout-view').hidden = true; $('order-view').hidden = false; $('checkout-title').textContent = '订单'; showDialog('checkout-dialog'); }
    queueOrderPoll(orderGeneration);
  }
  async function refreshOrder(manual = false, generation = orderGeneration) {
    if (!current || !token || creating || cancelling || orderRequest === generation) return;
    const id = current.id, accessToken = token;
    orderRequest = generation;
    if (manual) setBusy($('refresh-order'), true);
    try {
      const data = await api('/api/orders/' + encodeURIComponent(id), { headers: { Authorization: 'Bearer ' + accessToken } });
      if (generation !== orderGeneration || current?.id !== id) return;
      if (current.status !== 'pending' && data.order.status === 'pending') return;
      const changed = current.status !== data.order.status;
      current = data.order; restoring = false; saveOrder(); renderOrder();
      message($('order-message'), '');
      if (changed) loadProducts();
    } catch (error) {
      if (generation !== orderGeneration || current?.id !== id) return;
      if (error.status === 404) {
        restoring = false; current = null; token = ''; stopOrderTimers(); orderGeneration += 1;
        try { sessionStorage.removeItem(storageKey); } catch (_) { /* Storage is optional. */ }
        $('order-view').hidden = true;
        if ($('checkout-dialog').open && $('checkout-view').hidden) $('checkout-dialog').close();
        message($('store-message'), '请用联系方式查询订单', 'warning'); syncButtons();
      } else { message($('order-message'), error.message, 'error'); syncButtons(); }
    } finally {
      if (orderRequest === generation) orderRequest = null;
      if (generation === orderGeneration) { if (manual) setBusy($('refresh-order'), false); queueOrderPoll(generation); }
    }
  }
  $('checkout-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!selected || creating || cancelling || lookupTask !== null || ['checking', 'error'].includes(bridge?.status)) return;
    const contact = $('buyer-contact').value.trim();
    const payment = document.querySelector('input[name="channel"]:checked');
    if (!payment || quantityError()) { syncButtons(); return; }
    const quantity = purchaseQuantity();
    if (contact.length < 3 || contact.length > 128) { message($('checkout-message'), '请填写联系方式', 'error'); return; }
    creating = true; orderGeneration += 1; stopOrderTimers(); setBusy($('refresh-order'), false);
    setBusy($('create-order'), true, '创建中…'); syncButtons(); message($('checkout-message'), '');
    const productId = selected.id;
    try {
      const data = await api('/api/orders', { method: 'POST', headers: accountHeaders(), body: { product_id: productId, channel: payment.value, contact, quantity } });
      activateOrder(data.order, data.token, $('checkout-dialog').open); loadProducts();
    } catch (error) { if (error.code === 'sub2api_reauth') bridge?.invalidate(error.message); message($('checkout-message'), error.message, 'error'); }
    finally { creating = false; setBusy($('create-order'), false); syncButtons(); queueOrderPoll(orderGeneration); }
  });
  $('buy-quantity').addEventListener('input', syncButtons);
  $('buy-quantity').addEventListener('change', syncButtons);
  function changeQuantity(delta) {
    if (creating || cancelling || lookupTask !== null || !selected || !quantityLimit()) return;
    const quantity = purchaseQuantity();
    $('buy-quantity').value = String(Number.isInteger(quantity) && quantity > 0 ? Math.max(1, Math.min(quantityLimit(), quantity + delta)) : 1);
    syncButtons();
  }
  $('quantity-decrease').addEventListener('click', () => changeQuantity(-1));
  $('quantity-increase').addEventListener('click', () => changeQuantity(1));
  $('continue-order').addEventListener('click', () => {
    if (!current || restoring || creating) return;
    $('checkout-view').hidden = true; $('order-view').hidden = false; $('checkout-title').textContent = '订单';
    renderOrder(); showDialog('checkout-dialog'); refreshOrder();
  });
  $('close-checkout').addEventListener('click', () => $('checkout-dialog').close());
  $('refresh-order').addEventListener('click', () => refreshOrder(true));
  $('cancel-order').addEventListener('click', async () => {
    if (!current || current.status !== 'pending' || !secondsLeft(current) || !token || creating || cancelling) return;
    if (!confirm('确认尚未付款并取消订单？')) return;
    cancelling = true; stopOrderTimers(); setBusy($('refresh-order'), false); const generation = ++orderGeneration;
    const id = current.id, accessToken = token; setBusy($('cancel-order'), true); syncButtons();
    try {
      const data = await api('/api/orders/' + encodeURIComponent(id) + '/cancel', { method: 'POST', headers: { Authorization: 'Bearer ' + accessToken }, body: {} });
      if (generation !== orderGeneration) return;
      current = data.order; saveOrder(); renderOrder(); message($('order-message'), ''); loadProducts();
    } catch (error) { if (generation === orderGeneration) message($('order-message'), error.message, 'error'); }
    finally { cancelling = false; setBusy($('cancel-order'), false); syncButtons(); queueOrderPoll(orderGeneration); }
  });
  async function copyCard(code, target) {
    if (!code) return;
    try { await copy(code); message(target, '已复制', 'success'); }
    catch (error) { message(target, error.message, 'error'); }
  }
  $('copy-card').addEventListener('click', () => copyCard(issuedCodes(current), $('order-message')));
  $('payment-qr').addEventListener('error', () => { if (current?.status === 'pending' && secondsLeft(current)) message($('order-message'), '收款码加载失败，请刷新', 'error'); });

  function stopLookupTimers() { clearTimeout(lookupTimer); clearInterval(lookupCountdown); lookupTimer = null; lookupCountdown = null; }
  function tickLookup() {
    for (const item of lookupDetails.values()) {
      const payable = secondsLeft(item.order) > 0;
      item.details.hidden = !payable; item.expired.hidden = payable;
      item.timer.textContent = paymentTime(item.order);
      item.status.replaceChildren(badge(item.order.status, statusLabel(item.order)));
    }
  }
  function renderLookup() {
    const opened = new Set([...lookupDetails.entries()].filter(([, value]) => value.details.open).map(([id]) => id));
    lookupDetails = new Map();
    $('lookup-results').replaceChildren();
    for (const order of lookupOrders) {
      const status = el('span', {}, badge(order.status, statusLabel(order)));
      const card = el('article', { className: 'order-card', 'data-order': order.id },
        el('div', { className: 'section-heading compact' }, el('h3', { text: orderName(order) }), status),
        el('p', { className: 'small muted', text: money(order.price_cents) + ' · ' + channel(order.channel) + ' · ' + date(order.created_at) }));
      const redemption = redemptionView(order);
      if (redemption) card.append(redemption);
      if (order.status === 'paid') {
        const copyMessage = el('div', { role: 'status', hidden: true });
        card.append(el('pre', { className: 'card-code', text: issuedCodes(order) || '卡密暂未显示，请刷新' }),
          el('button', { className: 'button primary tiny', type: 'button', text: copyLabel(order), disabled: !issuedCodes(order), onclick: () => copyCard(issuedCodes(order), copyMessage) }), copyMessage);
      } else if (order.status === 'pending') {
        const timer = el('p', { className: 'small muted', role: 'timer' });
        const details = el('details', { className: 'lookup-payment' }, el('summary', { text: '查看付款码' }),
          el('div', { className: 'payment-amount' }, el('span', { text: '应付金额（请按此金额付款）' }), el('strong', { text: money(order.price_cents) })),
          el('button', { type: 'button', className: 'qr-enlarge', 'data-qr-preview': 'lookup:' + order.id, 'data-qr-expires': order.expires_at, 'data-qr-amount': order.price_cents, 'aria-label': '放大' + channel(order.channel) + '收款二维码', 'aria-haspopup': 'dialog' },
            el('img', { className: 'qr-image', src: '/qr/' + encodeURIComponent(order.channel), alt: channel(order.channel) + '收款二维码' }),
            el('span', { className: 'qr-enlarge-hint', text: '点击放大' })),
          el('p', { className: 'payment-note', text: '请按应付金额付款，勿抹零；备注填写联系方式，付款后等待发卡' }), timer);
        details.open = opened.has(order.id);
        const expired = el('p', { className: 'small muted', text: '已超时，请勿付款；已付款请联系店主', hidden: true });
        card.append(details, expired); lookupDetails.set(order.id, { order, details, timer, expired, status });
      } else if (order.status === 'expired') card.append(el('p', { className: 'small muted', text: '已超时，请勿付款；已付款请联系店主' }));
      card.append(el('p', { className: 'small muted mono break-all', text: order.id }));
      $('lookup-results').append(card);
    }
    if (!lookupOrders.length) $('lookup-results').append(el('p', { className: 'empty-state', text: '没有找到订单' }));
    $('lookup-more').hidden = !lookupHasMore;
    $('lookup-refresh').hidden = !lookupContact && !lookupMine;
    clearInterval(lookupCountdown); lookupCountdown = null; tickLookup();
    if (lookupDetails.size && $('lookup-dialog').open) lookupCountdown = setInterval(tickLookup, 1000);
  }
  function queueLookup(generation) {
    clearTimeout(lookupTimer); lookupTimer = null;
    if (generation === lookupGeneration && $('lookup-dialog').open && (lookupOrders.some(order => order.status === 'pending' || redemptionActive(order)) || lookupRetryAfter > Date.now()))
      lookupTimer = setTimeout(() => fetchLookup(false, generation), Math.max(document.hidden ? 15000 : 5000, lookupPages * 5000, lookupRetryAfter - Date.now()));
  }
  async function fetchLookup(more = false, generation = lookupGeneration) {
    if ((!lookupContact && !lookupMine) || lookupTask === generation || !$('lookup-dialog').open || creating) return;
    if (lookupRetryAfter > Date.now()) { setBusy($('lookup-submit'), false); queueLookup(generation); syncButtons(); return; }
    clearTimeout(lookupTimer); lookupTimer = null;
    lookupTask = generation;
    $('lookup-more').disabled = true; $('lookup-refresh').disabled = true; syncButtons();
    const contact = lookupContact;
    const pageCount = more ? lookupPages + 1 : lookupPages;
    const oldestLoaded = lookupOrders.at(-1)?.id;
    let collected = [], hasMore = false, fetchedPages = 0;
    try {
      // Refresh the complete loaded prefix, so polling never drops older pages.
      for (let page = 0; page < pageCount || (oldestLoaded && hasMore && !collected.some(order => order.id === oldestLoaded)); page += 1) {
        const data = lookupMine ? await api('/api/sub2api/orders?offset=' + (page * 50), { headers: accountHeaders() }) : await api('/api/orders/lookup', { method: 'POST', body: { contact, offset: page * 50 } });
        if (generation !== lookupGeneration) return;
        collected.push(...(data.orders || [])); fetchedPages += 1; hasMore = Boolean(data.has_more);
        if (!hasMore) break;
      }
      if (generation !== lookupGeneration) return;
      lookupOrders = [...new Map(collected.map(order => [order.id, order])).values()];
      lookupPages = fetchedPages; lookupHasMore = hasMore;
      lookupRetryAfter = 0;
      renderLookup(); message($('lookup-message'), '');
    } catch (error) {
      if (generation === lookupGeneration) {
        if (error.status === 429) lookupRetryAfter = Date.now() + 30000;
        if (lookupMine && error.status === 401) bridge?.invalidate(error.message);
        message($('lookup-message'), error.status === 429 ? '查询较频繁，稍后自动重试' : error.message, 'error');
      }
    }
    finally {
      if (lookupTask === generation) lookupTask = null;
      if (generation === lookupGeneration) {
        $('lookup-more').disabled = false; $('lookup-refresh').disabled = false;
        setBusy($('lookup-submit'), false); syncButtons(); queueLookup(generation);
      }
    }
  }
  $('open-lookup').addEventListener('click', () => {
    if (creating) return;
    if (lookupMine) { lookupGeneration++; stopLookupTimers(); lookupTask = null; lookupOrders = []; lookupPages = 1; lookupHasMore = false; lookupContact = ''; }
    lookupMine = false; $('lookup-form').hidden = false; $('lookup-title').textContent = '查订单';
    if (!$('lookup-contact').value && current?.contact) $('lookup-contact').value = current.contact;
    showDialog('lookup-dialog');
    renderLookup();
    if (lookupContact) fetchLookup();
  });
  $('lookup-form').addEventListener('submit', async event => {
    event.preventDefault(); if (creating) return;
    const contact = $('lookup-contact').value.trim();
    if (contact.length < 3 || contact.length > 128) { message($('lookup-message'), '请填写下单时的联系方式', 'error'); return; }
    const generation = ++lookupGeneration; stopLookupTimers(); lookupTask = null;
    lookupMine = false; lookupContact = contact; lookupPages = 1; lookupOrders = []; lookupHasMore = false; lookupDetails = new Map();
    $('lookup-results').replaceChildren(); $('lookup-more').hidden = true; $('lookup-refresh').hidden = true;
    setBusy($('lookup-submit'), true, '查询中…'); message($('lookup-message'), '');
    await fetchLookup(false, generation);
  });
  $('lookup-more').addEventListener('click', () => lookupHasMore ? fetchLookup(true) : undefined);
  $('lookup-refresh').addEventListener('click', () => fetchLookup());
  $('close-lookup').addEventListener('click', () => $('lookup-dialog').close());
  $('my-orders').addEventListener('click', async () => {
    if (creating || bridge?.status !== 'ready') return;
    lookupGeneration++; stopLookupTimers(); lookupTask = null; lookupMine = true; lookupContact = '';
    lookupOrders = []; lookupPages = 1; lookupHasMore = false; lookupRetryAfter = 0;
    $('lookup-form').hidden = true; $('lookup-title').textContent = '我的订单';
    showDialog('lookup-dialog'); renderLookup(); await fetchLookup();
  });
  $('sub2api-guest').addEventListener('click', () => { if (!creating) bridge?.guest(); });
  bridge?.onChange(() => {
    if (lookupMine && bridge.status !== 'ready') {
      lookupGeneration++; stopLookupTimers(); lookupTask = null; lookupMine = false; lookupContact = '';
      lookupOrders = []; lookupHasMore = false; renderLookup();
      $('lookup-form').hidden = false; $('lookup-title').textContent = '查订单';
    }
    syncButtons();
  });
  $('lookup-dialog').addEventListener('close', () => {
    lookupGeneration += 1; lookupTask = null; stopLookupTimers();
    setBusy($('lookup-submit'), false); $('lookup-more').disabled = false; $('lookup-refresh').disabled = false; syncButtons();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { tickOrder(); tickLookup(); if (current?.status === 'pending' || redemptionActive(current)) refreshOrder(); if ($('lookup-dialog').open) fetchLookup(); }
  });
  loadProducts();
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (saved?.id && saved?.token) { current = { id: saved.id, status: 'pending' }; token = saved.token; restoring = true; refreshOrder(); }
  } catch (_) { /* Invalid local storage must not block shopping. */ }
})();


