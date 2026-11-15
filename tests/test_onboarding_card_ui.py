"""Isolated intercepted Chromium card UI checks; not real-provider evidence."""
import csv
import io
import json
import unittest
from urllib.parse import urlsplit
from tests import test_onboarding_pool_ui as pool
from tests import test_onboarding_unified_ui as unified

BASE = '/api/onboarding/cards'
CARD = dict(id=pool.CID, alias='fixture-card', brand='visa', last4='1111', enabled=True,
            account_limit=3, linked_count=1, reserved_count=1, reconciliation_required=False,
            version=1, revision=1, sms_configured=True, created_at='2026-09-28T00:00:00Z',
            updated_at='2026-09-28T00:00:00Z', last_assigned_at=None)
HEADER = 'alias,pan,expiry_month,expiry_year,holder_name,country,address_line1,address_line2,city,region,postal_code,account_limit,sms_channel_ref'
VALUES = ['fixture-card','4111111111111111','1','2099','fixture:holder','US','fixture:line','','fixture:city','fixture:region','fixture:postal','3','fixture:sms']
CSV = HEADER + '\n' + ','.join(VALUES) + '\n'
PREVIEW = dict(items=[dict(line=2, alias='fixture-card', brand='visa', last4='1111', account_limit=3,
                          sms_configured=True, billing_configured=True, expiry_configured=True)],
               issues=[], accepted_count=1, duplicate_count=0, conflict_count=0, can_import=True,
               preview_digest='b'*64)


class CardAssetsTests(unittest.TestCase):
    def test_card_module_has_no_secret_persistence_or_parallel_auth(self):
        path = pool.STATIC / 'onboarding-cards.js'
        self.assertTrue(path.exists(), 'card UI module missing')
        source = path.read_text()
        for value in ('innerHTML', 'outerHTML', 'localStorage', 'sessionStorage', 'console.',
                      'document.cookie', 'fetch(', '/api/auth/session'):
            self.assertNotIn(value, source)
        self.assertIn('RFPoolRuntime', source)
        self.assertIn('synthetic_only', source)


