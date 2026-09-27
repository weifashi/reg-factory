'use strict';
(() => {
  const form = document.querySelector('#login-form');
  const password = document.querySelector('#password');
  const button = document.querySelector('#login-button');
  const message = document.querySelector('#message');
  const retry = document.querySelector('#retry');
  let csrf = '';
  const labels = {401:'用户名或密码无效，或登录验证已过期。',403:'请求来源或登录验证不匹配，请检查地址后重新准备登录。',429:'尝试次数过多，请稍后再试。',503:'登录服务暂不可用，仍保持保护模式；请联系管理员。'};
  function show(text, error = true) {
    message.textContent = text;
    message.className = 'message' + (error ? ' error' : '');
    message.hidden = false;
  }
  async function prepare(keepMessage = false) {
    csrf = ''; button.disabled = true; retry.hidden = true;
    try {
      const response = await fetch('/api/auth/bootstrap', {credentials:'same-origin',cache:'no-store'});
      if (!response.ok) { show(labels[response.status] || '无法准备登录。'); retry.hidden = false; return; }
      const data = await response.json();
      if (typeof data.csrf_token !== 'string' || !data.csrf_token) throw new Error('invalid response');
      csrf = data.csrf_token; button.disabled = false;
      if (!keepMessage) message.hidden = true;
    } catch (_) { show('无法连接本机登录服务，请检查连接；没有自动重试。'); retry.hidden = false; }
    finally { button.textContent = '登录控制面'; }
  }
  retry.addEventListener('click', () => prepare());
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!csrf || button.disabled) return;
    button.disabled = true; button.textContent = '正在核验…';
    const body = JSON.stringify({username:document.querySelector('#username').value,password:password.value});
    password.value = '';
    try {
      const response = await fetch('/api/auth/login', {method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body});
      csrf = '';
      if (response.ok) { window.location.replace('/'); return; }
      show(labels[response.status] || '登录请求未完成，请重新准备登录。');
    } catch (_) { show('登录结果未确认，请重新检查登录服务；没有自动重试。'); }
    finally { button.textContent = '登录控制面'; retry.hidden = false; csrf = ''; }
  });
  if (new URLSearchParams(window.location.search).get('reason') === 'expired') {
    show('会话已失效，请重新登录。未完成的操作不会自动重发。'); prepare(true);
  } else prepare();
})();
