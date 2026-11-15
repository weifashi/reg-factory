/* Owner-scoped synthetic_only card management; shared runtime owns session and writes. */
'use strict';
(() => {
  const runtime = window.RFPoolRuntime, panel = document.querySelector('#card-pool');
  if (!runtime || !panel) return;
  const $ = selector => panel.querySelector(selector);
  const all = selector => panel.querySelectorAll(selector);
  const base = '/api/onboarding/cards', permission = 'cards:manage';
  const fields = ['alias','pan','expiry_month','expiry_year','holder_name','country','address_line1','address_line2','city','region','postal_code','account_limit','sms_channel_ref'];
  const tabs = ['list','manual','csv'];
  let tab = 'list', cursor = null, preview = null, draft = '', editing = null;
  let epoch = 0, attempted = false, stale = false;
  let pages = [null], pageIndex = 0, filterDirty = false;
  const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value);
  const number = value => Number.isSafeInteger(value) && value >= 0;
  const metadata = (value, max = 128) => typeof value === 'string' && value.length <= max && !/\p{C}/u.test(value) && (value.match(/\p{Nd}/gu) || []).length < 12;
  const invalid = () => ({status:503, invalid:true});
  const cardPending = request => request && request.kind === 'card-import';
  const allowed = (write = false, importing = false) => {
    const state = runtime.state();
    return runtime.has(permission) && !state.inactive && !state.busy && (!write || !state.pending || (importing && cardPending(state.pending)));
  };
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  }
  function clearImport(keepIssues = false) {
    epoch++; preview = null; draft = '';
    $('#card-csv').value = ''; $('#card-file').value = '';
    for (const name of fields) $('#card-manual-' + name).value = '';
    if (!keepIssues) $('#card-preview').replaceChildren();
  }
  function closeEdit() {
    editing = null; $('#card-edit-form').hidden = true;
    $('#card-edit-sms').value = ''; $('#card-replace-sms').checked = false;
  }
  function controls(state = runtime.state()) {
    const blocked = state.busy || state.inactive || !runtime.has(permission);
    const importing = blocked || Boolean(state.pending && !cardPending(state.pending));
    const writing = blocked || Boolean(state.pending);
    for (const item of all('button,input,select,textarea')) item.disabled = blocked;
    for (const name of ['manual','csv']) $('#card-tab-' + name).disabled = importing;
    for (const item of all('#card-manual-form input,#card-csv-form input,#card-csv-form textarea,.card-clear')) item.disabled = importing;
    $('#card-template').disabled = importing;
    $('#card-preview-manual').disabled = importing; $('#card-preview-csv').disabled = importing;
    $('#card-confirm').disabled = importing || !preview;
    $('#card-next').disabled = blocked || stale || filterDirty || !cursor;
    $('#card-prev').disabled = blocked || stale || filterDirty || pageIndex === 0;
    for (const item of all('[data-card-edit],[data-card-toggle],#card-edit-form input,#card-edit-form button')) item.disabled = writing || stale;
    $('#card-save').disabled = writing || stale || !editing;
    $('#card-edit-sms').disabled = writing || stale || !editing || !$('#card-replace-sms').checked;
    $('#card-edit-cancel').disabled = blocked;
    if (!runtime.has(permission)) $('#card-state').textContent = state.inactive ? '会话已结束，卡片数据已清空。' : '需要 cards:manage 权限，未读取卡片。';
    maybeLoad();
  }
  function maybeLoad() {
    if (attempted || !allowed() || panel.closest('[hidden]') || !panel.getClientRects().length) return;
    attempted = true;
    queueMicrotask(() => { if (allowed()) runtime.run(() => load()); else attempted = false; });
  }
  function validRow(row) {
    return row && uuid(row.id) && metadata(row.alias,80) && ['visa','mastercard','amex','other'].includes(row.brand) && /^\d{4}$/.test(row.last4)
      && typeof row.enabled === 'boolean' && Number.isSafeInteger(row.account_limit) && row.account_limit > 0
      && number(row.linked_count) && number(row.reserved_count) && typeof row.reconciliation_required === 'boolean'
      && Number.isSafeInteger(row.version) && row.version > 0 && Number.isSafeInteger(row.revision) && row.revision > 0
      && typeof row.sms_configured === 'boolean';
  }
  function closeDetail() {
    if ($('#card-detail').open) $('#card-detail').close();
    $('#card-detail-content').replaceChildren(); closeEdit();
  }
  function openDetail(row) {
    closeEdit();
    const facts = node('dl', undefined, 'card-detail-facts');
    const dates = ['created_at','updated_at','last_assigned_at'].map(key => typeof row[key] === 'string' && /^[\dTZ:+.\- ]{1,40}$/.test(row[key]) ? row[key] : '无记录');
    for (const [label,value] of [
      ['别名',row.alias], ['品牌 / 尾号',row.brand.toUpperCase() + ' •••• ' + row.last4],
      ['状态',row.enabled ? '已启用' : '已停用'], ['核对状态',row.reconciliation_required ? '待核对 · 不可判定可用' : '无待核对标记'],
      ['已绑定 / 上限',row.linked_count + ' / ' + row.account_limit], ['预留',row.reserved_count],
      ['剩余名额',Math.max(0,row.account_limit-row.linked_count-row.reserved_count)],
      ['短信通道',row.sms_configured ? '已配置（不回显）' : '未配置'], ['版本',row.version], ['资料修订',row.revision],
      ['创建时间',dates[0]], ['更新时间',dates[1]], ['最近分配',dates[2]], ['卡片 ID',row.id]
    ]) facts.append(node('dt',label),node('dd',value));
    $('#card-detail-content').replaceChildren(facts,node('p','剩余名额是账号数量，不是银行卡余额；未锁定，不保证可分配。','footnote'));
    $('#card-detail').showModal();
  }
  $('#card-detail-close').addEventListener('click',closeDetail);
  $('#card-detail').addEventListener('cancel',event => { event.preventDefault(); closeDetail(); });
  function renderCard(row) {
    const item = node('tr'); item.dataset.cardRow = row.id;
    const alias = node('td'); const label = node('span',row.alias,'card-alias'); label.title = row.alias; alias.append(label);
    const status = node('td'); status.append(node('span',row.enabled ? '已启用' : '已停用','card-status'));
    if (row.reconciliation_required) status.append(node('span','待核对','card-reconcile'));
    const remaining = Math.max(0,row.account_limit-row.linked_count-row.reserved_count);
    item.append(alias,node('td',row.brand.toUpperCase() + ' •••• ' + row.last4),status,
      node('td',row.linked_count + ' / ' + row.account_limit),node('td',row.reserved_count),node('td',remaining));
    const actionCell = node('td'), actions = node('div',undefined,'card-row-actions');
    const detail = node('button','详情'); detail.type = 'button'; detail.dataset.cardDetail = row.id;
    detail.setAttribute('aria-label','查看 ' + row.alias);
    detail.addEventListener('click',() => { if (allowed() && !stale) openDetail(row); });
    const edit = node('button','编辑'); edit.type = 'button'; edit.dataset.cardEdit = row.id; edit.setAttribute('aria-label','编辑 ' + row.alias);
    edit.addEventListener('click',() => {
      if (!allowed(true) || stale) return;
      openDetail(row); editing = row; $('#card-edit-title').textContent = '编辑 ' + row.alias + ' · •••• ' + row.last4;
      $('#card-edit-alias').value = row.alias; $('#card-edit-limit').value = row.account_limit;
      $('#card-edit-limit').min = Math.max(1,row.linked_count + row.reserved_count);
      $('#card-edit-form').hidden = false; controls(); $('#card-edit-alias').focus();
    });
    const toggle = node('button',row.enabled ? '停用卡片' : '启用卡片'); toggle.type = 'button'; toggle.dataset.cardToggle = row.id;
    toggle.setAttribute('aria-label',(row.enabled ? '停用 ' : '启用 ') + row.alias);
    toggle.addEventListener('click',() => {
      if (!allowed(true) || stale) return;
      if (!window.confirm((row.enabled ? '停用仅阻止新分配，不释放已有绑定或预留。' : '启用不会清除待核对状态，也不启动分配器。') + '确认操作？')) return;
      update(row,{enabled:!row.enabled});
    });
    const more = node('details',undefined,'card-more'), summary = node('summary','更多'); summary.dataset.cardMore = row.id;
    more.append(summary,toggle); actions.append(detail,edit,more); actionCell.append(actions); item.append(actionCell); return item;
  }
  async function load(direction = 0) {
    $('#card-state').textContent = '正在读取本人卡片…';
    closeDetail(); stale = true; controls();
    try {
      const search = $('#card-search').value.trim();
      if (!metadata(search,80)) { runtime.message('只搜索别名或四位尾号，请勿输入完整卡号。'); $('#card-search').value = ''; $('#card-state').textContent = '搜索未执行，请使用别名或尾号。'; return; }
      const requested = direction === 1 ? pageIndex + 1 : direction === -1 ? Math.max(0,pageIndex - 1) : 0;
      const pageCursor = direction === 1 ? cursor : direction === -1 ? pages[requested] : null;
      const size = $('#card-page-size').value;
      const params = new URLSearchParams({search,limit:['25','50','100'].includes(size) ? size : '50'});
      if ($('#card-enabled').value) params.set('enabled',$('#card-enabled').value);
      if (pageCursor) params.set('cursor',pageCursor);
      const result = await runtime.api(base + '?' + params);
      if (runtime.state().inactive) return;
      if (!result || !Array.isArray(result.items) || !result.items.every(validRow) || !(result.next_cursor === null || typeof result.next_cursor === 'string')) throw invalid();
      if (direction === 0) pages = [null];
      else { pages = pages.slice(0,requested + 1); pages[requested] = pageCursor; }
      pageIndex = requested; filterDirty = false;
      cursor = result.next_cursor; stale = false;
      $('#card-page-label').textContent = '第 ' + (pageIndex + 1) + ' 页';
      $('#card-list').replaceChildren(...result.items.map(renderCard));
      $('#card-state').textContent = result.items.length ? '已读取本页 ' + result.items.length + ' 张卡片；非全池统计。' : '已读取：当前条件下没有卡片。可手动添加或导入合成 CSV。';
    } catch (error) {
      cursor = null; $('#card-list').replaceChildren();
      $('#card-state').textContent = '卡片读取失败，请手动刷新；不会自动重试。'; throw error;
    } finally { controls(); }
  }
  function update(row, changes) {
    const body = {expected_version:row.version,changes,request_key:crypto.randomUUID()};
    runtime.run(() => runtime.mutate(base + '/' + row.id,'PATCH',body,permission,async result => {
      if (!result || result.card_id !== row.id || !Number.isSafeInteger(result.version) || result.version <= row.version || result.request_key !== body.request_key) throw {status:0,invalid:true};
      closeEdit(); stale = true; runtime.message('卡片管理信息已保存；已有绑定和预留未释放。',false);
      try { await load(); } catch (error) { runtime.followupFail(error,'卡片已保存，但列表刷新失败。'); }
    }));
  }
  function switchTab(next) {
    if (!tabs.includes(next) || !allowed(false) || (next !== 'list' && !allowed(true,true))) return;
    if (next !== tab) { clearImport(); closeDetail(); }
    tab = next;
    for (const name of tabs) {
      $('#card-panel-' + name).hidden = name !== next;
      $('#card-tab-' + name).setAttribute('aria-selected',String(name === next));
      $('#card-tab-' + name).tabIndex = name === next ? 0 : -1;
    }
    $('#card-confirm-area').hidden = next === 'list'; controls();
  }
  for (const name of tabs) {
    const button = $('#card-tab-' + name);
    button.addEventListener('click',() => switchTab(name));
    button.addEventListener('keydown',event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
      if ($('#card-tab-' + tabs[index]).disabled) return;
      switchTab(tabs[index]); $('#card-tab-' + tabs[index]).focus();
    });
  }
  $('#card-filter').addEventListener('submit',event => { event.preventDefault(); if (allowed()) runtime.run(() => load()); });
  $('#card-next').addEventListener('click',() => { if (allowed() && !stale && !filterDirty && cursor) runtime.run(() => load(1)); });
  $('#card-prev').addEventListener('click',() => { if (allowed() && !stale && !filterDirty && pageIndex > 0) runtime.run(() => load(-1)); });
  $('#card-page-size').addEventListener('change',() => { if (allowed()) runtime.run(() => load()); });
  for (const id of ['card-search','card-enabled']) $('#' + id).addEventListener('input',() => { filterDirty = true; controls(); });
  $('#card-edit-cancel').addEventListener('click',() => { if (!runtime.state().busy) { closeDetail(); controls(); } });
  $('#card-replace-sms').addEventListener('change',() => { $('#card-edit-sms').value = ''; controls(); });
  $('#card-edit-form').addEventListener('submit',event => {
    event.preventDefault(); if (!allowed(true) || stale || !editing) return;
    const limit = Number($('#card-edit-limit').value), alias = $('#card-edit-alias').value;
    if (!metadata(alias,80) || !alias.trim() || !Number.isSafeInteger(limit) || limit < Math.max(1,editing.linked_count + editing.reserved_count) || limit > 10000) { runtime.message('别名或上限无效；上限不能低于已绑定数与预留数之和。'); return; }
    const changes = {alias,account_limit:limit};
    if ($('#card-replace-sms').checked) {
      if (!metadata($('#card-edit-sms').value)) { runtime.message('短信通道只接受非秘密引用，禁止输入卡号或验证码。'); return; }
      changes.sms_channel_ref = $('#card-edit-sms').value;
    }
    update(editing,changes);
  });
  function invalidate() { epoch++; preview = null; draft = ''; $('#card-preview').replaceChildren(); controls(); }
  for (const item of all('#card-manual-form input,#card-csv')) item.addEventListener('input',invalidate);
  for (const button of all('.card-clear')) button.addEventListener('click',() => { if (allowed(true,true)) { clearImport(); controls(); runtime.message('卡片原文已清空；未决请求仍须明确核对。',false); } });
  const quote = value => '"' + value.replace(/"/g,'""') + '"';
  function validPreview(result) {
    return result && Array.isArray(result.items) && result.items.every(row => row && number(row.line) && metadata(row.alias,80)
      && ['visa','mastercard','amex','other'].includes(row.brand) && /^\d{4}$/.test(row.last4) && number(row.account_limit)
      && ['sms_configured','billing_configured','expiry_configured'].every(key => typeof row[key] === 'boolean'))
      && Array.isArray(result.issues) && result.issues.every(row => row && number(row.line) && /^[A-Z_]{1,60}$/.test(row.code))
      && ['accepted_count','duplicate_count','conflict_count'].every(key => number(result[key])) && typeof result.can_import === 'boolean';
  }
  function previewInput(text) {
    if (!allowed(true,true)) return;
    runtime.run(async () => {
      invalidate(); const current = epoch;
      try {
        if (new TextEncoder().encode(text).length > 262144) { runtime.message('CSV 超过 256 KiB，已清空。'); clearImport(); return; }
        const result = await runtime.api(base + '/import-preview','POST',{text});
        if (runtime.state().inactive || current !== epoch) return;
        if (!validPreview(result)) throw invalid();
        const target = $('#card-preview');
        target.replaceChildren(node('h3','预览结果 · 尚未写入'),node('p','通过 ' + result.accepted_count + ' 条；重复 ' + result.duplicate_count + '；冲突 ' + result.conflict_count));
        for (const row of result.items) target.append(node('p','第 ' + row.line + ' 行 · ' + row.alias + ' · ' + row.brand.toUpperCase() + ' •••• ' + row.last4 + ' · 上限 ' + row.account_limit + ' · 短信 ' + (row.sms_configured ? '已配置' : '未配置') + ' · 账单 ' + (row.billing_configured ? '已配置' : '未配置') + ' · 到期 ' + (row.expiry_configured ? '已配置' : '未配置')));
        for (const issue of result.issues) target.append(node('p','第 ' + issue.line + ' 行：' + issue.code));
        if (result.can_import && result.accepted_count > 0 && typeof result.preview_digest === 'string' && /^[a-f0-9]{64}$/.test(result.preview_digest)) {
          const pending = runtime.state().pending;
          if (pending && (!cardPending(pending) || pending.digest !== result.preview_digest)) {
            clearImport(); runtime.message('输入与未决卡片导入摘要不一致，已清空；请提供原内容，不会换键导入。');
          } else { preview = {digest:result.preview_digest}; draft = text; }
        } else clearImport(true);
      } catch (error) { clearImport(); throw error; }
    });
  }
  $('#card-csv-form').addEventListener('submit',event => { event.preventDefault(); previewInput($('#card-csv').value); });
  $('#card-manual-form').addEventListener('submit',event => {
    event.preventDefault(); if (!allowed(true,true)) return;
    previewInput(fields.join(',') + '\n' + fields.map(name => quote($('#card-manual-' + name).value)).join(',') + '\n');
  });
  $('#card-file').addEventListener('change',() => {
    if (!allowed(true,true)) return;
    const file = $('#card-file').files[0]; invalidate(); if (!file) return;
    runtime.run(async () => {
      const current = epoch;
      try {
        if (file.size > 262144) { clearImport(); runtime.message('CSV 超过 256 KiB，已清空。'); return; }
        const text = await file.text();
        if (runtime.state().inactive || current !== epoch) return;
        $('#card-csv').value = text;
      } catch (error) { clearImport(); runtime.message('无法读取 CSV 文件，已清空。'); }
    });
  });
  $('#card-template').addEventListener('click',() => {
    if (!allowed(true,true)) return;
    runtime.run(async () => {
      const result = await runtime.api(base + '/template');
      if (runtime.state().inactive) return;
      if (!result || result.data_policy !== 'synthetic_only' || result.csv !== fields.join(',') + '\n') throw invalid();
      const url = URL.createObjectURL(new Blob([result.csv],{type:'text/csv;charset=utf-8'}));
      const link = node('a'); link.href = url; link.download = 'synthetic-card-header.csv'; link.click();
      setTimeout(() => URL.revokeObjectURL(url),1000);
    });
  });
  function confirmImport() {
    if (!allowed(true,true) || !preview) return;
    const replay = runtime.state().pending;
    if (!window.confirm(replay ? '使用原请求键和相同内容核对卡片导入？不会换键。' : '明确确认写入合成卡片？同内容仅跳过，冲突不覆盖，不启动真实分配。')) { clearImport(); controls(); return; }
    const digest = preview.digest, key = replay ? replay.key : crypto.randomUUID();
    runtime.run(async () => {
      try {
        const result = await runtime.api(base + '/import','POST',{text:draft,preview_digest:digest,request_key:key});
        if (runtime.state().inactive) return;
        if (!result || !Array.isArray(result.created_ids) || !Array.isArray(result.skipped_ids) || !result.created_ids.every(uuid) || !result.skipped_ids.every(uuid) || result.request_key !== key) throw {status:0,invalid:true};
        clearImport(); runtime.forget();
        runtime.message('卡片导入已确认：新增 ' + result.created_ids.length + '，跳过 ' + result.skipped_ids.length + '；不代表真实支付或已分配。',false);
        try { await load(); } catch (error) { runtime.followupFail(error,'卡片已导入，但列表刷新失败。'); }
      } catch (error) {
        error.mutation = true;
        if (error.code === 'COMMIT_UNKNOWN' || !error.status || (error.status >= 500 && error.status <= 599)) runtime.remember({kind:'card-import',secret:true,path:base + '/import',permission,key,digest});
        else if (replay && error.status !== 401) error.stillPending = true;
        throw error;
      } finally { clearImport(); }
    });
  }
  $('#card-confirm').addEventListener('click',confirmImport);
  runtime.register({
    controls,
    expire() { clearImport(); closeDetail(); cursor = null; $('#card-list').replaceChildren(); $('#card-state').textContent = '会话已结束，卡片数据已清空。'; },
    beforeNavigate(target) { if (target !== 'cards' && target !== 'card-pool') { clearImport(); closeDetail(); } return true; },
    failure(error) { if (error.status === 409 && !runtime.state().pending) { stale = true; closeDetail(); runtime.message('卡片或其他管理数据版本已变化，请刷新对应列表再操作。'); } },
    reconcile(request) {
      if (!cardPending(request)) return false;
      if (typeof window.showView === 'function') window.showView('cards');
      switchTab('csv'); $('#card-csv').focus();
      runtime.message('请在卡片手动添加或 CSV 标签重新提供原输入，再预览；摘要一致后明确确认，将使用原请求键。',false);
      return true;
    }
  });
  new MutationObserver(maybeLoad).observe(document.body,{attributes:true,subtree:true,attributeFilter:['hidden','class']});
  window.addEventListener('pagehide',() => { clearImport(); closeDetail(); });
})();
