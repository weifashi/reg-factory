'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const all = selector => document.querySelectorAll(selector);
  const base = '/api/onboarding';
  const configNames = ['model','region','instance_ref','group_ref','project_prefix','timeout_seconds','concurrency','retention_days'];
  let csrf = '', permissions = new Set(), busy = false;
  let mailboxCursor = null, taskCursor = null, selected = new Set(), mailboxStale = false;
  let preview = null, preflight = null, configRevision = null, configLoaded = false;
  let task = null, taskId = null, pending = null, inactive = false;
  // A hung request must end; a timed-out write keeps its original request for explicit reconcile.
  const REQUEST_TIMEOUT_MS = 30000;
  const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
  const has = permission => Boolean(csrf) && permissions.has(permission);
  const invalid = () => ({status:503,invalid:true});
  const reference = error => error.correlation ? ' 参考号：' + error.correlation : '';
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function message(text, error = true) {
    const target = $('#message'); target.textContent = text;
    target.className = 'message ' + (error ? 'error' : 'success'); target.hidden = false;
  }
  function clearImport() {
    $('#import-text').value = ''; preview = null; $('#import-preview').replaceChildren();
  }
  function invalidatePreflight() {
    preflight = null;
    $('#preflight-result').textContent = '选择、数量或配置变化后必须重新预检；观察结果不是分配许可。';
    controls();
  }
  function expire() {
    inactive = true; csrf = ''; permissions.clear(); clearImport(); pending = null; preflight = null;
    task = null; taskId = null; selected.clear(); configLoaded = false;
    $('#login-link').hidden = false; $('#reconcile-card').hidden = true; $('#reconcile-note').textContent = '';
    $('#preflight-result').textContent = '会话已结束；本页不再保留预检观察。';
    $('#task-facts').replaceChildren(); $('#task-facts').hidden = true; controls();
  }
  function controls() {
    for (const item of all('[data-permission]')) item.disabled = busy || !has(item.dataset.permission);
    const writing = busy || Boolean(pending);
    $('#logout').disabled = busy || !csrf;
    $('#mailbox-next').disabled = busy || !has('onboarding:read') || !mailboxCursor;
    $('#task-next').disabled = busy || !has('onboarding:read') || !taskCursor;
    $('#preview-import').disabled = busy || !has('mailboxes:manage') || Boolean(pending && !pending.secret);
    $('#confirm-import').disabled = busy || !has('mailboxes:manage') || !preview || Boolean(pending && !pending.secret);
    $('#import-text').disabled = busy || !has('mailboxes:manage') || Boolean(pending && !pending.secret);
    $('#import-group').disabled = busy || !has('mailboxes:manage') || Boolean(pending && !pending.secret);
    $('#cancel-import').disabled = busy;
    $('#tab-list').disabled = busy; $('#tab-import').disabled = busy;
    $('#config-fields').disabled = writing || !has('config:manage') || !configLoaded;
    $('#save-config').disabled = writing || !has('config:manage') || !configLoaded;
    $('#preflight').disabled = writing || !has('tasks:manage');
    $('#confirm-batch').disabled = writing || !has('tasks:manage') || !preflight || !preflight.can_create;
    $('#refresh-task').disabled = busy || !has('onboarding:read') || !taskId;
    for (const action of ['pause','cancel','recheck']) $('#' + action + '-task').disabled = writing || !has('tasks:manage') || !task;
    for (const item of all('[data-mailbox-write]')) item.disabled = writing || mailboxStale || !has('mailboxes:manage');
    for (const item of all('[data-mailbox-select]')) item.disabled = writing || !has('tasks:manage');
    $('#selection').disabled = writing || !has('tasks:manage');
    $('#requested-count').disabled = writing || !has('tasks:manage');
    $('#reconcile-request').disabled = busy || !pending || !has(pending.permission);
    for (const item of all('[data-filter]')) item.disabled = busy;
  }
  function fail(error) {
    // Only a write (marked by mutate/import) can have an unconfirmed outcome; a failed read just failed.
    const mutation = error.mutation === true;
    const labels = {401:'会话已失效，敏感输入已清空。请重新登录。',403:'没有权限或请求来源不符；不会自动提升权限。',409:'版本、资源或请求键冲突：已保留当前选择与 N；相关数据需刷新后才能再次写入，创建批次前须重新预检。不会自动重试。',422:'输入不符合要求。请检查格式、数量与分组。',429:'请求过于频繁，请稍后手动重试。',503:mutation ? '依赖暂不可用，操作未确认；不会自动重试或使用演示数据。' : '依赖暂不可用，未能读取；不会自动重试或使用演示数据。'};
    let text = labels[error.status] || (mutation ? '连接中断或响应无效，结果尚未确认；不会自动重试。' : '连接中断或响应无效，未能读取；不会自动重试。');
    if (error.invalid && !mutation) text = '服务端响应无效，未显示部分结果；不会自动重试。';
    if (error.timeout) text = mutation ? '请求超时，结果尚未确认；仅可用原请求核对，不会自动重试。' : '请求超时，未能读取；不会自动重试。';
    if (error.code === 'COMMIT_UNKNOWN') text = mutation ? 'COMMIT_UNKNOWN：提交结果未知。仅可明确使用原请求键与原内容核对，不能换键重建。' : '服务端提交状态未知，本次读取未完成；可手动刷新，不会自动重试。';
    if (error.stillPending) text += ' 原请求仍待核对：此拒绝不能证明原请求未提交。可稍后再次核对；如需放弃，请离开页面并交管理员按请求键核对记录。';
    text += reference(error);
    if (error.status === 401) expire();
    if (error.status === 409) {
      task = null; configLoaded = false; mailboxStale = true;
      $('#config-state').textContent = '发现版本冲突；请读取最新配置后再保存。';
      invalidatePreflight();
    }
    message(text); controls();
  }
  // A read that follows an accepted mutation must never report the mutation as unconfirmed.
  function followupFail(error, prefix) {
    if (error.status === 401) { expire(); message(prefix + '会话已失效，请重新登录后查看最新状态；不要重复提交。'); }
    else if (error.status === 403) message(prefix + '没有读取最新状态的权限；不要重复提交。' + reference(error));
    else message(prefix + '请稍后手动刷新查看最新状态，不要重复提交' + (error.timeout ? '（请求超时）' : error.invalid ? '（响应无效）' : error.status ? '（HTTP ' + error.status + '）' : '（连接中断或响应无效）') + '。' + reference(error));
    controls();
  }
  async function api(path, method, body) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const options = {credentials:'same-origin',cache:'no-store',signal:controller.signal};
    if (method) Object.assign(options,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
    try {
      let response, result;
      // No status: a write in this state stays pending for explicit reconcile.
      try { response = await fetch(path,options); } catch (_) { throw {status:0,timeout:controller.signal.aborted}; }
      // A known non-2xx status stands even if its body read was aborted.
      try { result = await response.json(); } catch (_) { throw {status:response.ok ? 0 : response.status,timeout:response.ok && controller.signal.aborted}; }
      if (!response.ok) throw {status:response.status,code:result && result.code,correlation:result && uuid(result.correlation_id) ? result.correlation_id : '',notCommitted:Boolean(result) && result.not_committed === true};
      return result;
    } finally { clearTimeout(timer); }
  }
  async function run(action) {
    if (busy || !csrf) return;
    busy = true; controls(); $('#main').setAttribute('aria-busy','true');
    try { await action(); } catch (error) { fail(error); }
    finally { busy = false; $('#main').setAttribute('aria-busy','false'); controls(); }
  }
  function operation(request) {
    if (request.path.endsWith('/batches')) return '创建批次';
    if (request.path.endsWith('/config')) return '保存全局配置';
    if (request.method === 'PATCH') return '修改邮箱';
    return {pause:'暂停任务',cancel:'请求取消任务',recheck:'排入只读核验'}[request.path.split('/').pop()] || '写操作';
  }
  function remember(request) {
    if (!csrf) return; // Expired/page-hidden pages must not revive cleared requests.
    pending = request;
    $('#reconcile-card').hidden = false;
    $('#reconcile-note').textContent = request.secret
      ? '导入结果未知。原秘密正文已清空；请重新提供完全相同文本和分组，再预览。摘要一致时才可用原键确认核对。请求键：' + request.key
      : '操作：' + operation(request) + '；原请求键：' + request.body.request_key + '。原非秘密请求内容仅留在本页内存。';
  }
  function forget() { pending = null; $('#reconcile-card').hidden = true; $('#reconcile-note').textContent = ''; }
  // Spec §6: only the server's not-committed proof, on a replay of this very pending request,
  // may release it, and only after the operator confirms. Anything else stays frozen.
  function releaseProven(error, request, reason) {
    if (error.status !== 409 || error.code !== 'VERSION_CONFLICT' || error.notCommitted !== true || !csrf || pending !== request) return false;
    if (!window.confirm('服务端已证明原请求未提交且不会再提交（' + reason + '）。解除后须刷新数据、重新预检，并按新请求操作。确认解除？')) return false;
    forget(); task = null; configLoaded = false; mailboxStale = true; invalidatePreflight();
    return true;
  }
  // Retain only nonsecret request bodies on uncertain mutation outcomes. Never retry implicitly.
  // A rejected replay stays pending unless releaseProven accepts the server's not-committed proof
  // for this very request and the operator confirms: pre-receipt environment checks (migration
  // catalog, database target) also answer 409/422, so no status alone proves anything.
  async function mutate(path, method, body, permission, accepted) {
    try {
      const result = await api(path,method,body); forget(); await accepted(result);
    } catch (error) {
      error.mutation = true;
      if (error.code === 'COMMIT_UNKNOWN' || !error.status || (error.status >= 500 && error.status <= 599)) remember({path,method,body,permission,accepted,secret:false});
      else if (pending && pending.body === body && error.status !== 401) error.stillPending = true;
      throw error;
    }
  }
  function validPage(value, kind) {
    if (!value || !Array.isArray(value.items) || !(value.next_cursor === null || typeof value.next_cursor === 'string')) throw invalid();
    for (const row of value.items) {
      if (!row || !uuid(row.id) || !Number.isSafeInteger(row.version) || row.version < 1) throw invalid();
      if (kind === 'mailbox' && (typeof row.email !== 'string' || typeof row.group_ref !== 'string' || typeof row.disabled !== 'boolean' || typeof row.occupied !== 'boolean' || !Array.isArray(row.platforms))) throw invalid();
      if (kind === 'task' && (typeof row.status !== 'string' || !['pool','fixture'].includes(row.execution_scope))) throw invalid();
    }
    return value;
  }
  function filters(cursor) {
    const query = new URLSearchParams({platform:$('#platform').value,limit:'50'});
    for (const [id,key] of [['search','search'],['filter-group','group_ref'],['health','health'],['occupied','occupied']]) if ($('#' + id).value) query.set(key,$('#' + id).value);
    if (cursor) query.set('cursor',cursor);
    return query;
  }
  function selectionChanged() {
    $('#selected-count').textContent = '已选择 ' + selected.size + ' 个邮箱'; invalidatePreflight();
  }
  function mailboxCard(row) {
    const card = node('article',undefined,'pool-item');
    const label = node('label',undefined,'pool-check'), check = node('input');
    check.type = 'checkbox'; check.dataset.mailboxSelect = ''; check.checked = selected.has(row.id); check.setAttribute('aria-label','选择邮箱：' + row.email);
    check.addEventListener('change',() => { if (check.checked) selected.add(row.id); else selected.delete(row.id); selectionChanged(); });
    label.append(check,node('span',row.email)); card.append(label);
    card.append(node('p','健康：' + row.health + ' · ' + (row.disabled ? '已停用' : '未停用') + ' · ' + (row.occupied ? '观察到逻辑占用' : '未观察到占用') + ' · 版本 ' + row.version));
    for (const platform of row.platforms) card.append(node('small',platform.platform + ' · ' + platform.identity_status + ' / ' + platform.usage_status));
    card.append(node('small',row.id,'mono'));
    const actions = node('div',undefined,'actions'), groupLabel = node('label','分组'), group = node('input');
    group.value = row.group_ref; group.maxLength = 128; group.dataset.mailboxGroup = ''; group.dataset.mailboxWrite = ''; groupLabel.append(group);
    const save = node('button','保存分组'), disable = node('button',row.disabled ? '启用新分配' : '停用新分配');
    save.type = disable.type = 'button'; save.dataset.saveGroup = ''; save.dataset.mailboxWrite = ''; disable.dataset.mailboxWrite = '';
    // Every card repeats these controls; name them per mailbox for assistive technology.
    group.setAttribute('aria-label','分组：' + row.email); save.setAttribute('aria-label','保存分组：' + row.email);
    disable.setAttribute('aria-label',(row.disabled ? '启用新分配：' : '停用新分配：') + row.email);
    async function patch(changes) {
      if (!has('mailboxes:manage') || pending || mailboxStale) return;
      await run(async () => {
        await mutate(base + '/mailboxes/' + row.id,'PATCH',{expected_version:row.version,changes,request_key:crypto.randomUUID()},'mailboxes:manage',async () => {
          invalidatePreflight();
          message('修改已受理；停用只阻止新分配，旧任务与占用保留。',false);
          try { await loadMailboxes(); } catch (error) { followupFail(error,'修改已受理，列表刷新失败。'); }
        });
      });
    }
    save.addEventListener('click',() => patch({group_ref:group.value}));
    disable.addEventListener('click',() => {
      if (window.confirm(row.disabled ? '启用只取消停用标记，不改变健康或历史资格。确认？' : '停用仅阻止新分配，不取消旧任务或释放 hold。确认？')) patch({disabled:!row.disabled});
    });
    actions.append(groupLabel,save,disable); card.append(actions); return card;
  }
  async function loadMailboxes(cursor = null) {
    $('#mailbox-state').textContent = '正在加载本人邮箱…'; mailboxCursor = null;
    try {
      const data = validPage(await api(base + '/mailboxes?' + filters(cursor)),'mailbox');
      const cards = data.items.map(mailboxCard); $('#mailbox-list').replaceChildren(...cards); mailboxCursor = data.next_cursor; mailboxStale = false;
      $('#mailbox-state').textContent = data.items.length ? '本页 ' + data.items.length + ' 个邮箱 · 仅本人可见' : '当前筛选没有邮箱。可调整筛选或导入合成文本。';
    } catch (error) { $('#mailbox-list').replaceChildren(); $('#mailbox-state').textContent = '邮箱读取失败；未显示部分坏行或伪造空列表。'; throw error; }
    controls();
  }
  function switchTab(name) {
    if (name !== 'import') clearImport();
    for (const tab of ['list','import']) {
      const active = name === tab; $('#tab-' + tab).setAttribute('aria-selected',String(active)); $('#tab-' + tab).tabIndex = active ? 0 : -1; $('#panel-' + tab).hidden = !active;
    }
    controls();
  }
  for (const name of ['list','import']) {
    $('#tab-' + name).addEventListener('click',() => switchTab(name));
    $('#tab-' + name).addEventListener('keydown',event => {
      if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
        event.preventDefault(); const next = event.key === 'Home' ? 'list' : event.key === 'End' ? 'import' : name === 'list' ? 'import' : 'list';
        switchTab(next); $('#tab-' + next).focus();
      }
    });
  }
  function resetFilters() { mailboxCursor = null; selected.clear(); selectionChanged(); }
  $('#filter-form').addEventListener('submit',event => { event.preventDefault(); if (busy) return; resetFilters(); run(loadMailboxes); });
  for (const id of ['platform','health','occupied']) $('#' + id).addEventListener('change',() => { if (busy) return; resetFilters(); run(loadMailboxes); });
  for (const id of ['platform','health','occupied','search','filter-group','task-scope','task-batch']) $('#' + id).dataset.filter = '';
  $('#mailbox-next').addEventListener('click',() => { const cursor = mailboxCursor; if (cursor) run(() => loadMailboxes(cursor)); });
  // Cursors are bound to the displayed page's filters; editing a text filter drops it until 查询.
  for (const id of ['search','filter-group']) $('#' + id).addEventListener('input',() => { mailboxCursor = null; controls(); });
  for (const id of ['import-text','import-group']) $('#' + id).addEventListener('input',() => { preview = null; $('#import-preview').replaceChildren(); controls(); });
  $('#cancel-import').addEventListener('click',() => { clearImport(); message('导入输入已清空；已提交的未决请求仍需核对。',false); controls(); });
  $('#import-form').addEventListener('submit',event => {
    event.preventDefault(); if (!has('mailboxes:manage') || (pending && !pending.secret)) return;
    run(async () => {
      preview = null;
      try {
        const result = await api(base + '/mailboxes/import-preview','POST',{text:$('#import-text').value,group_ref:$('#import-group').value});
        if (!result || !Array.isArray(result.items) || !Array.isArray(result.issues)) throw invalid();
        const target = $('#import-preview'); target.replaceChildren(node('p','解析 ' + result.accepted_count + ' 条；重复 ' + result.duplicate_count + '；冲突 ' + result.conflict_count));
        for (const row of result.items) target.append(node('p','第 ' + row.line + ' 行 · ' + row.email + ' · ' + row.provider + ' · ' + row.group_ref));
        for (const issue of result.issues) target.append(node('p','第 ' + issue.line + ' 行：' + issue.code));
        if (!result.issues.length && /^[a-f0-9]{64}$/.test(result.preview_digest) && result.accepted_count > 0) {
          if (pending && (pending.digest !== result.preview_digest || pending.group !== $('#import-group').value)) {
            clearImport(); message('重新输入的内容与未决导入摘要不一致，已清空。请提供完全相同的文本与分组，不会换键导入。');
          } else preview = {digest:result.preview_digest,group:$('#import-group').value};
        }
      } catch (error) { clearImport(); throw error; }
    });
  });
  $('#confirm-import').addEventListener('click',() => {
    if (!preview || !has('mailboxes:manage') || (pending && !pending.secret)) return;
    if (!window.confirm(pending ? '使用原请求键和重新提供的相同内容核对导入结果？不会创建新键。' : '确认合成导入？相同凭据仅跳过，不更新或洗白健康与历史资格。')) { clearImport(); controls(); return; }
    const digest = preview.digest, group = preview.group, key = pending ? pending.key : crypto.randomUUID(), replay = pending;
    run(async () => {
      try {
        const result = await api(base + '/mailboxes/import','POST',{text:$('#import-text').value,group_ref:group,duplicate_action:'skip',preview_digest:digest,expected_versions:{},request_key:key});
        if (!result || !Array.isArray(result.created_ids) || !Array.isArray(result.skipped_ids)) throw {status:0};
        clearImport(); forget(); invalidatePreflight();
        const summary = '导入已受理：新增 ' + result.created_ids.length + '，跳过 ' + result.skipped_ids.length + '。';
        message(summary + 'UNKNOWN / HISTORY_UNRECONCILED 保持不变，不代表可分配。',false);
        try { await loadMailboxes(); } catch (error) { followupFail(error,summary + '列表刷新失败。'); }
      } catch (error) {
        error.mutation = true;
        if (error.code === 'COMMIT_UNKNOWN' || !error.status || (error.status >= 500 && error.status <= 599)) remember({secret:true,key,digest,group,permission:'mailboxes:manage'});
        else if (replay && releaseProven(error,replay,'已存在指纹不同的同名邮箱')) { message('服务端已证明原导入未提交；未决已解除。请调整导入内容后按新请求导入。'); return; }
        else if (pending && pending.secret && pending.key === key && error.status !== 401) error.stillPending = true;
        throw error;
      } finally { clearImport(); }
    });
  });
  async function loadConfig() {
    configLoaded = false; $('#config-state').textContent = '正在读取全局配置…';
    let result;
    try {
      result = await api(base + '/config');
      if (result !== null && (!result || typeof result.revision !== 'string' || !result.nonsecret_config || configNames.some(name => !(name in result.nonsecret_config)))) throw invalid();
    } catch (error) { $('#config-state').textContent = '配置读取失败，禁止按未知版本保存。'; throw error; }
    configRevision = result ? result.revision : null; configLoaded = true;
    $('#config-state').textContent = result ? '当前版本：' + result.revision : '尚无全局配置；保存后生成首个版本。';
    if (result) for (const name of configNames) $('#config-' + name).value = result.nonsecret_config[name];
  }
  $('#load-config').addEventListener('click',() => run(async () => { invalidatePreflight(); await loadConfig(); }));
  $('#config-form').addEventListener('submit',event => {
    event.preventDefault(); if (!has('config:manage') || !configLoaded || pending) return;
    const fields = {};
    for (const name of configNames) fields[name] = ['timeout_seconds','concurrency','retention_days'].includes(name) ? Number($('#config-' + name).value) : $('#config-' + name).value;
    run(() => mutate(base + '/config','PUT',{expected_revision:configRevision,fields,request_key:crypto.randomUUID()},'config:manage',async () => {
      invalidatePreflight(); message('新配置版本已受理；旧任务不变，未启动任何 worker。',false);
      try { await loadConfig(); } catch (error) { followupFail(error,'配置已受理，读取新版本失败。'); }
    }));
  });
  function batchInput() {
    const count = Number($('#requested-count').value), selection = $('#selection').value;
    const ids = selection === 'specified' ? [...selected].sort() : [];
    if (!Number.isInteger(count) || count < 1 || count > 100 || (selection === 'specified' && ids.length !== count)) throw {status:422};
    return {selection,requested_count:count,mailbox_ids:ids};
  }
  for (const id of ['selection','requested-count']) $('#' + id).addEventListener('input',invalidatePreflight);
  $('#batch-form').addEventListener('submit',event => {
    event.preventDefault(); if (!has('tasks:manage') || pending) return;
    run(async () => {
      preflight = null; $('#preflight-result').textContent = '正在只读预检…';
      let input, result;
      try {
        input = batchInput();
        result = await api(base + '/preflight','POST',input);
        if (!result || result.observation_only !== true || typeof result.can_create !== 'boolean' || !Array.isArray(result.reason_codes) || !Number.isInteger(result.eligible_count)) throw invalid();
      } catch (error) { if (csrf) $('#preflight-result').textContent = '预检未完成；不能创建批次。'; throw error; }
      if (!csrf) return; // A late observation must not outlive the session.
      $('#preflight-result').textContent = '只读观察：要求 ' + input.requested_count + ' 个，观察合格 ' + result.eligible_count + ' 个。' + (result.can_create ? '可进入确认，但创建时仍会重新校验配置与资源锁。' : '当前不能创建：' + result.reason_codes.join(' / ')) + ' 配置版本：' + (result.config_revision || '尚无配置');
      preflight = {...result,input};
    });
  });
  async function batchAccepted(result) {
    if (!result || !uuid(result.batch_id) || !Array.isArray(result.task_ids)) throw {status:0};
    preflight = null;
    const target = $('#batch-result'); target.replaceChildren(node('p','合成批次已受理，非注册成功。批次：' + result.batch_id));
    for (const id of result.task_ids) target.append(node('p','任务：' + id,'mono'));
    message('批次已受理；仅创建逻辑任务，未运行注册、支付或模型调用。',false);
    $('#task-batch').value = result.batch_id;
    // The batch is committed: refresh both observations and report read failures as such.
    const failed = [];
    for (const [label,load] of [['任务列表',loadTasks],['邮箱列表',loadMailboxes]]) {
      if (!csrf) break;
      try { await load(); } catch (error) { failed.push({label,error}); if (error.status === 401) break; }
    }
    if (failed.length) followupFail(failed[failed.length - 1].error,'批次已受理，' + failed.map(item => item.label).join('、') + '刷新失败。');
  }
  $('#confirm-batch').addEventListener('click',() => {
    if (!preflight || !preflight.can_create || pending || !has('tasks:manage')) return;
    if (!window.confirm('确认创建足量 ' + preflight.input.requested_count + ' 个合成任务？这不代表注册成功，不执行真实流程。')) return;
    const body = {...preflight.input,expected_config_revision:preflight.config_revision,request_key:crypto.randomUUID()};
    preflight = null; $('#preflight-result').textContent = '本次观察已用于提交；再次创建须重新预检。';
    run(() => mutate(base + '/batches','POST',body,'tasks:manage',batchAccepted));
  });
  async function loadTasks(cursor = null) {
    $('#task-list-state').textContent = '正在加载本人任务…'; taskCursor = null;
    const query = new URLSearchParams({scope:$('#task-scope').value,limit:'50'});
    if ($('#task-batch').value.trim()) query.set('batch_id',$('#task-batch').value.trim());
    if (cursor) query.set('cursor',cursor);
    try {
      const result = validPage(await api(base + '/tasks?' + query),'task');
      const cards = result.items.map(row => {
        const card = node('article',undefined,'pool-item'), summary = node('p',row.execution_scope + ' · ' + row.status + ' · 版本 ' + row.version);
        card.dataset.taskRow = row.id; summary.dataset.taskSummary = ''; card.append(node('p',row.id,'mono'),summary);
        const open = node('button','查看任务'); open.type = 'button'; open.dataset.openTask = ''; open.dataset.permission = 'onboarding:read'; open.setAttribute('aria-label','查看任务 ' + row.id);
        open.addEventListener('click',() => run(() => loadTask(row.id))); card.append(open); return card;
      });
      $('#task-list').replaceChildren(...cards); taskCursor = result.next_cursor;
      $('#task-list-state').textContent = result.items.length ? '本页 ' + result.items.length + ' 个本人任务' : '当前范围没有任务。';
    } catch (error) { $('#task-list').replaceChildren(); $('#task-list-state').textContent = '任务列表读取失败，不代表没有任务。'; throw error; }
    controls();
  }
  function renderTask(value) {
    const fixtureFields = ['id','status','version','reason_code','current_step','generation','cancel_requested','synthetic'];
    const legacyFixture = value && value.synthetic === true && Object.keys(value).length === fixtureFields.length && fixtureFields.every(key => key in value);
    if (!value || !uuid(value.id) || !Number.isSafeInteger(value.version) || (!legacyFixture && !['fixture','pool'].includes(value.execution_scope))) throw invalid();
    // Original fixture detail deliberately retains its pre-pool wire contract.
    if (legacyFixture) value = {...value,execution_scope:'fixture'};
    task = value; taskId = value.id; $('#task-empty').hidden = true; $('#task-facts').hidden = false;
    const facts = [];
    for (const [label,content] of [['任务 ID',value.id],['范围',value.execution_scope],['状态',value.status],['当前版本',value.version],['配置版本',value.config_revision],['当前步骤',value.current_step || '尚未开始'],['原因码',value.reason_code || '无'],['取消请求',value.cancel_requested ? '已记录' : '无']]) facts.push(node('dt',label),node('dd',content == null ? '无' : content));
    $('#task-facts').replaceChildren(...facts);
    // Keep the list observation in step with this fresher detail read.
    for (const card of all('[data-task-row]')) if (card.dataset.taskRow === value.id) card.querySelector('[data-task-summary]').textContent = value.execution_scope + ' · ' + value.status + ' · 版本 ' + value.version;
  }
  async function loadTask(id) {
    task = null; taskId = id;
    $('#task-empty').hidden = false; $('#task-empty').textContent = '正在读取最新状态…'; $('#task-facts').hidden = true;
    try { renderTask(await api(base + '/tasks/' + id)); }
    catch (error) { $('#task-empty').textContent = '状态读取失败，请手动刷新；不能按旧版本操作。'; throw error; }
  }
  $('#refresh-tasks').addEventListener('click',() => run(loadTasks));
  $('#task-next').addEventListener('click',() => { const cursor = taskCursor; if (cursor) run(() => loadTasks(cursor)); });
  $('#task-batch').addEventListener('input',() => { taskCursor = null; controls(); });
  $('#task-scope').addEventListener('change',() => { taskCursor = null; controls(); });
  $('#task-filter-form').addEventListener('submit',event => { event.preventDefault(); run(loadTasks); });
  $('#refresh-task').addEventListener('click',() => { if (taskId) run(() => loadTask(taskId)); });
  for (const action of ['pause','cancel','recheck']) $('#' + action + '-task').addEventListener('click',() => {
    if (!task || !has('tasks:manage') || pending) return;
    if (action === 'cancel' && !window.confirm('仅请求取消后续步骤，不回滚外部结果、不立即释放 hold。确认？')) return;
    const id = task.id, version = task.version;
    run(() => mutate(base + '/tasks/' + id + '/' + action,'POST',{expected_version:version,request_key:crypto.randomUUID()},'tasks:manage',async result => {
      if (!result || result.accepted !== true) throw {status:0};
      task = null;
      $('#command-receipt').textContent = '已受理回执：' + result.receipt_id + '；回执阶段：' + (result.phase || '已提交') + '（不是外部业务成功）。';
      message(action === 'recheck' ? '命令已受理：仅排入核验标记，没有运行 inspector。' : '命令已受理；并非外部操作已回滚。',false);
      // Receipt acceptance and a subsequent GET are independent outcomes.
      try { await loadTask(id); } catch (error) { followupFail(error,'命令已受理，状态刷新失败。'); }
    }));
  });
  $('#reconcile-request').addEventListener('click',() => {
    if (!pending || !has(pending.permission)) return;
    if (pending.secret) { switchTab('import'); $('#import-text').focus(); message('请重新输入原导入文本与分组，预览核对摘要后明确确认；本页已不持有原秘密。'); return; }
    if (!window.confirm('明确使用原请求键与原内容核对提交结果？不会生成新键。')) return;
    const request = pending;
    run(async () => {
      try { await mutate(request.path,request.method,request.body,request.permission,request.accepted); }
      catch (error) {
        if (!releaseProven(error,request,'版本已变化')) throw error;
        message('服务端已证明原请求未提交；未决已解除。请刷新数据并重新预检后按新请求操作。');
      }
    });
  });
  $('#logout').addEventListener('click',() => run(async () => {
    // Logout revokes the session server-side: a failure leaves that write unconfirmed.
    try { await api('/api/auth/logout','POST',{}); } catch (error) { error.mutation = true; throw error; }
    expire(); window.location.replace('/login');
  }));
  window.addEventListener('pagehide',() => { expire(); });
  (async () => {
    busy = true; controls(); $('#main').setAttribute('aria-busy','true');
    try {
      const session = await api('/api/auth/session');
      if (inactive) return; // Hidden before the session answered: never re-enable this page.
      if (!session || typeof session.csrf_token !== 'string' || !Array.isArray(session.permissions)) throw invalid();
      // Read once per page and only cleared afterwards: releaseProven relies on this page never
      // switching sessions (another operator's proof must not release this operator's request).
      csrf = session.csrf_token; permissions = new Set(session.permissions); $('#operator').textContent = session.display_name;
      if (has('onboarding:read')) {
        try { await loadMailboxes(); } catch (error) { fail(error); }
        if (csrf) try { await loadTasks(); } catch (error) { fail(error); }
        else $('#task-list-state').textContent = '会话已失效，未读取任务。';
      } else { $('#mailbox-state').textContent = '没有邮箱读取权限。'; $('#task-list-state').textContent = '没有任务读取权限。'; }
      if (has('config:manage')) try { await loadConfig(); } catch (error) { $('#config-state').textContent = '配置读取失败，禁止按未知版本保存。'; fail(error); }
      else if (!csrf) $('#config-state').textContent = '会话已失效，未读取配置。';
    } catch (error) {
      $('#operator').textContent = '会话核验失败';
      $('#mailbox-state').textContent = '未能核验会话，未读取邮箱。'; $('#task-list-state').textContent = '未能核验会话，未读取任务。';
      $('#config-state').textContent = '未能核验会话，未读取配置。';
      fail(error);
    }
    finally { busy = false; $('#main').setAttribute('aria-busy','false'); controls(); }
  })();
})();
