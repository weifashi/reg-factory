"""Offline tests for real recovery/scanner consumers, not inventory-only reads."""
import os
from pathlib import Path
import tempfile
import threading
import queue
import unittest
from unittest.mock import patch

from common import asset_scanner, asset_store, managed_mailboxes, outlook_recovery

MAIL = 'managed@offline.invalid'
OTHER = 'legacy@offline.invalid'
OWNER = '00000000-0000-4000-8000-000000000001'


class ManagedMailboxConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rf-managed-consumers-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root / 'emails.txt'
        self.pool.write_text(f'{MAIL}----fixture-password\n{OTHER}----other-fixture-password\n')
        env = patch.dict(os.environ, {'REG_FACTORY_DATA_DIR': str(self.root),
            'REG_FACTORY_ENV_FILE': str(self.root / '.env'), 'TOKEN_OUTPUT_DIR': 'tokens'})
        env.start()
        self.addCleanup(env.stop)
        asset_store.invalidate_record_cache()
        asset_scanner.invalidate_report_cache()
        self.report = {'items': [{'platform': 'outlook', 'email': email,
            'status': 'unlock', 'evidence': 'fixture:unlock'} for email in (MAIL, OTHER)]}

    def register(self, email=MAIL):
        return managed_mailboxes.register_managed_mailbox(self.root, email, OWNER)

    def test_recovery_candidates_exclude_managed_and_block_later_adoption(self):
        self.register()
        with patch.object(asset_scanner, 'get_report', return_value=self.report):
            candidates = outlook_recovery.load_scan_candidates()
        self.assertEqual([item['email'] for item in candidates], [OTHER])
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)

    def test_recovery_does_not_deliver_if_exposure_write_fails(self):
        with managed_mailboxes.management_lock(self.root):
            pass
        with patch.object(asset_scanner, 'get_report', return_value=self.report), \
                patch.object(managed_mailboxes, '_write', side_effect=OSError('fixture-failure')):
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                outlook_recovery.load_scan_candidates()

    def test_recovery_cannot_overwrite_managed_credentials(self):
        self.register()
        original = self.pool.read_bytes()
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            outlook_recovery.upsert_refresh_tokens([{'email': MAIL, 'password': 'replacement-fixture',
                'refresh_token': 'fixture-new-token', 'client_id': 'fixture-client'}])
        self.assertEqual(self.pool.read_bytes(), original)

    def test_inventory_and_report_do_not_count_as_consumption(self):
        self.assertEqual(len(asset_scanner._inventory_records()), 2)
        self.assertEqual(len(asset_scanner.get_report()['items']), 2)
        self.assertEqual(asset_store.summary()['emails'], 2)
        self.register()

    def test_stale_inventory_cannot_start_provider_after_management_registration(self):
        record = next(item for item in asset_scanner._inventory_records() if item['email'] == MAIL)
        self.register()
        with patch.dict(asset_scanner._SCANNERS, {'outlook': lambda *_: self.fail('provider must not run')}):
            result = asset_scanner._scan_record(record, 5)
        self.assertEqual(result['evidence'], 'ownership:blocked')

    def test_scanner_persists_exposure_before_provider_exception(self):
        record = next(item for item in asset_scanner._inventory_records() if item['email'] == MAIL)
        def interrupted(*_):
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                self.register()
            raise RuntimeError('fixture-provider-interrupted')
        with patch.dict(asset_scanner._SCANNERS, {'outlook': interrupted}):
            asset_scanner._scan_record(record, 5)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register()

    def test_provider_work_does_not_hold_global_selection_lock(self):
        record = next(item for item in asset_scanner._inventory_records() if item['email'] == MAIL)
        output = queue.Queue()
        def provider(*_):
            def adopt_other():
                try:
                    self.register(OTHER)
                    output.put('other-managed')
                except Exception as error:
                    output.put(type(error).__name__)
            worker = threading.Thread(target=adopt_other)
            worker.start()
            try:
                self.assertEqual(output.get(timeout=5), 'other-managed')
            finally:
                worker.join(5)
            return {'status': 'normal'}
        with patch.dict(asset_scanner._SCANNERS, {'outlook': provider}):
            result = asset_scanner._scan_record(record, 5)
        self.assertEqual(result['status'], 'normal')

    def test_unidentified_provider_record_is_rejected_without_network(self):
        with patch.object(asset_scanner, '_web_session') as provider:
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                asset_scanner._scan_outlook({'_mailbox': {'refresh_token': 'fixture-rt'}}, 5)
        provider.assert_not_called()

    def test_direct_provider_handlers_refuse_managed_before_network(self):
        self.register()
        record = {'email': MAIL, 'platform': 'outlook', '_mailbox': {
            'email': MAIL, 'refresh_token': 'fixture-rt', 'client_id': 'fixture-client'},
            '_token': {'accessToken': 'fixture-token', 'sessionKey': 'fixture-cookie'}}
        calls = [lambda: asset_scanner._scan_outlook(record, 5),
                 lambda: asset_scanner._scan_chatgpt(record, 5),
                 lambda: asset_scanner._scan_claude(record, 5),
                 lambda: asset_scanner._scan_grok(record, 5),
                 lambda: asset_scanner._scan_kiro(record, 5),
                 lambda: asset_scanner._scan_chatgpt_plus_trial(record, 'fixture-token', 5),
                 lambda: asset_scanner._scan_chatgpt_coupon_trial(record, 'fixture-token', 5)]
        with patch.object(asset_scanner, '_web_session', side_effect=AssertionError('must not use network')):
            for call in calls:
                with self.subTest(call=call), self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                    call()

    def test_targeted_session_check_refuses_managed_even_with_replaced_handler(self):
        self.register()
        with patch.object(asset_scanner, '_scan_chatgpt_plus_trial') as provider:
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                asset_scanner.check_chatgpt_plus_trial_for_session({'accessToken': 'fixture-token'}, MAIL)
        provider.assert_not_called()

    def test_unsuffixed_original_registration_history_blocks_management(self):
        for name in ('emails_used.txt', 'emails_error.txt'):
            path = self.root / name
            path.write_text(f'{MAIL}----fixture-password----reserved\n')
            with self.subTest(name=name), self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                self.register()
            path.unlink()


if __name__ == '__main__':
    unittest.main()
