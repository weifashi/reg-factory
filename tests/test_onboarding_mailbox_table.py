"""Mailbox table interactions using intercepted fixture API, no external operations."""
import unittest
from urllib.parse import parse_qs, urlsplit
from tests import test_onboarding_unified_ui as unified
from tests import test_onboarding_pool_ui as pool


class MailboxTableTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    setUp = unified.UnifiedConsoleTests.setUp
    tearDown = pool.PoolDomTests.tearDown
    guarded = pool.PoolDomTests.guarded
    dialog = pool.PoolDomTests.dialog
    route = unified.UnifiedConsoleTests.route
    open = unified.UnifiedConsoleTests.open
    act = pool.PoolDomTests.act
    calls = pool.PoolDomTests.calls
    settle = pool.PoolDomTests.settle
    timed_out = pool.PoolDomTests.timed_out

    def test_compact_rows_and_detail_drawer_escape(self):
        self.open('#mailboxes')
        self.assertEqual(self.page.locator('#mailbox-list tr').count(),1)
        self.assertFalse(self.page.locator('[data-mailbox-group]').is_visible())
        self.assertNotIn('HISTORY_UNRECONCILED',self.page.locator('#mailbox-list').inner_text())
        self.page.locator('[data-mailbox-detail]').click()
        self.assertTrue(self.page.locator('#mailbox-detail').is_visible())
        self.assertIn('HISTORY_UNRECONCILED',self.page.locator('#mailbox-detail').inner_text())
        self.assertIn(pool.MID,self.page.locator('#mailbox-detail').inner_text())
        self.page.keyboard.press('Escape')
        self.assertFalse(self.page.locator('#mailbox-detail').is_visible())
        self.assertTrue(self.page.locator('[data-mailbox-detail]').evaluate('(n)=>n===document.activeElement'))

    def test_group_edit_and_more_keep_patch_contract(self):
        self.open('#mailboxes')
        self.page.locator('[data-mailbox-edit]').click()
        self.page.locator('[data-mailbox-group]').fill('fixture:edited')
        data=self.act('[data-save-group]','PATCH','/api/onboarding/mailboxes/'+pool.MID)
        self.assertEqual(data['changes'],{'group_ref':'fixture:edited'})
        self.assertIn('expected_version',data)
        self.page.locator('[data-mailbox-more]').click()
        data=self.act('button[data-mailbox-write]:not([data-save-group])','PATCH','/api/onboarding/mailboxes/'+pool.MID)
        self.assertEqual(data['changes'],{'disabled':True})

    def test_page_size_previous_and_filter_reset(self):
        self.open('#mailboxes')
        self.act(lambda:self.page.locator('#mailbox-page-size').select_option('25'),'GET','/api/onboarding/mailboxes')
        q=lambda:parse_qs(urlsplit(self.calls('GET','/api/onboarding/mailboxes')[-1][3]).query)
        self.assertEqual(q()['limit'],['25'])
        self.assertTrue(self.page.locator('#mailbox-prev').is_disabled())
        self.act('#mailbox-next','GET','/api/onboarding/mailboxes')
        self.assertIn('cursor',q())
        self.act('#mailbox-prev','GET','/api/onboarding/mailboxes')
        self.assertNotIn('cursor',q())
        self.act('#mailbox-next','GET','/api/onboarding/mailboxes')
        self.page.locator('#search').fill('changed')
        self.assertTrue(self.page.locator('#mailbox-prev').is_disabled())
        self.assertTrue(self.page.locator('#mailbox-next').is_disabled())

    def test_current_page_select_all_respects_permission(self):
        self.open('#mailboxes')
        self.page.locator('#mailbox-select-page').check()
        self.assertTrue(self.page.locator('[data-mailbox-select]').is_checked())
        self.assertIn('1',self.page.locator('#selected-count').inner_text())
        self.page.locator('[data-mailbox-select]').uncheck()
        self.assertFalse(self.page.locator('#mailbox-select-page').is_checked())
        self.assertEqual(self.calls('POST','/api/onboarding/batches'),[])

    def test_read_only_cannot_select_or_write_and_expiry_closes_drawer(self):
        self.permissions=['onboarding:read']
        self.open('#mailboxes')
        self.assertTrue(self.page.locator('#mailbox-select-page').is_disabled())
        self.assertTrue(self.page.locator('[data-mailbox-edit]').is_disabled())
        self.page.locator('[data-mailbox-detail]').click()
        self.page.evaluate("window.dispatchEvent(new Event('rf:session-expired'))")
        self.assertFalse(self.page.locator('#mailbox-detail').is_visible())
        self.assertEqual(self.page.locator('#mailbox-detail-content').inner_text(),'')

    def test_table_mobile_scroll_does_not_widen_document(self):
        self.page.set_viewport_size(dict(width=375,height=812))
        self.open('#mailboxes')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),375)
        self.assertEqual(self.page.locator('#mailbox-table-scroll').evaluate('(n)=>getComputedStyle(n).overflowY'),'auto')
        self.page.locator('[data-mailbox-detail]').click()
        self.assertLessEqual(self.page.locator('#mailbox-detail').bounding_box()['width'],375)
