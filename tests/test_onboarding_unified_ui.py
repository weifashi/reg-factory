"""Unified original-console browser checks; intercepted HTTP and isolated schema only."""
import json
import unittest
from urllib.parse import urlsplit
from tests import test_onboarding_pool_ui as pool
STATIC = pool.STATIC
from tests.onboarding_server_support import isolated_server
from webui.onboarding_console import catalog, render_console
from webui.onboarding_auth import _RESPONSE_HEADERS

with isolated_server() as server:
    SCHEMA = server.schema


class UnifiedConsoleTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    tearDown = pool.PoolDomTests.tearDown
    guarded = pool.PoolDomTests.guarded
    dialog = pool.PoolDomTests.dialog

    def setUp(self):
        pool.PoolDomTests.setUp(self)
        self.page.set_default_timeout(12000)
        self.overrides[('POST', '/api/auth/logout')] = (200, {'ok': True})
        self.errors = []
        self.documents = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def route(self, route):
        path = urlsplit(route.request.url).path
        if route.request.resource_type == 'document':
            self.documents.append(path)
        if path in ('/', '/onboarding/pools'):
            # The actual backend renderer is exercised, not a handcrafted second shell.
            html, _ = render_console((STATIC / 'index.html').read_text(), pool_source=(STATIC / 'onboarding-pools.html').read_text())
            route.fulfill(content_type='text/html', body=html, headers=_RESPONSE_HEADERS)
        elif path == '/api/onboarding/console/styles.css':
            _, css = render_console((STATIC / 'index.html').read_text())
            route.fulfill(content_type='text/css', body=css)
        elif path == '/api/onboarding/console/catalog':
            self.requests.append(('GET', path, None, route.request.url))
            status, data = self.overrides.get(('GET', path), (200, catalog(SCHEMA)))
            route.fulfill(status=status, content_type='application/json', body=json.dumps(data))
        elif path == '/assets/telegram_qr.jpg':
            route.fulfill(content_type='image/jpeg', body=(STATIC.parents[1] / 'assets/telegram_qr.jpg').read_bytes())
        elif path == '/login':
            route.fulfill(content_type='text/html', body='<h1>登录</h1>')
        else:
            pool.PoolDomTests.route(self, route)

    def open(self, hash=''):
        self.page.goto('https://unified.fixture/' + hash)
        self.page.wait_for_function('() => document.querySelectorAll("#script-nav button[data-id]").length === 16')
        self.page.wait_for_function('() => document.querySelector("#console-actor").textContent === "合成操作者"')
        self.page.wait_for_function('() => document.querySelector("#main").getAttribute("aria-busy") !== "true"')

    def test_sidebar_support_entry_and_dialog_are_removed(self):
        self.open()
        self.assertEqual(self.page.locator('#btn-telegram').count(),0)
        self.assertEqual(self.page.locator('#telegram-dialog').count(),0)
        self.assertNotIn('联系支持',self.page.locator('nav.sidebar').inner_text())
        self.assertEqual(self.page.locator('#script-nav button[data-id]').count(),16)
        self.assertEqual(self.errors,[])

    def test_original_tasks_plus_google_share_one_shell_and_document(self):
        self.open()
        self.assertEqual(self.page.locator('header.topbar').count(), 1)
        self.assertEqual(self.page.locator('nav.sidebar').count(), 1)
        self.assertEqual(self.page.locator('#task-count').inner_text(), '17')
        self.assertEqual(self.page.locator('#script-nav > details').count(), 6)
        for script in SCHEMA.SCRIPTS:
            self.page.evaluate('(id) => { const b = document.querySelector(`[data-id="${id}"]`); b.closest("details").open = true; b.click(); }', script['id'])
            self.assertEqual(self.page.locator('#form-panel h1').inner_text(), script['title'])
            self.assertTrue(self.page.locator('.btn-run-task').is_disabled())
            self.assertIn('执行服务尚未接入', self.page.locator('#form-panel').inner_text())
        self.page.locator('#script-nav [data-view="google"]').click()
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.assertIn('active', self.page.locator('[data-view="google"]').get_attribute('class'))
        self.assertFalse(self.page.locator('#view-run').is_visible())
        for view, target in [('mailboxes','mailbox-card'), ('cards','card-pool'), ('settings','global-config')]:
            if view == 'mailboxes': self.page.locator('.sidebar-tools [data-view="resources"]').click()
            elif view == 'cards': self.page.locator('#resources-tab-cards').click()
            else: self.page.locator('.sidebar-tools [data-view="' + view + '"]').click()
            self.assertTrue(self.page.locator('#' + target).is_visible())
            self.assertTrue(self.page.url.endswith('#' + view))
        self.assertEqual(self.documents, ['/'])
        self.assertEqual(sum(path == '/api/auth/session' for _,path,*_ in self.requests), 1)
        self.assertEqual(self.page.evaluate('() => { const ids = [...document.querySelectorAll("[id]")].map(n=>n.id); return ids.filter((id,i)=>ids.indexOf(id)!==i); }'), [])
        self.assertEqual(self.errors, [])
        visible = self.page.locator('body').inner_text()
        for text in ('新增 · 隔离合成开通', '保护模式', '执行隔离'):
            self.assertNotIn(text, visible)

    def test_deeplink_back_forward_retains_pool_selection_and_config(self):
        self.open('#mailboxes')
        checkbox = self.page.locator('#mailbox-card input[type=checkbox]').first
        checkbox.wait_for()
        checkbox.check()
        self.page.locator('[data-view="google"]').click()
        self.assertIn('1', self.page.locator('#selected-count').inner_text())
        self.page.locator('[data-view="settings"]').click()
        self.page.go_back()
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.page.go_back()
        self.assertTrue(checkbox.is_checked())
        self.page.go_forward()
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.assertEqual(self.documents, ['/'])

    def test_navigation_guard_keeps_hash_and_original_task_when_busy(self):
        self.open('#google')
        self.page.evaluate('window.RFPoolPanel = {beforeNavigate: () => false}')
        self.page.locator('[data-view="resources"]').click()
        self.assertTrue(self.page.url.endswith('#google'))
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.page.evaluate('document.querySelector("#script-nav [data-id]").click()')
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.assertTrue(self.page.url.endswith('#google'))

    def test_sidebar_task_list_scrolls_independently_and_views_do_not_overflow(self):
        self.open('#google')
        for width in (375, 1440):
            self.page.set_viewport_size(dict(width=width, height=900))
            for view in ('google', 'mailboxes', 'cards', 'settings', 'env', 'plus'):
                self.page.evaluate('(v)=>showView(v)', view)
                sizes = self.page.locator('.content').evaluate('(n)=>[n.scrollWidth,n.clientWidth]')
                self.assertLessEqual(sizes[0], sizes[1] + 1, (width,view,sizes))
            self.assertEqual(self.page.locator('#script-nav').evaluate('(n)=>getComputedStyle(n).overflowY'), 'auto')
            self.assertEqual(self.page.locator('.sidebar').evaluate('(n)=>getComputedStyle(n).overflowY'), 'auto')
        self.assertEqual(self.errors, [])

    def test_sidebar_tools_stay_above_footer_when_groups_expand_and_scroll(self):
        self.open('#resources')
        for width, height in ((1440, 1000), (1440, 600), (375, 812)):
            with self.subTest(width=width, height=height):
                self.page.set_viewport_size(dict(width=width, height=height))
                self.page.evaluate("document.body.classList.add('nav-open')")
                positions = []
                for expanded in (False, True):
                    self.page.evaluate('(open) => document.querySelectorAll("#script-nav details").forEach(n => n.open = open)', expanded)
                    geometry = self.page.evaluate("""() => {
                        const nav=document.querySelector('#script-nav'), tools=document.querySelector('.sidebar-tools'), footer=document.querySelector('.sidebar-footer'), side=document.querySelector('.sidebar');
                        const r=n=>n.getBoundingClientRect();
                        return {toolsY:r(tools).top, toolsBottom:r(tools).bottom, footerY:r(footer).top, footerBottom:r(footer).bottom, sideBottom:r(side).bottom, navHeight:nav.clientHeight, navScrollHeight:nav.scrollHeight};
                    }""")
                    self.assertAlmostEqual(geometry['toolsBottom'], geometry['footerY'], delta=1)
                    self.assertAlmostEqual(geometry['footerBottom'], geometry['sideBottom'], delta=1)
                    positions.append(geometry['toolsY'])
                    if expanded:
                        self.assertGreater(geometry['navScrollHeight'], geometry['navHeight'])
                        self.page.locator('#script-nav').evaluate('(n)=>n.scrollTop=n.scrollHeight')
                        self.assertGreater(self.page.locator('#script-nav').evaluate('(n)=>n.scrollTop'), 0)
                        self.assertAlmostEqual(self.page.locator('.sidebar-tools').bounding_box()['y'], geometry['toolsY'], delta=1)
                self.assertAlmostEqual(*positions, delta=1)
        self.assertEqual(self.errors, [])

    def test_catalog_503_does_not_disable_available_pool_operations(self):
        self.overrides[('GET','/api/onboarding/console/catalog')] = (503, {'code':'DEPENDENCY_UNAVAILABLE'})
        self.page.goto('https://unified.fixture/#mailboxes')
        self.page.locator('#mailbox-card input[type=checkbox]').first.wait_for()
        self.assertTrue(self.page.locator('#console-message').is_visible())
        self.assertEqual(self.page.locator('#script-nav [data-view="google"]').count(), 1)
        self.assertFalse(self.page.locator('#console-logout').is_disabled())
        self.assertFalse(self.page.locator('#filter-submit').is_disabled())
        self.assertEqual(sum(path == '/api/auth/session' for _,path,*_ in self.requests), 1)

    def test_session_failure_prevents_all_catalog_and_pool_reads(self):
        self.overrides[('GET','/api/auth/session')] = (401, {'code':'UNAUTHENTICATED'})
        self.page.goto('https://unified.fixture/#google')
        self.page.wait_for_function('() => document.querySelector("#console-login").hidden === false')
        self.assertEqual([p for _,p,*_ in self.requests], ['/api/auth/session'])
        self.assertTrue(self.page.locator('#console-logout').is_disabled())

    def test_expired_pool_cannot_be_revived_by_late_original_catalog(self):
        held = []
        self.page.route('**/api/onboarding/console/catalog', lambda route: held.append(route))
        self.overrides[('GET','/api/onboarding/mailboxes')] = (401, {'code':'UNAUTHENTICATED'})
        self.page.goto('https://unified.fixture/#mailboxes')
        self.page.wait_for_function('() => document.querySelector("#console-login").hidden === false')
        self.assertTrue(held)
        held[0].fulfill(content_type='application/json', body=json.dumps(catalog(SCHEMA)))
        self.page.wait_for_load_state('networkidle')
        self.assertEqual(self.page.locator('#script-nav [data-id]').count(), 0)
        self.assertTrue(self.page.locator('#console-logout').is_disabled())
        self.assertTrue(self.page.locator('#preflight').is_disabled())

    def test_real_pending_request_survives_panel_changes_and_blocks_old_tools(self):
        self.overrides[('POST','/api/onboarding/batches')] = (503, {'code':'COMMIT_UNKNOWN'})
        self.open('#google')
        self.page.locator('#google-advanced > summary').click()
        self.page.locator('#preflight').click()
        self.page.wait_for_function('() => !document.querySelector("#confirm-batch").disabled')
        self.page.locator('#confirm-batch').click()
        self.page.wait_for_function('() => document.querySelector("#message").textContent.includes("COMMIT_UNKNOWN") && document.querySelector("#main").getAttribute("aria-busy") !== "true"')
        request = [r[2] for r in self.requests if r[1] == '/api/onboarding/batches'][0]
        for view in ('settings', 'mailboxes', 'cards', 'google'):
            if view == 'mailboxes': self.page.locator('[data-view="resources"]').click()
            elif view == 'cards': self.page.locator('#resources-tab-cards').click()
            else: self.page.locator('[data-view="' + view + '"]').click()
            self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.page.evaluate("showView('env')")
        self.assertTrue(self.page.url.endswith('#google'))
        self.assertTrue(self.page.locator('#task-workbench').is_visible())
        self.page.locator('#reconcile-request').click()
        self.page.wait_for_function('() => document.querySelector("#main").getAttribute("aria-busy") !== "true"')
        self.assertEqual([r[2] for r in self.requests if r[1] == '/api/onboarding/batches'], [request, request])
        self.assertEqual(self.documents, ['/'])

    def test_legacy_pool_deeplinks_are_same_original_shell(self):
        for hash, target in [('', 'mailbox-card'), ('#batch-card','task-workbench'), ('#card-pool','card-pool'), ('#global-config','global-config')]:
            self.page.goto('https://unified.fixture/onboarding/pools' + hash)
            self.page.wait_for_function('() => document.querySelectorAll("#script-nav button[data-id]").length === 16')
            self.assertTrue(self.page.locator('#' + target).is_visible())
            self.assertEqual(self.page.locator('nav.sidebar').count(), 1)
            self.assertEqual(self.page.locator('header.topbar').count(), 1)

    def test_visible_logout_uses_pool_guard_and_one_shared_session(self):
        self.open('#google')
        self.page.locator('#console-logout').click()
        self.page.wait_for_url('**/login')
        self.assertEqual(sum(path == '/api/auth/session' for _,path,*_ in self.requests), 1)
        requests = [r for r in self.requests if r[1] == '/api/auth/logout']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0:3], ('POST', '/api/auth/logout', {}))
        self.assertEqual(self.errors, [])

    def test_expired_pool_does_not_get_google_fallback_from_late_catalog_failure(self):
        held = []
        self.page.route('**/api/onboarding/console/catalog', lambda route: held.append(route))
        self.overrides[('GET','/api/onboarding/mailboxes')] = (401, {'code':'UNAUTHENTICATED'})
        self.page.goto('https://unified.fixture/#mailboxes')
        self.page.wait_for_function('() => document.querySelector("#console-login").hidden === false')
        self.assertTrue(held)
        held[0].fulfill(status=503, content_type='application/json', body=json.dumps({'code':'DEPENDENCY_UNAVAILABLE'}))
        self.page.wait_for_load_state('networkidle')
        self.assertEqual(self.page.locator('#script-nav button').count(), 0)
        self.assertIn('会话', self.page.locator('#console-message').inner_text())


    def test_header_restores_original_desktop_and_tablet_height(self):
        self.open('#mailboxes')
        for width,height in ((1440,56),(901,56),(900,52),(761,52)):
            with self.subTest(width=width):
                self.page.set_viewport_size(dict(width=width,height=1000))
                box=self.page.locator('header.topbar').bounding_box()
                self.assertAlmostEqual(box['height'],height,delta=0.5)
                self.assertAlmostEqual(self.page.locator('.layout').bounding_box()['y'],box['y']+box['height'],delta=0.5)

    def test_header_mobile_wrap_contains_account_and_actions(self):
        self.open('#mailboxes')
        for width in (760,375,320):
            with self.subTest(width=width):
                self.page.set_viewport_size(dict(width=width,height=1000))
                box=self.page.locator('header.topbar').bounding_box()
                self.assertGreaterEqual(box['height'],64)
                for selector in ('#console-actor','#console-logout'):
                    item=self.page.locator(selector).bounding_box()
                    self.assertGreaterEqual(item['y'],box['y'])
                    self.assertLessEqual(item['y']+item['height'],box['y']+box['height']+1)
                self.assertTrue(self.page.evaluate('() => document.documentElement.scrollWidth <= innerWidth+1'))


    def test_settings_has_one_sidebar_entry_and_two_preserved_panels(self):
        self.open('#settings')
        self.assertEqual(self.page.locator('.sidebar-tools [data-view="settings"]').inner_text().replace('CFG','').strip(),'系统配置')
        self.assertEqual(self.page.locator('.sidebar-tools [data-view="env"]').count(),0)
        self.assertTrue(self.page.locator('#settings-header').is_visible())
        self.assertTrue(self.page.locator('#global-config').is_visible())
        self.page.locator('#config-timeout_seconds').fill('420')
        self.page.locator('#settings-tab-env').click()
        self.assertTrue(self.page.locator('#view-env').is_visible())
        self.assertFalse(self.page.locator('#global-config').is_visible())
        self.assertIn('active',self.page.locator('.sidebar-tools [data-view="settings"]').get_attribute('class'))
        self.assertEqual(self.page.locator('#settings-tab-env').get_attribute('aria-selected'),'true')
        self.assertTrue(self.page.locator('#btn-save-env').is_disabled())
        self.page.locator('#settings-tab-google').click()
        self.assertEqual(self.page.locator('#config-timeout_seconds').input_value(),'420')
        self.assertEqual(self.page.locator('#settings-tab-google').get_attribute('aria-selected'),'true')
        self.assertEqual([r for r in self.requests if r[0]!='GET'],[])
        self.assertEqual(self.documents,['/'])

    def test_settings_old_links_keyboard_and_history(self):
        self.open('#env')
        self.assertTrue(self.page.locator('#settings-header').is_visible())
        self.assertEqual(self.page.locator('#settings-tab-env').get_attribute('aria-selected'),'true')
        self.page.locator('#settings-tab-env').focus();self.page.keyboard.press('ArrowRight')
        self.page.wait_for_url('**/#settings')
        self.assertTrue(self.page.locator('#global-config').is_visible())
        self.page.go_back();self.page.wait_for_url('**/#env')
        self.assertTrue(self.page.locator('#view-env').is_visible())
        self.page.evaluate("location.hash='global-config'")
        self.page.wait_for_function("() => document.querySelector('#settings-tab-google').getAttribute('aria-selected') === 'true'")
        self.assertTrue(self.page.locator('#global-config').is_visible())

    def test_settings_google_save_keeps_existing_api_and_env_stays_disabled(self):
        self.open('#settings')
        self.page.locator('#config-timeout_seconds').fill('420')
        self.page.locator('#save-config').click()
        self.page.wait_for_function("() => document.querySelector('#main').getAttribute('aria-busy') === 'false'")
        writes=[r for r in self.requests if r[0]!='GET']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][:2],('PUT','/api/onboarding/config'))
        self.assertEqual(writes[0][2]['fields']['timeout_seconds'],420)
        self.assertEqual(writes[0][2]['expected_revision'],pool.REV)
        self.page.locator('#settings-tab-env').click()
        self.assertTrue(self.page.locator('#btn-save-env').is_disabled())
        self.assertFalse([r for r in self.requests if r[1].startswith('/api/env')])

    def test_settings_tab_cannot_bypass_busy_guard(self):
        self.open('#settings')
        self.page.evaluate('window.RFPoolPanel = {beforeNavigate: () => false}')
        self.page.locator('#settings-tab-env').click()
        self.assertTrue(self.page.url.endswith('#settings'))
        self.assertTrue(self.page.locator('#global-config').is_visible())
        self.assertEqual(self.page.locator('#settings-tab-google').get_attribute('aria-selected'),'true')


    def test_resources_single_entry_four_tabs_and_original_content(self):
        self.open('#resources')
        self.assertEqual(self.page.locator('.sidebar-tools [data-view="resources"]').count(),1)
        self.assertIn('资源管理',self.page.locator('.sidebar-tools [data-view="resources"]').inner_text())
        self.assertEqual(self.page.locator('.sidebar-tools [data-view="mailboxes"],.sidebar-tools [data-view="cards"],.sidebar-tools [data-view="assets"],.sidebar-tools [data-view="network"]').count(),0)
        self.assertEqual(self.page.locator('#resources-header [role=tab]').count(),4)
        self.assertTrue(self.page.locator('#mailbox-card').is_visible())
        for view,panel in (('mailboxes','mailbox-card'),('cards','card-pool'),('assets','view-assets'),('network','view-network')):
            self.page.locator('#resources-tab-'+view).click()
            self.assertTrue(self.page.locator('#'+panel).is_visible())
            self.assertEqual(self.page.locator('#resources-tab-'+view).get_attribute('aria-selected'),'true')
            self.assertIn('active',self.page.locator('.sidebar-tools [data-view="resources"]').get_attribute('class'))
        self.assertEqual([r for r in self.requests if r[0]!='GET'],[])
        self.assertEqual(self.errors,[])

    def test_resource_tabs_keep_legacy_links_and_keyboard_navigation(self):
        self.open('#mailpool')
        self.assertTrue(self.page.locator('#mailbox-card').is_visible())
        self.page.locator('#resources-tab-mailboxes').focus();self.page.keyboard.press('ArrowRight')
        self.page.wait_for_url('**/#cards')
        self.assertTrue(self.page.locator('#card-pool').is_visible())
        self.page.keyboard.press('End');self.page.wait_for_url('**/#network')
        self.assertTrue(self.page.locator('#view-network').is_visible())
        self.page.go_back();self.page.wait_for_url('**/#cards')
        for alias,view in (('mailbox-card','mailboxes'),('card-pool','cards'),('network-panel','network'),('legacy-tools','assets')):
            self.page.evaluate('(hash)=>location.hash=hash',alias)
            self.page.wait_for_function("(view)=>document.querySelector('#resources-tab-'+view).getAttribute('aria-selected')==='true'",arg=view)
        self.assertEqual(self.documents,['/'])

    def test_resource_tab_switch_erases_mail_import_input(self):
        self.open('#mailboxes');self.page.locator('#tab-import').click()
        self.page.locator('#import-text').fill('demo@fixture.invalid----fixture:password')
        self.page.locator('#resources-tab-cards').click()
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.page.locator('#resources-tab-mailboxes').click()
        self.assertTrue(self.page.locator('#panel-list').is_visible())
        self.assertEqual([r for r in self.requests if r[0]!='GET'],[])

    def test_resource_pending_receipt_cannot_enter_legacy_tools(self):
        self.overrides[('POST','/api/onboarding/batches')]=(503,{'code':'COMMIT_UNKNOWN'})
        self.open('#google');self.page.locator('#run-google').click()
        self.page.wait_for_function("() => document.querySelector('#main').getAttribute('aria-busy') === 'false' && !document.querySelector('#reconcile-card').hidden")
        self.page.locator('[data-view="resources"]').click()
        for view in ('assets','network'):
            self.page.locator('#resources-tab-'+view).click()
            self.assertTrue(self.page.url.endswith('#mailboxes'))
            self.assertTrue(self.page.locator('#mailbox-card').is_visible())
            self.assertTrue(self.page.locator('#reconcile-card').is_visible())
            self.assertEqual(self.page.locator('#resources-tab-mailboxes').get_attribute('aria-selected'),'true')
        self.assertEqual(len([r for r in self.requests if r[1]=='/api/onboarding/batches']),1)

    def test_resource_tabs_cannot_bypass_busy_guard(self):
        self.open('#cards');self.page.evaluate('window.RFPoolPanel={beforeNavigate:()=>false}')
        self.page.locator('#resources-tab-assets').click()
        self.assertTrue(self.page.url.endswith('#cards'))
        self.assertEqual(self.page.locator('#resources-tab-cards').get_attribute('aria-selected'),'true')
        self.assertTrue(self.page.locator('#card-pool').is_visible())

    def test_resource_tabs_mobile_no_overflow_and_hide_on_other_pages(self):
        self.open('#resources')
        self.page.set_viewport_size(dict(width=375,height=1000))
        for view in ('mailboxes','cards','assets','network'):
            self.page.locator('#resources-tab-'+view).click()
            self.assertTrue(self.page.evaluate('() => document.documentElement.scrollWidth <= innerWidth+1'))
        self.page.evaluate("showView('settings')")
        self.assertTrue(self.page.locator('#resources-header').is_hidden())
        self.assertTrue(self.page.locator('#settings-header').is_visible())


class MailboxImportPresentationTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    setUp = UnifiedConsoleTests.setUp
    tearDown = UnifiedConsoleTests.tearDown
    guarded = UnifiedConsoleTests.guarded
    dialog = UnifiedConsoleTests.dialog
    route = UnifiedConsoleTests.route
    open = UnifiedConsoleTests.open
    calls = pool.PoolDomTests.calls

    def start_import(self):
        self.open('#mailboxes')
        self.page.locator('#tab-import').click()

    def check(self):
        self.page.locator('#import-text').fill('demo@fixture.invalid----fixture:password')
        self.page.locator('#preview-import').click()
        self.page.wait_for_function("() => document.querySelector('#main').getAttribute('aria-busy') === 'false'")

    def test_initial_layout_has_clear_labels_empty_fields_and_no_import_button(self):
        self.start_import()
        self.assertEqual(self.page.locator('#panel-import h3').first.inner_text(),'导入邮箱')
        self.assertEqual(self.page.locator('#import-group').input_value(),'')
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertIn('每行一条',self.page.locator('#import-text').get_attribute('placeholder'))
        self.assertEqual(self.page.locator('#preview-import').inner_text(),'检查资料')
        self.assertEqual(self.page.locator('#cancel-import').inner_text(),'清空')
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())
        self.assertIn('仅接受测试凭据',self.page.locator('#import-environment').inner_text())

    def test_valid_check_shows_count_and_edits_invalidate_it(self):
        self.start_import(); self.check()
        self.assertEqual(self.page.locator('#confirm-import').inner_text(),'导入 1 条')
        self.assertTrue(self.page.locator('#confirm-import').is_visible())
        self.assertIn('检查通过 1 条',self.page.locator('#import-preview').inner_text())
        self.assertFalse(self.calls('POST','/api/onboarding/mailboxes/import'))
        self.page.locator('#import-group').fill('新分组')
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())
        self.assertEqual(self.page.locator('#import-preview').inner_text(),'')

    def test_issue_summary_does_not_offer_partial_import(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import-preview')]=(200,dict(items=[],issues=[dict(line=2,code='DUPLICATE_EMAIL'),dict(line=3,code='INVALID_RECORD')],accepted_count=1,duplicate_count=1,conflict_count=0,preview_digest=None))
        self.start_import(); self.check()
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())
        self.assertIn('重复 1 条',self.page.locator('#import-preview').inner_text())
        self.assertIn('第 2 行',self.page.locator('#import-preview').inner_text())
        self.assertIn('请先修正',self.page.locator('#import-preview').inner_text())
        self.assertFalse(self.calls('POST','/api/onboarding/mailboxes/import'))

    def test_invalid_preview_count_never_enables_import(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import-preview')]=(200,dict(items=[],issues=[],accepted_count='99',duplicate_count=0,conflict_count=0,preview_digest='a'*64))
        self.start_import(); self.check()
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertFalse(self.calls('POST','/api/onboarding/mailboxes/import'))

    def test_clear_hides_submit_and_erases_input(self):
        self.start_import(); self.check(); self.page.locator('#cancel-import').click()
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())

    def test_success_preserves_skip_rule_and_clears_input(self):
        self.start_import(); self.check(); self.page.locator('#confirm-import').click()
        self.page.wait_for_function("() => document.querySelector('#main').getAttribute('aria-busy') === 'false'")
        body=self.calls('POST','/api/onboarding/mailboxes/import')[0][2]
        self.assertEqual(body['duplicate_action'],'skip')
        self.assertEqual(body['group_ref'],'')
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertTrue(self.page.locator('#confirm-import').is_hidden())
        self.assertIn('新增 1',self.page.locator('#message').inner_text())

    def test_mobile_form_has_no_horizontal_overflow(self):
        self.page.set_viewport_size(dict(width=375,height=1000))
        self.start_import(); self.page.locator('#import-formats > summary').click()
        self.assertTrue(self.page.evaluate('() => document.documentElement.scrollWidth <= innerWidth + 1'))
        self.check()
        self.assertTrue(self.page.evaluate('() => document.documentElement.scrollWidth <= innerWidth + 1'))

    def test_examples_pass_existing_parser_and_vault_policy(self):
        from onboarding.legacy_mailboxes import parse_for_import
        from onboarding.pool_secret_types import MailboxCredential, _encode_payload
        self.start_import()
        for example in self.page.locator('#import-formats pre').all_text_contents():
            parsed=parse_for_import(example)
            self.assertFalse(parsed.issues)
            self.assertTrue(parsed.records)
            for r in parsed.records:
                _encode_payload(MailboxCredential(r.email_norm,r.password,r.refresh_token,r.client_id,r.provider,r.mail_api_url,r.mail_api_key,r.two_factor))
        self.assertEqual(self.page.locator('#import-formats pre').count(),2)
