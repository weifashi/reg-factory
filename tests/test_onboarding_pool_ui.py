"""Offline Chromium DOM tests: HTTP is intercepted, no database or provider calls.

Run with the isolated UI verification environment (Playwright + local Chromium).
These are NOT live API/PG acceptance evidence.

Synchronisation: an action waits for its exact request, then for the page to
leave aria-busy and show the expected outcome. Fixed sleeps are only bounded
"nothing further happens" windows after that point, never the sync itself.
"""
import json
import re
from pathlib import Path
import unittest
from urllib.parse import urlsplit, parse_qs

STATIC = Path(__file__).resolve().parents[1] / 'webui' / 'static'
MID = '00000000-0000-4000-8000-000000000001'
TID = '00000000-0000-4000-8000-000000000002'
REV = 'pool-00000000-0000-4000-8000-000000000003'
CID = '00000000-0000-4000-8000-0000000000cc'
PERMS = ['onboarding:read', 'mailboxes:manage', 'tasks:manage', 'config:manage']
FIELDS = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=300,
              concurrency=1, retention_days=7)
TASK = dict(id=TID, execution_scope='pool', batch_id=MID, config_id=MID,
            config_revision=REV, status='QUEUED', version=1, reason_code=None,
            current_step=None, generation=1, cancel_requested=False, synthetic=True)
MAILBOX = dict(id=MID, email='fixture-ui@example.test', group_ref='fixture:group',
               health='UNKNOWN', disabled=False, occupied=False, version=1,
               platforms=[dict(platform='google', identity_status='UNKNOWN', usage_status='HISTORY_UNRECONCILED')])
# Response failures beyond JSON 5xx: a non-JSON gateway page and a network abort.
GATEWAY = (502, '<html>bad gateway</html>')
FOLLOWUP_FAILURES = {'json-503': (503, dict(code='DEPENDENCY_UNAVAILABLE')), 'html-502': GATEWAY, 'abort': 'abort',
                     '401': (401, dict(code='UNAUTHENTICATED'))}
MUTATION_FAILURES = [(str(status), (status, dict(code='DEPENDENCY_UNAVAILABLE'))) for status in (500, 502, 503, 504)]
MUTATION_FAILURES += [('html-502', GATEWAY), ('abort', 'abort')]
# No rejection of a replay proves the original never committed: environment checks before
# the receipt read (migration catalog -> VERSION_CONFLICT, database target -> INVALID_INPUT)
# answer 409/422 as well. Every one of these must keep the request pending.
REPLAY_REJECTIONS = [(code, (409, dict(code=code))) for code in ('VERSION_CONFLICT', 'RESOURCE_HELD', 'IDEMPOTENCY_CONFLICT',
                                                               'STALE_FENCE', 'RECONCILIATION_REQUIRED', 'APPROVAL_INVALID')]
REPLAY_REJECTIONS += [('INVALID_INPUT', (422, dict(code='INVALID_INPUT'))), ('409-no-code', (409, {})), ('422-no-code', (422, {})),
                      ('429', (429, dict(code='RATE_LIMITED'))), ('403', (403, dict(code='FORBIDDEN')))]
PROVED = (409, dict(code='VERSION_CONFLICT', not_committed=True))
NOT_PROOFS = [('string', (409, dict(code='VERSION_CONFLICT', not_committed='true'))),
              ('one', (409, dict(code='VERSION_CONFLICT', not_committed=1))),
              ('null', (409, dict(code='VERSION_CONFLICT', not_committed=None))),
              ('false', (409, dict(code='VERSION_CONFLICT', not_committed=False))),
              ('held', (409, dict(code='RESOURCE_HELD', not_committed=True))),
              ('invalid', (422, dict(code='INVALID_INPUT', not_committed=True))),
              ('forbidden', (403, dict(code='FORBIDDEN', not_committed=True))),
              ('status-only', (422, dict(code='VERSION_CONFLICT', not_committed=True)))]


class PoolAssetsTests(unittest.TestCase):
    def test_assets_exist_and_no_unsafe_sinks_or_persistence(self):
        for filename in ('onboarding-pools.html', 'onboarding-pools.js'):
            self.assertTrue((STATIC / filename).is_file(), 'missing protected pool asset: ' + filename)
        script = (STATIC / 'onboarding-pools.js').read_text()
        for forbidden in ('innerHTML', 'outerHTML', 'localStorage', 'sessionStorage', 'document.cookie', 'console.', 'eval('):
            self.assertNotIn(forbidden, script)
        html = (STATIC / 'onboarding-pools.html').read_text()
        self.assertIn('method="post"', html)
        self.assertNotIn('onclick=', html)

    def test_session_token_is_read_once_and_only_cleared_afterwards(self):
        # Releasing a pending request on the server's proof relies on the page never switching
        # sessions: csrf comes once from /api/auth/session and is only ever cleared afterwards.
        # A refresh would let another operator's proof release this operator's pending request.
        for name, source in (('onboarding-pools.js', 'session.csrf_token'), ('onboarding.js', 'data.csrf_token')):
            values = re.findall(r'\bcsrf\s*=(?!=)\s*([^;,\n]+)', (STATIC / name).read_text())
            self.assertEqual(sorted(value.strip() for value in values), sorted(["''", "''", source]), name)


