"""Disposable PG composition, not a Google/Sub2API integration claim.

Source import, config, admission, both startup transactions, executor lifecycle,
stop proof, finalizer and receipt observation are real. Only SDK paths/token
and the official Billing GET are external-boundary doubles. The successful
lifecycle case parks _chain before its first external effect; it does not mock
run/heartbeat/stop/finalization. Lost-ACK cases inject an OperationalError only
AFTER the selected real psycopg transaction has committed.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import psycopg
from psycopg.pq import TransactionStatus

from onboarding import live_admission, live_commands, live_completion, live_entry, live_executor
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.live_key_store import ServiceAccountKeyStore
from onboarding.live_manager import LiveRunManager
from onboarding.sub2api_probe import ProbeConfig
from onboarding_live_support import populate
from onboarding_pool_support import PoolCase, after_query
from test_onboarding_live_config import FIELDS


class LiveManagerDBTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = PoolCase()
        self.addCleanup(self.db.doCleanups)
        self.db.setUp()
        # No pre-created batch, SQL mode promotion or synthetic worker/proof.
        populate(self.db, create=False)
        self.assertEqual(self.db.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_run_plans'), [(0,)])
        self.requests = []

    def manager(self):
        def billing(request):
            self.requests.append((request.method, str(request.url)))
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'cloudbilling.googleapis.com')
            self.assertEqual(request.url.path, '/v1/billingAccounts/' + FIELDS['billing_account'])
            return httpx.Response(200, json={
                'name': 'billingAccounts/' + FIELDS['billing_account'], 'open': True})

        db = self.db
        manager = LiveRunManager(db.settings, db.resource['id'], source=db.source,
            policy=db.live_policy, mac=db.mac, key_store=ServiceAccountKeyStore(db.settings, db.keyring),
            google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
            sub2api_config=ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'fixture-password'),
            google_transport=httpx.MockTransport(billing))
        # LIFO cleanup joins lifecycle tasks before dropping the disposable DB.
        self.addAsyncCleanup(manager.shutdown)
        return manager

    @contextmanager
    def authorized_sdk(self):
        with patch('onboarding.google_auth_runtime._verify_paths'), \
                patch('onboarding.live_admission.GoogleTokenProvider.__call__',
                      new=AsyncMock(return_value='unit-test-authorized-token')):
            yield

    def snapshot(self):
        result = self.db.business_snapshot()
        for table in ('live_run_plans', 'live_effect_journal', 'live_effect_observations',
                      'approvals', 'download_grants', 'billing_identities', 'payment_cards',
                      'card_reservations', 'card_account_links'):
            result[table] = self.db.read('SELECT * FROM ' + table + ' ORDER BY 1')
        return result

    async def observe_only(self, key, task_id, *, original=None):
        before, requests = self.snapshot(), list(self.requests)
        observer = self.manager()  # A new manager must not adopt a durable task.
        with patch.object(live_admission, 'prepare', new=AsyncMock(
                side_effect=AssertionError('receipt observation prepared admission'))) as prepare, \
                patch.object(live_entry, 'validate_dependencies',
                    side_effect=AssertionError('receipt observation checked new-run dependencies')) as ready, \
                patch.object(live_entry, 'LiveExecutor',
                    side_effect=AssertionError('receipt observation constructed executor')) as construct, \
                patch.object(live_completion, 'run_managed',
                    side_effect=AssertionError('receipt observation scheduled a run')) as run:
            replay = await observer.submit(self.db.actor, self.db.cfg['revision'], key)
            self.assertFalse(replay.scheduled)
            self.assertTrue(replay.result['replayed'])
            self.assertEqual(replay.result['task_ids'], [task_id])
            if original is not None:
                self.assertEqual(replay.result, {**original, 'replayed': True})
            self.assertFalse(observer.busy)
            prepare.assert_not_awaited()
            ready.assert_not_called()
            construct.assert_not_called()
            run.assert_not_called()
            await observer.shutdown()
        self.assertEqual(self.requests, requests)
        self.assertEqual(self.snapshot(), before)
        return replay

    async def test_real_start_cancel_shutdown_and_original_receipts_observe_only(self):
        manager = self.manager()
        entered, gate = asyncio.Event(), asyncio.Event()
        executors = []

        async def before_first_effect(executor):
            # Explicit chain-control test seam; the real executor run, heartbeat,
            # stop-proof publication and run_managed finalizer remain untouched.
            executors.append(executor)
            entered.set()
            await gate.wait()
            raise AssertionError('test chain gate must only exit by cancellation')

        with self.authorized_sdk(), patch.object(live_executor.LiveExecutor, '_chain', before_first_effect):
            submitted = await manager.submit(self.db.actor, self.db.cfg['revision'], 'manager:create')
            self.assertTrue(submitted.scheduled)
            self.assertFalse(submitted.result['replayed'])
            self.assertTrue(manager.busy)
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(len(executors), 1)
            executor = executors[0]
            tid = submitted.result['task_ids'][0]
            self.assertEqual(executor._worker.task_id, tid)
            self.assertEqual(self.db.read('SELECT execution_mode,status,version,current_step FROM onboarding_tasks'),
                             [('live', 'RUNNING', 3, 'project.create')])
            self.assertEqual(self.db.read('SELECT config_revision,plan,plan_digest FROM live_run_plans'),
                [(self.db.cfg['revision'], asdict(executor._plan), executor._plan.effect_digest('project.create'))])
            self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
            original_cancel = await asyncio.to_thread(live_commands.cancel, self.db.settings,
                self.db.actor, tid, 3, 'manager:cancel', policy=self.db.live_policy, mac=self.db.mac)
            self.assertEqual(self.db.read('SELECT status,cancel_requested,version FROM onboarding_tasks'),
                             [('PAUSED', True, 4)])
            self.assertEqual(self.db.read("SELECT usage_status FROM mailbox_platform_states WHERE platform='google'"),
                             [('RESERVED',)])
            await manager.shutdown()

        self.assertTrue(manager.closed)
        self.assertFalse(manager.busy)
        self.assertTrue(live_executor.require_stopped(executor.stop_proof(), executor._worker))
        stopped = live_completion.read_stop_result(self.db.settings, executor._worker)
        self.assertEqual((stopped.code, stopped.status, stopped.holds_released),
                         ('CANCELLED_SAFE_RELEASED', 'CANCELLED_SAFE', True))
        self.assertEqual(self.db.read('SELECT status,cancel_requested,version FROM onboarding_tasks'),
                         [('CANCELLED_SAFE', True, 5)])
        self.assertEqual(self.db.read("SELECT identity_status,usage_status,last_task_id "
                                     "FROM mailbox_platform_states WHERE platform='google'"),
                         [('UNKNOWN', 'HISTORY_UNRECONCILED', None)])
        self.assertEqual(self.db.read('SELECT health FROM mailbox_registry'), [('UNKNOWN',)])
        self.assertEqual(self.db.read('SELECT task_id,owner_id,lease_until,hold_reason FROM resource_leases'),
                         [(None, None, None, None)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_observations'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM secret_objects'), [(1,)])
        self.assertEqual(len(self.requests), 1)  # Only admission's official read-only GET.

        await self.observe_only('manager:create', tid, original=submitted.result)
        before = self.snapshot()
        self.assertEqual(live_commands.cancel(self.db.settings, self.db.actor, tid, 3, 'manager:cancel',
            policy=self.db.live_policy, mac=self.db.mac), original_cancel)
        self.assertEqual(self.snapshot(), before)

    async def lost_ack(self, at):
        manager = self.manager()
        transaction = psycopg.Connection.transaction
        marker = ('INSERT INTO onboarding_tasks(' if at == 1
                  else "UPDATE onboarding_tasks SET status='RUNNING'")
        marked, committed = [], []

        def select_transaction(cursor, text):
            if text.startswith(marker):
                marked.append(cursor.connection)

        @contextmanager
        def after_real_commit(conn, *args, **kwargs):
            with transaction(conn, *args, **kwargs) as current:
                yield current
            # Admission/observation transactions pass through unchanged. Select
            # TX1/TX2 by its real write, not by counting unrelated read UoWs.
            if any(conn is selected for selected in marked):
                self.assertEqual(conn.info.transaction_status, TransactionStatus.IDLE)
                committed.append(at)
                raise psycopg.OperationalError('fixture-manager-real-commit-lost-ack')

        with self.authorized_sdk(), after_query(select_transaction), \
                patch.object(psycopg.Connection, 'transaction', after_real_commit), \
                patch.object(live_entry, 'LiveExecutor',
                    side_effect=AssertionError('uncertain commit constructed executor')) as construct, \
                patch.object(live_completion, 'run_managed',
                    side_effect=AssertionError('uncertain commit scheduled executor')) as run:
            with self.assertRaises(ServiceError) as caught:
                await manager.submit(self.db.actor, self.db.cfg['revision'], 'manager:lost-ack')
            self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
            self.assertFalse(caught.exception.not_committed)
            construct.assert_not_called()
            run.assert_not_called()

        self.assertEqual(committed, [at])
        self.assertEqual(len(marked), 1)
        self.assertFalse(manager.busy)
        self.assertEqual(len(self.requests), 1)
        rows = self.db.read('SELECT id,execution_mode,status,version,current_step FROM onboarding_tasks')
        self.assertEqual(len(rows), 1)
        tid = str(rows[0][0])
        self.assertEqual(rows[0][1:], ('live', 'QUEUED', 2, None) if at == 1
                         else ('live', 'RUNNING', 3, 'project.create'))
        self.assertEqual(self.db.read('SELECT count(*) FROM live_run_plans'), [(at - 1,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_observations'), [(0,)])
        self.assertEqual(self.db.read("SELECT usage_status FROM mailbox_platform_states WHERE platform='google'"),
                         [('RESERVED',)])
        self.assertEqual(self.db.read('SELECT task_id,hold_reason FROM resource_leases'), [(rows[0][0], 'HELD')])
        self.assertEqual(self.db.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'"), [(0,)])
        # No worker was managed: shutdown must retain the uncertain hold, never
        # manufacture a stop proof or treat absence of a run as release evidence.
        before = self.snapshot()
        await manager.shutdown()
        self.assertEqual(self.snapshot(), before)
        replay = await self.observe_only('manager:lost-ack', tid)
        receipt = self.db.read("SELECT id FROM operation_receipts WHERE action='live.batch.create_one'")
        self.assertEqual(replay.result['receipt_id'], str(receipt[0][0]))

    async def test_tx1_real_commit_lost_ack_has_zero_run_and_readonly_replay(self):
        await self.lost_ack(1)

    async def test_tx2_real_commit_lost_ack_has_zero_run_and_readonly_replay(self):
        await self.lost_ack(2)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LiveManagerDBTests)
