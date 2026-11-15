// Session and same-document integration for the original console only.
'use strict';
(() => {
  if (document.body.dataset.consoleMode !== 'protected') return;
  const $ = selector => document.querySelector(selector);
  let csrf = '', sessionPromise = null, catalogPromise = null, expired = false;
  let activeView = 'run', activeHash = '', routeReady = false;
  const poolViews = new Set(['google', 'mailboxes', 'cards', 'settings']);
  const originalViews = new Set(['run', 'assets', 'network', 'env', 'k12', 'plus', 'embed']);
  const account = document.createElement('div'); account.className = 'console-account';
  const actor = document.createElement('span'); actor.id = 'console-actor'; actor.textContent = '正在核验会话';
  const logout = document.createElement('button'); logout.id = 'console-logout'; logout.type = 'button'; logout.textContent = '退出登录'; logout.disabled = true;
  const login = document.createElement('a'); login.id = 'console-login'; login.href = '/login'; login.textContent = '重新登录'; login.hidden = true;
  account.append(actor, logout, login); $('.top-actions').prepend(account);
  const message = document.createElement('p'); message.id = 'console-message'; message.className = 'console-status'; message.setAttribute('role', 'status'); message.hidden = true;
  $('.content').prepend(message);
  const resourceViews = ['mailboxes','cards','assets','network'];
  const resourceEntry = $('.sidebar-tools [data-view="mailpool"]');
  resourceEntry.dataset.view = 'resources'; resourceEntry.lastElementChild.textContent = '资源管理';
  resourceEntry.querySelector('.nav-icon').textContent = 'R';
  for (const view of ['assets','network']) $('.sidebar-tools [data-view="' + view + '"]').remove();
  const resourceHeader = document.createElement('section'); resourceHeader.id = 'resources-header'; resourceHeader.hidden = true;
  const resourceTitle = document.createElement('h1'); resourceTitle.textContent = '资源管理';
  const resourceTabs = document.createElement('div'); resourceTabs.className = 'resource-tabs'; resourceTabs.setAttribute('role','tablist'); resourceTabs.setAttribute('aria-label','资源分类');
  const resourcePanels = [['mailboxes','邮箱池','mailbox-card'],['cards','信用卡池','card-pool'],['assets','资产 API','view-assets'],['network','网络出口','view-network']];
  resourceHeader.append(resourceTitle,resourceTabs); $('.content').prepend(resourceHeader);
  for (const item of resourcePanels) {
    const [view,label] = item;
    if (!$('#' + item[2])) {
      item[2] = 'resources-unavailable-' + view;
      const unavailable = document.createElement('p'); unavailable.id = item[2]; unavailable.textContent = label + '组件尚未加载。'; resourceHeader.append(unavailable);
    }
    const tab = document.createElement('button'); tab.type = 'button'; tab.id = 'resources-tab-' + view; tab.textContent = label;
    tab.setAttribute('role','tab'); tab.setAttribute('aria-controls',item[2]); tab.setAttribute('aria-selected','false'); tab.tabIndex = -1;
    const panel = $('#' + item[2]); panel.setAttribute('role','tabpanel'); panel.setAttribute('aria-labelledby',tab.id);
    tab.addEventListener('click',() => window.showView(view));
    tab.addEventListener('keydown',async event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const index = resourceViews.indexOf(view), length = resourceViews.length;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + length) % length;
      const target = resourceViews[next];
      if (await window.showView(target)) $('#resources-tab-' + target).focus();
    });
    resourceTabs.append(tab);
  }
  function showResources(view) {
    resourceHeader.hidden = !resourceViews.includes(view);
    for (const [key,,panelId] of resourcePanels) {
      const selected = key === view, tab = $('#resources-tab-' + key);
      tab.setAttribute('aria-selected',String(selected)); tab.tabIndex = selected ? 0 : -1;
      $('#' + panelId).hidden = !selected;
    }
  }
  function navigationView(view) {
    return resourceViews.includes(view) ? 'resources' : view === 'env' ? 'settings' : view;
  }

  // One settings entry, preserving the original forms, routes and save boundaries.
  const settingsEntry = $('.sidebar-tools [data-view="env"]');
  settingsEntry.dataset.view = 'settings'; settingsEntry.lastElementChild.textContent = '系统配置';
  const settingsHeader = document.createElement('section'); settingsHeader.id = 'settings-header'; settingsHeader.hidden = true;
  const settingsTitle = document.createElement('h1'); settingsTitle.textContent = '系统配置';
  const settingsTabs = document.createElement('div'); settingsTabs.className = 'settings-tabs'; settingsTabs.setAttribute('role','tablist'); settingsTabs.setAttribute('aria-label','系统配置分类');
  const settingsPanels = [['env','运行环境','view-env'],['google','Google 开通配置',$('#global-config') ? 'global-config' : 'settings-unavailable']];
  settingsHeader.append(settingsTitle,settingsTabs); $('.content').prepend(settingsHeader);
  if (!$('#global-config')) {
    const unavailable = document.createElement('p'); unavailable.id = 'settings-unavailable'; unavailable.textContent = 'Google 开通配置组件尚未加载。'; settingsHeader.append(unavailable);
  } else $('#global-config h2').textContent = 'Google 开通配置';
  $('#view-env h2').textContent = '运行环境 (.env)';
  $('#view-env .hint').textContent = '运行环境配置保存在本机 .env；当前后端尚未接入，未读取原配置，也不能在此保存。';
  for (const [key,label,panelId] of settingsPanels) {
    const tab = document.createElement('button'); tab.type = 'button'; tab.id = 'settings-tab-' + key; tab.textContent = label;
    tab.setAttribute('role','tab'); tab.setAttribute('aria-controls',panelId); tab.setAttribute('aria-selected','false'); tab.tabIndex = -1;
    const panel = $('#' + panelId); panel.setAttribute('role','tabpanel'); panel.setAttribute('aria-labelledby',tab.id);
    tab.addEventListener('click',() => window.showView(key === 'env' ? 'env' : 'settings'));
    tab.addEventListener('keydown',async event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === 'Home' ? 'env' : event.key === 'End' ? 'settings' : key === 'env' ? 'settings' : 'env';
      if (await window.showView(target)) $('#settings-tab-' + (target === 'env' ? 'env' : 'google')).focus();
    });
    settingsTabs.append(tab);
  }
  function showSettings(view) {
    settingsHeader.hidden = !['env','settings'].includes(view);
    for (const [key,,panelId] of settingsPanels) {
      const selected = view === (key === 'env' ? 'env' : 'settings');
      const tab = $('#settings-tab-' + key); tab.setAttribute('aria-selected',String(selected)); tab.tabIndex = selected ? 0 : -1;
      $('#' + panelId).hidden = !selected;
    }
  }

  function restrict(view = 'run') {
    const panel = $(view === 'run' ? '#form-panel' : '#view-' + view);
    if (!panel || poolViews.has(view)) return;
    panel.querySelectorAll('#k12-open, #embed-open').forEach(link => { link.removeAttribute('href'); link.setAttribute('aria-disabled', 'true'); });
    panel.querySelectorAll('button,input,select,textarea').forEach(control => { control.disabled = true; control.autocomplete = 'off'; control.title = '执行服务尚未接入'; });
    panel.querySelectorAll('iframe').forEach(frame => { frame.hidden = true; });
    let notice = panel.querySelector('.console-status');
    if (!notice) {
      notice = document.createElement('p'); notice.className = 'console-status';
      const action = panel.querySelector('.btn-run-task') || panel.querySelector('.btn-run') || panel.querySelector('button');
      if (action) action.after(notice); else panel.append(notice);
    }
    notice.textContent = view === 'run' ? '执行服务尚未接入 · 当前参数仅供查看。' : '此工具的后端尚未接入，未读取原凭据或检测外部服务。';
    if (view === 'env' && !notice.querySelector('a')) {
      const editor = document.createElement('a'); editor.href = '/onboarding#config-card'; editor.textContent = '打开受保护配置编辑器';
      notice.append(document.createTextNode(' '), editor);
    }
    const custom = panel.querySelector('#single-custom-sms-summary'); if (custom) custom.textContent = '未读取 · 接口尚未接入';
  }
  function lock() {
    for (const view of ['run', 'mailpool', 'assets', 'network', 'env', 'k12', 'plus', 'embed']) restrict(view);
    for (const id of ['btn-stop', 'btn-stop-all', 'btn-update', 'btn-guide']) { const button = $('#' + id); button.disabled = true; button.title = '执行服务尚未接入'; }
    document.querySelectorAll('#plus-payment-dialog input, #plus-payment-dialog select, #plus-payment-dialog button, #gopay-wallet-dialog input, #gopay-wallet-dialog select, #gopay-wallet-dialog button').forEach(control => { control.disabled = true; });
    for (const id of ['health-label', 'k12-nav-state', 'plus-nav-state']) $('#' + id).textContent = '未接入';
    for (const id of ['dot-bb', 'dot-clash', 'dot-k12', 'dot-plus', 'health-dot']) $('#' + id).classList.remove('pending', 'on', 'partial');
    $('#run-state').textContent = '执行服务尚未接入'; $('#update-label').textContent = '更新未接入';
    $('#node').textContent = '出口未检测'; $('#version').textContent = '控制面板';
    $('#custom-sms-summary').textContent = '未读取 · 接口尚未接入';
    for (const id of ['plus-import-state', 'plus-protocol-state', 'k12-channel-status']) $('#' + id).textContent = '尚未接入';
    $('#k12-offline-detail').textContent = '未检测或启动外部服务。';
  }
  function expire(error = {}) {
    if (expired) return;
    expired = true; csrf = ''; logout.disabled = true; actor.textContent = '会话不可用'; login.hidden = false;
    message.hidden = false; message.textContent = error.status === 401 ? '会话已失效，请重新登录。' : '会话核验失败，请重新登录。';
    $('#script-nav').replaceChildren(); $('#form-panel').replaceChildren();
    window.dispatchEvent(new CustomEvent('rf:session-expired'));
    lock();
  }
  async function api(path, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal});
      let data; try { data = await response.json(); } catch (_) { throw {status: response.status}; }
      if (!response.ok) throw {status: response.status};
      return data;
    } finally { clearTimeout(timer); }
  }
  function session() {
    if (!sessionPromise) sessionPromise = (async () => {
      try {
        const value = await api('/api/auth/session');
        if (!value || typeof value.csrf_token !== 'string' || !value.csrf_token || !Array.isArray(value.permissions) || !value.permissions.includes('onboarding:read')) throw {status: 403};
        if (expired) throw {status: 401};
        csrf = value.csrf_token; actor.textContent = typeof value.display_name === 'string' ? value.display_name : '已登录操作者'; logout.disabled = Boolean($('#logout')?.disabled);
        return value;
      } catch (error) { expire(error); throw error; }
    })();
    return sessionPromise;
  }
  function googleFallback() {
    if ($('#script-nav [data-view="google"]')) return;
    const group = document.createElement('details'); group.className = 'nav-group'; group.open = true;
    const summary = document.createElement('summary'), title = document.createElement('span'), count = document.createElement('span');
    title.textContent = '主流程'; count.className = 'nav-count'; count.textContent = '1'; summary.append(title, count);
    const items = document.createElement('div'); items.className = 'nav-group-items';
    const button = document.createElement('button'); button.className = 'scriptbtn'; button.dataset.view = 'google'; button.textContent = 'Google 开通任务';
    button.onclick = () => window.showView('google'); button.classList.toggle('active', activeView === 'google');
    items.append(button); group.append(summary, items); $('#script-nav').append(group); $('#task-count').textContent = '1';
  }
  function catalog() {
    if (!catalogPromise) catalogPromise = (async () => {
      await session();
      try {
        const value = await api('/api/onboarding/console/catalog');
        if (expired) throw {status: 401};
        if (!value || value.execution_enabled !== false || !Array.isArray(value.scripts) || !Array.isArray(value.links) || !Array.isArray(value.embeds) || value.embeds.length) throw {status: 503};
        return value;
      } catch (error) {
        if (expired || error.status === 401) expire(error);
        else { message.hidden = false; message.textContent = '原任务目录暂不可用；邮箱池、信用卡池与系统配置仍可单独使用。'; $('#form-panel').textContent = '任务目录读取失败。'; googleFallback(); }
        throw error;
      }
    })();
    return catalogPromise;
  }
  function normalized(view) {
    view = ({resources:'mailboxes', mailpool:'mailboxes', 'mailbox-card':'mailboxes', 'batch-card':'google', 'tasks-card':'google', 'task-workbench':'google', 'card-pool':'cards', 'global-config':'settings', 'network-panel':'network', 'legacy-tools':'assets'})[view] || view;
    return poolViews.has(view) || originalViews.has(view) ? view : 'run';
  }

  function beforeNavigate(view) {
    return !expired && (!window.RFPoolPanel || window.RFPoolPanel.beforeNavigate(normalized(view)) !== false);
  }
  function showPanel(view, options = {}) {
    if (expired) return false;
    view = normalized(view);
    if (!poolViews.has(view) && !originalViews.has(view)) view = 'run';
    if (!options.initial && !beforeNavigate(view)) {
      if (location.hash !== activeHash) history.replaceState(null, '', location.pathname + location.search + activeHash);
      return false;
    }
    activeView = view;
    showSettings(view);
    showResources(view);
    const wrapper = $('#view-onboarding');
    if (wrapper) {
      wrapper.hidden = !poolViews.has(view);
      for (const [id, target] of [['mailbox-card', 'mailboxes'], ['task-workbench', 'google'], ['card-pool', 'cards'], ['global-config', 'settings'], ['network-panel', null], ['legacy-tools', null]]) {
        const panel = $('#' + id); if (panel) panel.hidden = target !== view;
      }
    }
    document.querySelectorAll('.navbtn, .scriptbtn[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === navigationView(view)));
    if (view !== 'run') document.querySelectorAll('.scriptbtn[data-id]').forEach(button => button.classList.remove('active'));
    else if (typeof curSrc !== 'undefined' && curSrc) document.querySelector(`.scriptbtn[data-id="${curSrc.id}"]`)?.classList.add('active');
    const targetHash = '#' + view;
    if (options.history !== false && location.hash !== targetHash) history.pushState(null, '', targetHash);
    activeHash = location.hash;
    return true;
  }
  function initRouting() {
    if (routeReady) return; routeReady = true;
    const navigateHash = () => window.showView(normalized(location.hash.slice(1) || (location.pathname === '/onboarding/pools' ? 'mailboxes' : 'run')), {history: false});
    window.addEventListener('hashchange', navigateHash);
    window.showView(normalized(location.hash.slice(1) || (location.pathname === '/onboarding/pools' ? 'mailboxes' : 'run')), {history: false, initial: true});
    const hiddenLogout = $('#logout');
    if (hiddenLogout) new MutationObserver(() => { logout.disabled = expired || !csrf || hiddenLogout.disabled; }).observe(hiddenLogout, {attributes: true, attributeFilter: ['disabled']});
  }
  logout.addEventListener('click', async () => {
    if (!csrf || logout.disabled) return;
    const hiddenLogout = $('#logout');
    if (hiddenLogout) { hiddenLogout.click(); return; }
    logout.disabled = true;
    try { await api('/api/auth/logout', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: '{}'}); csrf = ''; window.location.replace('/login'); }
    catch (error) { expire(error); }
  });
  window.RFProtectedConsole = Object.freeze({session, catalog, restrict, lock, expire, showPanel, initRouting, beforeNavigate, normalized, navigationView});
  lock();
})();
