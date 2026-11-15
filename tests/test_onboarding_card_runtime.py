"""Shared runtime extension guards; HTTP intercepted, no provider/real secrets."""
import unittest
from tests import test_onboarding_pool_ui as pool


class CardRuntimeTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    tearDown = pool.PoolDomTests.tearDown
    guarded = pool.PoolDomTests.guarded
    dialog = pool.PoolDomTests.dialog
    route = pool.PoolDomTests.route
    start = pool.PoolDomTests.start
    settle = pool.PoolDomTests.settle

    def setUp(self):
        pool.PoolDomTests.setUp(self)
        # These are adapter contracts, independent of the card UI under development.
        self.page.route('**/static/onboarding-cards.js', lambda r: r.fulfill(body='',content_type='text/javascript'))

    def test_runtime_exposes_one_session_with_state_callbacks(self):
        self.start()
        self.assertTrue(self.page.evaluate('Boolean(window.RFPoolRuntime)'), 'shared runtime missing')
        state=self.page.evaluate("""() => {
          window.calls=[]; RFPoolRuntime.register({controls:s=>calls.push({busy:s.busy,inactive:s.inactive}),expire:()=>calls.push('expired')});
          return RFPoolRuntime.state();
        }""")
        self.assertFalse(state['busy'])
        self.assertFalse(state['inactive'])
        self.assertIsNone(state['pending'])
        self.page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
        self.assertIn('expired', self.page.evaluate('calls'))
        self.assertEqual(sum(path=='/api/auth/session' for _,path,*_ in self.requests),1)

    def test_card_pending_cannot_be_replayed_as_mailbox_import(self):
        self.start()
        self.assertTrue(self.page.evaluate('Boolean(window.RFPoolRuntime)'))
        self.page.evaluate("""() => {
          window.reconciled=[];
          RFPoolRuntime.register({reconcile:r=>reconciled.push(r.kind)});
          RFPoolRuntime.remember({secret:true,kind:'card-import',path:'/api/onboarding/cards/import',
              permission:'mailboxes:manage',key:'fixture-key',digest:'d'.repeat(64)});
        }""")
        self.assertTrue(self.page.locator('#preview-import').is_disabled())
        self.assertTrue(self.page.locator('#confirm-import').is_disabled())
        self.page.locator('#reconcile-request').click()
        self.assertEqual(self.page.evaluate('reconciled'),['card-import'])
        self.assertNotIn('/api/onboarding/mailboxes/import',[p for _,p,*_ in self.requests])

    def test_navigation_and_late_register_clear_expired_addon(self):
        self.start()
        self.assertTrue(self.page.evaluate('Boolean(window.RFPoolRuntime)'))
        self.page.evaluate("""() => {
          window.targets=[]; RFPoolRuntime.register({beforeNavigate:v=>{targets.push(v); return true;}});
          RFPoolRuntime.beforeNavigate('google');
        }""")
        self.assertEqual(self.page.evaluate('targets'),['google'])
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        self.page.evaluate("() => { window.lateCleared=false; RFPoolRuntime.register({expire:()=>lateCleared=true}); }")
        self.assertTrue(self.page.evaluate('lateCleared'))

    def test_late_addon_read_after_expiry_is_discarded(self):
        self.start()
        held=[]
        self.page.route('**/api/onboarding/cards',lambda r:held.append(r))
        self.page.evaluate("""() => {
          window.lateResult=null;
          RFPoolRuntime.api('/api/onboarding/cards').then(
            ()=>lateResult='accepted',error=>lateResult=error.status);
        }""")
        self.page.wait_for_timeout(100)
        self.assertEqual(len(held),1)
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=200,content_type='application/json',body='{"items":[],"next_cursor":null}')
        self.page.wait_for_function('lateResult !== null')
        self.assertEqual(self.page.evaluate('lateResult'),401)

    def test_standalone_navigation_notifies_secret_cleanup_addon(self):
        self.start()
        self.page.evaluate("""() => {
          window.navigationTargets=[];
          RFPoolRuntime.register({beforeNavigate:target=>{navigationTargets.push(target); return true;}});
        }""")
        self.page.locator('[data-shell-target="card-pool"]').click()
        self.page.locator('[data-shell-target="global-config"]').click()
        self.assertEqual(self.page.evaluate('navigationTargets'),['cards','settings'])

    def test_late_addon_mutation_does_not_run_accepted_callback(self):
        self.start()
        held=[]
        self.page.route('**/api/onboarding/cards/fixture',lambda r:held.append(r))
        self.page.evaluate("""() => {
          window.callbackCount=0;
          RFPoolRuntime.run(()=>RFPoolRuntime.mutate('/api/onboarding/cards/fixture','PATCH',
            {request_key:'fixture-key'},'mailboxes:manage',()=>callbackCount++));
        }""")
        self.page.wait_for_timeout(100)
        self.assertEqual(len(held),1)
        self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        held[0].fulfill(status=200,content_type='application/json',body='{}')
        self.page.wait_for_function('!RFPoolRuntime.state().busy')
        self.assertEqual(self.page.evaluate('callbackCount'),0)
        self.assertIsNone(self.page.evaluate('RFPoolRuntime.state().pending'))