class CardDomTests(unittest.TestCase):
    setUpClass = classmethod(pool.PoolDomTests.setUpClass.__func__)
    tearDownClass = classmethod(pool.PoolDomTests.tearDownClass.__func__)
    tearDown = unified.UnifiedConsoleTests.tearDown
    guarded = pool.PoolDomTests.guarded
    dialog = pool.PoolDomTests.dialog
    settle = pool.PoolDomTests.settle
    act = pool.PoolDomTests.act
    calls = pool.PoolDomTests.calls
    timed_out = pool.PoolDomTests.timed_out
    force_click = pool.PoolDomTests.force_click
    assert_quiet = pool.PoolDomTests.assert_quiet
    open = unified.UnifiedConsoleTests.open

    def setUp(self):
        unified.UnifiedConsoleTests.setUp(self)
        self.permissions.append('cards:manage')
        self.overrides[('GET', BASE)] = (200, dict(items=[CARD], next_cursor=None))
        self.overrides[('GET', BASE+'/template')] = (200, dict(csv=HEADER+'\n', data_policy='synthetic_only'))
        self.overrides[('POST', BASE+'/import-preview')] = (200, PREVIEW)
        self.overrides[('POST', BASE+'/import')] = lambda r: (200, dict(created_ids=[pool.CID], skipped_ids=[], request_key=r.post_data_json['request_key']))
        self.overrides[('PATCH', BASE+'/'+pool.CID)] = lambda r: (200, dict(card_id=pool.CID, version=2, request_key=r.post_data_json['request_key']))

    route = unified.UnifiedConsoleTests.route

    def cards(self):
        self.open('#cards')
        self.assertEqual(self.page.locator('#card-tab-list').count(), 1, 'real card management tabs missing')
        self.page.wait_for_function("() => document.querySelector('#card-state').textContent.includes('读取')")
        self.settle()

    def preview(self, text=CSV):
        self.page.locator('#card-tab-csv').click()
        self.page.locator('#card-csv').fill(text)
        return self.act('#card-preview-csv', 'POST', BASE+'/import-preview')

    def test_masked_list_counts_edit_and_toggle(self):
        self.cards()
        text = self.page.locator('#card-list').inner_text()
        for expected in ('fixture-card', '1111', '1 / 3'):
            self.assertIn(expected, text)
        self.assertNotIn(VALUES[1], text)
        self.page.locator('[data-card-edit]').click()
        self.page.locator('#card-edit-alias').fill('fixture-renamed')
        body = self.act('#card-save', 'PATCH', BASE+'/'+pool.CID)
        self.assertEqual(body['expected_version'], 1)
        self.assertEqual(body['changes'], dict(alias='fixture-renamed', account_limit=3))
        self.page.locator('[data-card-more]').click()
        body = self.act('[data-card-toggle]', 'PATCH', BASE+'/'+pool.CID)
        self.assertEqual(body['changes'], dict(enabled=False))
        self.assertEqual(self.errors, [])

    def test_csv_preview_explicit_confirm_and_secret_clear(self):
        self.cards()
        body = self.preview()
        self.assertEqual(body, dict(text=CSV))
        self.assertEqual(self.calls('POST', BASE+'/import'), [])
        self.assertNotIn(VALUES[1], self.page.locator('#card-preview').inner_text())
        body = self.act('#card-confirm', 'POST', BASE+'/import')
        self.assertEqual(body['text'], CSV)
        self.assertEqual(body['preview_digest'], PREVIEW['preview_digest'])
        self.assertEqual(self.page.locator('#card-csv').input_value(), '')
        self.assertTrue(self.page.locator('#card-confirm').is_disabled())

    def test_manual_uses_csv_pipeline_and_navigation_erases_inputs(self):
        self.cards()
        self.page.locator('#card-tab-manual').click()
        for name, value in zip(HEADER.split(','), VALUES):
            self.page.locator('#card-manual-'+name).fill(value)
        body = self.act('#card-preview-manual', 'POST', BASE+'/import-preview')
        rows = list(csv.reader(io.StringIO(body['text'])))
        self.assertEqual(rows, [HEADER.split(','), VALUES])
        self.assertEqual(self.page.locator('#card-pool input[name=cvv]').count(), 0)
        self.page.locator('#resources-tab-mailboxes').click()
        self.assertEqual(self.page.locator('#card-manual-pan').input_value(), '')
        self.assertEqual(self.page.locator('#card-preview').inner_text(), '')

    def test_unknown_import_replays_same_key_and_digest_only(self):
        self.cards()
        self.preview()
        self.overrides[('POST', BASE+'/import')] = 'abort'
        first = self.act('#card-confirm', 'POST', BASE+'/import')
        self.assertEqual(self.page.locator('#card-csv').input_value(), '')
        pending = self.page.evaluate('RFPoolRuntime.state().pending')
        self.assertEqual(pending['kind'], 'card-import')
        self.assertNotIn('text', pending)
        self.assertNotIn(VALUES[1], json.dumps(pending))
        self.assert_quiet('POST', BASE+'/import', 1)
        self.page.locator('#card-csv').fill(CSV)
        self.act('#card-preview-csv', 'POST', BASE+'/import-preview')
        self.overrides[('POST', BASE+'/import')] = lambda r: (200, dict(created_ids=[], skipped_ids=[pool.CID], request_key=r.post_data_json['request_key']))
        second = self.act('#card-confirm', 'POST', BASE+'/import')
        self.assertEqual(first, second)
        self.assertIsNone(self.page.evaluate('RFPoolRuntime.state().pending'))

    def test_readonly_permission_and_mobile_overflow(self):
        self.permissions.remove('cards:manage')
        self.open('#cards')
        self.assertEqual(self.page.locator('#card-tab-list').count(), 1)
        self.assertTrue(self.page.locator('#card-search-submit').is_disabled())
        self.force_click('#card-search-submit')
        self.assertEqual(self.calls('GET', BASE), [])
        self.page.set_viewport_size(dict(width=375,height=900))
        sizes = self.page.locator('.content').evaluate('(n)=>[n.scrollWidth,n.clientWidth]')
        self.assertLessEqual(sizes[0], sizes[1]+1)

    def test_preview_401_expires_clears_and_cannot_revive(self):
        self.cards()
        self.overrides[('POST', BASE+'/import-preview')] = (401, dict(code='UNAUTHENTICATED'))
        self.preview()
        self.assertEqual(self.page.locator('#card-csv').input_value(), '')
        self.assertTrue(self.page.locator('#card-confirm').is_disabled())
        self.assertTrue(self.page.locator('#console-login').is_visible())
        self.assertIsNone(self.page.evaluate('RFPoolRuntime.state().pending'))

    def test_all_stored_brands_and_reconciliation_are_observations(self):
        self.overrides[('GET', BASE)] = (200, dict(items=[dict(CARD,brand='amex',reconciliation_required=True)],next_cursor=None))
        self.cards()
        self.assertIn('AMEX', self.page.locator('#card-list').inner_text())
        self.assertIn('待核对', self.page.locator('#card-list').inner_text())

    def test_rejected_preview_clears_plaintext_but_keeps_safe_issue(self):
        self.cards()
        self.overrides[('POST',BASE+'/import-preview')] = (200, dict(PREVIEW,can_import=False,items=[],issues=[dict(line=2,code='SYNTHETIC_DATA_REQUIRED')],accepted_count=0))
        self.preview()
        self.assertEqual(self.page.locator('#card-csv').input_value(), '')
        self.assertIn('SYNTHETIC_DATA_REQUIRED',self.page.locator('#card-preview').inner_text())
        self.assertTrue(self.page.locator('#card-confirm').is_disabled())

    def test_pending_card_blocks_mail_import_and_reconcile_returns_to_cards(self):
        self.cards(); self.preview()
        self.overrides[('POST',BASE+'/import')] = 'abort'
        self.act('#card-confirm','POST',BASE+'/import')
        self.page.locator('#resources-tab-mailboxes').click()
        self.page.locator('#tab-import').click()
        self.assertTrue(self.page.locator('#preview-import').is_disabled())
        self.force_click('#preview-import')
        self.assertEqual(self.calls('POST','/api/onboarding/mailboxes/import-preview'),[])
        self.page.locator('#reconcile-request').click()
        self.assertTrue(self.page.url.endswith('#cards'))
        self.assertTrue(self.page.locator('#card-csv').is_visible())

    def test_busy_shared_navigation_and_late_preview_cannot_revive_expired_session(self):
        self.cards()
        held = []
        self.page.route('**/api/onboarding/cards/import-preview', lambda r: held.append(r))
        self.page.locator('#card-tab-csv').click()
        self.page.locator('#card-csv').fill(CSV)
        with self.page.expect_request(lambda r: urlsplit(r.url).path == BASE+'/import-preview'):
            self.page.locator('#card-preview-csv').click()
        self.page.locator('#resources-tab-mailboxes').click()
        self.assertTrue(self.page.url.endswith('#cards'))
        self.page.evaluate("window.dispatchEvent(new CustomEvent('rf:session-expired'))")
        self.assertEqual(self.page.locator('#card-csv').input_value(),'')
        held[0].fulfill(content_type='application/json',body=json.dumps(PREVIEW))
        self.settle()
        self.assertEqual(self.page.locator('#card-preview').inner_text(),'')
        self.assertTrue(self.page.locator('#card-confirm').is_disabled())

    def test_failed_reads_empty_results_and_no_automatic_retry(self):
        self.overrides[('GET',BASE)] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.open('#cards')
        self.page.wait_for_function("() => document.querySelector('#card-state').textContent.includes('读取失败')")
        self.settle()
        self.assert_quiet('GET',BASE,1)
        self.overrides[('GET',BASE)] = (200,dict(items=[],next_cursor=None))
        self.act('#card-search-submit','GET',BASE)
        self.assertIn('没有卡片',self.page.locator('#card-state').inner_text())
        self.assertEqual(self.page.locator('#card-list tr').count(),0)

    def test_metadata_conflict_stales_controls_and_sms_replace_is_explicit(self):
        self.cards()
        self.page.locator('[data-card-edit]').click()
        self.assertTrue(self.page.locator('#card-edit-sms').is_disabled())
        self.page.locator('#card-replace-sms').check()
        self.page.locator('#card-edit-sms').fill('fixture:new-sms')
        self.overrides[('PATCH',BASE+'/'+pool.CID)] = (409,dict(code='VERSION_CONFLICT'))
        body = self.act('#card-save','PATCH',BASE+'/'+pool.CID)
        self.assertEqual(body['changes']['sms_channel_ref'],'fixture:new-sms')
        self.assertTrue(self.page.locator('[data-card-toggle]').is_disabled())
        self.assertEqual(self.page.locator('#card-edit-sms').input_value(),'')
        self.act('#card-search-submit','GET',BASE)
        self.assertFalse(self.page.locator('[data-card-toggle]').is_disabled())

    def test_unknown_digest_mismatch_never_changes_key_and_replay_rejection_stays_pending(self):
        self.cards(); self.preview()
        self.overrides[('POST',BASE+'/import')] = 'abort'
        first = self.act('#card-confirm','POST',BASE+'/import')
        self.overrides[('POST',BASE+'/import-preview')] = (200,dict(PREVIEW,preview_digest='c'*64))
        self.page.locator('#card-csv').fill(CSV+'\n')
        self.act('#card-preview-csv','POST',BASE+'/import-preview')
        self.assertEqual(self.page.locator('#card-csv').input_value(),'')
        self.assertTrue(self.page.locator('#card-confirm').is_disabled())
        self.assertEqual(self.page.evaluate('RFPoolRuntime.state().pending.key'), first['request_key'])
        self.overrides[('POST',BASE+'/import-preview')] = (200,PREVIEW)
        self.page.locator('#card-csv').fill(CSV)
        self.act('#card-preview-csv','POST',BASE+'/import-preview')
        self.overrides[('POST',BASE+'/import')] = (422,dict(code='INVALID_INPUT'))
        second = self.act('#card-confirm','POST',BASE+'/import')
        self.assertEqual(first,second)
        self.assertEqual(self.page.evaluate('RFPoolRuntime.state().pending.key'),first['request_key'])

    def test_mobile_all_tabs_no_overflow_and_no_secret_storage(self):
        self.cards()
        for width in (375,1440):
            self.page.set_viewport_size(dict(width=width,height=1000))
            for tab in ('manual','csv','list'):
                self.page.locator('#card-tab-'+tab).click()
                sizes = self.page.locator('.content').evaluate('(n)=>[n.scrollWidth,n.clientWidth]')
                self.assertLessEqual(sizes[0],sizes[1]+1,(width,tab,sizes))
        self.assertEqual(self.page.evaluate('Object.keys(localStorage)'),[])
        self.assertEqual(self.page.evaluate('Object.keys(sessionStorage)'),[])
        self.assertEqual(self.errors,[])

    def test_search_paging_and_secret_search_is_not_sent(self):
        self.overrides[('GET',BASE)] = (200,dict(items=[CARD],next_cursor='fixture-cursor'))
        self.cards()
        self.act('#card-next','GET',BASE)
        self.assertIn('cursor=fixture-cursor', self.calls('GET',BASE)[-1][3])
        self.page.locator('#card-search').fill('1111')
        self.page.locator('#card-enabled').select_option('true')
        self.act('#card-search-submit','GET',BASE)
        url = self.calls('GET',BASE)[-1][3]
        self.assertIn('search=1111',url)
        self.assertIn('enabled=true',url)
        self.assertNotIn('cursor=',url)
        count = len(self.calls('GET',BASE))
        self.page.locator('#card-search').fill(VALUES[1])
        self.page.locator('#card-search-submit').click()
        self.settle()
        self.assertEqual(len(self.calls('GET',BASE)),count)
        self.assertEqual(self.page.locator('#card-search').input_value(),'')

    def test_invalid_update_ack_is_pending_without_reporting_success(self):
        self.cards()
        self.overrides[('PATCH',BASE+'/'+pool.CID)] = (200,dict(card_id=pool.CID,version=2,request_key='wrong-key'))
        self.page.locator('[data-card-more]').click()
        self.act('[data-card-toggle]','PATCH',BASE+'/'+pool.CID)
        pending = self.page.evaluate('RFPoolRuntime.state().pending')
        self.assertFalse(pending['secret'])
        self.assertNotIn('已保存', self.page.locator('#message').inner_text())
        self.assertTrue(self.page.locator('[data-card-edit]').is_disabled())

    def test_accepted_import_followup_failure_never_becomes_unknown(self):
        self.cards(); self.preview()
        self.overrides[('GET',BASE)] = (503,dict(code='DEPENDENCY_UNAVAILABLE'))
        self.act('#card-confirm','POST',BASE+'/import')
        self.assertIsNone(self.page.evaluate('RFPoolRuntime.state().pending'))
        self.assertIn('已导入',self.page.locator('#message').inner_text())
        self.assertEqual(self.page.locator('#card-csv').input_value(),'')

    def test_invalid_masked_dto_is_not_rendered_and_html_alias_is_text(self):
        self.overrides[('GET',BASE)] = (200,dict(items=[dict(CARD,alias='<img src=x onerror=alert(1)>')],next_cursor=None))
        self.cards()
        self.assertEqual(self.page.locator('#card-list img').count(),0)
        self.assertIn('<img',self.page.locator('#card-list').inner_text())
        self.overrides[('GET',BASE)] = (200,dict(items=[dict(CARD,alias=VALUES[1])],next_cursor=None))
        self.act('#card-search-submit','GET',BASE)
        self.assertEqual(self.page.locator('#card-list tr').count(),0)
        self.assertNotIn(VALUES[1],self.page.locator('#card-pool').inner_text())
        self.assertEqual(self.errors,[])

    def test_unicode_pan_or_control_search_never_enters_get_url(self):
        self.cards()
        count = len(self.calls('GET',BASE))
        for text in ('４１１１１１１１１１１１１１１１','٤١١١١١١١١١١١١١١١','fixture\u200bcard'):
            self.page.locator('#card-search').fill(text)
            self.page.locator('#card-search-submit').click()
            self.settle()
            self.assertEqual(len(self.calls('GET',BASE)),count)
            self.assertEqual(self.page.locator('#card-search').input_value(),'')

    def test_compact_table_drawer_and_expiry(self):
        self.cards()
        self.assertEqual(self.page.locator('#card-list tr').count(), 1)
        self.assertNotIn('资料修订', self.page.locator('#card-list').inner_text())
        self.page.locator('[data-card-detail]').click()
        self.assertTrue(self.page.locator('#card-detail').is_visible())
        self.assertIn('资料修订', self.page.locator('#card-detail').inner_text())
        self.assertNotIn(VALUES[1], self.page.locator('#card-detail').inner_text())
        self.page.keyboard.press('Escape')
        self.assertFalse(self.page.locator('#card-detail').is_visible())
        self.assertTrue(self.page.locator('[data-card-detail]').evaluate('(n)=>n===document.activeElement'))
        self.page.locator('[data-card-edit]').click()
        self.page.locator('#card-replace-sms').check()
        self.page.locator('#card-edit-sms').fill('fixture:new-ref')
        self.page.keyboard.press('Escape')
        self.assertEqual(self.page.locator('#card-edit-sms').input_value(), '')
        self.page.locator('[data-card-detail]').click()
        self.page.evaluate("window.dispatchEvent(new Event('rf:session-expired'))")
        self.assertFalse(self.page.locator('#card-detail').is_visible())
        self.assertEqual(self.page.locator('#card-detail-content').inner_text(), '')

    def test_page_sizes_previous_and_filter_reset(self):
        self.overrides[('GET',BASE)] = (200,dict(items=[CARD],next_cursor='fixture-cursor'))
        self.cards()
        self.act(lambda:self.page.locator('#card-page-size').select_option('25'),'GET',BASE)
        self.assertIn('limit=25', self.calls('GET',BASE)[-1][3])
        self.assertTrue(self.page.locator('#card-prev').is_disabled())
        self.act('#card-next','GET',BASE)
        self.assertIn('第 2 页', self.page.locator('#card-page-label').inner_text())
        self.act('#card-prev','GET',BASE)
        self.assertNotIn('cursor=', self.calls('GET',BASE)[-1][3])
        self.act('#card-next','GET',BASE)
        self.page.locator('#card-search').fill('changed')
        self.assertTrue(self.page.locator('#card-prev').is_disabled())
        self.assertTrue(self.page.locator('#card-next').is_disabled())
        self.act('#card-search-submit','GET',BASE)
        self.assertIn('第 1 页', self.page.locator('#card-page-label').inner_text())
        self.act(lambda:self.page.locator('#card-page-size').select_option('100'),'GET',BASE)
        self.assertIn('limit=100', self.calls('GET',BASE)[-1][3])

    def test_hundred_rows_mobile_scroll_and_sticky_header(self):
        import uuid
        self.overrides[('GET',BASE)] = (200,dict(items=[dict(CARD,id=str(uuid.uuid4()),alias='fixture-long-alias-'+('x'*40)+str(i)) for i in range(100)],next_cursor=None))
        self.cards()
        self.assertEqual(self.page.locator('#card-list tr').count(),100)
        self.assertLessEqual(self.page.locator('#card-list tr').first.bounding_box()['height'],48)
        self.assertEqual(self.page.locator('.card-table th').first.evaluate('(n)=>getComputedStyle(n).position'),'sticky')
        for width in (375,1440):
            self.page.set_viewport_size(dict(width=width,height=900))
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)
            self.page.locator('[data-card-detail]').first.click()
            self.assertLessEqual(self.page.locator('#card-detail').bounding_box()['width'],width)
            self.page.locator('#card-detail-close').click()
        self.assertEqual(self.calls('PATCH',BASE+'/'+pool.CID),[])
