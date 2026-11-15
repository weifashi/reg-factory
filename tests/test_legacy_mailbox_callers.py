"""Offline caller tests: compile real functions without importing service config.

The AST loader strips only decorators/module startup, not function bodies. This
keeps browser, HTTP and process transports absent and all credentials fictional.
"""
import ast
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from common import emails, managed_mailboxes

ROOT = Path(__file__).resolve().parents[1]
MAIL = 'managed@offline.invalid'
OTHER = 'legacy@offline.invalid'
OWNER = '00000000-0000-4000-8000-000000000001'


def functions(path, names, **scope):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    selected = []
    for name in names:
        matches = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        if len(matches) != 1:
            raise AssertionError(f'expected one function: {name}')
        node = matches[0]
        node.decorator_list = []
        selected.append(node)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    ast.fix_missing_locations(module)
    scope.update({'MailboxBoundaryError': managed_mailboxes.MailboxBoundaryError,
                  'mailbox_source_root': managed_mailboxes.mailbox_source_root,
                  'management_lock': managed_mailboxes.management_lock})
    exec(compile(module, str(ROOT / path), 'exec'), scope)
    return scope


class CallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='rf-caller-fixture-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pool = self.root / 'emails.txt'
        self.pool.write_text(f'{MAIL}----fixture-password\n{OTHER}----fixture-other\n')
        self.patch = patch.object(emails, 'EMAILS_FILE', str(self.pool))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        env = patch.dict(os.environ, {'REG_FACTORY_DATA_DIR': str(self.root),
                                     'REG_FACTORY_ENV_FILE': str(self.root / '.env'),
                                     'TOKEN_OUTPUT_DIR': 'tokens'})
        env.start()
        self.addCleanup(env.stop)

    def register(self, address=MAIL):
        managed_mailboxes.register_managed_mailbox(self.root, address, OWNER)

    def reader(self):
        return functions('register.py', ['_load_used_emails', 'read_next_email_from_file'],
            os=os, _email_lock=threading.Lock(), EMAILS_FILE=str(self.pool),
            EMAILS_USED_FILE=str(self.root / 'emails_used.txt'),
            EMAILS_ERROR_FILE=str(self.root / 'emails_error.txt'))['read_next_email_from_file']

    def test_fallback_skips_managed_and_records_before_delivery(self):
        self.register()
        result = self.reader()()
        self.assertEqual(result[0], OTHER)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)
        self.assertNotIn(MAIL, (self.root / 'emails_used.txt').read_text())

    def test_fallback_failed_tombstone_neither_returns_nor_advances_old_ledger(self):
        with managed_mailboxes.management_lock(self.root):
            pass
        with patch.object(managed_mailboxes, '_write', side_effect=OSError('fixture failure')):
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                self.reader()()
        self.assertFalse((self.root / 'emails_used.txt').exists())

    def test_fallback_source_alias_uses_physical_root(self):
        self.register()
        alias_root = self.root / 'alias'
        alias_root.mkdir(mode=0o700)
        alias = alias_root / 'emails.txt'
        alias.symlink_to(self.pool)
        fn = self.reader()
        fn.__globals__['EMAILS_FILE'] = str(alias)
        self.assertEqual(fn()[0], OTHER)

    def test_fallback_hardlink_cannot_create_an_independent_ownership_registry(self):
        self.register()
        alias_root = self.root / 'hardlink'
        alias_root.mkdir(mode=0o700)
        alias = alias_root / 'emails.txt'
        os.link(self.pool, alias)
        fn = self.reader()
        fn.__globals__['EMAILS_FILE'] = str(alias)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            fn()
        self.assertFalse((self.root / 'emails_used.txt').exists())

    def tri(self):
        return functions('register_three_platforms.py', ['process_account'], datetime=datetime,
            asyncio=asyncio, build_command=Mock(), broker_release=Mock(),
            platform_retry_count=Mock(), run_platform=AsyncMock())['process_account']

    def test_tri_boundary_rejection_stops_before_child(self):
        self.register()
        fn = self.tri()
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(fn((MAIL, 'fixture', '', ''), SimpleNamespace(platforms=[], parallel=False, broker=None), {}))
        fn.__globals__['build_command'].assert_not_called()

    def test_tri_keeps_nonboundary_warning_tolerance(self):
        fn = self.tri()
        with patch.object(emails, 'mark_registration_started', side_effect=RuntimeError('old journal failure')):
            self.assertEqual(asyncio.run(fn((OTHER, '', '', ''), SimpleNamespace(platforms=[], parallel=False, broker=None), {})), [])

    def http(self):
        client = Mock()
        return functions('register_claude_http.py', ['register_one'],
            ClaudeProtocolClient=Mock(return_value=client), poll_outlook_magic_link=Mock(return_value='fixture-link'),
            save_protocol_session=Mock(return_value='fixture-session'),
            ClaudeProtocolError=RuntimeError)['register_one']

    def test_http_boundary_rejection_stops_before_client_or_mail_send(self):
        self.register()
        fn = self.http()
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            fn(MAIL, refresh_token='fixture-token')
        fn.__globals__['ClaudeProtocolClient'].assert_not_called()

    def test_http_explicit_address_without_graph_token_still_cannot_send_to_managed(self):
        self.register()
        fn = self.http()
        try:
            fn(MAIL)
        except managed_mailboxes.MailboxBoundaryError:
            pass
        except RuntimeError:
            self.fail('protocol guard ran only after provider calls')
        else:
            self.fail('managed address must be rejected')
        fn.__globals__['ClaudeProtocolClient'].assert_not_called()

    def test_http_keeps_nonboundary_warning_tolerance(self):
        fn = self.http()
        with patch.object(emails, 'mark_registration_started', side_effect=RuntimeError('old journal failure')), \
                patch.object(emails, 'mark_used'):
            self.assertEqual(fn(OTHER, refresh_token='fixture-token'), 'fixture-session')

    def browser_worker(self, address):
        class PastGuard(RuntimeError):
            pass
        fake_os = SimpleNamespace(environ=SimpleNamespace(get=Mock(side_effect=PastGuard())))
        fn = functions('register.py', ['run_worker'], total=1, BitBrowser=Mock(),
            email_list=[(address, 'fixture', '', '')] if address else [], use_temp_email=False,
            datetime=datetime, os=fake_os)['run_worker']
        return fn, PastGuard

    def test_browser_boundary_rejection_not_swallowed(self):
        self.register()
        fn, past_guard = self.browser_worker(MAIL)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            try:
                asyncio.run(fn(1, SimpleNamespace(worker_id='fixture', slot=1)))
            except past_guard:
                self.fail('caller swallowed ownership rejection and continued')

    def test_browser_empty_address_retains_later_self_registration_fallback(self):
        fn, past_guard = self.browser_worker('')
        with self.assertRaises(past_guard):
            asyncio.run(fn(1, SimpleNamespace(worker_id='fixture', slot=1)))

    def test_browser_keeps_nonboundary_warning_tolerance(self):
        fn, past_guard = self.browser_worker(OTHER)
        with patch.object(emails, 'mark_registration_started', side_effect=RuntimeError('old journal failure')):
            with self.assertRaises(past_guard):
                asyncio.run(fn(1, SimpleNamespace(worker_id='fixture', slot=1)))

    def k12(self):
        def normalize(text):
            return ([{'id': str(index), 'email': line.split('----')[0],
                      'password': 'fixture', 'status': 'free'}
                     for index, line in enumerate(text.splitlines()) if '@' in line], [])
        names = ['_configure_shared_email_pool', '_shared_email_records', '_refresh_shared_email_pool',
                 '_reserve_shared_email', 'import_factory_emails', 'create_tasks', 'retry_task',
                 'repair_inactive_at', 'export_data', 'split_emails', '_alias_email',
                 '_run_task_limited', 'check_task_tokens']
        tree = ast.parse((ROOT / 'k12/server.py').read_text())
        for optional in ('_shared_email_records_with_errors', '_record_shared_email_use',
                         '_deliver_shared_email_records', '_reserve_shared_emails', '_task_has_credentials'):
            if any(isinstance(node, ast.FunctionDef) and node.name == optional for node in tree.body):
                names.append(optional)
        return functions('k12/server.py', names, os=os, Path=Path, MAIN_EMAILS_PATH=self.pool,
            _normalize_records=normalize, EMAILS={}, TASKS={}, CONFIG={}, _persist=AsyncMock(),
            asyncio=SimpleNamespace(create_task=Mock()), _new_task=Mock(return_value={'id': 'created'}),
            _run_task=Mock(), _public_task=lambda value: value, _now=lambda: 'fixture-now',
            json=json, time=time, Response=Mock(side_effect=lambda **kw: SimpleNamespace(**kw)),
            secrets=SimpleNamespace(token_hex=lambda _: 'fixture-alias'),
            TEMP_ROOT=self.root / 'child-input', RUNTIME_ROOT=self.root / 'child-runtime',
            _account_line=Mock(side_effect=AssertionError('credential sink reached before ownership guard')),
            _token_for_email=Mock(return_value='fixture-token'), _jwt_expired=Mock(return_value=False))

    def test_k12_shared_copy_filters_managed_and_persists_exposure(self):
        self.register()
        ns = self.k12()
        records = ns['_shared_email_records']()
        self.assertEqual([item['email'] for item in records], [OTHER])
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)

    def test_k12_copy_failure_does_not_mutate_pool_or_persist(self):
        ns = self.k12()
        with managed_mailboxes.management_lock(self.root):
            pass
        with patch.object(managed_mailboxes, '_write', side_effect=OSError('fixture failure')):
            with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                ns['_refresh_shared_email_pool']()
        self.assertEqual(ns['EMAILS'], {})
        ns['_persist'].assert_not_called()

    def test_k12_factory_import_uses_same_filter_and_reservation(self):
        self.register()
        ns = self.k12()
        result = asyncio.run(ns['import_factory_emails']())
        self.assertEqual(result['added'], 1)
        self.assertEqual([row['email'] for row in ns['EMAILS'].values()], [OTHER])
        self.assertEqual(next(iter(ns['EMAILS'].values()))['pool_source'], 'main')
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)

    def test_k12_old_reserved_flag_cannot_bypass_current_ownership(self):
        self.register()
        ns = self.k12()
        for reserved in (False, True):
            record = {'email': MAIL, 'password': 'fixture', 'pool_source': 'main', 'shared_reserved': reserved}
            with self.subTest(reserved=reserved), self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                ns['_reserve_shared_email'](record)
            self.assertEqual(record['shared_reserved'], reserved)

    def test_k12_old_private_copy_cannot_start_or_retry_managed_email(self):
        self.register()
        for retry in (False, True):
            ns = self.k12()
            ns['_refresh_shared_email_pool'] = Mock(return_value=0)
            ns['EMAILS']['fixture'] = {'id': 'fixture', 'email': MAIL, 'password': 'fixture', 'status': 'free'}
            ns['TASKS']['old'] = {'id': 'old', 'email_id': 'fixture', 'status': 'failed'}
            with self.subTest(retry=retry), self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                if retry:
                    asyncio.run(ns['retry_task']('old'))
                else:
                    asyncio.run(ns['create_tasks'](SimpleNamespace(json=AsyncMock(return_value={'count': 1}))))
            ns['_new_task'].assert_not_called()
            ns['asyncio'].create_task.assert_not_called()
            ns['_persist'].assert_not_called()

    def test_k12_batch_boundary_failure_happens_before_any_task_is_created(self):
        self.register()
        ns = self.k12()
        ns['_refresh_shared_email_pool'] = Mock(return_value=0)
        for address in (OTHER, MAIL):
            ns['EMAILS'][address] = {'id': address, 'email': address,
                                   'status': 'free', 'pool_source': 'main'}
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(ns['create_tasks'](SimpleNamespace(json=AsyncMock(return_value={'count': 2}))))
        ns['_new_task'].assert_not_called()
        ns['asyncio'].create_task.assert_not_called()
        self.assertTrue(all(row['status'] == 'free' for row in ns['EMAILS'].values()))
        self.assertFalse((self.root / 'emails_used_k12.txt').exists())
        self.assertFalse((self.root / 'runtime' / 'state' / 'outlook_registration_emails.txt').exists())

    def test_k12_nonboundary_journal_failure_and_private_records_remain_usable(self):
        ns = self.k12()
        with patch.object(emails, 'mark_registration_started', side_effect=RuntimeError('old journal failure')) as reserve:
            ns['_reserve_shared_email']({'email': OTHER, 'pool_source': 'main'})
            self.assertEqual(reserve.call_count, 1)
            ns['_reserve_shared_email']({'email': OTHER})
            self.assertEqual(reserve.call_count, 1)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)

    def test_k12_shared_pool_alias_uses_physical_source_root(self):
        self.register()
        alias_root = self.root / 'alias'
        alias_root.mkdir(mode=0o700)
        alias = alias_root / 'emails.txt'
        alias.symlink_to(self.pool)
        ns = self.k12()
        ns['MAIN_EMAILS_PATH'] = alias
        self.assertEqual([row['email'] for row in ns['_shared_email_records']()], [OTHER])

    def test_k12_shared_pool_hardlink_cannot_bypass_ownership(self):
        self.register()
        alias_root = self.root / 'hardlink'
        alias_root.mkdir(mode=0o700)
        alias = alias_root / 'emails.txt'
        os.link(self.pool, alias)
        ns = self.k12()
        ns['MAIN_EMAILS_PATH'] = alias
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            ns['_shared_email_records']()

    def test_k12_repair_preflights_entire_batch_before_tasks_or_old_journal(self):
        self.register()
        ns = self.k12()
        for address in (OTHER, MAIL):
            ns['EMAILS'][address] = {'id': address, 'email': address, 'status': 'failed', 'pool_source': 'main'}
            ns['TASKS'][address] = {'id': address, 'email_id': address, 'at_status': 'inactive'}
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(ns['repair_inactive_at'](SimpleNamespace(json=AsyncMock(return_value={}))))
        ns['_new_task'].assert_not_called()
        ns['asyncio'].create_task.assert_not_called()
        ns['_persist'].assert_not_called()
        self.assertFalse((self.root / 'emails_used_k12.txt').exists())

    def test_k12_export_refuses_managed_cached_password_before_response(self):
        self.register()
        ns = self.k12()
        ns['EMAILS']['cached'] = {'id': 'cached', 'email': MAIL, 'password': 'fixture'}
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(ns['export_data']())
        ns['Response'].assert_not_called()

    def test_k12_export_of_unmanaged_records_stays_usable_and_prevents_adoption(self):
        ns = self.k12()
        ns['EMAILS']['cached'] = {'id': 'cached', 'email': OTHER, 'password': 'fixture'}
        result = asyncio.run(ns['export_data']())
        self.assertEqual(json.loads(result.content)['emails'], list(ns['EMAILS'].values()))
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            self.register(OTHER)

    def test_k12_split_cannot_copy_managed_parent_credentials(self):
        self.register()
        ns = self.k12()
        ns['EMAILS']['cached'] = {'id': 'cached', 'email': MAIL, 'password': 'fixture'}
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(ns['split_emails'](SimpleNamespace(json=AsyncMock(return_value={'ids': ['cached'], 'count': 1}))))
        self.assertEqual(set(ns['EMAILS']), {'cached'})
        ns['_persist'].assert_not_called()

    def test_k12_alias_credential_lineage_cannot_bypass_managed_parent(self):
        self.register()
        ns = self.k12()
        parent = {'id': 'first', 'email': 'managed+k121@offline.invalid', 'parent_email': MAIL}
        alias = {'id': 'second', 'email': 'managed+k121+k121@offline.invalid', 'parent_email': parent['email']}
        ns['EMAILS'].update(first=parent, second=alias)
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            ns['_reserve_shared_email'](alias)

    def test_k12_raw_payload_identity_cannot_differ_from_checked_record_email(self):
        self.register()
        ns = self.k12()
        for raw in (f'{MAIL}----fixture', f'{OTHER}----fixture\n{MAIL}----fixture'):
            with self.subTest(raw_shape='single' if '\n' not in raw else 'multi'):
                with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                    ns['_reserve_shared_email']({'email': OTHER, 'raw': raw})

    def test_k12_final_input_sink_rechecks_replaced_queued_record(self):
        self.register()
        ns = self.k12()
        ns['TASKS']['queued'] = {'id': 'queued', 'status': 'queued'}
        ns['EMAILS']['replaced'] = {'id': 'replaced', 'email': MAIL, 'password': 'fixture'}
        asyncio.run(ns['_run_task_limited']('queued', 'replaced'))
        ns['_account_line'].assert_not_called()
        self.assertFalse(ns['TEMP_ROOT'].exists())
        self.assertEqual(ns['TASKS']['queued']['status'], 'failed')
        self.assertEqual(ns['EMAILS']['replaced']['status'], 'failed')
        ns['_persist'].assert_awaited_once()

    def test_k12_live_token_probe_uses_the_same_main_pool_boundary(self):
        self.register()
        ns = self.k12()
        ns['TASKS']['cached'] = {'id': 'cached', 'email': MAIL}
        ns['asyncio'].to_thread = AsyncMock(return_value={'plus_trial': 'available'})
        result = asyncio.run(ns['check_task_tokens'](SimpleNamespace(json=AsyncMock(return_value={}))))
        ns['asyncio'].to_thread.assert_not_called()
        self.assertEqual(result['inactive'], 1)

    def test_k12_empty_export_needs_no_mailbox_boundary(self):
        ns = self.k12()
        ns['MAIN_EMAILS_PATH'] = self.root / 'nonexistent-root' / 'emails.txt'
        try:
            result = asyncio.run(ns['export_data']())
        except managed_mailboxes.MailboxBoundaryError:
            self.fail('empty inventory exports no mailbox credentials to guard')
        self.assertEqual(json.loads(result.content)['emails'], [])

    def test_k12_export_cannot_hide_imported_credentials_in_task_records(self):
        self.register()
        for extra in ({'access_token': 'fixture'}, {'credentials': {'refresh_token': 'fixture'}},
                      {'raw': f'{MAIL}----fixture'}):
            ns = self.k12()
            ns['TASKS']['cached'] = {'id': 'cached', 'email': MAIL, **extra}
            with self.subTest(field=next(iter(extra))), self.assertRaises(managed_mailboxes.MailboxBoundaryError):
                asyncio.run(ns['export_data']())
            ns['Response'].assert_not_called()

    def test_k12_task_secret_without_reliable_identity_cannot_be_exported(self):
        ns = self.k12()
        ns['TASKS']['cached'] = {'id': 'cached', 'access_token': 'fixture'}
        with self.assertRaises(managed_mailboxes.MailboxBoundaryError):
            asyncio.run(ns['export_data']())

    def test_k12_nonsecret_task_history_remains_exportable_without_reservation(self):
        self.register()
        ns = self.k12()
        ns['TASKS']['cached'] = {'id': 'cached', 'email': MAIL, 'status': 'failed', 'logs': ['fixture-status']}
        result = asyncio.run(ns['export_data']())
        self.assertEqual(json.loads(result.content)['tasks'], list(ns['TASKS'].values()))


if __name__ == '__main__':
    unittest.main()
