"""Actual disposable PG live-control transactions, using the formal live entry."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.pq import TransactionStatus

from onboarding import audit, live_commands, live_completion, live_runtime
from onboarding.errors import ErrorCode, ServiceError


class LiveCommandDBTests(unittest.TestCase):
    def setUp(self):
        from test_onboarding_live_runtime import LiveRuntimeDBTests
        self.case = LiveRuntimeDBTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def command(self, command='cancel', expected=2, key='live-control', actor=None):
        c = self.case
        return getattr(live_commands, command)(c.settings, actor or c.actor, c.tid, expected, key,
                                               policy=c.live_policy, mac=c.mac)

    def resources(self):
        return {name: self.case.read('SELECT * FROM ' + name + ' ORDER BY 1')
                for name in ('resource_leases', 'mailbox_registry', 'mailbox_platform_states', 'secret_objects')}

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.not_committed)

    def test_cancel_never_releases_or_changes_credentials_even_when_lease_expired(self):
        self.case.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        before = self.resources()
        with patch.object(live_runtime, '_final', side_effect=AssertionError('control must not claim TTL proves stopped')):
            self.command()
        self.assertEqual(self.resources(), before)
        self.assertEqual(self.case.read('SELECT status,cancel_requested,version,current_step FROM onboarding_tasks'),
                         [('PAUSED', True, 3, None)])
        self.assertEqual(self.case.read("SELECT action,phase,result_code FROM operation_receipts WHERE task_id=%s",
                                       (self.case.tid,)), [('live.command.cancel', 'SUCCEEDED', 'LIVE_COMMAND_ACCEPTED')])

    def test_pause_preserves_cancel_flag_and_both_actions_have_distinct_receipts(self):
        self.command()
        self.command('pause', expected=3)
        self.assertEqual(self.case.read('SELECT status,cancel_requested,version FROM onboarding_tasks'),
                         [('PAUSED', True, 4)])
        self.assertEqual(self.case.read('SELECT action FROM operation_receipts WHERE task_id=%s ORDER BY action',
                                       (self.case.tid,)), [('live.command.cancel',), ('live.command.pause',)])

    def test_same_request_replay_and_changed_body_are_distinct(self):
        first = self.command()
        before = self.case.business_snapshot()
        self.assertEqual(self.command(), first)
        self.assertEqual(self.case.business_snapshot(), before)
        self.reject(lambda: self.command(expected=3), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.case.business_snapshot(), before)

    def test_concurrent_same_request_serializes_to_one_command(self):
        original, replay = self.case.race_original(
            'execution_mode FROM onboarding_tasks WHERE id=%s FOR UPDATE', self.command, self.command)
        self.assertEqual((original[0], replay[0]), ('OK', 'OK'))
        self.assertEqual(original[1], replay[1])
        self.assertEqual(self.case.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s',
                                       (self.case.tid,)), [(1,)])

    def test_stale_version_and_revoked_session_do_not_mutate(self):
        before = self.case.business_snapshot()
        self.reject(lambda: self.command(expected=1), ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.case.business_snapshot(), before)
        self.case.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                         (self.case.actor.session_id,))
        before = self.case.business_snapshot()
        self.reject(self.command, ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.case.business_snapshot(), before)

    def test_auth_change_after_audit_rolls_back_entire_command(self):
        before = self.case.business_snapshot()
        append = audit.append
        def revoke(conn, *args, **kwargs):
            result = append(conn, *args, **kwargs)
            if args[2] == 'task.cancel':
                conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                             (self.case.actor.session_id,))
            return result
        with patch.object(audit, 'append', revoke):
            self.reject(self.command, ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.case.business_snapshot(), before)

    def test_real_commit_lost_ack_has_only_readonly_receipt_replay(self):
        transaction = psycopg.Connection.transaction
        committed = []
        @contextmanager
        def lost_ack(conn, *args, **kwargs):
            with transaction(conn, *args, **kwargs) as current:
                yield current
            if conn.info.transaction_status == TransactionStatus.IDLE:
                committed.append(True)
                raise psycopg.OperationalError('test-only-live-command-lost-ack')
        with patch.object(psycopg.Connection, 'transaction', lost_ack):
            self.reject(self.command, ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(committed, [True])
        before = self.case.business_snapshot()
        result = self.command()
        self.assertEqual(result['phase'], 'SUCCEEDED')
        self.assertEqual(self.case.business_snapshot(), before)

    def test_foreign_live_creation_proof_prevents_controls(self):
        self.case.change("UPDATE operation_receipts SET action='pool.batch.create' WHERE id=%s",
                         (self.case.batch['receipt_id'],))
        before = self.case.business_snapshot()
        self.reject(self.command, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.case.business_snapshot(), before)


class LiveCommandHistoryDBTests(unittest.IsolatedAsyncioTestCase):
    """Use the actual run_managed/finalizer path; never SQL-simulate release."""
    def setUp(self):
        from test_onboarding_live_executor import ExecutorTests
        self.case = ExecutorTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.db = self.case.fixture_case
        self.version = self.db.read('SELECT version FROM onboarding_tasks WHERE id=%s', (self.case.tid,))[0][0]

    def command(self, *, expected=None, key='history:cancel', actor=None):
        c = self.case
        return live_commands.cancel(c.settings, actor or c.actor, c.tid,
            self.version if expected is None else expected, key, policy=self.db.live_policy, mac=self.db.mac)

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.not_committed)

    async def stop(self):
        original = self.command()
        result = await live_completion.run_managed(self.case.executor())
        self.assertEqual(result.completion.code, 'CANCELLED_SAFE_RELEASED')
        self.assertTrue(result.completion.holds_released)
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.db.read("SELECT usage_status,last_task_id FROM mailbox_platform_states WHERE platform='google'"),
                         [('HISTORY_UNRECONCILED', None)])
        self.assertEqual(self.db.read('SELECT task_id,owner_id,lease_until,hold_reason FROM resource_leases'),
                         [(None, None, None, None)])
        self.assertEqual(self.case.seen, [])
        return original

    async def test_cancel_receipt_replays_after_real_managed_safe_release(self):
        original = await self.stop()
        before = self.db.business_snapshot()
        with patch.object(live_runtime, '_graph', side_effect=AssertionError('historical replay cannot inspect held graph')):
            self.assertEqual(self.command(), original)
            self.reject(lambda: self.command(expected=self.version + 1), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.db.business_snapshot(), before)
        # A new key is not observation and still rejects the released graph.
        version = self.db.read('SELECT version FROM onboarding_tasks WHERE id=%s', (self.case.tid,))[0][0]
        self.reject(lambda: self.command(expected=version, key='history:new-command'), ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.db.business_snapshot(), before)

    async def test_old_cancel_replay_cannot_mutate_a_new_formal_batch_hold(self):
        original = await self.stop()
        batch = self.db.call(self.db.cfg, [self.db.resource], request_key='history:next-batch')
        self.assertNotEqual(batch['task_ids'][0], self.case.tid)
        before = self.db.business_snapshot()
        self.assertEqual(self.command(), original)
        self.assertEqual(self.db.business_snapshot(), before)
        self.assertEqual(str(self.db.read('SELECT task_id FROM resource_leases')[0][0]), batch['task_ids'][0])

    async def test_historical_replay_requires_owner_provenance_and_current_manage_auth(self):
        await self.stop()
        self.db.change("UPDATE operation_receipts SET action='pool.batch.create' WHERE id=%s",
                       (self.db.batch['receipt_id'],))
        before = self.db.business_snapshot()
        self.reject(self.command, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.db.business_snapshot(), before)
        self.db.change("UPDATE operation_receipts SET action='live.batch.create_one' WHERE id=%s",
                       (self.db.batch['receipt_id'],))
        self.db.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                       (self.case.actor.session_id,))
        before = self.db.business_snapshot()
        self.reject(self.command, ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.db.business_snapshot(), before)


if __name__ == '__main__':
    unittest.main()
