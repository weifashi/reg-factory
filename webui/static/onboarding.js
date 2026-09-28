'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  let csrf = '', permissions = new Set(), task = null, busy = false, pendingCommand = null, inactive = false;
  const actionIds = ['pause','cancel','recheck'];
  function message(text, error = true) {
    $('#message').textContent = text; $('#message').className = 'message ' + (error ? 'error' : 'success');
    $('#message').hidden = false;
  }
  function controls() {
    $('#refresh').disabled = busy || !csrf || !permissions.has('onboarding:read');
    $('#query').disabled = busy || !csrf || !permissions.has('onboarding:read');
    $('#logout').disabled = busy || !csrf;
    for (const id of actionIds) $('#' + id).disabled = busy || !csrf || !task || Boolean(pendingCommand) || !permissions.has('tasks:manage');
    $('#reconcile-command').hidden = !pendingCommand;
    $('#reconcile-command').disabled = busy || !csrf || !pendingCommand || !permissions.has('tasks:manage');
    $('#main').setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  function clearPendingCommand() {
    pendingCommand = null; $('#pending').textContent = ''; $('#pending').hidden = true;
  }
  function expire() {
    inactive = true; csrf = ''; permissions.clear(); task = null; clearPendingCommand();
    $('#login-link').hidden = false; $('#config-card').hidden = true;
    for (const input of document.querySelectorAll('#config-fields input')) input.value = '';
    $('#config-fields').replaceChildren(); controls();
  }
  function fail(error) {
    const labels = {401:'会话已失效。请重新登录；未完成的请求不会自动重发。',403:'没有此操作权限，或请求来源不符。请联系管理员，不会自动提升权限。',409:'任务版本、状态或请求键发生冲突。请先重新查询，不会自动重试。',422:'输入不符合要求，请检查后再提交。',429:'请求过于频繁，请稍后再试。',503:'依赖暂不可用，操作未确认。保护模式保持开启，没有自动重试。'};
    let text = labels[error.status] || '连接中断或响应无效，操作结果未确认；请先重新查询，没有自动重试。';
    if (error.code === 'COMMIT_UNKNOWN') text = '事务提交结果未知。普通命令已冻结，查询状态不会解锁；仅可明确使用原操作、原版本与原请求键核对。';
    if (error.stillPending) text += ' 原命令仍待核对：此拒绝不能证明原命令未提交。可稍后再次核对；如需放弃，请离开页面并交管理员按请求键核对记录。';
    if (error.correlation) text += ' 参考号：' + error.correlation;
    message(text);
    if (error.status === 401) expire();
    controls();
  }
  async function api(path, options = {}) {
    // A hung request must end; a timed-out command stays pending for explicit reconcile.
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30000);
    try {
      let response;
      try {
        response = await fetch(path, {credentials:'same-origin',cache:'no-store',...options,signal:controller.signal,
          headers:{...(options.method ? {'Content-Type':'application/json','X-CSRF-Token':csrf} : {}),...options.headers}});
      } catch (_) { throw {status:0}; }
      let data; try { data = await response.json(); } catch (_) { throw {status:response.ok ? 0 : response.status}; }
      if (!response.ok) throw {status:response.status,code:data.code,
        correlation:typeof data.correlation_id === 'string' && /^[0-9a-f-]{36}$/.test(data.correlation_id) ? data.correlation_id : '',
        notCommitted:Boolean(data) && data.not_committed === true};
      return data;
    } finally { clearTimeout(timer); }
  }
  function renderTask(value) {
    // Spec §6.6: an unsafe or malformed version must never become an expected_version.
    if (!value || typeof value.id !== 'string' || !Number.isSafeInteger(value.version) || value.version < 1) {
      task = null; $('#task-facts').hidden = true; $('#task-empty').hidden = false;
      $('#task-empty').textContent = '任务响应无效或版本超出安全范围；不能据此发送命令。';
      controls(); return false;
    }
    task = value; $('#task-empty').hidden = true; $('#task-facts').hidden = false; $('#task-facts').replaceChildren();
    const states = {QUEUED:'排队中',RUNNING:'合成执行中',PAUSED:'已暂停',CANCELLED_SAFE:'已安全取消',SUCCEEDED:'合成任务完成',WAIT_HUMAN:'等待人工',CONFLICT:'结果冲突'};
    for (const [label, content] of [['任务 ID',value.id],['当前状态',states[value.status] || value.status],['当前版本',value.version],['当前步骤',value.current_step || '尚未开始'],['取消请求',value.cancel_requested ? '已记录' : '无']]) {
      const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = label; dd.textContent = String(content); $('#task-facts').append(dt, dd);
    }
    controls();
    return true;
  }
  async function diagnostic() {
    const data = await api('/api/onboarding/diagnostics');
    $('#ready').textContent = data.ready ? '离线底座就绪' : '未就绪'; $('#ready').className = 'badge' + (data.ready ? ' good' : '');
    $('#diagnostic').textContent = '保护模式 · schema v' + data.schema_version + ' · 真实流程未接入。没有访问 Google、银行或 Sub2API。';
  }
  $('#refresh').addEventListener('click', async () => {
    busy = true; controls(); try { await diagnostic(); message('已重新核验离线底座。', false); }
    catch (error) { $('#ready').textContent = '检查未通过'; $('#ready').className = 'badge'; fail(error); }
    finally { busy = false; controls(); }
  });
  $('#task-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !csrf) return;
    task = null; busy = true; controls();
    try {
      if (renderTask(await api('/api/onboarding/tasks/' + encodeURIComponent($('#task-id').value.trim())))) message('已取得最新合成任务状态。', false);
      else message('任务响应无效或版本超出安全范围，未显示；不能据此发送命令，请稍后重新查询。');
    }
    catch (error) { $('#task-facts').hidden = true; $('#task-empty').hidden = false; fail(error); }
    finally { busy = false; controls(); }
  });
  async function submitCommand(command) {
    busy = true; controls();
    $('#pending').hidden = false;
    $('#pending').textContent = '任务：' + command.id + '；操作：' + command.action +
      '；原请求键：' + command.body.request_key + '；原版本：' + command.body.expected_version;
    try {
      const data = await api('/api/onboarding/tasks/' + command.id + '/' + command.action,
        {method:'POST',body:JSON.stringify(command.body)});
      if (inactive) return;
      if (!data || data.accepted !== true) throw {status:0};
      clearPendingCommand();
      message(command.action === 'recheck' ? '只读核验标记已受理；没有实际执行核验，也不会恢复任务。' : '本地操作已受理；不代表撤销任何外部结果。', false);
      if (data.task) {
        if (!renderTask(data.task)) message('命令已受理，但返回的任务状态无效，未显示。请手动查询最新状态，不要重新发送命令。');
      } else {
        task = null;
        try {
          const latest = await api('/api/onboarding/tasks/' + command.id);
          if (!inactive && !renderTask(latest)) message('命令已受理，但刷新得到的任务状态无效，未显示。请手动查询最新状态，不要重新发送命令。');
        } catch (error) {
          if (inactive) return;
          fail(error);
          message(error.status === 401 ? '命令已受理；会话已失效，请重新登录后查询最新状态，不要重新发送命令。'
            : '命令已受理，状态刷新失败。请手动查询最新状态，不要重新发送命令。');
        }
      }
    } catch (error) {
      if (inactive) return;
      task = null;
      // A rejected replay stays pending unless the server proves this very command never committed
      // (409 VERSION_CONFLICT with the not-committed flag) and the operator confirms: environment
      // checks before the receipt read also answer 409/422, so no status alone proves anything.
      if (error.code === 'COMMIT_UNKNOWN' || !error.status || (error.status >= 500 && error.status <= 599)) pendingCommand = command;
      else if (command === pendingCommand && error.status === 409 && error.code === 'VERSION_CONFLICT' && error.notCommitted === true
               && window.confirm('服务端已证明原命令未提交且不会再提交（版本已变化）。解除后须重新查询最新状态。确认解除？')) {
        clearPendingCommand(); fail(error);
        message('服务端已证明原命令未提交；未决已解除。请重新查询最新状态后再操作。');
        return;
      }
      else if (command === pendingCommand && error.status !== 401) error.stillPending = true;
      if (!pendingCommand) clearPendingCommand(); // A rejected fresh command leaves no request display.
      fail(error);
    } finally { busy = false; controls(); }
  }
  for (const action of actionIds) $('#' + action).addEventListener('click', async () => {
    if (busy || !task || !csrf || pendingCommand || !permissions.has('tasks:manage')) return;
    if (action === 'cancel' && !window.confirm('仅取消后续步骤，不回滚外部结果。确认继续？')) return;
    await submitCommand({id:task.id,action,
      body:{expected_version:task.version,request_key:crypto.randomUUID()}});
  });
  $('#reconcile-command').addEventListener('click', async () => {
    if (busy || !csrf || !pendingCommand || !permissions.has('tasks:manage')) return;
    if (!window.confirm('明确使用保留的原任务、原操作、原版本与原请求键核对？不生成新键，不按查询结果更换版本。')) return;
    await submitCommand(pendingCommand);
  });
  window.addEventListener('pagehide',expire);
  $('#logout').addEventListener('click', async () => {
    if (busy || !csrf) return; busy = true; controls();
    try { await api('/api/auth/logout', {method:'POST',body:'{}'}); expire(); window.location.replace('/login'); }
    catch (error) { fail(error); } finally { busy = false; controls(); }
  });
  function configField(item) {
    const section = document.createElement('section'); section.className = 'config-item';
    const heading = document.createElement('div'); heading.className = 'config-heading';
    const label = document.createElement('label'); label.textContent = item.label || item.key;
    const input = document.createElement('input'); input.id = 'config-' + item.key; label.htmlFor = input.id;
    input.autocomplete = 'off'; input.type = item.secret ? 'password' : 'text'; input.value = item.secret ? '' : item.value;
    const code = document.createElement('small'); code.textContent = item.key; code.className = 'mono'; heading.append(label,code);
    const action = document.createElement('select'); action.setAttribute('aria-label', (item.label || item.key) + ' 更新方式');
    for (const [value,text] of [['keep','保留不变'],['replace','替换为新值'],['clear','明确清空']]) {
      const option = document.createElement('option'); option.value = value; option.textContent = text; action.append(option);
    }
    const state = document.createElement('p'); state.className = 'muted'; state.textContent = item.secret ? (item.configured ? '已配置 · 值不回显' : '未配置') : '普通配置';
    if (item.secret) { input.disabled = true; action.addEventListener('change', () => { input.disabled = action.value !== 'replace'; input.value = ''; }); }
    const save = document.createElement('button'); save.type = 'button'; save.textContent = '保存此项';
    save.addEventListener('click', async () => {
      if (!csrf || busy) return;
      if (item.secret && action.value === 'clear' && !window.confirm('确认清空这一项？已有调用可能受影响。')) return;
      const value = item.secret ? (action.value === 'replace' ? {action:'replace',value:input.value} : {action:action.value}) : input.value;
      save.disabled = true; busy = true; controls();
      try { await api('/api/env', {method:'POST',body:JSON.stringify({env:{[item.key]:value}})});
        if (item.secret) { input.value = ''; input.disabled = true; action.value = 'keep'; state.textContent = '保存已受理 · 请重新读取确认配置状态'; }
        message('配置保存已受理。未启动任何旧任务，也不能修改保护模式。',false);
      } catch (error) { fail(error); }
      finally { if (item.secret) input.value = ''; save.disabled = false; busy = false; controls(); }
    });
    section.append(heading,state); if (item.secret) section.append(action); section.append(input,save); return section;
  }
  $('#load-config').addEventListener('click', async () => {
    if (busy || !csrf) return; busy = true; controls(); $('#load-config').disabled = true;
    try {
      const data = await api('/api/env'); if (inactive) return; // expire() cleared these fields.
      $('#config-fields').replaceChildren();
      for (const group of data.groups) {
        const heading = document.createElement('h3'); heading.textContent = group.group; $('#config-fields').append(heading);
        for (const item of group.items) $('#config-fields').append(configField(item));
      }
    } catch (error) { fail(error); }
    finally { busy = false; controls(); $('#load-config').disabled = false; }
  });
  (async () => {
    // Busy through the whole initial load (session and diagnostics), like the pool page.
    busy = true; $('#main').setAttribute('aria-busy','true');
    try {
      // Read once per page and only cleared afterwards: releasing a pending command on the
      // server's proof relies on this page never switching sessions.
      const data = await api('/api/auth/session'); if (inactive) return; csrf = data.csrf_token; permissions = new Set(data.permissions);
      $('#operator').textContent = data.display_name; $('#config-card').hidden = !permissions.has('config:manage'); controls();
      await diagnostic();
    } catch (error) { $('#ready').textContent = '未就绪'; fail(error); }
    finally { busy = false; controls(); }
  })();
})();
