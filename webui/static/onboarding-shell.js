/* Navigation only: all business actions remain owned by onboarding-pools.js. */
(() => {
  'use strict';
  const sidebar = document.querySelector('#shell-sidebar');
  const menu = document.querySelector('#shell-menu');
  const layout = document.querySelector('.layout');
  const main = document.querySelector('#main');
  const links = [...sidebar.querySelectorAll('[data-shell-target]')];
  const mobile = window.matchMedia('(max-width: 900px)');
  let current = '';

  function menuState(open, restoreFocus = false) {
    layout.classList.toggle('nav-open', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? '关闭任务菜单' : '打开任务菜单');
    sidebar.inert = mobile.matches && !open;
    main.inert = mobile.matches && open;
    if (restoreFocus) menu.focus();
  }

  function selectTarget(focus = false) {
    const requested = location.hash.slice(1) || 'mailbox-card';
    if (requested === 'main') {
      // Skip navigation changes focus only, not the active business section.
      menuState(false);
      main.scrollIntoView({block: 'start'});
      main.focus({preventScroll: true});
      return;
    }
    const next = links.some(link => link.dataset.shellTarget === requested) ? requested : 'mailbox-card';
    if (current && main.getAttribute('aria-busy') === 'true') {
      // Never bypass disabled business controls to clear an in-flight import.
      history.replaceState(null, '', '#' + current);
      return;
    }
    const businessTarget = {'mailbox-card':'mailboxes','batch-card':'google','tasks-card':'google',
      'card-pool':'cards','global-config':'settings','network-panel':'network','legacy-tools':'assets'}[next];
    if (current && window.RFPoolRuntime && !window.RFPoolRuntime.beforeNavigate(businessTarget)) {
      history.replaceState(null, '', '#' + current);
      return;
    }
    if (next !== 'mailbox-card' && document.querySelector('#tab-import').getAttribute('aria-selected') === 'true') {
      const list = document.querySelector('#tab-list');
      if (list.disabled) return;
      list.click();
    }
    current = next;
    for (const link of links) {
      const active = link.dataset.shellTarget === next;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
    const target = document.getElementById(next);
    menuState(false);
    target.scrollIntoView({block: 'start'});
    if (focus) target.focus({preventScroll: true});
  }

  for (const link of links) link.addEventListener('click', event => {
    if (main.getAttribute('aria-busy') === 'true') {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, '', link.hash);
    selectTarget(true);
  });
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menuState(open);
    if (open) (sidebar.querySelector('[aria-current]') || links[0]).focus();
  });
  document.querySelector('#shell-backdrop').addEventListener('click', () => menuState(false, true));
  document.addEventListener('keydown', event => {
    if (!mobile.matches || menu.getAttribute('aria-expanded') !== 'true') return;
    if (event.key === 'Escape') { event.preventDefault(); menuState(false, true); }
    if (event.key === 'Tab') {
      const items = [menu, ...sidebar.querySelectorAll('a[href]')];
      const index = items.indexOf(document.activeElement);
      event.preventDefault();
      const next = (index + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next].focus();
    }
  });
  mobile.addEventListener('change', () => menuState(false));
  window.addEventListener('hashchange', () => selectTarget(true));
  menuState(false);
  selectTarget();
})();
