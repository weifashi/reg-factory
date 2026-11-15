"""Offline live-control boundaries; no PG, secret decryption or provider I/O."""
from contextlib import contextmanager, ExitStack
import copy
import importlib
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from onboarding import live_read, live_runtime, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_input_vault import LivePoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.settings import Settings, SOCKET


class LiveCommandOfflineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_commands'),
                             'explicit v2 pause/cancel service is missing')
        self.m = importlib.import_module('onboarding.live_commands')
        self.settings = Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
            'offline-password', 'rf-onboarding-p1b-v1:offline', 'rf_onboarding')
        self.policy = LivePoolPolicy(self.settings)
        self.actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        self.mac = object.__new__(RequestMac)
        self.task = dict(id=str(uuid4()), config_id=str(uuid4()), config_revision='pool-' + str(uuid4()),
            generation=1, status='RUNNING', version=3, cancel_requested=False, reason_code='PRIOR_REASON')
        self.receipts, self.sql, self.audits = {}, [], []
        self.conn = SimpleNamespace(execute=self.execute)
        self.graph_calls = []
        self.error_after_body = None
        self.graph_error = self.history_error = self.auth_error = None
        self.history_calls, self.auth_calls = [], []

    def execute(self, query, params=()):
        self.sql.append((query, params))
        if query == 'SELECT id,execution_mode FROM onboarding_tasks WHERE id=%s FOR UPDATE':
            self.assertEqual(params, (self.task['id'],))
            row = (UUID(self.task['id']), 'live')
        elif query.startswith('SELECT ') and 'FROM operation_receipts' in query:
            tid, action, key = params
            row = self.receipts.get((action, key))
        elif query.startswith('UPDATE onboarding_tasks'):
            status, cancelled, tid, expected = params
            self.assertEqual(tid, self.task['id'])
            if expected != self.task['version']:
                row = None
            else:
                self.task.update(status=status, cancel_requested=cancelled, version=expected + 1)
                row = (UUID(tid), expected + 1, status, cancelled, self.task['generation'])
        elif query.startswith('INSERT INTO operation_receipts'):
            rid, tid, action, revision, generation, key, digest, summary = params
            row = (UUID(rid),)
            self.receipts[action, key] = (UUID(rid), UUID(tid), None, action, revision,
                generation, 0, 'SUCCEEDED', key, digest, 'LIVE_COMMAND_ACCEPTED', None, summary.obj)
        else:
            raise AssertionError('Unexpected command SQL: ' + query)
        return SimpleNamespace(fetchone=lambda: row)

    @contextmanager
    def dependencies(self):
        @contextmanager
        def uow(settings):
            before = copy.deepcopy((self.task, self.receipts, self.audits))
            try:
                yield self.conn
                if self.error_after_body:
                    raise self.error_after_body
            except BaseException:
                self.task, self.receipts, self.audits = before
                raise
        def graph(conn, actor, tid, *, sending):
            self.graph_calls.append((conn, actor, tid, sending))
            self.assertIs(sending, False)
            if self.graph_error:
                raise self.graph_error
            # Expiry is not stop proof and must not prevent requesting a pause.
            return self.task.copy(), {'lease_until': 'expired'}, []
        def history(conn, actor, tid):
            self.history_calls.append((conn, actor, tid))
            if self.history_error:
                raise self.history_error
            return self.task.copy(), 'live'
        def authenticate(conn, actor, permission):
            self.auth_calls.append(permission)
            if self.auth_error:
                raise self.auth_error
            return self.actor
        with ExitStack() as stack:
            for owner, name, value in ((storage, 'unit_of_work', uow),
                    (live_runtime, '_graph', graph),
                    (live_read, '_history', history),
                    (LivePoolPolicy, '_connection', lambda *args: None),
                    (RequestMac, '_key', lambda *args: b'm' * 32),
                    (security, 'revalidate', authenticate),
                    (self.m.audit, 'append', lambda *args, **kwargs: self.audits.append((args[1:], kwargs)) or 1),
                    (live_runtime, '_final', lambda *args: self.fail('control checked worker lease TTL'))):
                stack.enter_context(patch.object(owner, name, value))
            yield

    def call(self, command='cancel', expected=3, key='offline-control', **overrides):
        params = dict(settings=self.settings, actor=self.actor, task_id=self.task['id'],
            expected_version=expected, request_key=key, policy=self.policy, mac=self.mac)
        params.update(overrides)
        with self.dependencies():
            return getattr(self.m, command)(**params)

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.not_committed)

    def test_cancel_only_pauses_and_requests_cancel_without_releasing(self):
        result = self.call()
        self.assertEqual((self.task['status'], self.task['cancel_requested'], self.task['version']),
                         ('PAUSED', True, 4))
        self.assertEqual(self.task['reason_code'], 'PRIOR_REASON')
        self.assertEqual(result['phase'], 'SUCCEEDED')
        self.assertEqual(set(self.receipts), {('live.command.cancel', 'offline-control')})
        self.assertEqual(len(self.audits), 1)
        self.assertEqual(len(self.graph_calls), 1)
        self.assertFalse(any('UPDATE resource_leases' in query or 'UPDATE mailbox_platform_states' in query
                             for query, _ in self.sql))

    def test_pause_does_not_clear_preexisting_cancel_request(self):
        self.task['cancel_requested'] = True
        self.call('pause')
        self.assertEqual((self.task['status'], self.task['cancel_requested']), ('PAUSED', True))
        self.assertEqual(set(self.receipts), {('live.command.pause', 'offline-control')})

    def test_exact_replay_is_readonly_after_task_version_advances(self):
        first = self.call()
        self.task['version'] += 1
        before = copy.deepcopy((self.task, self.receipts, self.audits))
        self.sql.clear()
        self.assertEqual(self.call(), first)
        self.assertEqual((self.task, self.receipts, self.audits), before)
        self.assertTrue(all(query.startswith('SELECT ') for query, _ in self.sql))

    def test_finalized_exact_receipt_does_not_require_reserved_state_or_live_lease(self):
        first = self.call()
        self.task.update(status='CANCELLED_SAFE', version=5)
        self.graph_error = ServiceError(ErrorCode.VERSION_CONFLICT)
        before = copy.deepcopy((self.task, self.receipts, self.audits))
        self.graph_calls.clear(); self.sql.clear()
        try:
            observed = self.call()
        except ServiceError as exc:
            observed = exc.code
        self.assertEqual(observed, first, 'historical command replay must not demand the released execution graph')
        self.assertEqual((self.task, self.receipts, self.audits), before)
        self.assertFalse(self.graph_calls)
        self.assertEqual(len(self.history_calls), 1)
        self.assertTrue(all(query.startswith('SELECT ') for query, _ in self.sql))
        self.assertEqual(self.sql[0][0], 'SELECT id,execution_mode FROM onboarding_tasks WHERE id=%s FOR UPDATE')
        self.assertTrue(self.auth_calls and set(self.auth_calls) == {'tasks:manage'})

    def test_finalized_new_key_still_requires_original_live_graph(self):
        self.call()
        self.task.update(status='CANCELLED_SAFE', version=5)
        self.graph_error = ServiceError(ErrorCode.VERSION_CONFLICT)
        before = copy.deepcopy((self.task, self.receipts, self.audits))
        self.reject(lambda: self.call(expected=5, key='new-command'), ErrorCode.VERSION_CONFLICT)
        self.assertEqual((self.task, self.receipts, self.audits), before)
        self.assertFalse(self.history_calls)

    def test_existing_receipt_requires_current_auth_and_historical_provenance(self):
        self.call()
        self.graph_calls.clear()
        self.history_error = ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        self.reject(self.call, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertFalse(self.graph_calls, 'receipt failure must never fall back to the mutable execution graph')
        self.history_error = None
        self.auth_error = ServiceError(ErrorCode.UNAUTHENTICATED)
        self.reject(self.call, ErrorCode.UNAUTHENTICATED)
        self.assertFalse(self.graph_calls)

    def test_same_key_changed_body_is_not_replay(self):
        self.call()
        self.reject(lambda: self.call(expected=4), ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_cas_conflict_never_writes_or_marks_not_committed(self):
        for expected in (2, 4):
            with self.subTest(expected=expected):
                self.reject(lambda: self.call(expected=expected), ErrorCode.VERSION_CONFLICT)
        self.assertFalse(self.receipts or self.audits)
        self.assertFalse(any(query.startswith('UPDATE ') for query, _ in self.sql))

    def test_terminal_and_conflict_tasks_cannot_be_downgraded_to_paused(self):
        for status in ('SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE', 'CONFLICT'):
            self.task['status'] = status
            with self.subTest(status=status):
                self.reject(self.call, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertFalse(self.receipts or self.audits)

    def test_foreign_action_or_malformed_receipt_never_falls_back(self):
        self.call()
        key = ('live.command.cancel', 'offline-control')
        original = self.receipts[key]
        for index, value in ((3, 'pool.command.cancel'), (5, True), (6, False),
                             (7, 'UNKNOWN'), (10, 'COMMAND_ACCEPTED')):
            row = list(original); row[index] = value; self.receipts[key] = tuple(row)
            with self.subTest(index=index):
                self.reject(self.call, ErrorCode.RECONCILIATION_REQUIRED)
        self.receipts[key] = original

    def test_bool_summary_version_and_unknown_fields_are_rejected(self):
        self.call()
        key = ('live.command.cancel', 'offline-control')
        original = self.receipts[key]
        for changes in ({'schema_version': True}, {'accepted_version': True}, {'extra': 'private-canary'}):
            row = list(original); row[12] = {**row[12], **changes}; self.receipts[key] = tuple(row)
            with self.subTest(changes=changes):
                self.reject(self.call, ErrorCode.RECONCILIATION_REQUIRED)
        self.receipts[key] = original

    def test_invalid_inputs_stop_before_task_lookup(self):
        for changes in ({'policy': object()}, {'settings': object()}, {'mac': object()},
                        {'task_id': 'bad'}, {'expected_version': True}, {'request_key': ''}):
            with self.subTest(changes=list(changes)):
                self.reject(lambda: self.call(**changes), ErrorCode.INVALID_INPUT)
        self.reject(lambda: self.call(actor=object()), ErrorCode.UNAUTHENTICATED)
        self.assertFalse(self.graph_calls)

    def test_commit_unknown_has_no_success_return(self):
        self.error_after_body = ServiceError(ErrorCode.COMMIT_UNKNOWN)
        self.reject(self.call, ErrorCode.COMMIT_UNKNOWN)
        self.assertFalse(self.receipts or self.audits)


if __name__ == '__main__':
    unittest.main()
