"""Original-shell Google run integration; intercepted HTTP, no provider execution."""
import copy
import unittest
from playwright.sync_api import expect
from tests import test_onboarding_pool_ui as pool
from tests import test_onboarding_unified_ui as unified

BASE='/api/onboarding'
EVENTS=BASE+'/tasks/'+pool.TID+'/events'
LOG=dict(task_id=pool.TID,mode='synthetic',execution_connected=False,has_more=False,
         events=[dict(id='1',occurred_at='2026-09-28T00:00:00+00:00',action='task.create',outcome='OK',version=1)])

class RunConsoleTests(unittest.TestCase):
    setUpClass=classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass=classmethod(pool.PoolDomTests.tearDownClass.__func__)
    tearDown=pool.PoolDomTests.tearDown
    guarded=pool.PoolDomTests.guarded
    dialog=pool.PoolDomTests.dialog
    route=unified.UnifiedConsoleTests.route
    open=unified.UnifiedConsoleTests.open
    calls=pool.PoolDomTests.calls
    def setUp(self):
        unified.UnifiedConsoleTests.setUp(self)
        self.overrides[('GET',EVENTS)]=(200,copy.deepcopy(LOG))
    def start(self):
        self.open('#google')
        self.assertEqual(self.page.locator('#run-google').count(),1,'unified run entry is missing')
    def run_task(self):
        self.page.locator('#run-google').click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
    def test_run_automates_preflight_create_without_dialog_and_persistent_log(self):
        self.start(); self.run_task()
        self.assertEqual(len(self.calls('POST',BASE+'/preflight')),1)
        self.assertEqual(len(self.calls('POST',BASE+'/batches')),1)
        self.assertEqual(len(self.calls('GET',EVENTS)),1)
        self.assertEqual(self.dialogs,[])
        self.assertNotRegex(self.page.locator('#task-workbench').inner_text(),r'模拟|合成')
        expect(self.page.locator('#google-log')).to_contain_text('服务端')
        expect(self.page.locator('#google-log')).to_contain_text('创建')
        expect(self.page.locator('#google-run-status')).to_contain_text('QUEUED')
        expect(self.page.locator('#message')).to_be_hidden()
        self.assertEqual(len(self.calls('GET','/api/auth/session')),1)
        self.assertEqual(self.errors,[])
    def test_run_rejection_still_displays_error_banner(self):
        self.overrides[('POST',BASE+'/batches')]=(403,dict(code='FORBIDDEN'))
        self.start();self.run_task()
        expect(self.page.locator('#message')).to_be_visible()
        expect(self.page.locator('#message')).to_have_class('message error')
        expect(self.page.locator('#google-log')).to_contain_text('被拒绝')
    def test_accepted_run_followup_failure_still_displays_warning(self):
        def accepted(request):
            self.overrides[('GET',BASE+'/tasks')]=(503,dict(code='DEPENDENCY_UNAVAILABLE'))
            return 202,dict(batch_id=pool.MID,task_ids=[pool.TID])
        self.overrides[('POST',BASE+'/batches')]=accepted
        self.start();self.run_task()
        expect(self.page.locator('#message')).to_be_visible()
        expect(self.page.locator('#message')).to_contain_text('刷新失败')
        expect(self.page.locator('#reconcile-card')).to_be_hidden()
    def test_plain_run_copy_preserves_unavailable_execution_notice(self):
        self.start()
        expect(self.page.locator('#run-google')).to_have_text('运行任务')
        expect(self.page.locator('#google-mode-help')).to_contain_text('执行服务尚未接入')
        expect(self.page.locator('#google-mode-help')).to_contain_text('不注册 Google、不绑卡、不申请赠金，也不调用模型')
        self.page.locator('#google-advanced > summary').click()
        self.assertNotRegex(self.page.locator('#task-workbench').inner_text(),r'模拟|合成')
        self.assertEqual(self.page.locator('#google-execution-mode').input_value(),'synthetic')
        self.assertTrue(self.page.locator('#google-execution-mode option[value="real"]').is_disabled())
        self.assertFalse([r for r in self.requests if r[0]=='POST'])
    def test_missing_resources_never_create(self):
        self.overrides[('POST',BASE+'/preflight')]=(200,dict(observation_only=True,can_create=False,eligible_count=0,config_revision=None,reason_codes=['CONFIG_MISSING','INSUFFICIENT_ELIGIBLE']))
        self.start();self.run_task()
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])
        expect(self.page.locator('#google-log')).to_contain_text('配置')
        expect(self.page.locator('#google-log')).to_contain_text('邮箱')
    def test_run_does_not_depend_on_browser_confirmation(self):
        self.dialog_decide=lambda _:False
        self.start();self.run_task()
        self.assertEqual(self.dialogs,[])
        self.assertEqual(len(self.calls('POST',BASE+'/batches')),1)
        expect(self.page.locator('#google-log')).to_contain_text('已受理')
    def test_real_mode_is_disabled_and_tampering_does_not_call_api(self):
        self.start()
        self.assertTrue(self.page.locator('#google-execution-mode option[value="real"]').is_disabled())
        self.page.evaluate("() => { const s=document.querySelector('#google-execution-mode'); s.value='real'; s.dispatchEvent(new Event('change')); }")
        self.page.evaluate("() => document.querySelector('#run-google').click()")
        self.assertEqual(self.calls('POST',BASE+'/preflight'),[])
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])
    def test_unknown_create_freezes_new_runs_and_reconcile_keeps_key(self):
        self.overrides[('POST',BASE+'/batches')]=(503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.start();self.run_task()
        original=self.calls('POST',BASE+'/batches')[0][2]
        expect(self.page.locator('#run-google')).to_be_disabled()
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        del self.overrides[('POST',BASE+'/batches')]
        self.page.locator('#reconcile-request').click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
        self.assertEqual(self.calls('POST',BASE+'/batches')[1][2],original)
        self.assertEqual(len(self.calls('POST',BASE+'/preflight')),1)
    def test_accepted_create_log_read_failure_is_not_unconfirmed_write(self):
        self.overrides[('GET',EVENTS)]=(503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.start();self.run_task()
        expect(self.page.locator('#reconcile-card')).to_be_hidden()
        expect(self.page.locator('#google-log-state')).to_contain_text('读取失败')
        expect(self.page.locator('#google-log')).to_contain_text('已受理')
        self.assertEqual(len(self.calls('POST',BASE+'/batches')),1)
    def test_expired_preflight_never_creates(self):
        def expired(request):
            self.page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
            return 200,dict(observation_only=True,can_create=True,eligible_count=1,config_revision=pool.REV,reason_codes=[])
        self.overrides[('POST',BASE+'/preflight')]=expired
        self.start(); self.run_task()
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])
        self.assertEqual(self.dialogs,[])
        expect(self.page.locator('#run-google')).to_be_disabled()
    def test_no_task_permission_and_collapsed_history(self):
        self.permissions.remove('tasks:manage');self.start()
        expect(self.page.locator('#run-google')).to_be_disabled()
        self.assertFalse(self.page.locator('#task-history').evaluate('(e)=>e.open'))
        self.assertFalse(self.page.locator('#google-advanced').evaluate('(e)=>e.open'))
    def test_log_rejects_untrusted_actions_and_non_synthetic_claims(self):
        for field,value in [('mode','real'),('execution_connected',True),('events',[dict(LOG['events'][0],outcome='SECRET_CANARY')]),('events',[dict(LOG['events'][0],action='<img src=x onerror=alert(1)>')])]:
            with self.subTest(field=field):
                self.overrides[('GET',EVENTS)]=(200,{**copy.deepcopy(LOG),field:value})
                self.start();self.run_task()
                expect(self.page.locator('#google-log-state')).to_contain_text('读取失败')
                self.assertEqual(self.page.locator('#google-log img').count(),0)
                self.assertEqual(self.dialogs,[])
                self.page.close();self.context.close();self.setUp()
    def test_refresh_log_does_not_resubmit_task(self):
        self.start();self.run_task()
        self.page.locator('#refresh-google-log').click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
        self.assertEqual(len(self.calls('POST',BASE+'/batches')),1)
        self.assertEqual(len(self.calls('GET',EVENTS)),2)
    def test_history_selection_reads_log_without_write(self):
        self.start();self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').first.click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
        expect(self.page.locator('#google-log')).to_contain_text('服务端')
        self.assertFalse([r for r in self.requests if r[0]=='POST'])

    def test_live_history_shows_real_step_receipts_without_enabling_execution(self):
        live = dict(pool.TASK, synthetic=False, execution_mode='live')
        self.overrides[('GET', BASE+'/tasks/'+pool.TID)] = (200, live)
        self.overrides[('GET', EVENTS)] = (200, dict(LOG, mode='live', effects_has_more=False,
            effect_events=[dict(receipt_id=pool.TID, action='project.create', phase='UNKNOWN',
                result_code='OPERATION_PENDING', version=2, updated_at='2026-09-29T00:00:00.000000Z')]))
        self.start(); self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').first.click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
        expect(self.page.locator('#task-facts')).to_contain_text('真实执行任务')
        expect(self.page.locator('#google-log')).to_contain_text('创建专用项目')
        expect(self.page.locator('#google-log')).to_contain_text('结果待核验')
        expect(self.page.locator('#google-log-state')).to_contain_text('不代表首次试用或赠金已核验')
        for action in ('pause','cancel','recheck'):
            expect(self.page.locator('#'+action+'-task')).to_be_disabled()
        self.assertFalse([r for r in self.requests if r[0]=='POST'])
        self.assertEqual(self.errors, [])

    def test_legacy_synthetic_wire_contract_still_loads_during_asset_upgrade(self):
        old = {key: value for key, value in pool.TASK.items() if key != 'execution_mode'}
        self.overrides[('GET', BASE+'/tasks/'+pool.TID)] = (200, old)
        self.start(); self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').first.click()
        expect(self.page.locator('#main')).to_have_attribute('aria-busy','false')
        expect(self.page.locator('#task-facts')).to_contain_text('执行类型未核实')
        for action in ('pause','cancel','recheck'):
            expect(self.page.locator('#'+action+'-task')).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_live_conflict_is_visible_but_untrusted_effect_code_is_rejected(self):
        self.overrides[('GET', BASE+'/tasks/'+pool.TID)] = (200, dict(pool.TASK, synthetic=False, execution_mode='live'))
        effect = dict(receipt_id=pool.TID, action='project.create', phase='CONFLICT',
                      result_code='CONFLICT', version=2, updated_at='2026-09-29T00:00:00.000000Z')
        self.overrides[('GET', EVENTS)] = (200, dict(LOG, mode='live', effects_has_more=False, effect_events=[effect]))
        self.start(); self.page.locator('#task-history > summary').click()
        self.page.locator('[data-open-task]').first.click()
        expect(self.page.locator('#google-log')).to_contain_text('证据冲突')
        self.overrides[('GET', EVENTS)] = (200, dict(LOG, mode='live', effects_has_more=False,
            effect_events=[dict(effect, result_code='SECRET_CANARY')]))
        self.page.locator('#refresh-google-log').click()
        expect(self.page.locator('#google-log-state')).to_contain_text('读取失败')
        expect(self.page.locator('#google-log')).not_to_contain_text('SECRET_CANARY')
        self.assertFalse([r for r in self.requests if r[0]=='POST'])

    def test_parameter_change_during_preflight_does_not_create(self):
        def change(request):
            self.page.evaluate("() => { const n=document.querySelector('#requested-count'); n.value='2'; n.dispatchEvent(new Event('input')); }")
            return 200,dict(observation_only=True,can_create=True,eligible_count=1,config_revision=pool.REV,reason_codes=[])
        self.overrides[('POST',BASE+'/preflight')]=change
        self.start();self.run_task()
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])
        self.assertEqual(self.dialogs,[])
        expect(self.page.locator('#google-log')).to_contain_text('参数已变化')
    def test_double_dispatch_during_preflight_still_submits_once(self):
        def again(request):
            self.page.evaluate("() => document.querySelector('#run-google').dispatchEvent(new Event('click'))")
            return 200,dict(observation_only=True,can_create=True,eligible_count=1,config_revision=pool.REV,reason_codes=[])
        self.overrides[('POST',BASE+'/preflight')]=again
        self.start();self.run_task()
        self.assertEqual(len(self.calls('POST',BASE+'/preflight')),1)
        self.assertEqual(len(self.calls('POST',BASE+'/batches')),1)
    def test_log_late_response_after_pagehide_cannot_revive(self):
        def expire(request):
            self.page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
            return 200,copy.deepcopy(LOG)
        self.overrides[('GET',EVENTS)]=expire
        self.start();self.run_task()
        expect(self.page.locator('#google-log')).to_be_empty()
        expect(self.page.locator('#run-google')).to_be_disabled()
        expect(self.page.locator('#google-run-status')).to_contain_text('会话已结束')
    def test_false_positive_preflight_shape_cannot_create(self):
        self.overrides[('POST',BASE+'/preflight')]=(200,dict(observation_only=True,can_create=True,eligible_count=0,config_revision=pool.REV,reason_codes=[]))
        self.start();self.run_task()
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])
        self.assertEqual(self.dialogs,[])
    def test_malformed_batch_receipt_keeps_pending_and_never_claims_accepted(self):
        self.overrides[('POST',BASE+'/batches')]=(202,dict(batch_id=pool.MID,task_ids=[]))
        self.start();self.run_task()
        expect(self.page.locator('#reconcile-card')).to_be_visible()
        self.assertNotIn('批次已受理',self.page.locator('#google-log').inner_text())

    def test_failed_preflight_finishes_all_loading_labels(self):
        self.overrides[('POST',BASE+'/preflight')]=(200,dict(selection='automatic',requested_count=1,eligible_count=0,config_revision=pool.REV,can_create=False,reason_codes=['INSUFFICIENT_ELIGIBLE'],observation_only=True))
        self.start();self.run_task()
        self.assertNotIn('正在',self.page.locator('#task-empty').inner_text())
        self.assertNotIn('正在',self.page.locator('#google-log-state').inner_text())
        self.assertIn('预检未通过',self.page.locator('#google-run-status').inner_text())
        self.assertEqual(self.calls('POST',BASE+'/batches'),[])

    def test_safe_cancel_log_and_terminal_controls(self):
        self.overrides[('GET',BASE+'/tasks/'+pool.TID)]=(200,dict(pool.TASK,status='CANCELLED_SAFE',cancel_requested=True,reason_code='CANCEL_SAFE_RELEASED'))
        self.overrides[('GET',EVENTS)]=(200,dict(LOG,events=[dict(id='2',occurred_at='2026-09-28T00:00:00+00:00',action='task.cancel',outcome='CANCELLED_SAFE',version=3)]))
        self.start();self.run_task()
        self.assertIn('邮箱已释放',self.page.locator('#google-log').inner_text())
        self.assertTrue(self.page.locator('#cancel-task').is_disabled())
        self.assertTrue(self.page.locator('#pause-task').is_disabled())
        self.assertIn('邮箱已释放',self.page.locator('#task-facts').inner_text())

    def test_cancel_retains_business_reason_but_log_explains_release_block(self):
        self.overrides[('GET',BASE+'/tasks/'+pool.TID)]=(200,dict(pool.TASK,status='PAUSED',cancel_requested=True,reason_code='EXTERNAL_UNKNOWN'))
        self.overrides[('GET',EVENTS)]=(200,dict(LOG,events=[dict(id='2',occurred_at='2026-09-28T00:00:00+00:00',action='task.cancel',outcome='CANCEL_STEP_EXISTS',version=3)]))
        self.start();self.run_task()
        self.assertIn('已有步骤记录',self.page.locator('#google-log').inner_text())
        self.assertIn('保留占用',self.page.locator('#google-log').inner_text())
        self.assertIn('EXTERNAL_UNKNOWN',self.page.locator('#task-facts').inner_text())
        self.assertNotIn('邮箱已释放',self.page.locator('#google-log').inner_text())
