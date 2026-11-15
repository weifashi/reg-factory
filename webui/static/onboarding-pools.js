'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const all = selector => document.querySelectorAll(selector);
  const embedded = document.body.dataset.consoleMode === 'protected' && Boolean($('#view-onboarding'));
  const base = '/api/onboarding';
  // Capture the server-rendered mode once; changing a select is not authority.
  const liveMode = $('#main').dataset.poolMode === 'live';
  let capabilities = null, liveConfigReady = false;
  const liveEditable = ['billing_account','project_prefix','service_account_prefix'];
  const liveFixed = {schema_version:2,workflow:'existing_billing_cloud_v1',region:'us-central1',
    approved_role:'roles/aiplatform.user',google_auth_profile_ref:'local-authorized-google',
    instance_ref:'local-sub2api-approved',group_ref:'2',timeout_seconds:180,concurrency:1,
    scheduling_enabled:false,model_verification_enabled:false};
  const addons = new Set();
  const runtimeState = () => ({busy,inactive,pending});
  const configNames = ['model','region','instance_ref','group_ref','project_prefix','timeout_seconds','concurrency','retention_days'];
  let csrf = '', permissions = new Set(), busy = false;
  let mailboxRows = [], mailboxPages = [null], mailboxPageIndex = 0;
  let mailboxCursor = null, taskCursor = null, selected = new Set(), mailboxStale = false;
  let preview = null, preflight = null, configRevision = null, configLoaded = false;
  let task = null, taskId = null, pending = null, inactive = false;
  let googleLocalLog = [], googleServerLog = [], googleInputRevision = 0;
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
  function expire(notifyShell = true) {
    closeMailboxDetail();
    inactive = true; csrf = ''; permissions.clear(); clearImport(); pending = null; preflight = null;
    task = null; taskId = null; selected.clear(); configLoaded = false; capabilities = null; liveConfigReady = false;
    googleLocalLog = []; googleServerLog = [];
    if ($('#google-log')) { $('#google-log').replaceChildren(); $('#google-log-state').textContent = '会话已结束，请重新登录。'; $('#google-run-status').textContent = '会话已结束'; }
    $('#login-link').hidden = false; $('#reconcile-card').hidden = true; $('#reconcile-note').textContent = '';
    $('#preflight-result').textContent = '会话已结束；本页不再保留预检观察。';
    $('#task-facts').replaceChildren(); $('#task-facts').hidden = true;
    for (const addon of addons) addon.expire?.();
    controls();
    if (embedded && notifyShell) window.RFProtectedConsole.expire({status:401});
  }
  function controls() {
    for (const item of all('[data-permission]')) item.disabled = busy || !has(item.dataset.permission);
    const writing = busy || Boolean(pending);
    $('#logout').disabled = busy || !csrf;
    $('#mailbox-next').disabled = busy || !has('onboarding:read') || !mailboxCursor;
    $('#mailbox-prev').disabled = busy || !has('onboarding:read') || mailboxPageIndex === 0;
    $('#mailbox-page-size').disabled = busy || !has('onboarding:read');
    $('#mailbox-select-page').disabled = liveMode || writing || !has('tasks:manage') || !mailboxRows.length;
    for (const item of all('[data-mailbox-edit]')) item.disabled = liveMode || writing || mailboxStale || !has('mailboxes:manage');
    for (const item of all('[data-mailbox-detail]')) item.disabled = busy || !has('onboarding:read');
    $('#task-next').disabled = busy || !has('onboarding:read') || !taskCursor;
    $('#preview-import').disabled = busy || !has('mailboxes:manage') || Boolean(pending && (!pending.secret || pending.kind === 'card-import'));
    $('#confirm-import').disabled = busy || !has('mailboxes:manage') || !preview || Boolean(pending && (!pending.secret || pending.kind === 'card-import'));
    $('#confirm-import').hidden = !preview;
    $('#confirm-import').textContent = preview ? (pending ? '核对导入 ' : '导入 ') + preview.count + ' 条' : '导入';
    $('#import-text').disabled = busy || !has('mailboxes:manage') || Boolean(pending && (!pending.secret || pending.kind === 'card-import'));
    $('#import-group').disabled = busy || !has('mailboxes:manage') || Boolean(pending && (!pending.secret || pending.kind === 'card-import'));
    $('#cancel-import').disabled = busy;
    $('#tab-list').disabled = busy; $('#tab-import').disabled = busy;
    $('#config-fields').disabled = writing || !has('config:manage') || !configLoaded;
    $('#save-config').disabled = writing || !has('config:manage') || !configLoaded;
    $('#preflight').disabled = liveMode || writing || !has('tasks:manage');
    $('#confirm-batch').disabled = liveMode || writing || !has('tasks:manage') || !preflight || !preflight.can_create;
    if ($('#run-google')) {
      $('#run-google').disabled = writing || !has('tasks:manage') || (liveMode ?
        !capabilities?.start_available || !liveConfigReady || !configLoaded || $('#google-execution-mode').value !== 'live' : $('#google-execution-mode').value !== 'synthetic');
      $('#google-execution-mode').disabled = liveMode || writing || !has('tasks:manage');
      $('#refresh-google-log').disabled = busy || !has('onboarding:read') || !taskId;
    }
    $('#refresh-task').disabled = busy || !has('onboarding:read') || !taskId;
    for (const action of ['pause','cancel','recheck']) $('#' + action + '-task').disabled = writing || !has('tasks:manage') || !task || taskReadOnly(task) || liveMode && action === 'recheck';
    for (const item of all('[data-mailbox-write]')) item.disabled = liveMode || writing || mailboxStale || !has('mailboxes:manage');
    for (const item of all('[data-mailbox-select]')) item.disabled = liveMode || writing || !has('tasks:manage');
    $('#selection').disabled = liveMode || writing || !has('tasks:manage');
    $('#requested-count').disabled = liveMode || writing || !has('tasks:manage');
    if (liveMode) {
      $('#import-text').disabled = true;
      $('#preview-import').disabled = writing || !has('mailboxes:manage') || !capabilities?.source.available;
      $('#confirm-import').disabled = writing || !has('mailboxes:manage') || !preview || !capabilities?.source.available;
      $('#import-group').disabled = writing || !has('mailboxes:manage');
      for (const name of configNames) $('#config-' + name).disabled = true;
    } else for (const name of liveEditable) $('#live-config-' + name).disabled = true;
    if (task && ['SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE'].includes(task.status)) { $('#pause-task').disabled = true; $('#cancel-task').disabled = true; }
    $('#reconcile-request').disabled = busy || !pending || !has(pending.permission);
    for (const item of all('[data-filter]')) item.disabled = busy;
    for (const addon of addons) addon.controls?.(runtimeState());
    if (embedded) window.dispatchEvent(new CustomEvent('rf:pool-state',{detail:{busy,canLogout:!busy && Boolean(csrf)}}));
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
    for (const addon of addons) addon.failure?.(error);
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
      // The shared shell can expire while this response is in flight. Never revive its observations.
      if (embedded && inactive) throw {status:401};
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
    if (request.path.endsWith('/live/start')) return '启动已有结算云资源链';
    if (request.path.endsWith('/live/source-import')) return '导入可信邮箱来源';
    if (request.path.endsWith('/batches')) return '创建批次';
    if (request.path.endsWith('/config')) return '保存全局配置';
    if (request.method === 'PATCH') return request.path.includes('/cards/') ? '修改信用卡' : '修改邮箱';
    return {pause:'暂停任务',cancel:'请求取消任务',recheck:'排入只读核验'}[request.path.split('/').pop()] || '写操作';
  }
  function remember(request) {
    if (!csrf) return; // Expired/page-hidden pages must not revive cleared requests.
    pending = request;
    $('#reconcile-card').hidden = false;
    $('#reconcile-note').textContent = request.secret
      ? (request.kind === 'card-import' ? '信用卡导入结果未知。原卡片正文已清空；请重新提供相同资料并预览，只能用原摘要和原键核对。请求键：' : '导入结果未知。原秘密正文已清空；请重新提供完全相同文本和分组，再预览。摘要一致时才可用原键确认核对。请求键：') + request.key
      : '操作：' + operation(request) + '；原请求键：' + request.body.request_key + '。原非秘密请求内容仅留在本页内存。';
    controls();
  }
  function forget() { pending = null; $('#reconcile-card').hidden = true; $('#reconcile-note').textContent = ''; controls(); }
  function setupLiveView() {
    if (!liveMode) return;
    if ($('.environment')) $('.environment').textContent = '已有结算云资源 · 首次试用未接入';
    if ($('#shell-sidebar .script-nav .sidebar-copy')) $('#shell-sidebar .script-nav .sidebar-copy').textContent = '仅已批准的云资源链；不启用旧任务或首次试用。';
    if ($('#main > footer')) $('#main > footer').textContent = '真实云资源操作须当前授权；首次注册、试用、银行卡及赠金未接入。旧历史不提升为真实任务。';
    const option = node('option','已有结算账号 · 云资源链'); option.value = 'live';
    $('#google-execution-mode').replaceChildren(option);
    $('#google-mode-help').textContent = '仅使用服务端固定邮箱与已有结算账号创建云资源；不注册 Google、不申请试用、不绑卡、不申请赠金，不调用模型或启用调度。';
    $('#batch-card .form-title').textContent = 'Google 云资源任务';
    $('#batch-card .form-desc').textContent = '固定单邮箱，使用已有结算账号；在右侧查看真实回执与待核验状态。';
    $('#google-advanced').hidden = true;
    $('#requested-count').value = '1';
    $('label[for="requested-count"]').textContent = '固定数量';
    const fixedMailbox = node('option','服务端固定邮箱'); fixedMailbox.value = 'fixed';
    $('#selection').replaceChildren(fixedMailbox);
    $('#selected-count + .muted a').textContent = '前往邮箱池查看可信来源';
    $('#selected-count').textContent = '仅使用服务端登记的 1 个邮箱；列表选择不影响真实启动。';
    $('.google-run-actions .footnote').textContent = '点击运行 → 服务端重新校验 → 双提交确认后移交后台。202 不代表开通成功；原键重放只观察，不重启。';
    $('#live-source-state').hidden = false;
    $('#live-source-state').textContent = '正在核对服务端可信邮箱绑定；未完成前不能运行。';
    $('#import-secret-fields').hidden = true; $('#import-formats').hidden = true;
    $('.mail-import-heading .muted').textContent = '从服务端已批准的本地来源预览并导入唯一授权邮箱；页面不接收密码、令牌或文件路径。';
    $('#import-environment').textContent = '导入仅登记管理权和加密凭据，不验证账号资格，也不启动任务。';
    $('#preview-import').textContent = '预览可信来源';
    $('.mail-import-rules').textContent = '只导入可信来源，不覆盖已有不同凭据；完成后须由可信启动配置绑定邮箱，不能在页面自动换绑。';
    $('#card-pool > .notice').textContent = '真实云资源模式未接入银行卡池，旧合成卡服务不会用于真实支付。本页不收集真实卡号、CVV 或验证码，也不自动绑卡。';
    $('#synthetic-config-values').hidden = true; $('#live-config-values').hidden = false;
    $('#config-legend').textContent = '已有结算云资源配置 · 无密码和密钥';
    for (const name of liveEditable) $('#live-config-' + name).required = true;
    $('#global-config > .muted').textContent = '保存追加 v2 配置，仅影响新任务；旧任务固定原版本。保存不启动云操作，也不代表首次试用资格已核验。';
  }
  async function loadCapabilities() {
    capabilities = null;
    try {
      const value = await api(base + '/live/capabilities');
      if (!value || Object.keys(value).sort().join(',') !== 'mode,source,start_available,workflow' || value.mode !== 'live' ||
          value.workflow !== liveFixed.workflow || typeof value.start_available !== 'boolean' || !value.source ||
          Object.keys(value.source).sort().join(',') !== 'available,mailbox_id,ref' || typeof value.source.available !== 'boolean' ||
          value.source.ref !== 'local-authorized-mailbox' || !(value.source.mailbox_id === null || uuid(value.source.mailbox_id)) ||
          value.start_available && (!value.source.available || !uuid(value.source.mailbox_id))) throw invalid();
      if (!csrf || inactive) return;
      capabilities = value;
      $('#live-source-state').textContent = value.start_available ? '已读取服务端固定单邮箱绑定；启动时仍重新核验权限、配置、凭据和占用。' :
        '当前不能启动：可信来源或邮箱绑定未就绪。导入后仍需管理员核对可信启动配置。';
    } catch (error) { $('#live-source-state').textContent = '可信启动能力读取失败；禁止启动，不回退旧执行器。'; throw error; }
  }
  async function previewSource() {
    if (!capabilities?.source.available || pending) return;
    preview = null;
    try {
      const group = $('#import-group').value;
      const result = await api(base + '/live/source-preview','POST',{group_ref:group});
      if (!result || typeof result.email !== 'string' || result.email.length > 320 || result.provider !== 'other' ||
          result.group_ref !== group || typeof result.preview_digest !== 'string' || !/^[a-f0-9]{64}$/.test(result.preview_digest)) throw invalid();
      if (!csrf || inactive) return;
      $('#import-preview').replaceChildren(node('h4','可信来源预览（未导入）'),node('p',result.email + ' · ' + (group || '未分组')));
      preview = {digest:result.preview_digest,group,count:1};
    } catch (error) { clearImport(); throw error; }
  }
  function importSource() {
    if (!preview || pending || !capabilities?.source.available || !has('mailboxes:manage')) return;
    if (!window.confirm('确认从固定可信来源导入此邮箱？这不会验证试用资格或启动任务。')) return;
    const body = {group_ref:preview.group,preview_digest:preview.digest,request_key:crypto.randomUUID()};
    run(() => mutate(base + '/live/source-import','POST',body,'mailboxes:manage',async result => {
      if (!result || !uuid(result.mailbox_id) || typeof result.created !== 'boolean' || result.request_key !== body.request_key) throw {status:0};
      clearImport(); capabilities = null;
      $('#live-source-state').textContent = '导入回执已收到；须由管理员核对可信启动配置并绑定邮箱后再运行。';
      const summary = '可信来源导入已受理；需由可信启动配置绑定邮箱。不会自动启动或将账号标记为可用。';
      message(summary,false);
      try { await loadMailboxes(); } catch (error) { followupFail(error,summary + '列表刷新失败。'); }
    }));
  }
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
      if (kind === 'task') taskMode(row);
    }
    return value;
  }
  function filters(cursor) {
    const query = new URLSearchParams({platform:$('#platform').value,limit:$('#mailbox-page-size').value});
    for (const [id,key] of [['search','search'],['filter-group','group_ref'],['health','health'],['occupied','occupied']]) if ($('#' + id).value) query.set(key,$('#' + id).value);
    if (cursor) query.set('cursor',cursor);
    return query;
  }
  function selectionChanged() {
    $('#selected-count').textContent = '已选择 ' + selected.size + ' 个邮箱'; syncPageSelection(); invalidatePreflight();
  }
  function syncPageSelection() {
    const count = mailboxRows.filter(row => selected.has(row.id)).length;
    const check = $('#mailbox-select-page');
    check.checked = mailboxRows.length > 0 && count === mailboxRows.length;
    check.indeterminate = count > 0 && count < mailboxRows.length;
    $('#mailbox-selection-summary').textContent = '当前页已选 ' + count + ' · 共已选 ' + selected.size;
  }
  function closeMailboxDetail() {
    const dialog = $('#mailbox-detail');
    if (dialog.open) dialog.close();
    $('#mailbox-detail-content').replaceChildren();
  }
  function showMailboxDetail(row) {
    if (busy || !has('onboarding:read')) return;
    const content = $('#mailbox-detail-content'); content.replaceChildren();
    const facts = node('dl',undefined,'mailbox-detail-facts');
    for (const [label,value] of [['邮箱',row.email],['分组',row.group_ref || '未分组'],['健康',row.health],['新分配',row.disabled ? '已停用' : '未停用'],['逻辑占用',row.occupied ? '观察到逻辑占用' : '未观察到占用'],['内部 ID',row.id],['版本',row.version]]) facts.append(node('dt',label),node('dd',value));
    content.append(facts,node('h3','各平台使用状态'));
    for (const platform of row.platforms) {
      const item = node('div',undefined,'mailbox-platform-state');
      item.append(node('strong',platform.platform),node('p',platform.identity_status + ' / ' + platform.usage_status)); content.append(item);
    }
    content.append(node('p','UNKNOWN / HISTORY_UNRECONCILED 不等于可用。这里只展示已读取的状态，不会执行注册、释放占用或改变资格。','footnote'));
    $('#mailbox-detail').showModal();
  }
  $('#mailbox-detail-close').addEventListener('click',closeMailboxDetail);
  $('#mailbox-detail').addEventListener('close',() => $('#mailbox-detail-content').replaceChildren());
  function mailboxCard(row) {
    const card = node('tr'); card.dataset.mailboxRow = row.id;
    const choice = node('td'), check = node('input');
    check.type = 'checkbox'; check.dataset.mailboxSelect = ''; check.checked = selected.has(row.id); check.setAttribute('aria-label','选择邮箱：' + row.email);
    check.addEventListener('change',() => { if (busy || pending || !has('tasks:manage')) { check.checked = selected.has(row.id); return; } if (check.checked) selected.add(row.id); else selected.delete(row.id); selectionChanged(); });
    choice.append(check);
    const email = node('td'), address = node('span',row.email,'mailbox-email'); address.title = row.email; email.append(address);
    const grouping = node('td'), edit = node('button',row.group_ref || '未分组','mailbox-group-label');
    edit.type = 'button'; edit.dataset.mailboxEdit = ''; edit.title = row.group_ref || '未分组'; edit.setAttribute('aria-label','编辑分组：' + row.email); edit.setAttribute('aria-expanded','false');
    const actions = node('div',undefined,'mailbox-group-editor'), groupLabel = node('label','分组'), group = node('input'); actions.hidden = true;
    group.value = row.group_ref; group.maxLength = 128; group.dataset.mailboxGroup = ''; group.dataset.mailboxWrite = ''; groupLabel.append(group);
    const save = node('button','保存分组'), cancel = node('button','取消'), disable = node('button',row.disabled ? '启用新分配' : '停用新分配');
    save.type = cancel.type = disable.type = 'button'; save.dataset.saveGroup = ''; save.dataset.mailboxWrite = ''; disable.dataset.mailboxWrite = '';
    group.setAttribute('aria-label','分组：' + row.email); save.setAttribute('aria-label','保存分组：' + row.email);
    disable.setAttribute('aria-label',(row.disabled ? '启用新分配：' : '停用新分配：') + row.email);
    edit.addEventListener('click',() => { if (liveMode || busy || pending || mailboxStale || !has('mailboxes:manage')) return; actions.hidden = !actions.hidden; edit.setAttribute('aria-expanded',String(!actions.hidden)); if (!actions.hidden) group.focus(); });
    cancel.addEventListener('click',() => { actions.hidden = true; group.value = row.group_ref; edit.setAttribute('aria-expanded','false'); edit.focus(); });
    async function patch(changes) {
      if (liveMode || !has('mailboxes:manage') || pending || mailboxStale) return;
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
    actions.append(groupLabel,save,cancel); grouping.append(edit,actions);
    const health = node('td');
    const healthLabel = ({HEALTHY:'正常',UNKNOWN:'未知',NEEDS_REVIEW:'待核验',DISABLED:'停用'})[row.health] || row.health;
    const status = node('span',healthLabel,'mailbox-status'); status.title = row.health; health.append(status);
    if (row.disabled) health.append(node('span','已停用','mailbox-status'));
    const occupancy = node('td',row.occupied ? '占用中' : '未观察到占用');
    const tools = node('td'), toolRow = node('div',undefined,'mailbox-row-actions');
    const detail = node('button','详情'); detail.type = 'button'; detail.dataset.mailboxDetail = ''; detail.setAttribute('aria-label','邮箱详情：' + row.email); detail.addEventListener('click',() => showMailboxDetail(row));
    const more = node('details',undefined,'mailbox-more'), summary = node('summary','更多'); summary.dataset.mailboxMore = ''; summary.setAttribute('aria-label','更多操作：' + row.email); more.append(summary,disable);
    toolRow.append(detail,more); tools.append(toolRow); card.append(choice,email,grouping,health,occupancy,tools); return card;
  }
  async function loadMailboxes(cursor = null, pageIndex = 0) {
    closeMailboxDetail();
    $('#mailbox-state').textContent = '正在加载本人邮箱…'; mailboxCursor = null;
    try {
      const data = validPage(await api(base + '/mailboxes?' + filters(cursor)),'mailbox');
      const cards = data.items.map(mailboxCard); $('#mailbox-list').replaceChildren(...cards); mailboxCursor = data.next_cursor; mailboxStale = false; mailboxRows = data.items;
      mailboxPageIndex = pageIndex; mailboxPages = mailboxPages.slice(0,pageIndex); mailboxPages.push(cursor);
      $('#mailbox-page-label').textContent = '第 ' + (pageIndex + 1) + ' 页'; syncPageSelection();
      $('#mailbox-state').textContent = data.items.length ? '本页 ' + data.items.length + ' 个邮箱 · 仅本人可见' : '当前筛选没有邮箱。可调整筛选或导入合成文本。';
    } catch (error) { mailboxRows = []; mailboxPages = [null]; mailboxPageIndex = 0; mailboxStale = true; syncPageSelection(); $('#mailbox-page-label').textContent = '未加载'; $('#mailbox-list').replaceChildren(); $('#mailbox-state').textContent = '邮箱读取失败；未显示部分坏行或伪造空列表。'; throw error; }
    controls();
  }
  function switchTab(name) {
    closeMailboxDetail();
    if (name !== 'import') clearImport();
    for (const tab of ['list','import']) {
      const active = name === tab; $('#tab-' + tab).setAttribute('aria-selected',String(active)); $('#tab-' + tab).tabIndex = active ? 0 : -1; $('#panel-' + tab).hidden = !active;
    }
    controls();
  }
  function beforeNavigate(target) {
    if (inactive || busy) return false;
    if (pending && !['google','mailboxes','mailpool','cards','settings'].includes(target)) {
      message('原请求结果尚未确认；请先在当前模块核对，不能切换到其他工具。');
      return false;
    }
    for (const addon of addons) if (addon.beforeNavigate?.(target) === false) return false;
    if (!['mailboxes','mailpool'].includes(target)) switchTab('list');
    return true;
  }
  async function addonApi(path, method, body) {
    if (inactive) throw {status:401};
    const result = await api(path,method,body);
    if (inactive) throw {status:401};
    return result;
  }
  function addonMutate(path, method, body, permission, accepted) {
    if (inactive) return Promise.reject({status:401});
    return mutate(path,method,body,permission,result => {
      if (inactive) throw {status:401};
      return accepted(result);
    });
  }
  // First-party modules share this single session and write lifecycle. No credential
  // or CSRF value is exposed, persisted, or renewed by the adapter.
  window.RFPoolRuntime = Object.freeze({
    api:addonApi,run,mutate:addonMutate,has:permission => !(liveMode && permission === 'cards:manage') && has(permission),message,followupFail,remember,forget,beforeNavigate,
    state:runtimeState,
    register(addon) {
      addons.add(addon);
      if (inactive) addon.expire?.();
      addon.controls?.(runtimeState());
    }
  });
  if (embedded) {
    window.RFPoolPanel = Object.freeze({beforeNavigate});
    window.addEventListener('rf:session-expired',() => expire(false));
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
  function resetFilters() { mailboxPages = [null]; mailboxPageIndex = 0; mailboxCursor = null; selected.clear(); selectionChanged(); }
  $('#filter-form').addEventListener('submit',event => { event.preventDefault(); if (busy) return; resetFilters(); run(loadMailboxes); });
  for (const id of ['platform','health','occupied','mailbox-page-size']) $('#' + id).addEventListener('change',() => { if (busy) return; resetFilters(); run(loadMailboxes); });
  for (const id of ['platform','health','occupied','search','filter-group','task-scope','task-batch']) $('#' + id).dataset.filter = '';
  $('#mailbox-next').addEventListener('click',() => { const cursor = mailboxCursor; if (cursor && !busy && has('onboarding:read')) run(() => loadMailboxes(cursor,mailboxPageIndex + 1)); });
  $('#mailbox-prev').addEventListener('click',() => { if (mailboxPageIndex > 0 && !busy && has('onboarding:read')) { const index = mailboxPageIndex - 1; run(() => loadMailboxes(mailboxPages[index],index)); } });
  $('#mailbox-select-page').addEventListener('change',() => {
    if (busy || pending || !has('tasks:manage')) { syncPageSelection(); return; }
    const checked = $('#mailbox-select-page').checked;
    for (const row of mailboxRows) { if (checked) selected.add(row.id); else selected.delete(row.id); }
    for (const check of all('[data-mailbox-select]')) check.checked = checked;
    selectionChanged();
  });
  // Cursors are bound to the displayed page's filters; editing a text filter drops it until 查询.
  for (const id of ['search','filter-group']) $('#' + id).addEventListener('input',() => { mailboxCursor = null; mailboxPages = [null]; mailboxPageIndex = 0; controls(); });
  for (const id of ['import-text','import-group']) $('#' + id).addEventListener('input',() => { preview = null; $('#import-preview').replaceChildren(); controls(); });
  $('#cancel-import').addEventListener('click',() => { clearImport(); message('导入输入已清空；已提交的未决请求仍需核对。',false); controls(); });
  $('#import-form').addEventListener('submit',event => {
    event.preventDefault(); if (!has('mailboxes:manage') || (pending && (!pending.secret || pending.kind === 'card-import'))) return;
    if (liveMode) { run(previewSource); return; }
    run(async () => {
      preview = null;
      try {
        const result = await api(base + '/mailboxes/import-preview','POST',{text:$('#import-text').value,group_ref:$('#import-group').value});
        if (!result || !Array.isArray(result.items) || !Array.isArray(result.issues) ||
            ['accepted_count','duplicate_count','conflict_count'].some(key => !Number.isSafeInteger(result[key]) || result[key] < 0 || result[key] > 1000)) throw invalid();
        const target = $('#import-preview');
        const otherIssues = result.issues.filter(issue => !['DUPLICATE_EMAIL','CONFLICTING_EMAIL'].includes(issue.code)).length;
        target.replaceChildren(node('h4',result.issues.length ? '请先修正以下资料，再重新检查' : result.accepted_count ? '资料检查通过' : '未找到可导入的邮箱资料'));
        target.append(node('p',(result.issues.length ? '已识别 ' : '检查通过 ') + result.accepted_count + ' 条 · 重复 ' + result.duplicate_count + ' 条 · 冲突 ' + result.conflict_count + ' 条 · 其他问题 ' + otherIssues + ' 条','mail-import-summary'));
        for (const row of result.items) target.append(node('p','第 ' + row.line + ' 行 · ' + row.email + ' · ' + row.provider + ' · ' + (row.group_ref || '未分组')));
        const issueLabels = {DUPLICATE_EMAIL:'邮箱重复，请保留一条',CONFLICTING_EMAIL:'同一邮箱资料不一致，请核对',INVALID_RECORD:'资料格式不正确',INVALID_JSON:'JSON 格式不正确',DUPLICATE_JSON_KEY:'JSON 字段重复',UNSUPPORTED_RECORD_TYPE:'不支持此类资料',PLATFORM_BINDING_REQUIRED:'请勿混入平台账号密码',TOO_MANY_RECORDS:'超过 1000 条，请分批导入',TOO_LARGE:'资料超过大小限制，请分批导入'};
        for (const issue of result.issues) target.append(node('p','第 ' + issue.line + ' 行：' + (Object.hasOwn(issueLabels,issue.code) ? issueLabels[issue.code] : '资料不符合要求，请检查该行')));
        if (!result.issues.length && result.accepted_count) target.append(node('p','以下为待提交数量；实际新增或跳过数量以导入结果为准。','footnote'));
        if (!result.issues.length && /^[a-f0-9]{64}$/.test(result.preview_digest) && result.accepted_count > 0) {
          if (pending && (pending.digest !== result.preview_digest || pending.group !== $('#import-group').value)) {
            clearImport(); message('重新输入的内容与未决导入摘要不一致，已清空。请提供完全相同的文本与分组，不会换键导入。');
          } else preview = {digest:result.preview_digest,group:$('#import-group').value,count:result.accepted_count};
        }
      } catch (error) { clearImport(); throw error; }
    });
  });
  $('#confirm-import').addEventListener('click',() => {
    if (liveMode) { importSource(); return; }
    if (!preview || !has('mailboxes:manage') || (pending && (!pending.secret || pending.kind === 'card-import'))) return;
    if (!window.confirm(pending ? '使用原请求键和重新提供的相同内容核对导入结果？不会创建新键。' : '确认导入 ' + preview.count + ' 条邮箱资料？已有相同资料会跳过，不覆盖已有记录。')) { clearImport(); controls(); return; }
    const digest = preview.digest, group = preview.group, key = pending ? pending.key : crypto.randomUUID(), replay = pending;
    run(async () => {
      try {
        const result = await api(base + '/mailboxes/import','POST',{text:$('#import-text').value,group_ref:group,duplicate_action:'skip',preview_digest:digest,expected_versions:{},request_key:key});
        if (!result || !Array.isArray(result.created_ids) || !Array.isArray(result.skipped_ids)) throw {status:0};
        clearImport(); forget(); invalidatePreflight();
        const summary = '导入已受理：新增 ' + result.created_ids.length + '，跳过 ' + result.skipped_ids.length + '。';
        message(summary + '账号状态仍待核验，不能直接用于开通。',false);
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
    configLoaded = false; liveConfigReady = false; $('#config-state').textContent = '正在读取全局配置…';
    let result;
    try {
      result = await api(base + '/config');
      if (result !== null && (!result || typeof result.revision !== 'string' || !result.revision.startsWith('pool-') || !uuid(result.revision.slice(5)) || !result.nonsecret_config)) throw invalid();
      if (result) {
        const fields = result.nonsecret_config;
        if (liveMode && fields.schema_version === 2) {
          if (Object.keys(fields).length !== Object.keys(liveFixed).length + liveEditable.length ||
              Object.entries(liveFixed).some(([key,value]) => fields[key] !== value) ||
              liveEditable.some(key => typeof fields[key] !== 'string') ||
              !/^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$/.test(fields.billing_account) ||
              ['project_prefix','service_account_prefix'].some(key => !/^[a-z](?:[a-z0-9-]{0,10}[a-z0-9])?$/.test(fields[key]))) throw invalid();
          liveConfigReady = true;
        } else if (configNames.some(name => !(name in fields)) || liveMode && Object.keys(fields).length !== configNames.length) throw invalid();
      }
    } catch (error) { $('#config-state').textContent = '配置读取失败，禁止按未知版本保存。'; throw error; }
    configRevision = result ? result.revision : null; configLoaded = true;
    $('#config-state').textContent = result ? '当前版本：' + result.revision : '尚无全局配置；保存后生成首个版本。';
    if (liveMode) {
      $('#legacy-config-snapshot').hidden = !result || liveConfigReady;
      $('#legacy-config-fields').textContent = result && !liveConfigReady ? JSON.stringify(Object.fromEntries(configNames.map(name => [name,result.nonsecret_config[name]])),null,2) : '';
      for (const name of liveEditable) $('#live-config-' + name).value = liveConfigReady ? result.nonsecret_config[name] : '';
      if (result && !liveConfigReady) $('#config-state').textContent = '当前为旧配置（只读）：' + result.revision + '。不会自动转成真实执行；如需新任务，请明确填写已有结算字段并保存 v2 新版本。';
    } else if (result) for (const name of configNames) $('#config-' + name).value = result.nonsecret_config[name];
  }
  $('#load-config').addEventListener('click',() => run(async () => { invalidatePreflight(); await loadConfig(); }));
  $('#config-form').addEventListener('submit',event => {
    event.preventDefault(); if (!has('config:manage') || !configLoaded || pending) return;
    const fields = liveMode ? {...liveFixed} : {};
    if (liveMode) for (const name of liveEditable) fields[name] = $('#live-config-' + name).value;
    else for (const name of configNames) fields[name] = ['timeout_seconds','concurrency','retention_days'].includes(name) ? Number($('#config-' + name).value) : $('#config-' + name).value;
    run(() => mutate(base + '/config','PUT',{expected_revision:configRevision,fields,request_key:crypto.randomUUID()},'config:manage',async () => {
      invalidatePreflight(); message('新配置版本已受理；旧任务不变，未启动任何 worker。',false);
      try { await loadConfig(); } catch (error) { followupFail(error,'配置已受理，读取新版本失败。'); }
    }));
  });
  // The console logs actual local orchestration and persisted server events only.
  // It never synthesizes a Google/bank/worker success, nor stores logs in the browser.
  function renderGoogleLog() {
    if (!$('#google-log') || !csrf || inactive) return;
    const lines = [...googleLocalLog, ...googleServerLog];
    $('#google-log').replaceChildren(...lines.map(text => node('div',text,'google-log-line')));
    $('#google-log').scrollTop = $('#google-log').scrollHeight;
  }
  function googleNote(text) {
    if (!csrf || inactive || !$('#google-log')) return;
    googleLocalLog.push('[本页操作] ' + text);
    if (googleLocalLog.length > 100) googleLocalLog.shift();
    renderGoogleLog();
  }
  const cancelReasons = {
    CANCEL_SAFE_RELEASED:'已确认未执行，邮箱已释放，可重新参与分配',
    CANCEL_EXECUTION_UNCLEAR:'执行状态或代次不满足安全释放条件，保留占用',
    CANCEL_STEP_EXISTS:'已有步骤记录，不能证明从未执行，保留占用',
    CANCEL_PROOF_MISSING:'缺少完整创建证据，保留占用',
    CANCEL_HISTORY_UNCLEAR:'操作历史或回执无法证明未执行，保留占用',
    CANCEL_RELATED_RESOURCE:'存在卡片、授权或其他资源记录，保留占用',
    CANCEL_OTHER_TASK:'同一邮箱存在其他未结束任务，保留占用',
    CANCEL_HOLD_CHANGED:'租约归属或版本已变化，保留占用',
    CANCEL_PLATFORM_CHANGED:'平台状态或凭据版本已变化，保留占用'
  };
  const googleActions = {'task.create':'任务记录已创建（不代表注册成功）', 'task.pause':'暂停请求已记录',
    'task.cancel':'取消请求已记录（不代表旧占用已释放）', 'task.recheck':'已排入只读核验（未执行核验）'};
  const googleOutcomes = {'task.create':['OK','CREATED'], 'task.pause':['ACCEPTED'], 'task.cancel':['ACCEPTED','CANCELLED_SAFE',...Object.keys(cancelReasons).filter(code => code !== 'CANCEL_SAFE_RELEASED')], 'task.recheck':['ACCEPTED']};
  const cloudActions = {'project.create':'创建专用项目', 'billing.link':'关联已有结算账号',
    'service.enable.vertex':'开通 Vertex AI API', 'service_account.create':'创建服务账号',
    'project.iam.bind_sa':'授予 Vertex AI 使用权限', 'service_account.key.create':'加密保存服务账号密钥',
    'sub2api.import_disabled':'导入 Sub2API（保持停用）'};
  const cloudPhases = {INTENT:'操作意图已记录，结果未确认', UNKNOWN:'结果待核验',
    SUCCEEDED:'本步骤已核验（不代表整个任务完成）', FAILED_CONFIRMED:'本步骤确认失败', CONFLICT:'证据冲突，等待处理'};
  const cloudSuccess = {'project.create':['PROJECT_MATCHED'], 'billing.link':['BILLING_MATCHED'],
    'service.enable.vertex':['SERVICE_ENABLED'], 'service_account.create':['SA_CREATED','SA_MATCHED'],
    'project.iam.bind_sa':['IAM_BINDING_MATCHED'], 'service_account.key.create':['KEY_SAVED'],
    'sub2api.import_disabled':['DISABLED_IMPORT_VERIFIED']};
  const cloudReasons = new Set(('BILLING_UNAVAILABLE TOKEN_UNAVAILABLE NOT_FOUND READ_FAILED BILLING_ALREADY_LINKED '+
    'EXISTING_KEY_REQUIRES_RECONCILIATION PROJECT_IDENTITY_MISMATCH PROVIDER_CONFLICT SA_IDENTITY_MISMATCH '+
    'BILLING_NOT_CONFIRMED HTTP_UNCONFIRMED IAM_BINDING_NOT_CONFIRMED IAM_POLICY_INVALID KEY_LIST_INVALID '+
    'KEY_POSSESSION_REQUIRES_SECURE_STORAGE_RECONCILIATION KEY_STORAGE_OR_RESPONSE_UNKNOWN '+
    'OPERATION_ERROR_REQUIRES_READBACK OPERATION_INVALID READBACK_UNAVAILABLE RESPONSE_INVALID SERVICE_NOT_CONFIRMED '+
    'TRANSPORT_OR_RESPONSE_UNKNOWN PROVIDER_REJECTED GUARD_DENIED INVALID_CREDENTIAL PERMIT_TARGET_MISMATCH '+
    'PREFLIGHT_FAILED READBACK_MISMATCH ACCOUNT_ID_REQUIRED RECONCILIATION_REQUIRED EXECUTOR_STOPPED EXECUTOR_ERROR '+
    'READBACK_REQUIRED OPERATION_PENDING').split(' '));
  const liveStops = {'task.pause':{CLOUD_CHAIN_VERIFIED:'云资源步骤已核验；保留占用，首次试用、支付及赠金仍未核验',
    LIVE_RECONCILIATION_REQUIRED:'执行链已停止；保留占用，等待核验'},
    'task.cancel':{CANCELLED_SAFE_RELEASED:'未发送云资源或导入写入；执行链已停止，本地邮箱占用已释放'}};
  function validGoogleEvents(value, id) {
    if (!value || value.task_id !== id || !['synthetic','live'].includes(value.mode) || value.execution_connected !== false ||
        typeof value.has_more !== 'boolean' || !Array.isArray(value.events) || value.events.length > 100) throw invalid();
    if (task && task.id === id && value.mode !== taskMode(task)) throw invalid();
    let last = 0n;
    const lines = value.events.map(event => {
      if (!event || typeof event.id !== 'string' || !/^[1-9][0-9]{0,19}$/.test(event.id) || BigInt(event.id) <= last ||
          !Object.hasOwn(googleActions,event.action) || typeof event.occurred_at !== 'string' ||
          !/^\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)$/.test(event.occurred_at) || !Number.isFinite(Date.parse(event.occurred_at)) ||
          typeof event.outcome !== 'string' || !(googleOutcomes[event.action].includes(event.outcome) ||
            value.mode === 'live' && Object.hasOwn(liveStops,event.action) && Object.hasOwn(liveStops[event.action],event.outcome)) ||
          !(event.version === null || Number.isSafeInteger(event.version) && event.version > 0)) throw invalid();
      last = BigInt(event.id);
      if (value.mode === 'live' && Object.hasOwn(liveStops,event.action) && Object.hasOwn(liveStops[event.action],event.outcome))
        return '[服务端 #' + event.id + '] ' + event.occurred_at + ' · ' + liveStops[event.action][event.outcome];
      return '[服务端 #' + event.id + '] ' + event.occurred_at + ' · ' + (event.action === 'task.cancel' && event.outcome === 'CANCELLED_SAFE' ? '已安全取消，邮箱已释放（未回滚任何外部业务）' : event.action === 'task.cancel' && Object.hasOwn(cancelReasons,event.outcome) ? '取消请求已记录：' + cancelReasons[event.outcome] : googleActions[event.action]) +
        ' · ' + event.outcome + (event.version === null ? '' : ' · 版本 ' + event.version);
    });
    if (value.mode === 'live') {
      if (!Array.isArray(value.effect_events) || value.effect_events.length > 100 || typeof value.effects_has_more !== 'boolean') throw invalid();
      const seen = new Set();
      for (const event of value.effect_events) {
        if (!event || !uuid(event.receipt_id) || seen.has(event.receipt_id) || !Object.hasOwn(cloudActions,event.action) ||
            !Object.hasOwn(cloudPhases,event.phase) || !Number.isSafeInteger(event.version) || event.version < 1 ||
            typeof event.updated_at !== 'string' || !/^\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)$/.test(event.updated_at) ||
            !Number.isFinite(Date.parse(event.updated_at))) throw invalid();
        const validCode = event.phase === 'INTENT' ? event.result_code === null : typeof event.result_code === 'string' &&
          (event.phase === 'CONFLICT' ? event.result_code === 'CONFLICT' : event.phase === 'SUCCEEDED' ? cloudSuccess[event.action].includes(event.result_code) : cloudReasons.has(event.result_code));
        if (!validCode) throw invalid();
        seen.add(event.receipt_id);
        lines.push('[云资源回执] ' + event.updated_at + ' · ' + cloudActions[event.action] + ' · ' + cloudPhases[event.phase]);
      }
    } else if ('effect_events' in value || 'effects_has_more' in value) throw invalid();
    return lines;
  }
  async function loadGoogleEvents(id) {
    if (!$('#google-log') || !csrf || inactive) return;
    $('#google-log-state').textContent = '正在读取服务端操作记录…';
    try {
      const value = await api(base + '/tasks/' + id + '/events');
      if (!csrf || inactive || taskId !== id) return;
      const lines = validGoogleEvents(value,id);
      googleServerLog = lines; renderGoogleLog();
      $('#google-log-state').textContent = value.mode === 'live' ?
        '已读取云资源步骤记录；不代表首次试用或赠金已核验。' + (value.effects_has_more || value.has_more ? '仅显示最近 100 条。' : '') :
        (value.has_more ? '仅显示最近 100 条；更早记录仍保留在服务端。' : '已读取服务端操作记录。');
    } catch (error) {
      if (error.status === 401) { expire(); return; }
      if (!csrf || inactive || taskId !== id) return;
      googleServerLog = []; renderGoogleLog();
      $('#google-log-state').textContent = '日志读取失败，请手动刷新；已受理的操作不会因此回滚或重复提交。';
    }
  }
  function googlePreflight(value, input) {
    if (!value || value.observation_only !== true || typeof value.can_create !== 'boolean' ||
        !Array.isArray(value.reason_codes) || !Number.isSafeInteger(value.eligible_count) || value.eligible_count < 0 ||
        value.eligible_count > input.requested_count || value.reason_codes.some(code => typeof code !== 'string' || !/^[A-Z_]{1,64}$/.test(code))) throw invalid();
    if (value.can_create && (value.eligible_count !== input.requested_count || value.reason_codes.length ||
        typeof value.config_revision !== 'string' || !value.config_revision.startsWith('pool-') || !uuid(value.config_revision.slice(5)))) throw invalid();
    return value;
  }
  async function googleBatchAccepted(result, expectedCount) {
    if (!csrf || inactive) return;
    if (!result || !uuid(result.batch_id) || !Array.isArray(result.task_ids) ||
        result.task_ids.length !== expectedCount || result.task_ids.some(id => !uuid(id)) || new Set(result.task_ids).size !== expectedCount) throw {status:0};
    // Existing accepted handler preserves pending/receipt and follow-up read semantics.
    await batchAccepted(result);
    if (!csrf || inactive) return;
    googleNote('批次已受理：' + result.batch_id + '，共 ' + result.task_ids.length + ' 条任务记录。');
    if (result.task_ids.length && uuid(result.task_ids[0])) {
      taskId = result.task_ids[0];
      try { await loadTask(taskId); }
      catch (error) { followupFail(error,'批次已受理，当前任务读取失败。'); }
    }
  }
  async function liveStartAccepted(result, original) {
    if (!result || result.accepted !== true || result.synthetic !== false || result.execution_mode !== 'live' ||
        !uuid(result.batch_id) || !uuid(result.config_id) || !uuid(result.receipt_id) ||
        !Array.isArray(result.task_ids) || result.task_ids.length !== 1 || !uuid(result.task_ids[0]) ||
        !Array.isArray(result.mailbox_ids) || result.mailbox_ids.length !== 1 || result.mailbox_ids[0] !== original.mailbox_id ||
        result.config_revision !== original.expected_config_revision ||
        typeof result.replayed !== 'boolean' || result.scheduled !== !result.replayed) throw {status:0};
    if (!csrf || inactive) return;
    googleNote(result.scheduled ? '启动已移交后台；不代表首次试用、支付或赠金成功。' : '仅观察原回执；本次未重新启动，不代表旧任务没有外部效果。');
    await googleBatchAccepted(result,1);
  }
  function startLive() {
    if (!capabilities?.start_available || !configLoaded || !liveConfigReady || $('#google-execution-mode').value !== 'live') return;
    const body = {mailbox_id:capabilities.source.mailbox_id,expected_config_revision:configRevision,request_key:crypto.randomUUID()};
    run(async () => {
      googleLocalLog = []; googleServerLog = []; task = null; taskId = null;
      $('#task-facts').hidden = true; $('#command-receipt').textContent = '';
      googleNote('正在提交固定单邮箱已有结算云资源链；服务端仍将核验当前授权与资源。');
      $('#google-run-status').textContent = '正在提交';
      try { await mutate(base + '/live/start','POST',body,'tasks:manage',result => liveStartAccepted(result,body)); }
      catch (error) {
        googleNote(pending ? '提交结果未确认，仅可用原请求核对；不会自动重试。' : '启动请求被拒绝；不会回退旧执行器。');
        if (csrf && !inactive) $('#google-run-status').textContent = pending ? '提交未确认' : '请求被拒绝';
        throw error;
      }
    });
  }
  if ($('#run-google')) {
    $('#google-execution-mode').addEventListener('change',() => { googleInputRevision++; invalidatePreflight(); controls(); });
    for (const id of ['selection','requested-count']) $('#' + id).addEventListener('input',() => { googleInputRevision++; });
    $('#run-google').addEventListener('click',() => {
      if (liveMode) { if (has('tasks:manage') && !busy && !pending && !inactive) startLive(); return; }
      if (!has('tasks:manage') || busy || pending || inactive || $('#google-execution-mode').value !== 'synthetic') return;
      run(async () => {
        googleLocalLog = []; googleServerLog = []; task = null; taskId = null;
        $('#task-facts').hidden = true; $('#task-empty').hidden = false;
        $('#task-empty').textContent = '正在检查运行条件…'; $('#command-receipt').textContent = '';
        $('#google-run-status').textContent = '正在预检';
        $('#google-log-state').textContent = '正在检查任务创建条件。';
        invalidatePreflight();
        googleNote('运行任务：先检查配置与邮箱。');
        try {
          const input = batchInput(), revision = googleInputRevision;
          const result = googlePreflight(await api(base + '/preflight','POST',input),input);
          if (!csrf || inactive) return;
          if (revision !== googleInputRevision || JSON.stringify(batchInput()) !== JSON.stringify(input) || $('#google-execution-mode').value !== 'synthetic') {
            googleNote('参数已变化，本次检查作废；请重新运行。'); $('#google-run-status').textContent = '参数已变化'; $('#google-log-state').textContent = '检查已作废。'; $('#task-empty').textContent = '参数已变化，未创建新任务。'; return;
          }
          if (!result.can_create) {
            const labels = {CONFIG_MISSING:'尚无全局配置', INSUFFICIENT_ELIGIBLE:'合格且未占用的邮箱不足'};
            googleNote('预检未通过：' + (result.reason_codes.map(code => labels[code] || '条件未满足').join('；') || '条件未满足'));
            $('#preflight-result').textContent = '当前不能创建：' + result.reason_codes.join(' / ');
            $('#google-run-status').textContent = '预检未通过';
            $('#google-log-state').textContent = '检查已结束，未创建任务。';
            $('#task-empty').textContent = '预检未通过，未创建新任务；可从下方查看已有任务。'; return;
          }
          googleNote('预检通过：要求 ' + input.requested_count + ' 个，合格 ' + result.eligible_count + ' 个；创建时仍重新校验。');
          if (!csrf || inactive || revision !== googleInputRevision || JSON.stringify(batchInput()) !== JSON.stringify(input) || $('#google-execution-mode').value !== 'synthetic') return;
          const body = {...input,expected_config_revision:result.config_revision,request_key:crypto.randomUUID()};
          googleNote('正在提交批次…'); $('#google-run-status').textContent = '正在提交';
          await mutate(base + '/batches','POST',body,'tasks:manage',result => googleBatchAccepted(result,input.requested_count));
        } catch (error) {
          const uncertain = error.mutation && (error.code === 'COMMIT_UNKNOWN' || !error.status || error.status >= 500);
          googleNote(uncertain ? '提交未能确认，请按页面回执处理；不会自动重试。' : error.mutation ? '创建请求被拒绝，请检查提示；不会自动重试。' : '运行条件检查失败，未提交批次。');
          if (csrf && !inactive) $('#google-run-status').textContent = uncertain ? '提交未确认' : error.mutation ? '请求被拒绝' : '检查失败';
          throw error;
        }
      });
    });
    $('#refresh-google-log').addEventListener('click',() => { if (taskId) run(() => loadTask(taskId)); });
  }
  function batchInput() {
    const count = Number($('#requested-count').value), selection = $('#selection').value;
    const ids = selection === 'specified' ? [...selected].sort() : [];
    if (!Number.isInteger(count) || count < 1 || count > 100 || (selection === 'specified' && ids.length !== count)) throw {status:422};
    return {selection,requested_count:count,mailbox_ids:ids};
  }
  for (const id of ['selection','requested-count']) $('#' + id).addEventListener('input',invalidatePreflight);
  $('#batch-form').addEventListener('submit',event => {
    event.preventDefault(); if (liveMode || !has('tasks:manage') || pending) return;
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
    const target = $('#batch-result'); target.replaceChildren(node('p','任务批次已受理。批次：' + result.batch_id));
    for (const id of result.task_ids) target.append(node('p','任务：' + id,'mono'));
    $('#message').textContent = ''; $('#message').hidden = true;
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
    if (liveMode || !preflight || !preflight.can_create || pending || !has('tasks:manage')) return;
    if (!window.confirm('确认创建足量 ' + preflight.input.requested_count + ' 条任务记录？执行服务尚未接入，不执行注册、绑卡或模型调用。')) return;
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
        const card = node('article',undefined,'pool-item'), summary = node('p',taskLabel(row) + ' · ' + row.execution_scope + ' · ' + row.status + ' · 版本 ' + row.version);
        card.dataset.taskRow = row.id; summary.dataset.taskSummary = ''; card.append(node('p',row.id,'mono'),summary);
        const open = node('button','查看任务'); open.type = 'button'; open.dataset.openTask = ''; open.dataset.permission = 'onboarding:read'; open.setAttribute('aria-label','查看任务 ' + row.id);
        open.addEventListener('click',() => run(() => loadTask(row.id))); card.append(open); return card;
      });
      $('#task-list').replaceChildren(...cards); taskCursor = result.next_cursor;
      $('#task-list-state').textContent = result.items.length ? '本页 ' + result.items.length + ' 个本人任务' : '当前范围没有任务。';
    } catch (error) { $('#task-list').replaceChildren(); $('#task-list-state').textContent = '任务列表读取失败，不代表没有任务。'; throw error; }
    controls();
  }
  function taskMode(value) {
    // Previous assets/API had only synthetic:true. Keep that exact old wire
    // contract during a rolling asset refresh, never infer live from absence.
    const mode = value && !Object.hasOwn(value,'execution_mode') && value.synthetic === true &&
      ['pool','fixture'].includes(value.execution_scope) ? 'synthetic' : value?.execution_mode;
    if (!value || !['synthetic','live'].includes(mode) || value.synthetic !== (mode === 'synthetic') ||
        mode === 'live' && value.execution_scope !== 'pool') throw invalid();
    return mode;
  }
  function taskReadOnly(value) {
    if (liveMode) return value.execution_mode !== 'live' || !capabilities;
    return value.execution_mode === 'live' || value.execution_scope === 'pool' && !Object.hasOwn(value,'execution_mode');
  }
  function taskLabel(value) {
    if (value.execution_scope === 'pool' && !Object.hasOwn(value,'execution_mode')) return '历史任务（执行类型未核实，只读）';
    return taskMode(value) === 'live' ? (liveMode && capabilities ? '已有结算云资源任务' : '真实执行任务（当前页面只读）') : '任务编排';
  }
  function renderTask(value) {
    const fixtureFields = ['id','status','version','reason_code','current_step','generation','cancel_requested','synthetic'];
    const legacyFixture = value && value.synthetic === true && Object.keys(value).length === fixtureFields.length && fixtureFields.every(key => key in value);
    if (!value || !uuid(value.id) || !Number.isSafeInteger(value.version) || (!legacyFixture && !['fixture','pool'].includes(value.execution_scope))) throw invalid();
    // Original fixture detail deliberately retains its pre-pool wire contract.
    if (legacyFixture) value = {...value,execution_scope:'fixture',execution_mode:'synthetic'};
    const mode = taskMode(value);
    task = value; taskId = value.id; $('#task-empty').hidden = true; $('#task-facts').hidden = false;
    const facts = [];
    for (const [label,content] of [['任务 ID',value.id],['类型',taskLabel(value)],['范围',value.execution_scope],['状态',value.status],['当前版本',value.version],['配置版本',value.config_revision],['当前步骤',value.current_step || '尚未开始'],['原因码',value.reason_code || '无'],['取消请求',value.cancel_requested ? '已记录' : '无']]) facts.push(node('dt',label),node('dd',content == null ? '无' : content));
    if (value.cancel_requested) facts.push(node('dt','取消结果'),node('dd',
      Object.hasOwn(cancelReasons,value.reason_code) ? cancelReasons[value.reason_code] :
      '已记录取消请求；是否安全结束及释放须核对服务端状态，不能仅凭请求成功判断。'));
    $('#task-facts').replaceChildren(...facts);
    if ($('#google-run-status')) $('#google-run-status').textContent = '任务状态 · ' + value.status;
    // Keep the list observation in step with this fresher detail read.
    for (const card of all('[data-task-row]')) if (card.dataset.taskRow === value.id) card.querySelector('[data-task-summary]').textContent = taskLabel(value) + ' · ' + value.execution_scope + ' · ' + value.status + ' · 版本 ' + value.version;
  }
  async function loadTask(id) {
    if (taskId !== id) { googleLocalLog = []; googleServerLog = []; renderGoogleLog(); }
    task = null; taskId = id;
    $('#task-empty').hidden = false; $('#task-empty').textContent = '正在读取最新状态…'; $('#task-facts').hidden = true;
    try {
      const value = await api(base + '/tasks/' + id);
      if (!csrf || inactive) return;
      renderTask(value); await loadGoogleEvents(id);
    }
    catch (error) { $('#task-empty').textContent = '状态读取失败，请手动刷新；不能按旧版本操作。'; throw error; }
  }
  $('#refresh-tasks').addEventListener('click',() => run(loadTasks));
  $('#task-next').addEventListener('click',() => { const cursor = taskCursor; if (cursor) run(() => loadTasks(cursor)); });
  $('#task-batch').addEventListener('input',() => { taskCursor = null; controls(); });
  $('#task-scope').addEventListener('change',() => { taskCursor = null; controls(); });
  $('#task-filter-form').addEventListener('submit',event => { event.preventDefault(); run(loadTasks); });
  $('#refresh-task').addEventListener('click',() => { if (taskId) run(() => loadTask(taskId)); });
  for (const action of ['pause','cancel','recheck']) $('#' + action + '-task').addEventListener('click',() => {
    if (!task || !has('tasks:manage') || pending || taskReadOnly(task) || liveMode && action === 'recheck') return;
    if (action !== 'recheck' && ['SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE'].includes(task.status)) return;
    if (action === 'cancel' && !window.confirm('确认取消任务？仅经后端核验从未执行的任务会安全结束并释放邮箱；其余保留占用。不回滚外部结果，不释放信用卡绑定。')) return;
    const id = task.id, version = task.version, liveCommand = task.execution_mode === 'live';
    run(() => mutate(base + '/tasks/' + id + '/' + action,'POST',{expected_version:version,request_key:crypto.randomUUID()},'tasks:manage',async result => {
      if (!result || result.accepted !== true) throw {status:0};
      if (liveCommand && (result.execution_mode !== 'live' || result.synthetic !== false || !uuid(result.receipt_id) ||
          result.phase !== 'SUCCEEDED' || result.task !== null || result.snapshot_pending !== true)) throw {status:0};
      task = null;
      $('#command-receipt').textContent = '已受理回执：' + result.receipt_id + '；回执阶段：' + (result.phase || '已提交') + '（不是外部业务成功）。';
      message(action === 'recheck' ? '命令已受理：仅排入核验标记，没有运行 inspector。' : '命令已受理；并非外部操作已回滚。',false);
      // Receipt acceptance and a subsequent GET are independent outcomes.
      try { await loadTask(id); } catch (error) { followupFail(error,'命令已受理，状态刷新失败。'); }
    }));
  });
  $('#reconcile-request').addEventListener('click',() => {
    if (!pending || !has(pending.permission)) return;
    if (pending.secret && pending.kind === 'card-import') {
      for (const addon of addons) addon.reconcile?.(pending);
      return;
    }
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
  setupLiveView();
  (async () => {
    busy = true; controls(); $('#main').setAttribute('aria-busy','true');
    try {
      const session = embedded ? await window.RFProtectedConsole.session() : await api('/api/auth/session');
      if (inactive) return; // Hidden before the session answered: never re-enable this page.
      if (!session || typeof session.csrf_token !== 'string' || !Array.isArray(session.permissions)) throw invalid();
      // Read once per page and only cleared afterwards: releaseProven relies on this page never
      // switching sessions (another operator's proof must not release this operator's request).
      csrf = session.csrf_token; permissions = new Set(session.permissions); $('#operator').textContent = session.display_name;
      if (has('onboarding:read')) {
        if (liveMode) try { await loadCapabilities(); } catch (error) { fail(error); }
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