class PoolDomTests(unittest.TestCase):
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
        self.page.set_default_timeout(6000)
        # Only the navigation budget is wider: page loads slowed past 6s on the shared, loaded host.
        self.page.set_default_navigation_timeout(20000)
        self.requests = []
        self.overrides = {}
        self.permissions = PERMS[:]
        self.dialogs = []
        self.dialog_decide = lambda text: True
        self.harness_errors = []
        self.page.route('**/*', self.guarded(self.route))
        self.page.on('dialog', self.guarded(self.dialog))

    def tearDown(self):
        self.context.close()

    def guarded(self, callback):
        """Record exceptions from harness callbacks: a hang they cause is a harness error, not a caught defect."""
        def run(value):  # one parameter: Playwright passes as many arguments as the handler declares
            try:
                return callback(value)
            except Exception as exc:
                self.harness_errors.append('%s: %r' % (callback.__name__, exc))
                raise
        return run

    def timed_out(self, what, selector='#message'):
        """A page that never reaches the expected state is a failed expectation, unless the harness broke."""
        if self.harness_errors:
            raise RuntimeError('harness callback failed: ' + '; '.join(self.harness_errors))
        shown = self.page.evaluate("s => document.querySelector(s)?.textContent ?? ''", selector)
        self.fail(what + '; shown: ' + shown[:200])

    def dialog(self, dialog):
        self.dialogs.append(dialog.message)
        if self.dialog_decide(dialog.message):
            dialog.accept()
        else:
            dialog.dismiss()

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path in ('/onboarding/pools', '/onboarding'):
            name = 'onboarding-pools.html' if path.endswith('pools') else 'onboarding.html'
            if not (STATIC / name).exists():
                route.fulfill(status=404, body='Pool UI not implemented')
            else:
                route.fulfill(content_type='text/html', body=(STATIC / name).read_text())
            return
        if path.startswith('/static/'):
            name = path.split('/')[-1]
            route.fulfill(content_type='text/css' if name.endswith('.css') else 'text/javascript',
                          body=(STATIC / name).read_text() if (STATIC / name).exists() else '')
            return
        self.requests.append((request.method, path, request.post_data_json if request.post_data else None, request.url))
        override = self.overrides.get((request.method, path))
        if callable(override):
            override = override(request)
        if override == 'abort':
            route.abort()
            return
        if override:
            status, value = override
        elif path == '/api/auth/session':
            status, value = 200, dict(csrf_token='fixture-csrf', permissions=self.permissions, display_name='合成操作者')
        elif path == '/api/onboarding/mailboxes':
            status, value = 200, dict(items=[MAILBOX], next_cursor='fixture-cursor')
        elif path == '/api/onboarding/tasks':
            status, value = 200, dict(items=[TASK], next_cursor=None)
        elif path == '/api/onboarding/tasks/' + TID:
            status, value = 200, TASK
        elif path == '/api/onboarding/config':
            status, value = 200, dict(revision=REV, nonsecret_config=FIELDS)
        elif path == '/api/onboarding/diagnostics':
            status, value = 200, dict(ready=True, schema_version=2)
        elif path.endswith('/import-preview'):
            status, value = 200, dict(items=[dict(line=1,email=MAILBOX['email'],provider='outlook',group_ref='fixture:group')], issues=[], accepted_count=1,duplicate_count=0,conflict_count=0,preview_digest='a'*64)
        elif path.endswith('/import'):
            status, value = 200, dict(created_ids=[MID], skipped_ids=[], request_key='fixture-key')
        elif path.endswith('/preflight'):
            status, value = 200, dict(selection='automatic',requested_count=1,eligible_count=1,config_revision=REV,can_create=True,reason_codes=[],observation_only=True)
        elif path.endswith('/batches'):
            status, value = 202, dict(batch_id=MID,task_ids=[TID],mailbox_ids=[MID],receipt_id=MID,config_id=MID,config_revision=REV,phase='SUCCEEDED')
        elif path.endswith(('/pause','/cancel','/recheck')):
            status, value = 202, dict(accepted=True,synthetic=True,execution_scope='pool',receipt_id=MID,phase='SUCCEEDED',task=None,snapshot_pending=True)
        elif request.method == 'PATCH':
            status, value = 200, dict(mailbox_id=MID,version=2,request_key='fixture-key')
        else:
            status, value = 503, dict(code='DEPENDENCY_UNAVAILABLE')
        if isinstance(value, str):
            route.fulfill(status=status, content_type='text/html', body=value)
        else:
            route.fulfill(status=status, content_type='application/json', body=json.dumps(value))

    def start(self, path='/onboarding/pools'):
        self.page.goto('https://pool.invalid' + path)
        self.page.wait_for_load_state('networkidle')
        self.assertEqual(self.page.locator('#main').count(), 1, 'protected page has not been implemented')
        self.settle()

    def calls(self, method, path):
        return [entry for entry in self.requests if entry[0:2] == (method,path)]

    def settle(self, text=None, selector='#message'):
        """Wait until the page left aria-busy and, optionally, shows the outcome text."""
        from playwright.sync_api import TimeoutError as PageTimeout
        try:
            self.page.wait_for_function(
                "([text, selector]) => document.querySelector('#main').getAttribute('aria-busy') !== 'true'"
                " && (!text || document.querySelector(selector).textContent.includes(text))", arg=[text, selector])
        except PageTimeout:
            self.timed_out('page never settled' + (' on ' + repr(text) if text else ''), selector)

    def act(self, trigger, method, path, text=None, selector='#message'):
        """Issue exactly the named request, then wait for its handler to finish; returns the body."""
        if isinstance(trigger, str):
            target = trigger
            trigger = lambda: self.page.locator(target).click()
        from playwright.sync_api import TimeoutError as PageTimeout
        try:
            with self.page.expect_request(lambda request: request.method == method and urlsplit(request.url).path == path) as info:
                trigger()
        except PageTimeout:
            self.timed_out('%s %s was never issued' % (method, path), selector)
        self.settle(text, selector)
        return info.value.post_data_json

    def until(self, predicate, reason):
        for _ in range(300):
            if predicate():
                return
            self.page.wait_for_timeout(20)
        self.timed_out('condition never held: ' + reason)

    def assert_quiet(self, method, path, count, window=300):
        """Bounded observation after settling: no further request of this kind appears."""
        self.page.wait_for_timeout(window)
        self.assertEqual(len(self.calls(method, path)), count)

    def force_click(self, selector):
        """Exercise the handler guard itself, not only the disabled attribute."""
        self.page.locator(selector).first.evaluate('button => { button.disabled = false; button.click(); }')

    def import_preview(self):
        self.page.locator('#tab-import').click()
        self.page.locator('#import-text').fill('fixture-ui@example.test----fixture-ui-password')
        self.act('#preview-import', 'POST', '/api/onboarding/mailboxes/import-preview')

    def test_list_platform_filter_and_cursor_reset(self):
        self.start()
        self.assertIn('HISTORY_UNRECONCILED', self.page.locator('#mailbox-list').inner_text())
        self.act('#mailbox-next', 'GET', '/api/onboarding/mailboxes')
        self.assertIn('cursor=fixture-cursor', self.calls('GET','/api/onboarding/mailboxes')[-1][3])
        self.act(lambda: self.page.locator('#platform').select_option('google'), 'GET', '/api/onboarding/mailboxes')
        query = parse_qs(urlsplit(self.calls('GET','/api/onboarding/mailboxes')[-1][3]).query)
        self.assertEqual(query['platform'], ['google'])
        self.assertNotIn('cursor', query)
        self.assertEqual(self.page.locator('#tab-history').get_attribute('disabled'), '')

    def test_edited_text_filters_drop_cursor_until_requery(self):
        self.overrides[('GET','/api/onboarding/tasks')] = (200,dict(items=[TASK],next_cursor='fixture-task-cursor'))
        self.start()
        for selector in ('#search','#filter-group'):
            with self.subTest(selector=selector):
                self.act('#filter-submit','GET','/api/onboarding/mailboxes')
                self.assertTrue(self.page.locator('#mailbox-next').is_enabled())
                self.page.locator(selector).fill('fixture-edited')
                self.assertTrue(self.page.locator('#mailbox-next').is_disabled(),'edited filter must not pair with the old cursor')
                count = len(self.calls('GET','/api/onboarding/mailboxes'))
                self.force_click('#mailbox-next')
                self.assert_quiet('GET','/api/onboarding/mailboxes',count)
                self.page.locator(selector).fill('')
        self.page.locator('#search').fill('fixture-edited')
        self.act('#filter-submit','GET','/api/onboarding/mailboxes')
        query = parse_qs(urlsplit(self.calls('GET','/api/onboarding/mailboxes')[-1][3]).query)
        self.assertEqual(query['search'],['fixture-edited'])
        self.assertNotIn('cursor',query)
        self.assertTrue(self.page.locator('#task-next').is_enabled())
        for edit in (lambda: self.page.locator('#task-batch').fill(MID), lambda: self.page.locator('#task-scope').select_option('pool')):
            edit()
            self.assertTrue(self.page.locator('#task-next').is_disabled(),'edited task filter must not pair with the old cursor')
            count = len(self.calls('GET','/api/onboarding/tasks'))
            self.force_click('#task-next')
            self.assert_quiet('GET','/api/onboarding/tasks',count)
            self.act('#task-filter-form button[type=submit]','GET','/api/onboarding/tasks')
            self.assertTrue(self.page.locator('#task-next').is_enabled())

    def test_import_skip_and_clear_secret_on_success(self):
        self.start(); self.import_preview()
        body = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='导入已受理')
        self.assertEqual(body['duplicate_action'], 'skip')
        self.assertEqual(body['expected_versions'], {})
        self.assertEqual(self.page.locator('#import-text').input_value(), '')
        self.assertIn('UNKNOWN', self.page.locator('#message').inner_text())

    def test_declined_import_confirmation_clears_secret_without_request(self):
        self.start(); self.import_preview()
        self.dialog_decide = lambda text: False
        self.page.locator('#confirm-import').click()
        self.assert_quiet('POST','/api/onboarding/mailboxes/import',0)
        self.assertEqual(self.page.locator('#import-text').input_value(), '')
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())

    def test_import_issues_disable_confirm_cancel_clears(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import-preview')] = (200, dict(items=[],issues=[dict(line=1,code='INVALID_RECORD')],accepted_count=0,duplicate_count=0,conflict_count=0,preview_digest=None))
        self.start(); self.import_preview()
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())
        self.page.locator('#cancel-import').click()
        self.assertEqual(self.page.locator('#import-text').input_value(), '')

    def test_401_clears_sensitive_inputs_and_disables_actions(self):
        self.start(); self.import_preview()
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (401, dict(code='UNAUTHENTICATED'))
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='会话已失效')
        self.assertEqual(self.page.locator('#import-text').input_value(), '')
        self.assertTrue(self.page.locator('#login-link').is_visible())
        for selector in ('#preview-import','#preflight','#save-config','[data-save-group]'):
            self.assertTrue(self.page.locator(selector).is_disabled(), selector)

    def test_cas_patch_only_group_or_disabled(self):
        self.start()
        self.page.locator('[data-mailbox-group]').fill('fixture:group-alt')
        body = self.act('[data-save-group]','PATCH','/api/onboarding/mailboxes/' + MID,text='修改已受理')
        self.assertEqual(body['expected_version'], 1)
        self.assertEqual(body['changes'], {'group_ref':'fixture:group-alt'})
        body = self.act('button[data-mailbox-write]:not([data-save-group])','PATCH','/api/onboarding/mailboxes/' + MID,text='修改已受理')
        self.assertEqual(body['expected_version'], 1)
        self.assertEqual(body['changes'], {'disabled':True})

    def test_config_eight_fields_and_revision(self):
        self.start()
        body = self.act('#save-config','PUT','/api/onboarding/config',text='新配置版本已受理')
        self.assertEqual(body['expected_revision'], REV)
        self.assertEqual(body['fields'], FIELDS)

    def test_first_config_is_saved_with_null_revision(self):
        self.overrides[('GET','/api/onboarding/config')] = (200, None)
        self.start()
        self.assertIn('尚无全局配置', self.page.locator('#config-state').inner_text())
        body = self.act('#save-config','PUT','/api/onboarding/config',text='新配置版本已受理')
        self.assertIsNone(body['expected_revision'])
        self.assertEqual(set(body['fields']), set(FIELDS))

    def test_preflight_is_observation_then_explicit_batch_create(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assert_quiet('POST','/api/onboarding/batches',0)
        self.assertIn('观察',self.page.locator('#preflight-result').inner_text())
        body = self.act('#confirm-batch','POST','/api/onboarding/batches',text='批次已受理')
        self.assertEqual(body['expected_config_revision'], REV)
        self.assertEqual(body['requested_count'], 1)
        self.assertEqual(body['mailbox_ids'], [])
        self.assertIn('非注册成功',self.page.locator('#batch-result').inner_text())
        self.assertNotIn('可进入确认',self.page.locator('#preflight-result').inner_text())
        self.assertIn('重新预检',self.page.locator('#preflight-result').inner_text())

    def test_batch_accepted_after_pagehide_issues_no_reads(self):
        self.start(); held = []
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.page.route('**/api/onboarding/batches', lambda route: held.append(route))
        self.page.locator('#confirm-batch').click()
        self.until(lambda: held, 'batch POST was not issued')
        reads = {path: len(self.calls('GET', path)) for path in ('/api/onboarding/tasks','/api/onboarding/mailboxes')}
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=202,content_type='application/json',body=json.dumps(dict(batch_id=MID,task_ids=[TID],mailbox_ids=[MID],receipt_id=MID,config_id=MID,config_revision=REV,phase='SUCCEEDED')))
        self.settle('批次已受理')
        self.page.wait_for_timeout(300)
        self.assertEqual({path: len(self.calls('GET', path)) for path in reads}, reads)

    def test_insufficient_or_edit_invalidates_preflight(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertTrue(self.page.locator('#confirm-batch').is_enabled())
        self.page.locator('#requested-count').fill('2')
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled(),'editing N must invalidate the observation')
        self.overrides[('POST','/api/onboarding/preflight')] = (200,dict(selection='automatic',requested_count=2,eligible_count=1,config_revision=REV,can_create=False,reason_codes=['INSUFFICIENT_ELIGIBLE'],observation_only=True))
        body = self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertEqual(body['requested_count'],2)
        self.assertIn('INSUFFICIENT_ELIGIBLE',self.page.locator('#preflight-result').inner_text())
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled(),'insufficient observation cannot be confirmed')
        self.assertEqual(self.page.locator('#requested-count').input_value(),'2')
        self.force_click('#confirm-batch')
        self.assert_quiet('POST','/api/onboarding/batches',0)

    def test_command_202_followed_by_failed_get_still_accepted(self):
        self.start()
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (503, dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#pause-task','POST','/api/onboarding/tasks/' + TID + '/pause',text='命令已受理，状态刷新失败')
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)
        self.assertEqual(len(self.calls('GET','/api/onboarding/tasks/' + TID)),2)

    def test_accepted_mutation_followup_read_failure_is_never_reported_unconfirmed(self):
        flows = {
            'pause': (('[data-open-task]','GET','/api/onboarding/tasks/' + TID), '#pause-task', ('POST','/api/onboarding/tasks/' + TID + '/pause'), ('GET','/api/onboarding/tasks/' + TID), '命令已受理'),
            'config': (None, '#save-config', ('PUT','/api/onboarding/config'), ('GET','/api/onboarding/config'), '配置已受理'),
            'patch': (None, '[data-save-group]', ('PATCH','/api/onboarding/mailboxes/' + MID), ('GET','/api/onboarding/mailboxes'), '修改已受理'),
            'batch': (('#preflight','POST','/api/onboarding/preflight'), '#confirm-batch', ('POST','/api/onboarding/batches'), ('GET','/api/onboarding/tasks'), '批次已受理'),
            'import': ('import', '#confirm-import', ('POST','/api/onboarding/mailboxes/import'), ('GET','/api/onboarding/mailboxes'), '导入已受理'),
        }
        for flow, (prepare, trigger, mutation, followup, accepted) in flows.items():
            for kind, failure in FOLLOWUP_FAILURES.items():
                with self.subTest(flow=flow, failure=kind):
                    self.overrides.clear(); self.requests.clear()
                    self.start()
                    if prepare == 'import':
                        self.import_preview()
                    elif prepare:
                        self.act(*prepare)
                    self.overrides[followup] = failure
                    self.act(trigger, *mutation, text=accepted)
                    text = self.page.locator('#message').inner_text()
                    self.assertIn('不要重复提交', text)
                    self.assertNotIn('未确认', text, 'a committed mutation must not be reported as unconfirmed')
                    if flow == 'import':
                        self.assertIn('新增 1', text, 'accepted import counts must survive a failed refresh')
                    if kind == '401':
                        self.assertIn('会话已失效', text)
                        self.assertTrue(self.page.locator('#login-link').is_visible())
                        for selector in ('#save-config','#preflight','#preview-import'):
                            self.assertTrue(self.page.locator(selector).is_disabled(), selector + ' must lock after 401')
                        if flow == 'batch':
                            self.assertEqual(len(self.calls('GET','/api/onboarding/mailboxes')),1,'no further reads after 401')
                    self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
                    self.assert_quiet(*mutation, 1)

    def test_detail_refresh_keeps_task_list_row_in_step(self):
        self.start()
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200, {**TASK, 'status':'PAUSED', 'version':2})
        self.act('#pause-task','POST','/api/onboarding/tasks/' + TID + '/pause',text='命令已受理')
        self.assertIn('PAUSED · 版本 2', self.page.locator('#task-list').inner_text())
        self.assertNotIn('QUEUED', self.page.locator('#task-list').inner_text())

    def test_batch_acceptance_refreshes_mailbox_observation(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        count = len(self.calls('GET','/api/onboarding/mailboxes'))
        reserved = {**MAILBOX, 'occupied':True, 'version':2, 'platforms':[dict(platform='google',identity_status='UNKNOWN',usage_status='RESERVED')]}
        self.overrides[('GET','/api/onboarding/mailboxes')] = (200, dict(items=[reserved], next_cursor=None))
        self.act('#confirm-batch','POST','/api/onboarding/batches',text='批次已受理')
        self.assertEqual(len(self.calls('GET','/api/onboarding/mailboxes')), count + 1)
        self.assertIn('RESERVED', self.page.locator('#mailbox-list').inner_text())
        self.assertIn('观察到逻辑占用', self.page.locator('#mailbox-list').inner_text())

    def test_failed_reads_do_not_leave_loading_or_previous_results(self):
        self.start()
        self.overrides[('GET','/api/onboarding/config')] = (503, dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#load-config','GET','/api/onboarding/config')
        self.assertNotIn('正在读取', self.page.locator('#config-state').inner_text())
        self.assertIn('读取失败', self.page.locator('#config-state').inner_text())
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertIn('可进入确认', self.page.locator('#preflight-result').inner_text())
        self.overrides[('POST','/api/onboarding/preflight')] = (503, dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertNotIn('可进入确认', self.page.locator('#preflight-result').inner_text())
        self.assertIn('预检未完成', self.page.locator('#preflight-result').inner_text())
        self.assertNotIn('正在', self.page.locator('#preflight-result').inner_text())
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled())

    def test_first_read_401_does_not_leave_loading_text(self):
        self.overrides[('GET','/api/onboarding/mailboxes')] = (401,dict(code='UNAUTHENTICATED'))
        self.start()
        self.assertTrue(self.page.locator('#login-link').is_visible())
        self.assertNotIn('正在', self.page.locator('#task-list-state').inner_text())
        self.assertIn('会话已失效', self.page.locator('#config-state').inner_text())
        self.assertFalse(self.calls('GET','/api/onboarding/tasks'))
        self.assertFalse(self.calls('GET','/api/onboarding/config'))

    def test_session_failure_does_not_leave_loading_text(self):
        self.overrides[('GET','/api/auth/session')] = (503, dict(code='DEPENDENCY_UNAVAILABLE'))
        self.start()
        self.assertEqual(self.page.locator('#main').get_attribute('aria-busy'), 'false')
        for selector in ('#operator', '#mailbox-state', '#task-list-state'):
            self.assertNotIn('正在', self.page.locator(selector).inner_text(), selector)
        self.assertIn('未能核验会话', self.page.locator('#config-state').inner_text())

    def test_401_after_positive_preflight_clears_observation(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertIn('可进入确认', self.page.locator('#preflight-result').inner_text())
        # Opening a task does not invalidate the preflight itself, so only expire() can clear it.
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (401,dict(code='UNAUTHENTICATED'))
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID,text='会话已失效')
        self.assertNotIn('可进入确认', self.page.locator('#preflight-result').inner_text())

    def test_followup_failure_wording_distinguishes_causes(self):
        cases = [('403', (403,dict(code='FORBIDDEN')), '没有读取最新状态的权限'),
                 ('invalid', (200,dict(id='not-a-task')), '响应无效'),
                 ('abort', 'abort', '连接中断'),
                 ('correlation', (503,dict(code='DEPENDENCY_UNAVAILABLE',correlation_id=CID)), '参考号：' + CID)]
        for name, failure, expected in cases:
            with self.subTest(failure=name):
                self.overrides.clear(); self.requests.clear()
                self.start()
                self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
                self.overrides[('GET','/api/onboarding/tasks/' + TID)] = failure
                self.act('#pause-task','POST','/api/onboarding/tasks/' + TID + '/pause',text='命令已受理')
                text = self.page.locator('#message').inner_text()
                self.assertIn(expected, text)
                self.assertIn('不要重复提交', text)
                self.assertNotIn('未确认', text)

    def test_read_failures_are_not_reported_as_unconfirmed(self):
        reads = [('json-503',(503,dict(code='DEPENDENCY_UNAVAILABLE'))),('html-502',GATEWAY),('abort','abort'),
                 ('commit-unknown',(503,dict(code='COMMIT_UNKNOWN'))),('invalid',(200,dict(items='broken',next_cursor=None)))]
        for name, failure in reads:
            with self.subTest(failure=name):
                self.overrides.clear(); self.requests.clear()
                self.start()
                self.overrides[('GET','/api/onboarding/mailboxes')] = failure
                self.act('#filter-submit','GET','/api/onboarding/mailboxes')
                text = self.page.locator('#message').inner_text()
                self.assertNotIn('未确认', text, 'a failed read has no outcome to confirm')
                self.assertNotIn('核对', text)
                self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.overrides.clear(); self.start()
        self.overrides[('PUT','/api/onboarding/config')] = (503,dict(code='DEPENDENCY_UNAVAILABLE',correlation_id=CID))
        self.act('#save-config','PUT','/api/onboarding/config')
        self.assertIn('未确认', self.page.locator('#message').inner_text(), 'a failed write stays unconfirmed')
        self.assertIn('参考号：' + CID, self.page.locator('#message').inner_text())

    def test_per_row_controls_have_distinct_accessible_names(self):
        self.start()
        email = MAILBOX['email']
        for selector in ('[data-mailbox-select]','[data-mailbox-group]','[data-save-group]','button[data-mailbox-write]:not([data-save-group])'):
            self.assertIn(email, self.page.locator(selector).get_attribute('aria-label') or '', selector)
        self.assertIn(TID, self.page.locator('[data-open-task]').get_attribute('aria-label') or '')

    def test_pagehide_during_session_check_stops_initial_reads(self):
        held = []
        self.page.route('**/api/auth/session', lambda route: held.append(route))
        self.page.goto('https://pool.invalid/onboarding/pools')
        self.until(lambda: held, 'session check was not issued')
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=200,content_type='application/json',body=json.dumps(dict(csrf_token='fixture-csrf',permissions=PERMS,display_name='合成操作者')))
        self.settle()
        self.page.wait_for_timeout(300)
        for path in ('/api/onboarding/mailboxes','/api/onboarding/tasks','/api/onboarding/config'):
            self.assertFalse(self.calls('GET',path), path + ' must not be read after pagehide')
        self.assertTrue(self.page.locator('#preflight').is_disabled())

    def test_pagehide_immediately_disables_idle_controls(self):
        self.start()
        self.assertTrue(self.page.locator('#preflight').is_enabled())
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        for selector in ('#preflight','#save-config','#preview-import','[data-save-group]','#filter-submit'):
            self.assertTrue(self.page.locator(selector).is_disabled(), selector)

    def test_hung_mutation_times_out_into_explicit_reconcile(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.page.clock.install()
        held = []
        self.page.route('**/api/onboarding/batches', lambda route: held.append(route))
        self.page.locator('#confirm-batch').click()
        self.until(lambda: held, 'batch POST was not issued')
        self.page.clock.run_for(31000)
        # Fake timers: poll with evaluate instead of wait_for_function.
        self.until(lambda: self.page.evaluate("document.querySelector('#main').getAttribute('aria-busy') !== 'true'"
                                              " && document.querySelector('#message').textContent.includes('请求超时')"), 'timeout was not reported')
        self.assertIn('未确认', self.page.locator('#message').inner_text())
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.assertTrue(self.page.locator('#preflight').is_disabled())

    def test_unknown_batch_explicit_same_key_and_body_no_auto_retry(self):
        self.overrides[('POST','/api/onboarding/batches')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        first = self.act('#confirm-batch','POST','/api/onboarding/batches',text='COMMIT_UNKNOWN')
        self.assert_quiet('POST','/api/onboarding/batches',1)
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.assertNotIn('可进入确认',self.page.locator('#preflight-result').inner_text())
        self.assertIn('创建批次',self.page.locator('#reconcile-note').inner_text())
        self.assertTrue(self.page.locator('#import-text').is_disabled(),'no secret entry while another write is pending')
        for selector in ('#preflight','#save-config','[data-save-group]','#selection'):
            self.assertTrue(self.page.locator(selector).is_disabled(), selector + ' must freeze while the outcome is unknown')
        self.force_click('#preflight')
        self.assert_quiet('POST','/api/onboarding/preflight',1)
        replay = self.act('#reconcile-request','POST','/api/onboarding/batches',text='COMMIT_UNKNOWN')
        self.assertEqual(replay,first)

    def unknown_batch(self):
        """Leave one batch POST with an unknown outcome; returns its original body."""
        self.overrides[('POST','/api/onboarding/batches')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        first = self.act('#confirm-batch','POST','/api/onboarding/batches',text='COMMIT_UNKNOWN')
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        return first

    def test_successful_reconcile_clears_request_memory(self):
        self.unknown_batch()
        del self.overrides[('POST','/api/onboarding/batches')]
        self.act('#reconcile-request','POST','/api/onboarding/batches',text='批次已受理')
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assertEqual(self.page.locator('#reconcile-note').text_content(),'')
        self.assertTrue(self.page.locator('#preflight').is_enabled())

    def test_rejected_reconcile_keeps_pending(self):
        for name, rejection in REPLAY_REJECTIONS:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                first = self.unknown_batch()
                self.overrides[('POST','/api/onboarding/batches')] = rejection
                replay = self.act('#reconcile-request','POST','/api/onboarding/batches')
                self.assertEqual(replay,first,'reconcile must replay the original key and body')
                self.assertFalse(any('解除' in text for text in self.dialogs),'no rejection may offer a release')
                self.assertIn('仍待核对',self.page.locator('#message').inner_text(),'the rejection must not read as a fresh write')
                self.assertTrue(self.page.locator('#reconcile-card').is_visible())
                self.assertIn(first['request_key'],self.page.locator('#reconcile-note').inner_text())
                self.assertTrue(self.page.locator('#preflight').is_disabled())
                self.assertTrue(self.page.locator('#reconcile-request').is_enabled())

    def test_import_reentry_rejection_keeps_pending(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start(); self.import_preview()
        first = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (409,dict(code='VERSION_CONFLICT'))
        self.import_preview()
        replay = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='冲突')
        self.assertEqual(replay,first)
        self.assertFalse(any('解除' in text for text in self.dialogs))
        self.assertIn('仍待核对',self.page.locator('#message').inner_text())
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.import_preview()
        again = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
        self.assertEqual(again['request_key'],first['request_key'],'still pending: only the original key may be used')

    def test_read_only_role_and_untrusted_text(self):
        self.permissions = ['onboarding:read']
        self.overrides[('GET','/api/onboarding/mailboxes')] = (200,dict(items=[{**MAILBOX,'email':'<img src=x onerror=alert(1)>'}],next_cursor=None))
        self.start()
        self.assertEqual(self.page.locator('#mailbox-list img').count(),0)
        self.assertTrue(self.page.locator('#preview-import').is_disabled())
        self.assertTrue(self.page.locator('#preflight').is_disabled())
        self.assertFalse(self.calls('GET','/api/onboarding/config'))
        self.assertIn('需要 config:manage', self.page.locator('#config-state').inner_text())
        self.assertNotIn('会话已失效', self.page.locator('#config-state').inner_text())

    def test_fixture_detail_preserves_legacy_snapshot_contract(self):
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200, {key:value for key,value in TASK.items() if key in ('id','status','version','reason_code','current_step','generation','cancel_requested','synthetic')})
        self.start()
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
        self.assertTrue(self.page.locator('#pause-task').is_enabled())
        self.assertIn('fixture', self.page.locator('#task-facts').inner_text())

    def test_command_409_requires_fresh_detail_before_another_command(self):
        self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = (409,dict(code='VERSION_CONFLICT'))
        self.start()
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
        self.act('#pause-task','POST','/api/onboarding/tasks/' + TID + '/pause',text='冲突')
        for action in ('pause','cancel','recheck'):
            self.assertTrue(self.page.locator('#' + action + '-task').is_disabled())
        self.assertTrue(self.page.locator('#refresh-task').is_enabled())
        self.force_click('#pause-task')
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)
        self.act('#refresh-task','GET','/api/onboarding/tasks/' + TID)
        self.assertTrue(self.page.locator('#pause-task').is_enabled())

    def test_config_409_requires_fresh_revision_before_save(self):
        self.overrides[('PUT','/api/onboarding/config')] = (409,dict(code='VERSION_CONFLICT'))
        self.start()
        self.act('#save-config','PUT','/api/onboarding/config',text='冲突')
        self.assertTrue(self.page.locator('#save-config').is_disabled())
        self.assertTrue(self.page.locator('#load-config').is_enabled())
        self.force_click('#save-config')
        self.assert_quiet('PUT','/api/onboarding/config',1)
        self.act('#load-config','GET','/api/onboarding/config')
        self.assertTrue(self.page.locator('#save-config').is_enabled())

    def test_patch_409_requires_fresh_rows_before_write(self):
        path = '/api/onboarding/mailboxes/' + MID
        self.overrides[('PATCH',path)] = (409,dict(code='VERSION_CONFLICT'))
        self.start()
        self.act('[data-save-group]','PATCH',path,text='冲突')
        self.assertTrue(self.page.locator('[data-save-group]').is_disabled())
        self.assertTrue(self.page.locator('#filter-submit').is_enabled())
        self.force_click('[data-save-group]')
        self.assert_quiet('PATCH',path,1)
        self.act('#filter-submit','GET','/api/onboarding/mailboxes')
        self.assertTrue(self.page.locator('[data-save-group]').is_enabled())

    def test_409_keeps_selection_and_n_requires_new_preflight(self):
        self.overrides[('POST','/api/onboarding/batches')] = (409,dict(code='VERSION_CONFLICT'))
        self.start()
        self.page.locator('[data-mailbox-select]').check()
        self.page.locator('#selection').select_option('specified')
        self.act('#preflight','POST','/api/onboarding/preflight')
        body = self.act('#confirm-batch','POST','/api/onboarding/batches',text='冲突')
        self.assertEqual(body['mailbox_ids'],[MID])
        self.assertTrue(self.page.locator('[data-mailbox-select]').is_checked())
        self.assertEqual(self.page.locator('#selection').input_value(),'specified')
        self.assertEqual(self.page.locator('#requested-count').input_value(),'1')
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled())
        self.assertTrue(self.page.locator('#preflight').is_enabled(),'409 is not pending; a fresh preflight must be possible')
        self.assert_quiet('POST','/api/onboarding/batches',1)
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertTrue(self.page.locator('#confirm-batch').is_enabled())

    def test_import_unknown_clears_secret_and_reuses_key_after_reentry(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start(); self.import_preview()
        first = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())
        self.page.locator('#reconcile-request').click()
        self.assert_quiet('POST','/api/onboarding/mailboxes/import',1)
        self.import_preview()
        second = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.assertEqual(second,first)
        self.assertEqual(len(self.calls('POST','/api/onboarding/mailboxes/import')),2)
        self.assertEqual(self.page.locator('#import-text').input_value(),'')

    def test_import_unknown_different_digest_never_gets_new_key(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start(); self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.overrides[('POST','/api/onboarding/mailboxes/import-preview')] = (200,dict(items=[],issues=[],accepted_count=1,duplicate_count=0,conflict_count=0,preview_digest='b'*64))
        self.import_preview()
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.force_click('#confirm-import')
        self.assert_quiet('POST','/api/onboarding/mailboxes/import',1)

    def test_import_ack_clears_secret_before_dependent_list_refresh(self):
        self.start(); self.import_preview()
        held = []
        self.page.route('**/api/onboarding/mailboxes?*', lambda route: held.append(route))
        self.page.locator('#confirm-import').click()
        self.until(lambda: held, 'dependent list refresh was not requested')
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertEqual(len(held),1)
        held[0].fulfill(status=200,content_type='application/json',body=json.dumps(dict(items=[MAILBOX],next_cursor=None)))
        self.settle('导入已受理')

    def test_import_failure_and_tab_exit_clear_secret(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (422,dict(code='INVALID_INPUT'))
        self.start(); self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='输入不符合要求')
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.import_preview(); self.page.locator('#tab-list').click()
        self.assertEqual(self.page.locator('#import-text').input_value(),'')
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())

    def test_dependency_and_permission_errors_are_not_empty_success(self):
        self.overrides[('GET','/api/onboarding/mailboxes')] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.overrides[('GET','/api/onboarding/tasks')] = (403,dict(code='FORBIDDEN'))
        self.start()
        self.assertIn('读取失败',self.page.locator('#mailbox-state').inner_text())
        self.assertIn('读取失败',self.page.locator('#task-list-state').inner_text())
        self.assertTrue(self.page.locator('#mailbox-next').is_disabled())
        self.assertTrue(self.page.locator('#task-next').is_disabled())

    def test_corrupt_mailbox_page_never_displays_partial_rows(self):
        self.overrides[('GET','/api/onboarding/mailboxes')] = (200,dict(items=[MAILBOX,{'id':'broken'}],next_cursor=None))
        self.start()
        self.assertEqual(self.page.locator('#mailbox-list .pool-item').count(),0)
        self.assertIn('部分坏行',self.page.locator('#mailbox-state').inner_text())

    def test_empty_state_is_explicit(self):
        self.overrides[('GET','/api/onboarding/mailboxes')] = (200,dict(items=[],next_cursor=None))
        self.overrides[('GET','/api/onboarding/tasks')] = (200,dict(items=[],next_cursor=None))
        self.start()
        self.assertIn('没有邮箱',self.page.locator('#mailbox-state').inner_text())
        self.assertIn('没有任务',self.page.locator('#task-list-state').inner_text())

    def test_double_click_issues_only_one_batch_post(self):
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertTrue(self.page.locator('#confirm-batch').is_enabled())
        # The second click bypasses the disabled attribute so the handler guards themselves are tested.
        self.act(lambda: self.page.locator('#confirm-batch').evaluate('(button) => { button.click(); button.disabled = false; button.click(); }'),
                 'POST','/api/onboarding/batches',text='批次已受理')
        self.assert_quiet('POST','/api/onboarding/batches',1)

    def test_sensitive_form_values_never_enter_url_or_storage(self):
        self.start(); self.import_preview()
        self.assertEqual(self.page.evaluate('localStorage.length + sessionStorage.length'),0)
        self.assertFalse(any('fixture-ui-password' in entry[3] or 'fixture-csrf' in entry[3] for entry in self.requests))
        self.assertEqual(self.page.url,'https://pool.invalid/onboarding/pools')

    def legacy_unknown(self):
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = (503,dict(code='COMMIT_UNKNOWN'))
        first = self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='事务提交结果未知')
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)
        return first

    def test_legacy_unknown_manual_read_cannot_unlock_new_key_command(self):
        first = self.legacy_unknown()
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200,{**TASK,'version':7})
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        for action in ('pause','cancel','recheck'):
            self.assertTrue(self.page.locator('#' + action).is_disabled(),'UNKNOWN must freeze ordinary command buttons after a read')
            self.force_click('#' + action)
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)
        self.assertFalse(self.calls('POST','/api/onboarding/tasks/' + TID + '/cancel'))
        self.assertFalse(self.calls('POST','/api/onboarding/tasks/' + TID + '/recheck'))
        self.assertTrue(self.page.locator('#reconcile-command').is_enabled())
        self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = (202,dict(accepted=True,synthetic=True,execution_scope='pool',receipt_id=MID,phase='SUCCEEDED',task=None,snapshot_pending=True))
        replay = self.act('#reconcile-command','POST','/api/onboarding/tasks/' + TID + '/pause',text='已受理')
        self.assertEqual(replay,first)
        self.assertEqual(replay['expected_version'],1)
        self.assertEqual(len(self.calls('POST','/api/onboarding/tasks/' + TID + '/pause')),2)
        self.assertTrue(self.page.locator('#pause').is_enabled())
        self.assertTrue(self.page.locator('#reconcile-command').is_hidden())

    def test_legacy_unknown_401_erases_key_and_disables_reconcile(self):
        self.legacy_unknown()
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (401,dict(code='UNAUTHENTICATED'))
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='会话已失效')
        self.assertEqual(self.page.locator('#pending').inner_text(),'')
        self.assertTrue(self.page.locator('#query').is_disabled())
        self.assertTrue(self.page.locator('#pause').is_disabled())
        self.assertEqual(self.page.locator('#reconcile-command').count(),1)
        self.assertTrue(self.page.locator('#reconcile-command').is_disabled())
        self.assertTrue(self.page.locator('#login-link').is_visible())
        self.force_click('#reconcile-command')
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)

    def test_legacy_unknown_pagehide_erases_memory_and_freezes_actions(self):
        self.legacy_unknown()
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        self.assertTrue(self.page.locator('#query').is_disabled())
        self.assertEqual(self.page.locator('#pending').inner_text(),'')
        self.assertEqual(self.page.locator('#reconcile-command').count(),1)
        self.assertTrue(self.page.locator('#reconcile-command').is_disabled())
        reads = len(self.calls('GET','/api/onboarding/tasks/' + TID))
        self.page.locator('#task-form').evaluate("form => form.dispatchEvent(new Event('submit',{cancelable:true}))")
        self.force_click('#reconcile-command')
        self.assert_quiet('GET','/api/onboarding/tasks/' + TID,reads)
        self.assertEqual(len(self.calls('POST','/api/onboarding/tasks/' + TID + '/pause')),1)

    def test_legacy_rejected_reconcile_keeps_pending(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        for name, rejection in REPLAY_REJECTIONS:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                first = self.legacy_unknown()
                self.overrides[('POST',path)] = rejection
                replay = self.act('#reconcile-command','POST',path)
                self.assertEqual(replay,first)
                self.assertFalse(any('解除' in text for text in self.dialogs),'no rejection may offer a release')
                self.assertIn('仍待核对',self.page.locator('#message').inner_text())
                self.assertTrue(self.page.locator('#reconcile-command').is_visible())
                self.assertIn(first['request_key'],self.page.locator('#pending').inner_text())
                self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
                self.assertTrue(self.page.locator('#pause').is_disabled())

    def test_legacy_fixture_command_snapshot_keeps_original_success_path(self):
        fixture = {key:value for key,value in TASK.items() if key in ('id','status','version','reason_code','current_step','generation','cancel_requested','synthetic')}
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200,fixture)
        self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = (202,dict(accepted=True,synthetic=True,receipt_id=MID,task={**fixture,'version':2,'status':'PAUSED'}))
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='已受理')
        self.assert_quiet('GET','/api/onboarding/tasks/' + TID,1)
        self.assertIn('已暂停',self.page.locator('#task-facts').inner_text())
        self.assertTrue(self.page.locator('#pause').is_enabled())

    def test_batch_mutation_5xx_keeps_original_request_for_explicit_reconcile(self):
        for name, failure in MUTATION_FAILURES:
            with self.subTest(failure=name):
                self.requests.clear()
                self.overrides[('POST','/api/onboarding/batches')] = failure
                self.start()
                self.act('#preflight','POST','/api/onboarding/preflight')
                first = self.act('#confirm-batch','POST','/api/onboarding/batches')
                self.assertTrue(self.page.locator('#reconcile-request').is_enabled(),'mutation 5xx cannot prove rollback')
                self.assertTrue(self.page.locator('#preflight').is_disabled())
                self.assert_quiet('POST','/api/onboarding/batches',1)
                replay = self.act('#reconcile-request','POST','/api/onboarding/batches')
                self.assertEqual(replay,first)
                self.assertEqual(len(self.calls('POST','/api/onboarding/batches')),2)

    def test_import_mutation_5xx_clears_secret_then_reuses_key_after_reentry(self):
        for name, failure in MUTATION_FAILURES[1:3] + MUTATION_FAILURES[4:]:
            with self.subTest(failure=name):
                self.requests.clear()
                self.overrides[('POST','/api/onboarding/mailboxes/import')] = failure
                self.start(); self.import_preview()
                first = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
                self.assertEqual(self.page.locator('#import-text').input_value(),'','import text must be cleared')
                self.assertTrue(self.page.locator('#reconcile-request').is_enabled(),'import 5xx must retain only key/digest/group')
                self.page.locator('#reconcile-request').click()
                self.assert_quiet('POST','/api/onboarding/mailboxes/import',1)
                self.import_preview()
                second = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
                self.assertEqual(second,first)
                self.assertEqual(len(self.calls('POST','/api/onboarding/mailboxes/import')),2)

    def test_legacy_mutation_5xx_read_cannot_unlock_new_command(self):
        for name, failure in MUTATION_FAILURES:
            with self.subTest(failure=name):
                self.requests.clear()
                self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = failure
                self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
                self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
                first = self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause')
                self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
                self.assertTrue(self.page.locator('#pause').is_disabled(),'legacy mutation 5xx must freeze ordinary commands')
                self.force_click('#pause')
                self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)
                self.assertTrue(self.page.locator('#reconcile-command').is_enabled())
                replay = self.act('#reconcile-command','POST','/api/onboarding/tasks/' + TID + '/pause')
                self.assertEqual(replay,first)
                self.assertEqual(len(self.calls('POST','/api/onboarding/tasks/' + TID + '/pause')),2)

    def test_patch_mutation_500_freezes_write_and_preserves_body(self):
        path = '/api/onboarding/mailboxes/' + MID
        self.overrides[('PATCH',path)] = (500,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.start()
        first = self.act('[data-save-group]','PATCH',path)
        self.assertTrue(self.page.locator('[data-save-group]').is_disabled())
        self.assertTrue(self.page.locator('#reconcile-request').is_enabled())
        self.force_click('[data-save-group]')
        self.assert_quiet('PATCH',path,1)
        replay = self.act('#reconcile-request','PATCH',path)
        self.assertEqual(replay,first)

    def test_read_5xx_and_accepted_batch_followup_5xx_never_become_pending(self):
        self.overrides[('GET','/api/onboarding/mailboxes')] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.overrides[('GET','/api/onboarding/tasks')] = (502,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.start()
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assertTrue(self.page.locator('#preflight').is_enabled())
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.act('#confirm-batch','POST','/api/onboarding/batches',text='批次已受理')
        text = self.page.locator('#message').inner_text()
        self.assertIn('任务列表',text)
        self.assertIn('邮箱列表',text)
        self.assertIn('刷新失败',text)
        self.assertNotIn('未确认',text)
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assertTrue(self.page.locator('#preflight').is_enabled())
        self.assert_quiet('POST','/api/onboarding/batches',1)

    def test_accepted_import_followup_5xx_does_not_preserve_secret_or_pending(self):
        self.start(); self.import_preview()
        self.overrides[('GET','/api/onboarding/mailboxes')] = (504,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='列表刷新失败')
        self.assertIn('导入已受理',self.page.locator('#message').inner_text())
        self.assertNotIn('未确认',self.page.locator('#message').inner_text())
        self.assertEqual(self.page.locator('#import-text').input_value(),'','import text must be cleared')
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assert_quiet('POST','/api/onboarding/mailboxes/import',1)

    def test_late_mutation_5xx_after_pagehide_cannot_restore_pending(self):
        self.start(); held = []
        self.page.route('**/api/onboarding/mailboxes/' + MID, lambda route: held.append(route))
        self.page.locator('[data-save-group]').click()
        self.until(lambda: held, 'PATCH was not issued')
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=503,content_type='application/json',body=json.dumps(dict(code='DEPENDENCY_UNAVAILABLE')))
        self.settle('依赖暂不可用')
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden(),'late response must not recreate cleared pending memory')
        self.force_click('#reconcile-request')
        self.page.wait_for_timeout(300)
        self.assertEqual(len(held),1)

    def test_late_preflight_success_after_pagehide_cannot_enable_actions(self):
        self.start(); held = []
        self.page.route('**/api/onboarding/preflight', lambda route: held.append(route))
        self.page.locator('#preflight').click()
        self.until(lambda: held, 'preflight was not issued')
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=200,content_type='application/json',body=json.dumps(dict(selection='automatic',requested_count=1,eligible_count=1,config_revision=REV,can_create=True,reason_codes=[],observation_only=True)))
        self.settle()
        self.assertNotIn('可进入确认', self.page.locator('#preflight-result').inner_text(), 'a late observation must not outlive the session')
        for selector in ('#preflight','#confirm-batch','#pause-task'):
            self.assertTrue(self.page.locator(selector).is_disabled(), selector)
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.force_click('#confirm-batch')
        self.assert_quiet('POST','/api/onboarding/batches',0)

    def test_legacy_accepted_command_failed_refresh_remains_accepted(self):
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='命令已受理，状态刷新失败')
        self.assertNotIn('未确认',self.page.locator('#message').inner_text())
        self.assertTrue(self.page.locator('#reconcile-command').is_hidden())
        self.assert_quiet('POST','/api/onboarding/tasks/' + TID + '/pause',1)

    def test_legacy_init_stays_busy_until_diagnostics_arrive(self):
        held = []
        self.page.route('**/api/onboarding/diagnostics', lambda route: held.append(route))
        self.page.goto('https://pool.invalid/onboarding')
        self.until(lambda: held, 'diagnostics were not requested')
        self.assertEqual(self.page.locator('#main').get_attribute('aria-busy'),'true')
        held[0].fulfill(status=200,content_type='application/json',body=json.dumps(dict(ready=True,schema_version=2)))
        self.settle()
        self.assertEqual(self.page.locator('#main').get_attribute('aria-busy'),'false')
        self.assertTrue(self.page.locator('#query').is_enabled())

    def test_failed_logout_is_reported_as_unconfirmed(self):
        self.start()
        self.overrides[('POST','/api/auth/logout')] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#logout','POST','/api/auth/logout')
        text = self.page.locator('#message').inner_text()
        self.assertIn('未确认', text, 'logout is a write')
        self.assertNotIn('未能读取', text)

    def test_legacy_accepted_command_refresh_401_reports_expired_session(self):
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (401,dict(code='UNAUTHENTICATED'))
        self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='会话已失效')
        text = self.page.locator('#message').inner_text()
        self.assertIn('命令已受理', text)
        self.assertNotIn('请手动查询', text, 'querying is unavailable once the session expired')
        self.assertTrue(self.page.locator('#login-link').is_visible())

    def test_legacy_rejected_fresh_command_leaves_no_pending_display(self):
        self.overrides[('POST','/api/onboarding/tasks/' + TID + '/pause')] = (409,dict(code='VERSION_CONFLICT'))
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='冲突')
        self.assertEqual(self.page.locator('#pending').text_content(),'')
        self.assertTrue(self.page.locator('#reconcile-command').is_hidden())

    def test_legacy_late_config_read_after_pagehide_is_discarded(self):
        self.start('/onboarding'); held = []
        self.page.route('**/api/env', lambda route: held.append(route))
        self.page.locator('#load-config').click()
        self.until(lambda: held, 'config read was not issued')
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=200,content_type='application/json',body=json.dumps(dict(groups=[dict(group='代理',items=[dict(key='PROXY_URL',label='代理地址',value='http://fixture-proxy',secret=False,configured=True)])])))
        self.settle()
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.locator('#config-fields input').count(),0,'expired page must not be refilled')

    def test_responsive_no_document_overflow(self):
        self.start()
        for width in (390,768,1024,1440):
            self.page.set_viewport_size(dict(width=width,height=1000))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'overflow at {width}')

    def test_legacy_task_null_response_not_reported_unsubmitted(self):
        self.start('/onboarding')
        self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.act('#pause','POST','/api/onboarding/tasks/' + TID + '/pause',text='已受理')
        self.assertNotIn('未确认', self.page.locator('#message').inner_text())

    def test_server_proof_releases_pending_after_confirmation(self):
        first = self.unknown_batch()
        self.overrides[('POST','/api/onboarding/batches')] = PROVED
        replay = self.act('#reconcile-request','POST','/api/onboarding/batches',text='已解除')
        self.assertEqual(replay,first)
        self.assertTrue(any('已证明' in text and '解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        self.assertEqual(self.page.locator('#reconcile-note').text_content(),'')
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled())
        self.assertTrue(self.page.locator('[data-save-group]').is_disabled(),'mailbox rows must be refreshed first')
        del self.overrides[('POST','/api/onboarding/batches')]
        self.act('#preflight','POST','/api/onboarding/preflight')
        again = self.act('#confirm-batch','POST','/api/onboarding/batches',text='批次已受理')
        self.assertNotEqual(again['request_key'],first['request_key'])

    def test_only_boolean_proof_on_version_conflict_releases(self):
        for name, rejection in NOT_PROOFS:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.unknown_batch()
                self.overrides[('POST','/api/onboarding/batches')] = rejection
                self.act('#reconcile-request','POST','/api/onboarding/batches')
                self.assertFalse(any('解除' in text for text in self.dialogs))
                self.assertTrue(self.page.locator('#reconcile-card').is_visible())

    def test_declined_server_proof_keeps_pending(self):
        self.unknown_batch()
        self.overrides[('POST','/api/onboarding/batches')] = PROVED
        self.dialog_decide = lambda text: '解除' not in text
        self.act('#reconcile-request','POST','/api/onboarding/batches')
        self.assertTrue(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.assertTrue(self.page.locator('#preflight').is_disabled())

    def test_first_submission_proof_is_an_ordinary_conflict(self):
        self.overrides[('POST','/api/onboarding/batches')] = PROVED
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.act('#confirm-batch','POST','/api/onboarding/batches',text='冲突')
        self.assertFalse(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())

    def test_proof_on_401_or_503_never_releases(self):
        for status, code in ((401,'UNAUTHENTICATED'),(503,'DEPENDENCY_UNAVAILABLE')):
            with self.subTest(status=status):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.unknown_batch()
                self.overrides[('POST','/api/onboarding/batches')] = (status,dict(code=code,not_committed=True))
                self.act('#reconcile-request','POST','/api/onboarding/batches')
                self.assertFalse(any('解除' in text for text in self.dialogs))
                if status == 401:
                    self.assertTrue(self.page.locator('#login-link').is_visible())
                else:
                    self.assertTrue(self.page.locator('#reconcile-card').is_visible())

    def test_import_server_proof_releases_pending(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start(); self.import_preview()
        first = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = PROVED
        self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='已解除')
        self.assertTrue(any('指纹不同的同名邮箱' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())
        del self.overrides[('POST','/api/onboarding/mailboxes/import')]
        self.import_preview()
        fresh = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='导入已受理')
        self.assertNotEqual(fresh['request_key'],first['request_key'])

    def test_legacy_server_proof_releases_pending(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        first = self.legacy_unknown()
        self.overrides[('POST',path)] = PROVED
        self.act('#reconcile-command','POST',path,text='已解除')
        self.assertTrue(any('已证明' in text and '解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-command').is_hidden())
        self.assertEqual(self.page.locator('#pending').text_content(),'')
        del self.overrides[('POST',path)]
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        fresh = self.act('#pause','POST',path,text='已受理')
        self.assertNotEqual(fresh['request_key'],first['request_key'])

    def test_legacy_only_boolean_proof_releases(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        for name, rejection in NOT_PROOFS:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.legacy_unknown()
                self.overrides[('POST',path)] = rejection
                self.act('#reconcile-command','POST',path)
                self.assertFalse(any('解除' in text for text in self.dialogs))
                self.assertTrue(self.page.locator('#reconcile-command').is_visible())

    def test_legacy_unsafe_version_never_becomes_expected_version(self):
        self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200,{**TASK,'version':2**53 + 2})
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID)
        self.assertTrue(self.page.locator('#pause').is_disabled())
        self.assertIn('安全范围', self.page.locator('#task-empty').inner_text())

    def unknown_import(self):
        """Leave one import with an unknown outcome and re-preview identical text; returns its body."""
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start(); self.import_preview()
        first = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.import_preview()
        return first

    def test_import_first_submission_proof_is_an_ordinary_conflict(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = PROVED
        self.start(); self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='冲突')
        self.assertFalse(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_hidden())

    def test_import_declined_server_proof_keeps_pending(self):
        first = self.unknown_import()
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = PROVED
        self.dialog_decide = lambda text: '解除' not in text
        replay = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='仍待核对')
        self.assertEqual(replay,first)
        self.assertTrue(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-card').is_visible())
        self.import_preview()
        again = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
        self.assertEqual(again['request_key'],first['request_key'],'still pending: only the original key may be used')

    def test_import_only_boolean_proof_on_version_conflict_releases(self):
        rejections = NOT_PROOFS + [('401',(401,dict(code='UNAUTHENTICATED',not_committed=True))),
                                   ('503',(503,dict(code='DEPENDENCY_UNAVAILABLE',not_committed=True)))]
        for name, rejection in rejections:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.unknown_import()
                self.overrides[('POST','/api/onboarding/mailboxes/import')] = rejection
                self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
                self.assertFalse(any('解除' in text for text in self.dialogs))
                if name == '401':
                    self.assertTrue(self.page.locator('#login-link').is_visible())
                else:
                    self.assertTrue(self.page.locator('#reconcile-card').is_visible())

    def test_legacy_first_command_proof_is_an_ordinary_conflict(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
        self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
        self.overrides[('POST',path)] = PROVED
        self.act('#pause','POST',path,text='冲突')
        self.assertFalse(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-command').is_hidden())
        self.assertEqual(self.page.locator('#pending').text_content(),'')

    def test_legacy_declined_server_proof_keeps_pending(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        first = self.legacy_unknown()
        self.overrides[('POST',path)] = PROVED
        self.dialog_decide = lambda text: '解除' not in text
        replay = self.act('#reconcile-command','POST',path,text='仍待核对')
        self.assertEqual(replay,first)
        self.assertTrue(any('解除' in text for text in self.dialogs))
        self.assertTrue(self.page.locator('#reconcile-command').is_visible())
        self.assertIn(first['request_key'],self.page.locator('#pending').inner_text())

    def test_legacy_proof_on_401_or_503_never_releases(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        for status, code in ((401,'UNAUTHENTICATED'),(503,'DEPENDENCY_UNAVAILABLE')):
            with self.subTest(status=status):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.legacy_unknown()
                self.overrides[('POST',path)] = (status,dict(code=code,not_committed=True))
                self.act('#reconcile-command','POST',path)
                self.assertFalse(any('解除' in text for text in self.dialogs))
                if status == 401:
                    self.assertTrue(self.page.locator('#login-link').is_visible())
                else:
                    self.assertTrue(self.page.locator('#reconcile-command').is_visible())

    def test_server_proof_release_invalidates_task_config_and_rows(self):
        self.overrides[('POST','/api/onboarding/batches')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start()
        self.act('[data-open-task]','GET','/api/onboarding/tasks/' + TID)
        self.assertTrue(self.page.locator('#pause-task').is_enabled())
        self.assertTrue(self.page.locator('#save-config').is_enabled())
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.act('#confirm-batch','POST','/api/onboarding/batches',text='COMMIT_UNKNOWN')
        self.overrides[('POST','/api/onboarding/batches')] = PROVED
        self.act('#reconcile-request','POST','/api/onboarding/batches',text='已解除')
        # The batch submission already consumed the preflight; see the import test for a live one.
        for selector in ('#pause-task','#save-config','[data-save-group]'):
            self.assertTrue(self.page.locator(selector).first.is_disabled(), selector + ' must need a fresh read after the release')
        self.assertTrue(self.page.locator('#preflight').is_enabled())

    def test_import_rejected_reconcile_keeps_pending(self):
        for name, rejection in REPLAY_REJECTIONS:
            with self.subTest(rejection=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                first = self.unknown_import()
                self.overrides[('POST','/api/onboarding/mailboxes/import')] = rejection
                replay = self.act('#confirm-import','POST','/api/onboarding/mailboxes/import')
                self.assertEqual(replay,first,'reconcile must replay the original key and body')
                self.assertFalse(any('解除' in text for text in self.dialogs),'no rejection may offer a release')
                self.assertIn('仍待核对',self.page.locator('#message').inner_text())
                self.assertTrue(self.page.locator('#reconcile-card').is_visible())
                self.assertIn(first['request_key'],self.page.locator('#reconcile-note').inner_text())

    def test_legacy_invalid_task_read_is_reported_as_invalid_not_success(self):
        for name, value in (('unsafe-version', {**TASK,'version':2**53 + 2}), ('null', None), ('no-id', {**TASK,'id':None})):
            with self.subTest(response=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.overrides[('GET','/api/onboarding/tasks/' + TID)] = (200,value)
                self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
                self.act('#query','GET','/api/onboarding/tasks/' + TID)
                text = self.page.locator('#message').inner_text()
                self.assertNotIn('已取得最新', text)
                self.assertIn('无效', text)
                self.assertTrue(self.page.locator('#pause').is_disabled())

    def test_legacy_accepted_command_with_invalid_task_stays_accepted(self):
        path = '/api/onboarding/tasks/' + TID + '/pause'
        accepted = dict(accepted=True,synthetic=True,execution_scope='pool',receipt_id=MID,phase='SUCCEEDED')
        for name, response, followup in (
                ('snapshot', {**accepted,'task':{**TASK,'version':2**53 + 2},'snapshot_pending':False}, None),
                ('followup', {**accepted,'task':None,'snapshot_pending':True}, (200,{**TASK,'version':2**53 + 2}))):
            with self.subTest(source=name):
                self.overrides.clear(); self.requests.clear(); self.dialogs.clear()
                self.start('/onboarding'); self.page.locator('#task-id').fill(TID)
                self.act('#query','GET','/api/onboarding/tasks/' + TID,text='已取得最新')
                self.overrides[('POST',path)] = (202,response)
                if followup:
                    self.overrides[('GET','/api/onboarding/tasks/' + TID)] = followup
                self.act('#pause','POST',path)
                text = self.page.locator('#message').inner_text()
                self.assertIn('已受理', text)
                self.assertIn('无效', text)
                self.assertNotIn('未确认', text)
                self.assertTrue(self.page.locator('#reconcile-command').is_hidden())
                self.assertTrue(self.page.locator('#pause').is_disabled())

    def test_import_release_invalidates_an_earlier_preflight(self):
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = (503,dict(code='COMMIT_UNKNOWN'))
        self.start()
        self.act('#preflight','POST','/api/onboarding/preflight')
        self.assertTrue(self.page.locator('#confirm-batch').is_enabled())
        self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='COMMIT_UNKNOWN')
        self.overrides[('POST','/api/onboarding/mailboxes/import')] = PROVED
        self.import_preview()
        self.act('#confirm-import','POST','/api/onboarding/mailboxes/import',text='已解除')
        self.assertTrue(self.page.locator('#confirm-batch').is_disabled(), 'a preflight observed before the release must not survive it')
