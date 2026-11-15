"""Intercepted Chromium HTTP composition; no PG, provider or real credentials."""
import copy
import json
import unittest
from urllib.parse import urlsplit
from playwright.sync_api import expect
from tests import test_onboarding_pool_ui as pool
from tests.test_onboarding_live_config import FIELDS

BASE = '/api/onboarding'
CAPS = dict(mode='live', workflow='existing_billing_cloud_v1',
            source=dict(available=True, mailbox_id=pool.MID, ref='local-authorized-mailbox'),
            start_available=True)
LIVE_TASK = dict(pool.TASK, synthetic=False, execution_mode='live', status='RUNNING', version=3)
START = dict(accepted=True, synthetic=False, execution_mode='live', config_id=pool.MID, batch_id=pool.MID, task_ids=[pool.TID], mailbox_ids=[pool.MID],
             receipt_id=pool.MID, config_revision=pool.REV, replayed=False, scheduled=True)


class LiveUiTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    tearDown = pool.PoolDomTests.tearDown
    guarded = pool.PoolDomTests.guarded
    dialog = pool.PoolDomTests.dialog
    calls = pool.PoolDomTests.calls

    def setUp(self):
        pool.PoolDomTests.setUp(self)
        self.mode = 'live'
        self.embedded = False
        self.documents = []
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.overrides.update({
            ('GET', BASE + '/live/capabilities'): (200, copy.deepcopy(CAPS)),
            ('GET', BASE + '/config'): (200, dict(revision=pool.REV, nonsecret_config=FIELDS.copy())),
            ('GET', BASE + '/tasks'): (200, dict(items=[LIVE_TASK], next_cursor=None)),
            ('GET', BASE + '/tasks/' + pool.TID): (200, LIVE_TASK),
            ('GET', BASE + '/tasks/' + pool.TID + '/events'): (200, dict(
                task_id=pool.TID, mode='live', execution_connected=False, has_more=False,
                events=[], effect_events=[], effects_has_more=False)),
            ('POST', BASE + '/live/start'): (202, START.copy()),
            ('POST', BASE + '/tasks/' + pool.TID + '/pause'): (202, dict(accepted=True, synthetic=False, execution_mode='live', receipt_id=pool.MID, phase='SUCCEEDED', task=None, snapshot_pending=True)),
            ('POST', BASE + '/tasks/' + pool.TID + '/cancel'): (202, dict(accepted=True, synthetic=False, execution_mode='live', receipt_id=pool.MID, phase='SUCCEEDED', task=None, snapshot_pending=True)),
            ('POST', BASE + '/live/source-preview'): (200, dict(email='weifashi68@gmail.com',
                provider='other', group_ref='owned', preview_digest='a' * 64)),
            ('POST', BASE + '/live/source-import'): lambda request: (200, dict(mailbox_id=pool.MID,
                created=True, request_key=request.post_data_json['request_key'])),
        })

    def route(self, route):
        if self.embedded:
            from tests import test_onboarding_unified_ui as unified
            if urlsplit(route.request.url).path in ('/', '/onboarding/pools'):
                from webui.onboarding_console import render_console
                html, _ = render_console((pool.STATIC / 'index.html').read_text(),
                    pool_source=(pool.STATIC / 'onboarding-pools.html').read_text(), pool_mode=self.mode)
                route.fulfill(content_type='text/html', body=html)
            else:
                unified.UnifiedConsoleTests.route(self, route)
        elif urlsplit(route.request.url).path == '/onboarding/pools':
            html = (pool.STATIC / 'onboarding-pools.html').read_text()
            # Same exact server-owned marker contract; only HTTP is a fixture.
            html = html.replace('<main id="main"', '<main data-pool-mode="' + self.mode + '" id="main"')
            route.fulfill(content_type='text/html', body=html)
        else:
            pool.PoolDomTests.route(self, route)

    def start(self):
        self.page.goto('https://pool.invalid/onboarding/pools')
        self.page.wait_for_load_state('networkidle')
        expect(self.page.locator('#main')).to_have_attribute('aria-busy', 'false')

    def settled(self):
        expect(self.page.locator('#main')).to_have_attribute('aria-busy', 'false')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.harness_errors, [])

    def click_run(self):
        self.page.locator('#run-google').click()
        self.settled()

    def test_live_fixed_single_start_never_calls_legacy_or_synthetic(self):
        self.start()
        expect(self.page.locator('#run-google')).to_be_enabled()
        expect(self.page.locator('#requested-count')).to_be_disabled()
        expect(self.page.locator('label[for=requested-count]')).to_have_text('固定数量')
        expect(self.page.locator('#google-mode-help')).to_contain_text('已有结算')
        expect(self.page.locator('#google-mode-help')).to_contain_text('不申请试用')
        self.click_run()
        body = self.calls('POST', BASE + '/live/start')[0][2]
        self.assertEqual(set(body), {'mailbox_id', 'expected_config_revision', 'request_key'})
        self.assertEqual(body['mailbox_id'], pool.MID)
        self.assertEqual(body['expected_config_revision'], pool.REV)
        self.assertEqual(self.calls('POST', BASE + '/preflight'), [])
        self.assertEqual(self.calls('POST', BASE + '/batches'), [])
        self.assertEqual(self.calls('POST', '/api/run'), [])
        expect(self.page.locator('#google-log')).to_contain_text('已移交后台')
        expect(self.page.locator('#google-log')).not_to_contain_text('开通成功')

    def test_missing_capability_never_enables_live_or_falls_back(self):
        self.overrides[('GET', BASE + '/live/capabilities')] = (503, {'code':'DEPENDENCY_UNAVAILABLE'})
        self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        self.page.evaluate("() => { document.querySelector('#run-google').click(); document.querySelector('#batch-form').dispatchEvent(new Event('submit',{cancelable:true})); }")
        self.settled()
        self.assertFalse([r for r in self.requests if r[0] == 'POST'])

    def test_unbound_source_and_missing_config_permission_keep_start_disabled(self):
        self.overrides[('GET', BASE + '/live/capabilities')] = (200, dict(CAPS,
            source=dict(available=True, mailbox_id=None, ref='local-authorized-mailbox'), start_available=False))
        self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        expect(self.page.locator('#live-source-state')).to_contain_text('绑定')

    def test_task_only_operator_does_not_read_privileged_config_or_start(self):
        self.permissions.remove('config:manage')
        self.start()
        self.assertEqual(self.calls('GET', BASE + '/config'), [])
        expect(self.page.locator('#run-google')).to_be_disabled()

    def test_v2_config_exact_fields_only_on_explicit_save(self):
        self.start()
        self.assertFalse([r for r in self.requests if r[0] in ('POST','PUT')])
        self.assertEqual(self.page.locator('#live-config-project_prefix').count(), 1, 'live v2 form missing')
        self.page.locator('#live-config-project_prefix').fill('approved')
        self.page.locator('#save-config').click()
        self.settled()
        body = self.calls('PUT', BASE + '/config')[0][2]
        self.assertEqual(body['fields'], dict(FIELDS, project_prefix='approved'))
        self.assertEqual(body['expected_revision'], pool.REV)
        self.assertFalse(body['fields']['scheduling_enabled'])
        self.assertFalse(body['fields']['model_verification_enabled'])

    def test_legacy_v1_config_is_not_silently_promoted_or_started(self):
        self.overrides[('GET', BASE + '/config')] = (200, dict(revision=pool.REV, nonsecret_config=pool.FIELDS))
        self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        expect(self.page.locator('#config-state')).to_contain_text('旧配置')
        self.assertFalse([r for r in self.requests if r[0] in ('POST','PUT')])

    def test_trusted_source_preview_import_has_no_secret_text_or_path(self):
        self.start()
        self.page.locator('#tab-import').click()
        expect(self.page.locator('#import-text')).to_be_hidden()
        self.page.locator('#import-group').fill('owned')
        self.page.locator('#preview-import').click(); self.settled()
        self.assertEqual(self.calls('POST', BASE + '/live/source-preview')[0][2], {'group_ref':'owned'})
        self.page.locator('#confirm-import').click(); self.settled()
        body = self.calls('POST', BASE + '/live/source-import')[0][2]
        self.assertEqual(set(body), {'group_ref','preview_digest','request_key'})
        self.assertEqual(body['preview_digest'], 'a' * 64)
        expect(self.page.locator('#message')).to_contain_text('可信启动配置')
        expect(self.page.locator('#run-google')).to_be_disabled()
        self.assertEqual(self.calls('POST', BASE + '/mailboxes/import'), [])

    def test_unknown_live_start_retains_exact_original_request_for_observe(self):
        self.overrides[('POST', BASE + '/live/start')] = (503, {'code':'COMMIT_UNKNOWN'})
        self.start(); self.click_run()
        original = self.calls('POST', BASE + '/live/start')[0][2]
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        expect(self.page.locator('#run-google')).to_be_disabled()
        self.overrides[('POST', BASE + '/live/start')] = (202, dict(START, replayed=True, scheduled=False))
        self.page.locator('#reconcile-request').click(); self.settled()
        self.assertEqual(self.calls('POST', BASE + '/live/start')[1][2], original)
        expect(self.page.locator('#google-log')).to_contain_text('仅观察原回执')
        expect(self.page.locator('#reconcile-card')).to_be_hidden()

    def test_accepted_followup_failure_keeps_receipt_not_unknown_mutation(self):
        def accepted(request):
            self.overrides[('GET', BASE + '/tasks')] = (503, {'code':'DEPENDENCY_UNAVAILABLE'})
            self.overrides[('GET', BASE + '/tasks/' + pool.TID)] = (503, {'code':'DEPENDENCY_UNAVAILABLE'})
            return 202, START
        self.overrides[('POST', BASE + '/live/start')] = accepted
        self.start(); self.click_run()
        expect(self.page.locator('#batch-result')).to_contain_text(pool.MID)
        expect(self.page.locator('#reconcile-card')).to_be_hidden()
        expect(self.page.locator('#message')).to_contain_text('读取失败')
        self.assertEqual(len(self.calls('POST', BASE + '/live/start')), 1)

    def test_live_pause_cancel_enabled_only_after_capabilities_recheck_never(self):
        self.start()
        self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').click(); self.settled()
        expect(self.page.locator('#pause-task')).to_be_enabled()
        expect(self.page.locator('#cancel-task')).to_be_enabled()
        expect(self.page.locator('#recheck-task')).to_be_disabled()
        self.page.evaluate("() => document.querySelector('#recheck-task').click()")
        self.page.locator('#pause-task').click(); self.settled()
        self.assertEqual(self.calls('POST', BASE + '/tasks/' + pool.TID + '/recheck'), [])
        self.assertEqual(len(self.calls('POST', BASE + '/tasks/' + pool.TID + '/pause')), 1)

    def test_capability_failure_keeps_live_history_readonly(self):
        self.overrides[('GET', BASE + '/live/capabilities')] = (503, {'code':'DEPENDENCY_UNAVAILABLE'})
        self.start(); self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').click(); self.settled()
        expect(self.page.locator('#pause-task')).to_be_disabled()
        expect(self.page.locator('#cancel-task')).to_be_disabled()
        self.page.evaluate("() => document.querySelector('#pause-task').click()")
        self.assertEqual(self.calls('POST', BASE + '/tasks/' + pool.TID + '/pause'), [])

    def test_card_and_mailbox_metadata_writes_stay_disabled_in_live(self):
        self.permissions.append('cards:manage')
        self.start()
        expect(self.page.locator('[data-mailbox-edit]').first).to_be_disabled()
        self.page.evaluate("() => document.querySelector('[data-save-group]').dispatchEvent(new Event('click'))")
        expect(self.page.locator('#card-preview-manual')).to_be_disabled()
        self.page.evaluate("() => document.querySelector('#card-manual-form').dispatchEvent(new Event('submit',{cancelable:true}))")
        self.settled()
        self.assertFalse([r for r in self.requests if r[0] in ('POST','PATCH')])

    def test_invalid_capabilities_do_not_expose_untrusted_metadata_or_authorize(self):
        self.overrides[('GET', BASE + '/live/capabilities')] = (200, dict(CAPS,
            source=dict(CAPS['source'], owner='UNTRUSTED_SECRET_MARKER')))
        self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        expect(self.page.locator('body')).not_to_contain_text('UNTRUSTED_SECRET_MARKER')

    def test_start_receipt_must_match_original_config_and_mailbox(self):
        self.overrides[('POST', BASE + '/live/start')] = (202, dict(START, mailbox_ids=[pool.CID]))
        self.start(); self.click_run()
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        expect(self.page.locator('#google-log')).not_to_contain_text('已移交后台')

    def test_source_unknown_result_replays_same_digest_without_secret_input(self):
        self.overrides[('POST', BASE + '/live/source-import')] = (503, {'code':'COMMIT_UNKNOWN'})
        self.start(); self.page.locator('#tab-import').click()
        self.page.locator('#import-group').fill('owned')
        self.page.locator('#preview-import').click(); self.settled()
        self.page.locator('#confirm-import').click(); self.settled()
        original = self.calls('POST', BASE + '/live/source-import')[0][2]
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        self.overrides[('POST', BASE + '/live/source-import')] = lambda request: (200, dict(
            mailbox_id=pool.MID, created=False, request_key=request.post_data_json['request_key']))
        self.page.locator('#reconcile-request').click(); self.settled()
        self.assertEqual(self.calls('POST', BASE + '/live/source-import')[1][2], original)
        expect(self.page.locator('#reconcile-card')).to_be_hidden()
        self.assertEqual(len(self.calls('POST', BASE + '/live/source-preview')), 1)

    def test_client_selection_tampering_never_changes_fixed_live_mailbox(self):
        self.start()
        self.page.evaluate("() => { document.querySelector('#selection').value='specified'; document.querySelector('#requested-count').value=99; }")
        self.click_run()
        body = self.calls('POST', BASE + '/live/start')[0][2]
        self.assertEqual(body['mailbox_id'], pool.MID)
        self.assertNotIn('requested_count', body)

    def test_session_expiry_during_capability_read_cannot_enable_late_start(self):
        def expired(request):
            self.page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
            return 200, CAPS
        self.overrides[('GET', BASE + '/live/capabilities')] = expired
        self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        self.page.evaluate("() => document.querySelector('#run-google').click()")
        self.assertFalse([r for r in self.requests if r[0] == 'POST'])

    def test_original_server_renderer_live_mode_preserves_shell_and_fixed_run(self):
        self.embedded = True
        self.page.set_viewport_size({'width':1440, 'height':1000})
        self.page.goto('https://pool.invalid/#google')
        self.page.wait_for_load_state('networkidle')
        expect(self.page.locator('#main')).to_have_attribute('data-pool-mode', 'live')
        expect(self.page.locator('#run-google')).to_be_visible()
        expect(self.page.locator('#run-google')).to_be_enabled()
        self.assertEqual(self.page.locator('header.topbar').count(), 1)
        self.assertEqual(self.page.locator('nav.sidebar').count(), 1)
        self.assertEqual(self.page.locator('#run-google').count(), 1)
        self.assertEqual(len(self.calls('GET', '/api/auth/session')), 1)
        self.page.screenshot(path='/tmp/rf-live-ui-evidence/live-original-1440.png', full_page=True)
        self.click_run()
        self.assertEqual(len(self.calls('POST', BASE + '/live/start')), 1)
        self.assertEqual(self.calls('POST', '/api/run'), [])
        self.page.set_viewport_size({'width':375, 'height':900})
        self.page.wait_for_function("() => document.querySelector('nav.sidebar').getBoundingClientRect().right <= 0")
        self.page.screenshot(path='/tmp/rf-live-ui-evidence/live-original-375.png', full_page=True, animations='disabled')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 375)

    def test_live_boot_synthetic_history_is_readonly_even_with_explicit_mode(self):
        for scope in ('pool', 'fixture'):
            with self.subTest(scope=scope):
                value = dict(pool.TASK, execution_scope=scope)
                self.overrides[('GET', BASE + '/tasks/' + pool.TID)] = (200, value)
                self.start(); self.page.locator('#task-history > summary').click()
                self.page.locator('[data-open-task]').click(); self.settled()
                expect(self.page.locator('#pause-task')).to_be_disabled()
                expect(self.page.locator('#cancel-task')).to_be_disabled()
                self.page.evaluate("() => document.querySelector('#pause-task').dispatchEvent(new Event('click'))")
                self.assertEqual(self.calls('POST', BASE + '/tasks/' + pool.TID + '/pause'), [])

    def test_malformed_live_command_receipt_is_not_accepted_or_forgotten(self):
        self.overrides[('POST', BASE + '/tasks/' + pool.TID + '/pause')] = (202, {'accepted':True})
        self.start(); self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').click(); self.settled()
        self.page.locator('#pause-task').click(); self.settled()
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        expect(self.page.locator('#command-receipt')).not_to_contain_text('已受理')

    def test_mobile_live_view_keeps_original_layout_without_overflow(self):
        self.page.set_viewport_size({'width':375, 'height':900})
        self.start()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 375)
        self.assertEqual(self.page.locator('#run-google').count(), 1)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LiveUiTests)
