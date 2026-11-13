'use strict';
(() => {
  const { el, api, money, date, channel, message, copy, badge, setBusy } = App;
  const $ = id => document.getElementById(id);
  let csrf = '', overview = null, products = [], activeTab = 'orders', refreshTimer = null;
  let orders = [], orderQuery = '', orderStatus = 'all', ordersGeneration = 0, ordersLoading = false;
  let inventoryProduct = null, cards = [], cardsGeneration = 0, cardsLoading = false, settingsDirty = false, settingsRevision = 0;
  let productGeneration = 0, editCardGeneration = 0, inventoryEpoch = 0, authGeneration = 0;
  let passwordRequest = null;
  let integrationDirty = false, integrationEdit = 0, integrationRevision = null, integrationBusy = false, integrationRead = 0;
  const actingOrders = new Set(), actingCards = new Set();
  const notice = (text, type = 'info') => message($('admin-message'), text, type);
  async function adminApi(path, options = {}) {
    const requestSession = csrf, requestGeneration = authGeneration;
    try { return await api(path, { ...options, headers: { ...options.headers, 'X-CSRF-Token': csrf } }); }
    catch (error) { if (error.status === 401 && csrf === requestSession && requestGeneration === authGeneration) showLogin(); throw error; }
  }
  function clearPasswordForm() {
    passwordRequest = null;
    ['current-password', 'new-password', 'confirm-password'].forEach(id => { $(id).value = ''; });
    message($('password-message'), ''); setBusy($('change-password'), false);
  }
  function showLogin() {
    authGeneration++;
    csrf = ''; clearTimeout(refreshTimer); ordersGeneration++; cardsGeneration++; ordersLoading = false; cardsLoading = false;
    orders = []; cards = []; products = []; overview = null; inventoryProduct = null; settingsDirty = false;
    integrationDirty = false; integrationRevision = null; integrationEdit++; integrationRead++; integrationBusy = false;
    $('sub2api-url').value = ''; $('sub2api-enabled').checked = false; $('sub2api-relay').checked = false;
    $('sub2api-diagnostics').textContent = '读取配置中…'; message($('sub2api-message'), '');
    setBusy($('sub2api-save'), false); $('sub2api-save').disabled = true;
    setBusy($('sub2api-check'), false); setBusy($('sub2api-reload'), false);
    $('admin-view').hidden = true; $('logout').hidden = true; $('login-view').hidden = false;
    ['all-orders', 'admin-products', 'inventory-cards', 'qr-settings'].forEach(id => $(id).replaceChildren());
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    $('card-codes').value = ''; $('edit-card-code').value = '';
    $('admin-password').value = ''; clearPasswordForm(); setBusy($('logout'), false);
  }
  function scheduleRefresh() {
    clearTimeout(refreshTimer); if (!csrf) return;
    refreshTimer = setTimeout(async () => {
      try { if (!document.hidden && !passwordRequest) { if (activeTab === 'orders' && !ordersLoading && !actingOrders.size) await loadOrders(false, true); else if (activeTab === 'settings') await loadIntegration(); } }
      catch (error) { if (csrf) notice(error.message, 'error'); }
      finally { scheduleRefresh(); }
    }, 8000);
  }
  async function showAdmin(token) {
    clearPasswordForm(); setBusy($('logout'), false);
    csrf = token; $('login-view').hidden = true; $('admin-view').hidden = false; $('logout').hidden = false;
    orderQuery = ''; orderStatus = 'all'; $('order-query').value = ''; $('order-filter').value = 'all';
    setTab('orders');
    scheduleRefresh();
    const results = await Promise.allSettled([loadOrders(), loadProducts(), loadOverview(true)]);
    const failure = results.find(result => result.status === 'rejected');
    if (failure && csrf) notice(failure.reason.message, 'error');
  }
  function setTab(name) {
    activeTab = name; document.querySelectorAll('.tab-content').forEach(node => { node.hidden = node.id !== 'tab-' + name; });
    document.querySelectorAll('[data-tab]').forEach(button => {
      const active = button.dataset.tab === name; button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
  }
  document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', async () => {
    setTab(button.dataset.tab);
    try { if (activeTab === 'orders') await loadOrders(false, true); else if (activeTab === 'products') await loadProducts(); else await loadOverview(true); }
    catch (error) { notice(error.message, 'error'); }
  }));
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  $('product-dialog').addEventListener('close', () => { productGeneration++; });
  $('edit-card-dialog').addEventListener('close', () => { editCardGeneration++; });
  $('inventory-dialog').addEventListener('close', () => { inventoryEpoch++; cardsGeneration++; cardsLoading = false; cards = []; inventoryProduct = null; $('inventory-cards').replaceChildren(); $('card-codes').value = ''; });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter; if (button.disabled) return; setBusy(button, true);
    authGeneration++;
    message($('login-message'), '');
    try { const result = await api('/api/admin/login', { method: 'POST', body: { password: $('admin-password').value } }); $('admin-password').value = ''; await showAdmin(result.csrf_token); }
    catch (error) { message($('login-message'), error.message, 'error'); if (csrf) notice(error.message, 'error'); }
    finally { setBusy(button, false); }
  });
  $('logout').addEventListener('click', async () => {
    const button = $('logout'); if (button.disabled) return;
    const generation = authGeneration, session = csrf; clearPasswordForm(); setBusy(button, true);
    try { await adminApi('/api/admin/logout', { method: 'POST' }); if (generation === authGeneration && session === csrf) showLogin(); }
    catch (error) { if (generation === authGeneration && session === csrf) notice(error.message, 'error'); }
    finally { if (generation === authGeneration && session === csrf) setBusy(button, false); }
  });
  function meta(items) { return el('dl', { className: 'order-meta' }, items.map(([label, value]) => el('div', {}, el('dt', { text: label }), el('dd', { text: value })))); }
  const orderQuantity = order => order.quantity ?? 1;
  const orderCodes = order => Array.isArray(order.card_codes) ? order.card_codes.join('\n') : order.card_code || '';
  async function copyCode(code, target) { try { await copy(code); message(target, '已复制', 'success'); } catch (error) { message(target, error.message, 'error'); } }
  function renderOrders() {
    const expanded = new Set([...$('all-orders').querySelectorAll('[data-order-id]')].filter(node => node.querySelector('details')?.open).map(node => node.dataset.orderId));
    $('all-orders').replaceChildren(...orders.map(order => {
      const controls = el('div', { className: 'row-actions' });
      if (['pending', 'expired'].includes(order.status)) controls.append(el('button', { type: 'button', className: 'button primary', text: '发卡', disabled: actingOrders.has(order.id), onclick: () => actOnOrder(order, 'confirm') }));
      if (['pending', 'expired'].includes(order.status)) controls.append(el('button', { type: 'button', className: 'button text-button', text: '取消订单', disabled: actingOrders.has(order.id), onclick: () => actOnOrder(order, 'cancel') }));
      if (order.redemption?.can_retry) controls.append(el('button', { type: 'button', className: 'button secondary tiny', text: '重试兑换', disabled: actingOrders.has(order.id), onclick: () => actOnOrder(order, 'redeem-retry') }));
      const node = el('article', { className: 'order-card', 'data-order-id': order.id },
        el('div', { className: 'section-heading' }, el('h3', { text: order.product_name }), badge(order.status, order.status === 'pending' ? '待发卡' : undefined)),
        meta([['联系方式', order.contact || '旧订单未留联系方式'], ['数量 / 单价', orderQuantity(order) + ' 张 · ' + money(order.unit_price_cents ?? order.price_cents)], ['应付金额 / 支付', money(order.price_cents) + ' · ' + channel(order.channel)], ['下单时间', date(order.created_at)], ['订单号', order.id]]));
      if (order.payment_adjustment_cents > 0) node.append(el('p', { className: 'small muted', text: '商品合计 ' + money(order.base_price_cents) + ' ＋ 金额区分加价 ' + money(order.payment_adjustment_cents) }));
      const codes = order.status === 'paid' ? orderCodes(order) : '';
      if (typeof Sub2api !== 'undefined') { const redemption = Sub2api.render(order); if (redemption) node.append(redemption); }
      if (codes) node.append(el('details', { open: expanded.has(order.id) }, el('summary', { text: orderQuantity(order) > 1 ? '查看卡密（' + orderQuantity(order) + ' 张）' : '查看卡密' }), el('pre', { className: 'card-code', text: codes }), el('button', { type: 'button', className: 'button secondary tiny', text: orderQuantity(order) > 1 ? '复制全部' : '复制', onclick: () => copyCode(codes, $('admin-message')) })));
      if (controls.childElementCount) node.append(controls);
      return node;
    }));
    if (!orders.length) $('all-orders').append(el('div', { className: 'empty-state', text: '暂无订单' }));
  }
  async function loadOrders(append = false, preserve = false) {
    if (append && ordersLoading) return;
    const generation = ++ordersGeneration, session = csrf, query = orderQuery, status = orderStatus; ordersLoading = true; $('more-orders').disabled = true;
    const count = preserve ? Math.max(50, orders.length) : 50; let next = append ? [...orders] : [], result;
    try {
      do {
        const params = new URLSearchParams({ query, status, offset: String(next.length) });
        result = await adminApi('/api/admin/orders?' + params);
        if (generation !== ordersGeneration || session !== csrf) return;
        const ids = new Set(next.map(order => order.id)); next.push(...result.orders.filter(order => !ids.has(order.id)));
      } while (!append && preserve && next.length < count && result.has_more && result.orders.length);
      orders = next; renderOrders(); $('order-count').textContent = String(result.total); $('more-orders').hidden = !result.has_more;
    } finally { if (generation === ordersGeneration) { ordersLoading = false; $('more-orders').disabled = false; } }
  }
  async function actOnOrder(order, action) {
    if (actingOrders.has(order.id)) return;
    if (action === 'cancel' && !confirm('确认未收到付款？取消订单将释放全部预留库存。')) return;
    if (action === 'redeem-retry' && !confirm('确认已检查兑换失败原因？将重试未成功项，并解除本实例的接口兼容暂停。')) return;
    actingOrders.add(order.id); renderOrders(); const session = csrf;
    try {
      const result = await adminApi('/api/admin/orders/' + encodeURIComponent(order.id) + '/' + action, { method: 'POST', body: {} });
      if (session !== csrf) return;
      orders = orders.map(item => item.id === order.id ? result.order : item); renderOrders();
      notice(action === 'confirm' ? '已发卡' : action === 'redeem-retry' ? '已安排兑换重试' : '订单已取消，库存已释放', 'success'); await Promise.all([loadOrders(false, true), loadProducts()]);
    } catch (error) { if (session === csrf) notice(error.message, 'error'); }
    finally { actingOrders.delete(order.id); if (session === csrf) renderOrders(); }
  }
  $('order-search').addEventListener('submit', async event => { event.preventDefault(); orderQuery = $('order-query').value.trim(); try { await loadOrders(); } catch (error) { notice(error.message, 'error'); } });
  $('order-filter').addEventListener('change', async () => { orderStatus = $('order-filter').value; try { await loadOrders(); } catch (error) { notice(error.message, 'error'); } });
  $('more-orders').addEventListener('click', async () => { try { await loadOrders(true); } catch (error) { notice(error.message, 'error'); } });
  $('refresh-admin').addEventListener('click', async () => { const button = $('refresh-admin'); setBusy(button, true); try { await loadOrders(false, true); notice(''); } catch (error) { notice(error.message, 'error'); } finally { setBusy(button, false); } });
  async function loadProducts() { const session = csrf; const result = await adminApi('/api/admin/products'); if (session !== csrf) return; products = result.products; renderProducts(); }
  function renderProducts() {
    $('admin-products').replaceChildren(...products.map(product => {
      const toggle = el('button', { type: 'button', className: 'button tiny text-button', text: product.active ? '下架' : '上架' });
      toggle.addEventListener('click', async () => { setBusy(toggle, true); try { await adminApi('/api/admin/products/' + product.id, { method: 'PATCH', body: { active: !product.active } }); await loadProducts(); } catch (error) { notice(error.message, 'error'); } finally { setBusy(toggle, false); } });
      return el('article', { className: 'panel admin-product' }, el('div', { className: 'section-heading' }, el('h3', { text: product.name }), badge(product.active ? 'paid' : 'cancelled', product.active ? '上架中' : '已下架')),
        product.description ? el('p', { className: 'product-description', text: product.description }) : null,
        el('div', { className: 'product-figures' }, el('strong', { text: money(product.price_cents) }), el('span', { className: 'muted small', text: '可用 ' + product.stock + ' 张' })),
        el('div', { className: 'row-actions' }, el('button', { type: 'button', className: 'button tiny primary', text: '库存', onclick: () => openInventory(product) }), el('button', { type: 'button', className: 'button tiny secondary', text: '编辑', onclick: () => editProduct(product) }), toggle));
    }));
    if (!products.length) $('admin-products').append(el('div', { className: 'empty-state', text: '暂无商品，点击新增商品' }));
  }
  function editProduct(product = null) {
    productGeneration++; setBusy($('product-form').querySelector('[type=submit]'), false);
    $('product-form').reset(); $('product-title').textContent = product ? '编辑商品' : '新增商品';
    $('product-id').value = product?.id || ''; $('product-name').value = product?.name || '';
    $('product-price').value = product ? (product.price_cents / 100).toFixed(2) : ''; $('product-description').value = product?.description || '';
    $('product-active').checked = product?.active !== false; message($('product-message'), ''); $('product-dialog').showModal();
    $('product-sub2api').checked = Boolean(product?.sub2api_redeem);
  }
  $('add-product').addEventListener('click', () => editProduct());
  $('product-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter; if (button.disabled) return; const amount = $('product-price').value.trim();
    if (!/^[0-9]+([.][0-9]{1,2})?$/.test(amount)) { message($('product-message'), '请输入正确的价格', 'error'); return; }
    const pieces = amount.split('.'), cents = Number(pieces[0]) * 100 + Number((pieces[1] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(cents) || cents < 1 || cents > 1000000) { message($('product-message'), '价格须为 0.01–10000 元', 'error'); return; }
    const id = $('product-id').value, generation = productGeneration, session = csrf, body = { name: $('product-name').value.trim(), description: $('product-description').value.trim(), price_cents: cents, active: $('product-active').checked };
    body.sub2api_redeem = $('product-sub2api').checked;
    setBusy(button, true);
    try { await adminApi('/api/admin/products' + (id ? '/' + id : ''), { method: id ? 'PATCH' : 'POST', body }); if (session !== csrf) return; if (generation === productGeneration) { setBusy(button, false); $('product-dialog').close(); notice('商品已保存', 'success'); } await loadProducts(); }
    catch (error) { if (generation === productGeneration && session === csrf) message($('product-message'), error.message, 'error'); } finally { if (generation === productGeneration) setBusy(button, false); }
  });
  async function openInventory(product) {
    inventoryEpoch++; setBusy($('cards-form').querySelector('[type=submit]'), false);
    inventoryProduct = product; cards = []; $('inventory-title').textContent = product.name + ' · 库存'; $('card-filter').value = 'all'; $('card-codes').value = '';
    $('import-section').open = product.stock === 0; $('inventory-cards').replaceChildren(); message($('inventory-message'), ''); $('inventory-dialog').showModal();
    try { await loadCards(); } catch (error) { message($('inventory-message'), error.message, 'error'); }
  }
  async function loadCards(append = false) {
    if (!inventoryProduct || (append && cardsLoading)) return;
    const productId = inventoryProduct.id, generation = ++cardsGeneration, session = csrf; cardsLoading = true; $('more-cards').disabled = true;
    try {
      const params = new URLSearchParams({ status: $('card-filter').value, offset: String(append ? cards.length : 0) });
      const result = await adminApi('/api/admin/products/' + productId + '/cards?' + params);
      if (generation !== cardsGeneration || productId !== inventoryProduct?.id || session !== csrf) return;
      const ids = new Set((append ? cards : []).map(card => card.id)); cards = (append ? cards : []).concat(result.cards.filter(card => !ids.has(card.id)));
      renderCards(); $('card-count').textContent = result.total + ' 张'; $('more-cards').hidden = !result.has_more;
    } finally { if (generation === cardsGeneration) { cardsLoading = false; $('more-cards').disabled = false; } }
  }
  function renderCards() {
    const names = { available: '可用', reserved: '预留', sold: '已售' };
    $('inventory-cards').replaceChildren(...cards.map(card => {
      const controls = el('div', { className: 'row-actions' }, el('button', { type: 'button', className: 'button tiny secondary', text: '复制', onclick: () => copyCode(card.code, $('inventory-message')) }));
      if (card.status === 'available') {
        controls.append(el('button', { type: 'button', className: 'button tiny secondary', text: '修改', disabled: actingCards.has(card.id), onclick: () => {
          editCardGeneration++; setBusy($('edit-card-form').querySelector('[type=submit]'), false);
          $('edit-card-id').value = card.id; $('edit-card-code').value = card.code; message($('edit-card-message'), ''); $('edit-card-dialog').showModal();
        } }), el('button', { type: 'button', className: 'button tiny text-button danger-text', text: '删除', disabled: actingCards.has(card.id), onclick: () => deleteCard(card) }));
      }
      return el('article', { className: 'inventory-item', 'data-card-id': card.id }, el('div', { className: 'section-heading' }, el('span', { className: 'muted small', text: '#' + card.id }), badge(card.status === 'available' ? 'paid' : 'pending', names[card.status])), el('pre', { className: 'card-code', text: card.code }), controls);
    }));
    if (!cards.length) $('inventory-cards').append(el('div', { className: 'empty-state', text: '暂无卡密' }));
  }
  async function deleteCard(card) {
    if (actingCards.has(card.id) || !confirm('删除这张可用卡密？')) return;
    const productId = inventoryProduct?.id; actingCards.add(card.id); renderCards();
    try { await adminApi('/api/admin/cards/' + card.id, { method: 'DELETE' }); if (productId === inventoryProduct?.id) { message($('inventory-message'), '已删除', 'success'); await loadCards(); } await loadProducts(); }
    catch (error) { if (productId === inventoryProduct?.id) message($('inventory-message'), error.message, 'error'); }
    finally { actingCards.delete(card.id); if (productId === inventoryProduct?.id) renderCards(); }
  }
  $('card-filter').addEventListener('change', async () => { try { await loadCards(); } catch (error) { message($('inventory-message'), error.message, 'error'); } });
  $('more-cards').addEventListener('click', async () => { try { await loadCards(true); } catch (error) { message($('inventory-message'), error.message, 'error'); } });
  $('cards-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter, productId = inventoryProduct?.id, epoch = inventoryEpoch, session = csrf; if (!productId || button.disabled) return; setBusy(button, true);
    try { const result = await adminApi('/api/admin/products/' + productId + '/cards', { method: 'POST', body: { codes: $('card-codes').value } });
      if (session !== csrf) return; if (epoch === inventoryEpoch && productId === inventoryProduct?.id) { $('card-codes').value = ''; message($('inventory-message'), '已导入 ' + result.inserted + ' 张，重复 ' + result.duplicates + ' 张', 'success'); await loadCards(); } await loadProducts();
    } catch (error) { if (epoch === inventoryEpoch && session === csrf) message($('inventory-message'), error.message, 'error'); } finally { if (epoch === inventoryEpoch) setBusy(button, false); }
  });
  $('edit-card-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter, id = $('edit-card-id').value, productId = inventoryProduct?.id, generation = editCardGeneration, epoch = inventoryEpoch, session = csrf; if (button.disabled) return; setBusy(button, true);
    try { await adminApi('/api/admin/cards/' + encodeURIComponent(id), { method: 'PATCH', body: { code: $('edit-card-code').value } }); if (session !== csrf) return; if (generation === editCardGeneration) { setBusy(button, false); $('edit-card-dialog').close(); } if (epoch === inventoryEpoch && productId === inventoryProduct?.id) { message($('inventory-message'), '卡密已更新', 'success'); await loadCards(); } }
    catch (error) { if (generation === editCardGeneration && session === csrf) message($('edit-card-message'), error.message, 'error'); } finally { if (generation === editCardGeneration) setBusy(button, false); }
  });
  async function loadOverview(fillSettings = false) {
    const session = csrf, revision = settingsRevision, edit = integrationEdit, generation = authGeneration, read = ++integrationRead, result = await adminApi('/api/admin/overview'); if (session !== csrf || generation !== authGeneration) return; overview = result;
    const integration = result.sub2api;
    if (integration && read === integrationRead) renderIntegration(integration, edit);
    if (fillSettings && !settingsDirty && revision === settingsRevision) { $('shop-name').value = result.settings.shop_name || ''; $('support-notice').value = result.settings.support_notice || ''; }
    renderQr();
  }
  function renderIntegration(config, edit) {
    const stale = !config.worker_seen || Date.now() / 1000 - config.worker_seen > 180;
    const status = config.paused ? '对接已暂停：' + config.message : config.enabled ? '自动兑换已启用' : '自动兑换未启用';
    const worker = stale ? '后台兑换服务未就绪，请检查服务' : config.worker_revision === config.revision ? '后台已应用当前配置' : '等待后台应用新配置';
    $('sub2api-diagnostics').textContent = status + ' · ' + worker + (config.key_configured ? ' · 加密密钥已配置' : '');
    if (!integrationDirty && edit === integrationEdit && !integrationBusy) {
      $('sub2api-url').value = config.base_url || ''; $('sub2api-enabled').checked = config.enabled; $('sub2api-relay').checked = config.relay_confirmed;
      integrationRevision = config.revision; $('sub2api-save').disabled = false;
    }
  }
  async function loadIntegration(reset = false) {
    const generation = authGeneration, session = csrf, edit = integrationEdit, read = ++integrationRead;
    const result = await adminApi('/api/admin/sub2api');
    if (generation !== authGeneration || session !== csrf || read !== integrationRead) return;
    if (reset && edit === integrationEdit) integrationDirty = false;
    renderIntegration(result.sub2api, edit);
  }
  $('sub2api-form').addEventListener('input', event => {
    integrationDirty = true; integrationEdit++;
    if (event.target === $('sub2api-url')) $('sub2api-relay').checked = false;
    message($('sub2api-message'), '有未保存修改');
  });
  $('sub2api-form').addEventListener('submit', async event => {
    event.preventDefault(); if (integrationBusy || integrationRevision === null || !csrf) return;
    const generation = authGeneration, session = csrf, edit = integrationEdit, button = $('sub2api-save');
    const body = { base_url: $('sub2api-url').value.trim(), enabled: $('sub2api-enabled').checked, relay_confirmed: $('sub2api-relay').checked, revision: integrationRevision };
    integrationBusy = true; integrationRead++; setBusy(button, true);
    try {
      const result = await adminApi('/api/admin/sub2api', { method: 'POST', body });
      if (generation !== authGeneration || session !== csrf) return;
      integrationRead++; integrationRevision = result.sub2api.revision; integrationDirty = edit !== integrationEdit; integrationBusy = false;
      renderIntegration(result.sub2api, edit);
      message($('sub2api-message'), integrationDirty ? '已保存提交的配置，仍有未保存修改' : '配置已保存，无需重启服务', 'success');
    } catch (error) { if (generation === authGeneration && session === csrf) message($('sub2api-message'), error.message, 'error'); }
    finally { if (generation === authGeneration && session === csrf) { integrationBusy = false; setBusy(button, false); } }
  });
  $('sub2api-check').addEventListener('click', async () => {
    const button = $('sub2api-check'); if (button.disabled || !csrf) return;
    const generation = authGeneration, session = csrf, edit = integrationEdit;
    setBusy(button, true); message($('sub2api-message'), '正在检查连接…');
    try {
      const result = await adminApi('/api/admin/sub2api/check', { method: 'POST', body: { base_url: $('sub2api-url').value.trim() } });
      if (generation === authGeneration && session === csrf && edit === integrationEdit) message($('sub2api-message'), result.site_name + '：' + result.message, 'success');
    } catch (error) { if (generation === authGeneration && session === csrf && edit === integrationEdit) message($('sub2api-message'), error.message, 'error'); }
    finally { if (generation === authGeneration && session === csrf) setBusy(button, false); }
  });
  $('sub2api-reload').addEventListener('click', async () => {
    const button = $('sub2api-reload'); if (button.disabled || integrationBusy || !csrf) return;
    const generation = authGeneration, session = csrf; setBusy(button, true);
    try { await loadIntegration(true); if (generation === authGeneration && session === csrf) message($('sub2api-message'), integrationDirty ? '已读取配置，保留读取期间的新修改' : '已重新加载配置'); }
    catch (error) { if (generation === authGeneration && session === csrf) message($('sub2api-message'), error.message, 'error'); }
    finally { if (generation === authGeneration && session === csrf) setBusy(button, false); }
  });
  function renderQr() {
    $('qr-settings').replaceChildren(...(overview?.channels || []).map(item => {
      const input = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', required: true, 'aria-label': '上传' + channel(item.id) + '收款码' });
      const button = el('button', { className: 'button secondary tiny', type: 'submit', text: item.enabled ? '更换' : '上传' });
      const form = el('form', { className: 'qr-upload' }, input, button), status = el('div', { role: 'status', hidden: true });
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (button.disabled || !input.files[0]) return; const file = input.files[0];
        if (file.size > 2 * 1024 * 1024) { message(status, '图片不能超过 2MB', 'error'); return; }
        const body = new FormData(); body.append('file', file); setBusy(button, true);
        try { await adminApi('/api/admin/qr/' + item.id, { method: 'POST', body }); await loadOverview(); notice('收款码已更新', 'success'); }
        catch (error) { message(status, error.message, 'error'); } finally { setBusy(button, false); }
      });
      const preview = item.enabled ? el('button', { type: 'button', className: 'qr-enlarge qr-enlarge-admin', 'data-qr-preview': 'admin:' + item.id, 'aria-label': '放大' + channel(item.id) + '收款码', 'aria-haspopup': 'dialog' },
        el('img', { className: 'qr-preview', src: '/qr/' + item.id + '?v=' + Date.now(), alt: channel(item.id) + '收款码' }),
        el('span', { className: 'qr-enlarge-hint', text: '点击放大' })) : el('div', { className: 'qr-placeholder', text: '未上传' });
      return el('section', { className: 'qr-setting' }, el('h3', { text: channel(item.id) }), el('div', { className: 'qr-setting-body' }, preview, form), status);
    }));
  }
  $('settings-form').addEventListener('input', () => { settingsDirty = true; settingsRevision++; });
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter; if (button.disabled) return; const revision = settingsRevision; setBusy(button, true);
    try { await adminApi('/api/admin/settings', { method: 'POST', body: { shop_name: $('shop-name').value.trim(), support_notice: $('support-notice').value.trim() } }); settingsDirty = revision !== settingsRevision; notice(settingsDirty ? '已保存，仍有未保存修改' : '设置已保存', 'success'); }
    catch (error) { notice(error.message, 'error'); } finally { setBusy(button, false); }
  });
  $('password-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('change-password');
    if (!csrf || passwordRequest || button.disabled) return;
    const body = { current_password: $('current-password').value, new_password: $('new-password').value, confirm_password: $('confirm-password').value };
    if (!body.current_password || body.current_password.length > 256) { message($('password-message'), '请填写当前密码', 'error'); return; }
    if (body.new_password.length < 12 || body.new_password.length > 256 || !body.new_password.trim()) { message($('password-message'), '新密码须为 12–256 个字符', 'error'); return; }
    if (body.new_password !== body.confirm_password) { message($('password-message'), '两次密码不一致', 'error'); return; }
    const request = { generation: authGeneration, session: csrf }; passwordRequest = request;
    const isCurrent = () => passwordRequest === request && authGeneration === request.generation && csrf === request.session;
    message($('password-message'), ''); setBusy(button, true);
    try {
      const result = await adminApi('/api/admin/password', { method: 'POST', body });
      if (!isCurrent()) return;
      if (!result.ok) throw new Error('Password change failed');
      showLogin(); message($('login-message'), '密码已更新，请重新登录', 'success');
    } catch (error) {
      if (isCurrent()) message($('password-message'), error.status === 429 ? '操作频繁，请稍后重试' : [400, 403].includes(error.status) ? '修改失败，请核对当前密码后重试' : '修改失败，请稍后重试', 'error');
    } finally { if (isCurrent()) { passwordRequest = null; setBusy(button, false); } }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && csrf) { scheduleRefresh(); if (activeTab === 'orders' && !ordersLoading) loadOrders(false, true).catch(error => notice(error.message, 'error')); } });
  (async () => { const generation = authGeneration; try { const session = await api('/api/admin/session'); if (generation !== authGeneration) return; if (session.authenticated) await showAdmin(session.csrf_token); else showLogin(); } catch (error) { if (generation !== authGeneration) return; showLogin(); message($('login-message'), error.message, 'error'); } })();
})();
