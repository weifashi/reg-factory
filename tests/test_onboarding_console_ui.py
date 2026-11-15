"""Original console browser regression, fully intercepted HTTP; no provider/database I/O.

Uses the original index/app and catalog. Protected renderer is injected only by the
backend (or contract fixture before that backend lands); off mode uses exact index.
"""
import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from tests.onboarding_server_support import isolated_server
from webui.onboarding_console import render_console, catalog
from webui.onboarding_auth import _RESPONSE_HEADERS

with isolated_server() as server:
    schema = server.schema

STATIC = Path(__file__).resolve().parents[1] / 'webui' / 'static'


class OriginalConsoleDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                            args=['--no-sandbox', '--disable-background-networking'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.requests, self.errors, self.assets = [], [], []
        self.catalog_value = catalog(schema)
        self.protected, self.session_status, self.catalog_status = True, 200, 200
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda message: self.errors.append(message.text) if message.type == "error" and "Content Security Policy" in message.text else None)
        self.page.route('**/*', self.route)

    def tearDown(self):
        self.context.close()

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path == '/':
            html = (STATIC / 'index.html').read_text()
            if self.protected:
                html, _ = render_console(html)
            route.fulfill(content_type='text/html', body=html, headers=_RESPONSE_HEADERS if self.protected else {})
            return
        if path == '/api/onboarding/console/styles.css':
            self.assets.append(path)
            _, css = render_console((STATIC / 'index.html').read_text())
            route.fulfill(content_type='text/css', body=css)
            return
        if path == '/assets/telegram_qr.jpg':
            self.assets.append(path)
            route.fulfill(content_type='image/jpeg', body=(STATIC.parents[1] / 'assets' / 'telegram_qr.jpg').read_bytes())
            return
        if path.startswith('/static/'):
            self.assets.append(path)
            asset = STATIC / path.rsplit('/', 1)[-1]
            route.fulfill(content_type='text/css' if asset.suffix == '.css' else 'text/javascript', body=asset.read_text() if asset.exists() else '')
            return
        if path == '/onboarding/pools':
            route.fulfill(content_type='text/html', body='<h1 id=mailbox-card>fixture unified mailbox</h1>')
            return
        if path == '/login':
            route.fulfill(content_type='text/html', body='<h1>fixture login</h1>')
            return
        self.requests.append((request.method, path, request.headers, request.post_data))
        status = 200
        if path == '/api/auth/session':
            status = self.session_status
            data = dict(display_name='合成操作者', csrf_token='fixture-console-csrf', permissions=['onboarding:read'])
        elif path == '/api/onboarding/console/catalog':
            status = self.catalog_status
            data = self.catalog_value
        elif path == '/api/scripts':
            data = dict(scripts=schema.SCRIPTS)
        elif path == '/api/links':
            data = dict(links=schema.EXTERNAL_LINKS)
        elif path == '/api/embeds':
            data = dict(embeds=[])
        elif path == '/api/status':
            data = dict(version='fixture', running=0)
        elif path in ('/api/k12/status', '/api/chatgpt-plus/protocol-status', '/api/sms/custom'):
            data = {}
        elif path == '/api/auth/logout':
            data = dict(ok=True)
        else:
            status, data = 403, dict(code='LEGACY_BLOCKED')
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    def open_console(self):
        self.page.goto('http://console.fixture/')
        self.page.wait_for_function('() => document.querySelectorAll("#script-nav button[data-id]").length === ' + str(len(schema.SCRIPTS)))

    def assert_catalog(self, protected=True):
        self.assertEqual(self.page.locator('#task-count').inner_text(), str(len(schema.SCRIPTS) + (1 if protected else 0)))
        groups = self.page.locator('#script-nav > details > summary > span:first-child').all_text_contents()
        self.assertEqual(groups, [*dict.fromkeys(s['category'] for s in schema.SCRIPTS), '外部工具'])
        self.assertEqual(len(groups), 6)
        for script in schema.SCRIPTS:
            self.page.evaluate('(id) => { const b = document.querySelector(`[data-id="${id}"]`); b.closest("details").open = true; b.click(); }', script['id'])
            self.assertEqual(self.page.locator('#form-panel h1').inner_text(), script['title'])
            rendered = self.page.locator('#form-panel [data-arg-flag]').evaluate_all('(nodes) => nodes.map(n => n.dataset.argFlag)')
            self.assertEqual(rendered, [a['flag'] for a in script['args'] if not a.get('hidden')], script['id'])
            self.assertEqual(self.page.locator('.btn-run-task').is_disabled(), protected)
            if protected:
                self.assertEqual(self.page.locator('#form-panel input:enabled, #form-panel select:enabled, #form-panel textarea:enabled').count(), 0)
                self.assertIn('执行服务尚未接入', self.page.locator('#form-panel').inner_text())

    def test_every_original_task_and_all_six_groups_are_preserved(self):
        self.open_console()
        self.assertEqual(self.page.locator('#view-plus').is_visible(), False)
        self.assertEqual(self.page.locator('#view-run').is_visible(), True)
        self.assert_catalog()
        self.assertEqual(self.errors, [])

    def test_navigation_tools_and_no_legacy_requests(self):
        self.open_console()
        self.assertEqual(self.page.locator('#console-actor').inner_text(), '合成操作者')
        for view, label in [('google', 'Google 开通任务'), ('resources', '资源管理'), ('settings', '系统配置')]:
            self.assertIn(label, self.page.locator('[data-view="' + view + '"]').inner_text())
        for view, label in [('assets', '资产 API'), ('network', '网络出口'), ('env', '环境配置'), ('k12', 'Codex K12'), ('plus', 'Plus 导入')]:
            if view in ('assets','network'):
                self.page.locator('.navbtn[data-view="resources"]').click()
                self.page.locator('#resources-tab-'+view).click()
            elif view == 'env':
                self.page.locator('.navbtn[data-view="settings"]').click()
                self.page.locator('#settings-tab-env').click()
            else:
                self.page.locator('.navbtn[data-view="' + view + '"]').click()
            self.assertTrue(self.page.locator('#view-' + view).is_visible(), label)
            self.assertIn('后端尚未接入', self.page.locator('#view-' + view + ' .console-status').inner_text())
            self.assertEqual(self.page.locator('#view-' + view + ' input:enabled, #view-' + view + ' select:enabled, #view-' + view + ' button:enabled, #view-' + view + ' textarea:enabled').count(), 0)
        self.page.evaluate('() => { showView("run"); selectScript("register_chatgpt"); document.querySelector("#form-panel .custom-sms-import").open = true; }')
        self.assertIn('未读取', self.page.locator('#single-custom-sms-summary').inner_text())
        self.assertEqual(self.page.evaluate('scriptDrafts.size'), 0)
        self.page.evaluate('async () => { await runScript(); await loadCustomSmsPool(); await startUpdate(); await document.querySelector("#btn-stop").onclick(); await document.querySelector("#btn-stop-all").onclick(); await pollStatus(); document.querySelector("#mailpool-input").value = "fixture@example.test----not-a-secret"; await document.querySelector("#btn-save-env").onclick(); await document.querySelector("#btn-rent").onclick(); await document.querySelector("#btn-import-mail").onclick(); }')
        self.page.wait_for_timeout(5300)  # Bounded assertion: legacy 5s poll must never fire.
        self.assertEqual([(m, p) for m, p, _, _ in self.requests], [('GET', '/api/auth/session'), ('GET', '/api/onboarding/console/catalog')])
        self.assertEqual(self.page.evaluate('localStorage.length + sessionStorage.length'), 0)
        self.assertEqual(self.errors, [])
        self.assertEqual(set(self.assets), {'/static/style.css', '/static/app.js', '/static/console-protected.js', '/static/console-protected.css', '/api/onboarding/console/styles.css'})

    def test_environment_view_links_existing_safe_editor_and_blocks_loopback(self):
        self.open_console()
        self.page.locator('.navbtn[data-view="settings"]').click()
        self.page.locator('#settings-tab-env').click()
        links = self.page.locator('#view-env a').evaluate_all('(nodes) => nodes.map(n => [n.textContent, n.getAttribute("href")])')
        self.assertIn(['打开受保护配置编辑器', '/onboarding#config-card'], links)
        self.page.locator('.navbtn[data-view="k12"]').click()
        self.assertIsNone(self.page.locator('#k12-open').get_attribute('href'))
        self.assertIsNone(self.page.locator('#embed-open').get_attribute('href'))

    def test_plus_view_has_no_internal_horizontal_overflow(self):
        self.open_console()
        for width in (375, 1440):
            self.page.set_viewport_size(dict(width=width, height=900))
            self.page.evaluate("showView('plus')")
            sizes = self.page.locator('.content').evaluate('(node) => [node.scrollWidth, node.clientWidth]')
            self.assertLessEqual(sizes[0], sizes[1] + 1, (width, sizes))
            box = self.page.locator('.plus-import-panel').bounding_box()
            self.assertLessEqual(box['x'] + box['width'], width + 1)

    def test_original_mailbox_entry_is_same_document_route(self):
        self.open_console()
        self.page.locator('.navbtn[data-view="resources"]').click()
        self.assertTrue(self.page.url.endswith('/#mailboxes'))
        self.assertEqual([(m, p) for m, p, _, _ in self.requests], [('GET', '/api/auth/session'), ('GET', '/api/onboarding/console/catalog')])

    def test_session_failure_is_fail_closed_without_catalog(self):
        self.session_status = 401
        self.page.goto('http://console.fixture/')
        self.page.wait_for_function('() => document.querySelector("#console-message")?.textContent.includes("登录")')
        self.assertEqual(self.page.locator('#script-nav button[data-id]').count(), 0)
        self.assertEqual([(m, p) for m, p, _, _ in self.requests], [('GET', '/api/auth/session')])
        self.assertEqual(self.page.locator('#console-logout').is_disabled(), True)

    def test_catalog_expiry_disables_logout_and_clears_actor(self):
        self.catalog_status = 401
        self.page.goto('http://console.fixture/')
        self.page.wait_for_function('() => document.querySelector("#console-message")?.textContent.includes("登录")')
        self.assertEqual(self.page.locator('#script-nav button[data-id]').count(), 0)
        self.assertEqual(self.page.locator('#console-logout').is_disabled(), True)
        self.assertNotEqual(self.page.locator('#console-actor').inner_text(), '合成操作者')

    def test_untrusted_catalog_text_cannot_inject_dom(self):
        attack = '"><img src="/api/unsafe-canary" onerror="window.injected=true">'
        first = self.catalog_value['scripts'][0]
        first['title'] = first['desc'] = first['category'] = attack
        arg = next(arg for arg in first['args'] if arg['type'] == 'str')
        arg['default'] = arg['help'] = attack
        self.open_console()
        self.assertEqual(self.page.locator('#form-panel img').count(), 0)
        self.assertEqual(self.page.locator('#form-panel h1').inner_text(), attack)
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.assertNotIn('/api/unsafe-canary', [p for _, p, _, _ in self.requests])

    def test_logout_uses_in_memory_session_csrf(self):
        self.open_console()
        self.page.locator('#console-logout').click()
        self.page.wait_for_url('**/login')
        logout = [r for r in self.requests if r[1] == '/api/auth/logout']
        self.assertEqual(len(logout), 1)
        self.assertEqual(logout[0][0], 'POST')
        self.assertEqual(logout[0][2]['x-csrf-token'], 'fixture-console-csrf')
        self.assertEqual(json.loads(logout[0][3]), {})
        self.assertEqual(self.page.evaluate('localStorage.length + sessionStorage.length'), 0)

    def test_original_off_mode_still_loads_original_catalog_and_forms(self):
        self.protected = False
        self.page.add_init_script('localStorage.setItem("reg-factory-guide-v3", "complete")')
        self.open_console()
        self.assert_catalog(protected=False)
        paths = [p for _, p, _, _ in self.requests]
        for path in ('/api/scripts', '/api/links', '/api/embeds', '/api/status', '/api/k12/status', '/api/sms/custom', '/api/chatgpt-plus/protocol-status'):
            self.assertIn(path, paths)
        self.assertNotIn('/api/auth/session', paths)
        self.assertEqual(self.page.locator('#console-nav').count(), 0)
        self.assertNotIn('console-protected.js', (STATIC / 'index.html').read_text())

    def test_protected_plus_never_claims_checks_are_in_progress(self):
        self.open_console()
        for selector in ('#plus-import-state', '#plus-protocol-state'):
            self.assertEqual(self.page.locator(selector).inner_text(), '尚未接入')
