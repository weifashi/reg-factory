"""Offline ownership boundary tests; all addresses/material are generated fixtures."""
from contextlib import contextmanager
import importlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from common import asset_store, emails

OWNER = '00000000-0000-4000-8000-000000000001'
OTHER_OWNER = '00000000-0000-4000-8000-000000000002'
MAIL = 'managed@offline.invalid'
OTHER = 'legacy@offline.invalid'


def _register_process(root, output, started=None):
    from common.managed_mailboxes import register_managed_mailbox
    if started is not None:
        started.set()
    try:
        register_managed_mailbox(Path(root), MAIL, OWNER)
        output.put('managed')
    except Exception as error:
        output.put(type(error).__name__)


def _claim_process(root, output):
    os.environ['REG_FACTORY_DATA_DIR'] = root
    try:
        output.put(asset_store.get_email(claim_once=True)['email'])
    except Exception as error:
        output.put(type(error).__name__)


def _delayed_export(root, entered, release, output):
    os.environ['REG_FACTORY_DATA_DIR'] = root
    original = asset_store._write_claims
    def delayed(value):
        entered.set()
        if not release.wait(60):
            raise RuntimeError('offline synchronization timeout')
        return original(value)
    with patch.object(asset_store, '_write_claims', delayed):
        try:
            output.put(asset_store.export_batch('emails')[0]['email'])
        except Exception as error:
            output.put(type(error).__name__)


class ManagedMailboxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('common.managed_mailboxes'),
                             'shared ownership boundary must exist')
        self.subject = importlib.import_module('common.managed_mailboxes')
        self.temp = tempfile.TemporaryDirectory(prefix='rf-managed-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root / 'emails.txt'
        self.pool.write_text(f'{MAIL}----fixture-password\n{OTHER}----other-fixture-password\n')
        self.state = self.root / 'runtime' / 'state'
        self.env = patch.dict(os.environ, {'REG_FACTORY_DATA_DIR': str(self.root),
            'REG_FACTORY_ENV_FILE': str(self.root / '.env'), 'TOKEN_OUTPUT_DIR': 'tokens'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pool_patch = patch.object(emails, 'EMAILS_FILE', str(self.pool))
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        asset_store.invalidate_record_cache()
        self.addCleanup(asset_store.invalidate_record_cache)

    def register(self, email=MAIL, owner=OWNER):
        return self.subject.register_managed_mailbox(self.root, email, owner)

    def test_batch_filter_reads_registry_once_and_preserves_unidentified_records(self):
        self.register()
        records = [{'email': MAIL}, {'email': OTHER}] * 100 + [{'source': 'fixture-cookie'}]
        with self.subject.management_lock(self.root) as boundary:
            with patch.object(boundary, '_load', wraps=boundary._load) as load:
                selected = boundary.filter_unmanaged(records, lambda row: row.get('email'))
            self.assertEqual(load.call_count, 1)
            self.assertEqual(selected, [{'email': OTHER}] * 100 + [records[-1]])

    def test_asset_filter_and_direct_claim_do_not_reload_registry_per_candidate(self):
        self.register()
        records = [{'email': MAIL}, {'email': OTHER}] * 100
        with self.subject.management_lock(self.root) as boundary:
            with patch.object(boundary, '_load', wraps=boundary._load) as load:
                selected = asset_store._legacy_records(records, lambda row: row['email'])
            self.assertEqual(load.call_count, 1)
            self.assertEqual(selected, [{'email': OTHER}] * 100)
            with patch.object(boundary, '_load', wraps=boundary._load) as load:
                selected = asset_store._claim_record(records, 'chatgpt',
                    lambda row: asset_store._asset_identity(row['email'], ''), None)
            self.assertEqual(selected[1]['email'], OTHER)
            self.assertEqual(load.call_count, 2, 'one filter read plus final durable delivery read')

    def test_batch_filter_rejects_corrupt_state_instead_of_reusing_a_snapshot(self):
        with self.subject.management_lock(self.root) as boundary:
            selected = boundary.filter_unmanaged([{'email': OTHER}], lambda row: row['email'])
            self.assertEqual(len(selected), 1)
            boundary.path.write_text('{broken')
            with self.assertRaises(self.subject.MailboxBoundaryError):
                boundary.filter_unmanaged([{'email': OTHER}], lambda row: row['email'])

    def test_batch_snapshot_never_authorizes_delivery_after_state_damage(self):
        with self.subject.management_lock(self.root) as boundary:
            original_state = boundary.path.read_bytes()
            original_filter = getattr(boundary, 'filter_unmanaged', None)
            self.assertTrue(callable(original_filter))
            for removed in (False, True):
                for claim in (False, True):
                    with self.subTest(removed=removed, claim=claim):
                        boundary.path.write_bytes(original_state)
                        boundary.path.chmod(0o600)
                        def damaged(*args, **kwargs):
                            result = original_filter(*args, **kwargs)
                            if removed:
                                boundary.path.unlink()
                            else:
                                boundary.path.write_text('{broken')
                            return result
                        with patch.object(boundary, 'filter_unmanaged', side_effect=damaged) as filtered:
                            with self.assertRaises(self.subject.MailboxBoundaryError):
                                if claim:
                                    asset_store._claim_record([{'email': OTHER}], 'chatgpt',
                                        lambda row: asset_store._asset_identity(row['email'], ''), None)
                                else:
                                    asset_store.get_email()
                        self.assertEqual(filtered.call_count, 1)
                        self.assertFalse((self.state / 'asset_api_cursors.json').exists())
                        self.assertFalse((self.state / 'asset_api_claims.json').exists())

    def test_lock_inode_is_initialized_before_entering_existing_file_lock(self):
        original = self.subject.file_lock
        @contextmanager
        def checked(target):
            self.assertEqual(Path(str(target) + '.lock').read_bytes(), b'\0')
            with original(target):
                yield
        with patch.object(self.subject, 'file_lock', checked):
            self.register()

    def test_registration_is_idempotent_owner_bound_secret_free_and_not_usage(self):
        original = self.pool.read_bytes()
        self.register()
        self.register(MAIL.upper(), OWNER)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register(owner=OTHER_OWNER)
        with self.subject.management_lock(self.root) as boundary:
            self.assertTrue(boundary.is_managed(MAIL.upper()))
            self.assertFalse(boundary.is_managed(OTHER))
        document = (self.state / 'managed_mailbox_ownership.json').read_text()
        for sensitive in (MAIL, 'fixture-password', 'reserved', 'qualified', 'UNUSED'):
            self.assertNotIn(sensitive, document)
        self.assertEqual(self.pool.read_bytes(), original)
        self.assertEqual(list(self.root.glob('emails_used_*.txt')), [])
        self.assertFalse((self.state / 'outlook_registration_emails.txt').exists())

    def test_next_and_latest_skip_managed_without_touching_its_history(self):
        for function in (emails.next_email, emails.latest_email):
            with self.subTest(function=function.__name__):
                self.register()
                used = self.root / f'emails_used_{function.__name__}.txt'
                chosen = function(function.__name__)
                if chosen is not None:  # prior test iteration may reserve OTHER globally
                    self.assertEqual(chosen[0], OTHER)
                self.assertNotIn(MAIL, used.read_text() if used.exists() else '')

    def test_explicit_registration_and_completion_refuse_managed(self):
        self.register()
        for function, args in ((emails.mark_registration_started, ('chatgpt', MAIL)),
                               (emails.mark_used, ('chatgpt', MAIL)),
                               (emails.mark_error, ('chatgpt', MAIL))):
            with self.subTest(function=function.__name__):
                with self.assertRaises(self.subject.MailboxBoundaryError):
                    function(*args)
        self.assertEqual(list(self.root.glob('emails_used_*.txt')), [])
        self.assertFalse((self.state / 'outlook_registration_emails.txt').exists())

    def test_retry_does_not_bypass_managed(self):
        self.register()
        (self.root / 'emails_error_chatgpt.txt').write_text(
            f'{MAIL}----fixture-password----email_submit_stuck\n')
        self.assertIsNone(emails.retryable_email('chatgpt'))
        self.assertFalse((self.root / 'emails_used_chatgpt.txt').exists())

    def test_get_email_modes_and_batch_never_return_managed(self):
        self.register()
        for kwargs in ({'index': 0}, {'claim_once': True}, {'index': 0, '_defer_claim': True}):
            self.assertEqual(asset_store.get_email(**kwargs)['email'], OTHER)
        self.assertEqual([r['email'] for r in asset_store.export_batch('emails', include_claimed=True)],
                         [OTHER])

    def test_direct_claim_record_filters_managed_and_retains_unmanaged(self):
        self.register()
        selected = asset_store._claim_record([{'email': MAIL}, {'email': OTHER}], 'chatgpt',
            lambda record: asset_store._asset_identity(record['email'], ''), None)
        self.assertEqual(selected[1]['email'], OTHER)

    def test_platform_delivery_and_email_four_never_leak_managed_mailbox(self):
        self.register()
        directory = self.root / 'tokens' / 'chatgpt'
        directory.mkdir(parents=True)
        for address in (MAIL, OTHER):
            (directory / f'{address}.session.json').write_text(json.dumps({'email': address}))
        for fmt in ('session', 'email_four'):
            self.assertEqual(asset_store.get_platform_asset('chatgpt', fmt, index=0)['email'], OTHER)
        self.assertEqual([r['email'] for r in asset_store.export_batch('chatgpt', 'session')], [OTHER])

    def test_legacy_delivery_is_permanent_registration_barrier_after_cursor_reset(self):
        self.assertEqual(asset_store.get_email(index=0)['email'], MAIL)
        asset_store.reset_cursor()
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()
        # Legacy cross-platform reuse remains permitted, not an invented global used state.
        self.assertEqual(asset_store.get_email(index=0)['email'], MAIL)
        self.assertEqual(emails.next_email('chatgpt')[0], MAIL)

    def test_existing_used_error_and_exclusion_history_reject_registration(self):
        paths = ['emails_used_chatgpt.txt', 'emails_error_google.txt',
                 'runtime/state/outlook_sale_emails.txt',
                 'runtime/state/outlook_registration_emails.txt']
        for relative in paths:
            with self.subTest(path=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'{MAIL}----fixture-password----reserved\n')
                with self.assertRaises(self.subject.MailboxBoundaryError):
                    self.register()
                path.unlink()

    def test_existing_claim_hash_rejects_registration_in_every_scope(self):
        self.state.mkdir(parents=True)
        claim = asset_store._claim_id('chatgpt', asset_store._asset_identity(MAIL, ''))
        (self.state / 'asset_api_claims.json').write_text(json.dumps(
            {'version': 1, 'scopes': {'chatgpt': [claim]}}))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_unknown_source_claim_or_advanced_cursor_rejects_registration(self):
        self.state.mkdir(parents=True)
        path = self.state / 'asset_api_cursors.json'
        path.write_text(json.dumps({'email': 1}))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()
        path.unlink()
        (self.state / 'asset_api_claims.json').write_text(json.dumps(
            {'version': 1, 'scopes': {'chatgpt': ['0' * 64]}}))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_damaged_history_is_not_treated_as_empty(self):
        self.state.mkdir(parents=True)
        paths = [self.state / 'asset_api_claims.json', self.state / 'asset_api_cursors.json',
                 self.root / 'emails_used_chatgpt.txt']
        for path in paths:
            path.write_text('not-valid-state')
            with self.subTest(path=path.name), self.assertRaises(self.subject.MailboxBoundaryError):
                self.register()
            path.unlink()

    def test_corrupt_or_removed_ownership_state_fails_closed_for_legacy_and_register(self):
        self.register()
        path = self.state / 'managed_mailbox_ownership.json'
        for corrupt in ('{broken', '{}', '{"version":1,"entries":[]}'):
            path.write_text(corrupt)
            for call in (lambda: emails.next_email('chatgpt'), lambda: asset_store.get_email(index=0),
                         lambda: asset_store.export_batch('emails'), self.register):
                with self.assertRaises(self.subject.MailboxBoundaryError):
                    call()
        path.unlink()
        with self.assertRaises(self.subject.MailboxBoundaryError):
            asset_store.get_email(index=0)

    def test_symlink_state_is_rejected_without_reading_or_replacing_target(self):
        self.register()
        path = self.state / 'managed_mailbox_ownership.json'
        path.unlink()
        outside = self.root / 'unrelated'
        outside.write_text('untouched')
        path.symlink_to(outside)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            asset_store.get_email(index=0)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_require_owner_is_read_only_and_never_registers_missing_mailbox(self):
        with self.subject.management_lock(self.root) as boundary:
            self.assertTrue(callable(getattr(boundary, 'require_owner', None)))
            before = boundary.path.read_bytes()
            with self.assertRaises(self.subject.MailboxBoundaryError):
                boundary.require_owner(MAIL, OWNER)
            self.assertEqual(boundary.path.read_bytes(), before)
        self.register()
        with self.subject.management_lock(self.root) as boundary:
            before = boundary.path.read_bytes()
            boundary.require_owner(MAIL.upper(), OWNER)
            with self.assertRaises(self.subject.MailboxBoundaryError):
                boundary.require_owner(MAIL, OTHER_OWNER)
            self.assertEqual(boundary.path.read_bytes(), before)

    def test_boundary_cannot_be_used_from_another_thread_without_lock(self):
        output = queue.Queue()
        with self.subject.management_lock(self.root) as boundary:
            def attempt():
                try:
                    boundary.register(MAIL, OWNER)
                    output.put('unexpected-managed')
                except Exception as error:
                    output.put(type(error).__name__)
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(output.get(timeout=1), 'MailboxBoundaryError')

    def test_symlink_platform_directory_is_unknown_not_absent(self):
        external = self.root / 'external-tokens'
        external.mkdir()
        (external / 'generated.session.json').write_text(json.dumps({'email': MAIL}))
        (self.root / 'tokens').mkdir()
        (self.root / 'tokens' / 'chatgpt').symlink_to(external, target_is_directory=True)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_legacy_source_alias_uses_actual_pool_root_not_alias_parent(self):
        self.register()
        alias = self.root / 'alias'
        alias.mkdir()
        (alias / 'emails.txt').symlink_to(self.pool)
        with patch.object(emails, 'EMAILS_FILE', str(alias / 'emails.txt')):
            self.assertEqual(emails.next_email('chatgpt')[0], OTHER)

    def test_hard_link_source_cannot_create_a_second_ownership_root(self):
        self.register()
        alias = self.root / 'hardlink-alias'
        alias.mkdir(mode=0o700)
        linked = alias / 'emails.txt'
        os.link(self.pool, linked)
        self.assertEqual(linked.stat().st_nlink, 2)
        with patch.object(emails, 'EMAILS_FILE', str(linked)):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                emails.next_email('chatgpt')
        with patch.dict(os.environ, {'REG_FACTORY_DATA_DIR': str(alias)}):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_email(index=0)
        self.assertFalse((alias / '.managed-mailbox-boundary.lock').exists())

    def test_public_source_root_helper_normalizes_symlink_and_rejects_hard_link(self):
        helper = getattr(self.subject, 'mailbox_source_root', None)
        self.assertTrue(callable(helper), 'all consumers must share the same source-path check')
        alias = self.root / 'helper-alias'
        alias.mkdir(mode=0o700)
        symbolic = alias / 'symbolic.txt'
        symbolic.symlink_to(self.pool)
        self.assertEqual(helper(symbolic), self.root)
        self.assertEqual(helper(alias / 'absent.txt'), alias)
        hard = alias / 'hard.txt'
        os.link(self.pool, hard)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            helper(hard)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            helper(self.pool)

    def test_asset_mailbox_file_alias_cannot_bypass_actual_root_ownership(self):
        self.register()
        alias = self.root / 'asset-alias'
        alias.mkdir(mode=0o700)
        (alias / 'emails.txt').symlink_to(self.pool)
        with patch.dict(os.environ, {'REG_FACTORY_DATA_DIR': str(alias)}):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_email(index=0)

    def test_validation_provider_is_not_called_for_managed_newest_mailbox(self):
        self.register()
        self.pool.write_text(f'{MAIL}----fixture-password----fixture-rt----fixture-client\n')
        with patch('common.mailbox.check_mailbox_access', side_effect=AssertionError('must not contact provider')):
            self.assertIsNone(emails.latest_email('chatgpt', validate_token=True))

    def test_interrupted_provider_validation_retains_legacy_exposure(self):
        self.pool.write_text(f'{MAIL}----fixture-password----fixture-rt----fixture-client\n')
        with patch('common.mailbox.check_mailbox_access', side_effect=RuntimeError('fixture-interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'fixture-interrupted'):
                emails.latest_email('chatgpt', validate_token=True)
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()
        self.assertFalse((self.root / 'emails_used_chatgpt.txt').exists())

    def test_existing_platform_credentials_and_unidentifiable_cookie_block_registration(self):
        directory = self.root / 'tokens' / 'chatgpt'
        directory.mkdir(parents=True)
        token = directory / 'generated.session.json'
        token.write_text(json.dumps({'user': {'email': MAIL}, 'accessToken': 'fixture-token'}))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()
        token.unlink()
        cookies = self.root / 'cookies' / 'chatgpt'
        cookies.mkdir(parents=True)
        path = cookies / 'full_generated.json'
        path.write_text(json.dumps([{'name': 'session', 'value': 'fixture-cookie'}]))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_unknown_platform_secret_file_is_not_silently_ignored(self):
        directory = self.root / 'tokens' / 'chatgpt'
        directory.mkdir(parents=True)
        (directory / 'generated.session.json').write_text('{broken')
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_registry_write_failure_does_not_advance_implicit_email_cursor(self):
        with self.subject.management_lock(self.root):
            pass
        with patch.object(self.subject, '_write', side_effect=OSError('fixture-write-failure')):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_email()
        self.assertFalse((self.state / 'asset_api_cursors.json').exists())

    def test_registry_write_failure_does_not_advance_implicit_platform_cursor(self):
        directory = self.root / 'tokens' / 'chatgpt'
        directory.mkdir(parents=True)
        (directory / 'generated.session.json').write_text(json.dumps({'email': MAIL}))
        with self.subject.management_lock(self.root):
            pass
        with patch.object(self.subject, '_write', side_effect=OSError('fixture-write-failure')):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_platform_asset('chatgpt', 'session')
        self.assertFalse((self.state / 'asset_api_cursors.json').exists())

    def test_registry_write_failure_never_delivers_or_reserves_secret(self):
        with self.subject.management_lock(self.root):
            pass
        with patch.object(self.subject.os, 'replace', side_effect=OSError('fixture-write-failure')):
            with self.assertRaises(self.subject.MailboxBoundaryError):
                emails.next_email('chatgpt')
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_email(index=0)
        self.assertFalse((self.root / 'emails_used_chatgpt.txt').exists())
        self.assertFalse((self.state / 'asset_api_claims.json').exists())

    def test_idempotent_registration_rechecks_new_conflicting_history(self):
        self.register()
        (self.root / 'emails_used_google.txt').write_text(f'{MAIL}----fixture-password----reserved\n')
        with self.assertRaises(self.subject.MailboxBoundaryError):
            self.register()

    def test_registry_duplicate_keys_and_unknown_modes_fail_closed(self):
        self.register()
        path = self.state / 'managed_mailbox_ownership.json'
        key = next(iter(json.loads(path.read_text())['entries']))
        for raw in ('{"version":1,"version":1,"entries":{}}',
                    json.dumps({'version': 1, 'entries': {key: {'mode': 'expired'}}})):
            path.write_text(raw)
            with self.assertRaises(self.subject.MailboxBoundaryError):
                asset_store.get_email(index=0)

    def test_boundary_object_cannot_write_after_lock_release(self):
        with self.subject.management_lock(self.root) as boundary:
            self.assertFalse(boundary.is_managed(MAIL))
        with self.assertRaises(self.subject.MailboxBoundaryError):
            boundary.register(MAIL, OWNER)

    def test_known_unrelated_old_claim_does_not_prevent_management(self):
        self.state.mkdir(parents=True)
        claim = asset_store._claim_id('chatgpt', asset_store._asset_identity(OTHER, ''))
        (self.state / 'asset_api_claims.json').write_text(json.dumps(
            {'version': 1, 'scopes': {'chatgpt': [claim]}}))
        self.register()
        with self.subject.management_lock(self.root) as boundary:
            self.assertTrue(boundary.is_managed(MAIL))

    def test_nested_lock_keeps_caller_exception_type(self):
        with self.assertRaisesRegex(ValueError, 'caller-not-boundary'):
            with self.subject.management_lock(self.root):
                raise ValueError('caller-not-boundary')

    def test_cross_process_claim_and_registration_are_mutually_exclusive(self):
        self.pool.write_text(f'{MAIL}----fixture-password\n')
        ctx = multiprocessing.get_context('spawn')
        output = ctx.Queue()
        processes = [ctx.Process(target=_register_process, args=(str(self.root), output)),
                     ctx.Process(target=_claim_process, args=(str(self.root), output))]
        for process in processes:
            process.start()
            self.addCleanup(lambda p=process: p.terminate() if p.is_alive() else None)
        for process in processes:
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        outcomes = [output.get(timeout=2), output.get(timeout=2)]
        self.assertFalse('managed' in outcomes and MAIL in outcomes, outcomes)
        self.assertTrue('managed' in outcomes or MAIL in outcomes, outcomes)

    def test_delayed_export_holds_cross_process_lock_through_claim_persistence(self):
        self.pool.write_text(f'{MAIL}----fixture-password\n')
        ctx = multiprocessing.get_context('spawn')
        entered, release, started = ctx.Event(), ctx.Event(), ctx.Event()
        exports, registrations = ctx.Queue(), ctx.Queue()
        exporter = ctx.Process(target=_delayed_export,
            args=(str(self.root), entered, release, exports))
        registrar = ctx.Process(target=_register_process,
            args=(str(self.root), registrations, started))
        for process in (exporter, registrar):
            self.addCleanup(lambda p=process: p.terminate() if p.is_alive() else None)
        self.addCleanup(release.set)
        exporter.start()
        self.assertTrue(entered.wait(60))
        registrar.start()
        self.assertTrue(started.wait(60))
        with self.assertRaises(queue.Empty):
            registrations.get(timeout=0.3)
        release.set()
        for process in (exporter, registrar):
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(exports.get(timeout=2), MAIL)
        self.assertEqual(registrations.get(timeout=2), 'MailboxBoundaryError')


if __name__ == '__main__':
    unittest.main()
