"""Local stop proof + real isolated PG finalization; never real provider I/O."""
import asyncio
from contextlib import contextmanager, ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib
import importlib.util
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import httpx
import psycopg

from onboarding import live_commands, live_effects, live_executor, live_runtime, storage
from onboarding.errors import ErrorCode, ServiceError


def module(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.live_completion'),
                         'stopped-worker completion boundary is missing')
    return importlib.import_module('onboarding.live_completion')


class CompletionInputTests(unittest.IsolatedAsyncioTestCase):
    def executor(self):
        from test_onboarding_executor_stop_proof import StopProofTests
        return StopProofTests().executor()

    async def test_completion_exists_and_rejects_forged_proof_before_database(self):
        m = module(self)
        executor = self.executor()
        with patch.object(storage, 'unit_of_work', side_effect=AssertionError('forged proof reached DB')):
            for proof in (True, object(), executor._result('CHAIN_VERIFIED')):
                with self.assertRaises(ServiceError) as caught:
                    m.finish_stopped(executor._settings, executor._worker, proof)
                self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    async def test_managed_run_returns_separate_execution_and_completion_results(self):
        m = module(self)
        executor = self.executor()
        completion = object()
        def finish(settings, worker, proof):
            self.assertTrue(live_executor.require_stopped(proof, worker))
            return completion
        with patch.object(m, 'finish_stopped', side_effect=finish) as call:
            result = await m.run_managed(executor)
        self.assertEqual(result.execution.code, 'CHAIN_VERIFIED')
        self.assertIs(result.completion, completion)
        call.assert_called_once()

    async def test_repeated_cancel_joins_finalizer_then_preserves_cancellation(self):
        m = module(self)
        executor = self.executor()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def finish(settings, worker, proof):
            self.assertTrue(live_executor.require_stopped(proof, worker))
            entered.set()
            release.wait(5)
            finished.set()
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(m, 'finish_stopped', side_effect=finish) as call:
            task = asyncio.create_task(m.run_managed(executor))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        call.assert_called_once()

    async def test_cancelled_run_is_finalized_only_after_actual_stop(self):
        m = module(self)
        executor = self.executor()
        entered = asyncio.Event()
        async def chain():
            entered.set()
            await asyncio.Future()
        executor._chain = chain
        def finish(settings, worker, proof):
            self.assertTrue(live_executor.require_stopped(proof, worker))
        with patch.object(m, 'finish_stopped', side_effect=finish) as call:
            task = asyncio.create_task(m.run_managed(executor))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        call.assert_called_once()


class CompletionRestoreHistoryTests(unittest.IsolatedAsyncioTestCase):
    """Offline SQL-boundary assertions; not a substitute for real PG release tests."""
    async def finish_case(self, config, details, *, cancelled=True):
        import onboarding
        from test_onboarding_executor_stop_proof import StopProofTests
        m = module(self)
        executor = StopProofTests().executor()
        await executor.run()
        worker = executor._worker
        task = dict(id=worker.task_id, config_id=worker.config_id,
            config_revision=worker.config_revision, nonsecret_config=config,
            status='RUNNING', current_step='project.create', cancel_requested=cancelled, version=3)
        lease = dict(task_id=worker.task_id, resource_kind='mailbox',
            owner_id=worker.binding.owner_id, fence=1, version=2,
            hold_reason='HELD', lease_until=object())
        commands, restored, events = [], [], [('creation-event',)]
        def execute(query, params=()):
            commands.append((query, params))
            cursor = Mock()
            if query.startswith('SELECT id,action,phase,generation,effect_digest'):
                cursor.fetchall.return_value = []
            elif query.startswith('SELECT actor_id,action,object_ref,outcome_code,correlation_id'):
                cursor.fetchall.return_value = events
            elif query.startswith('SELECT version FROM mailbox_platform_states'):
                cursor.fetchone.return_value = (2,)
            elif query.startswith('UPDATE resource_leases'):
                cursor.fetchone.return_value = (2, 3)
            elif query.startswith('UPDATE mailbox_platform_states'):
                restored.append('UNUSED' if "usage_status='UNUSED'" in query else params[0])
                cursor.fetchone.return_value = (3,)
            elif query.startswith('UPDATE onboarding_tasks'):
                cursor.fetchone.return_value = (4,)
            elif not query.startswith('INSERT INTO operation_receipts'):
                raise AssertionError('Unexpected offline SQL: ' + query)
            return cursor
        conn = Mock(); conn.execute.side_effect = execute
        creation = Mock(return_value=details)
        @contextmanager
        def uow(settings):
            yield conn
        with ExitStack() as stack:
            for owner, name, value in ((storage, 'unit_of_work', uow),
                    (m, '_read_identity', lambda *args: executor._plan),
                    (m, '_receipt', lambda *args: None),
                    (m, '_related', lambda *args, **kwargs: False),
                    (m, '_tail', lambda *args: None),
                    (live_runtime, '_worker_graph', lambda *args: (task, lease, [])),
                    (live_runtime, '_final', lambda *args: None),
                    (m.audit, 'append', lambda *args, **kwargs: 1)):
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(onboarding, 'live_batches',
                SimpleNamespace(creation_details=creation), create=True))
            result = m._finish(executor._settings, worker, executor.stop_proof())
        return result, restored, commands, creation, (conn, worker.actor, task, events)

    async def test_v2_cancel_restores_proven_unknown_history_never_unused(self):
        result, restored, _, creation, arguments = await self.finish_case(
            {'schema_version': 2, 'workflow': 'existing_billing_cloud_v1'},
            {'original_usage_status': 'HISTORY_UNRECONCILED'})
        self.assertEqual(restored, ['HISTORY_UNRECONCILED'])
        self.assertEqual((result.code, result.holds_released), ('CANCELLED_SAFE_RELEASED', True))
        creation.assert_called_once_with(*arguments)

    async def test_v2_missing_or_wrong_creation_proof_keeps_hold(self):
        for details in (None, {}, {'original_usage_status': 'UNUSED'},
                        {'original_usage_status': True}, {'original_usage_status': 'UNKNOWN'}):
            with self.subTest(details=details):
                result, restored, commands, _, _ = await self.finish_case(
                    {'schema_version': 2, 'workflow': 'existing_billing_cloud_v1'}, details)
                self.assertFalse(result.holds_released)
                self.assertEqual(result.code, 'LIVE_RECONCILIATION_REQUIRED')
                self.assertEqual(restored, [])
                self.assertFalse(any(query.startswith('UPDATE resource_leases') for query, _ in commands))

    async def test_v1_cancel_preserves_legacy_unused_restore(self):
        result, restored, _, creation, _ = await self.finish_case(
            {'model': 'fixture-model'}, None)
        self.assertTrue(result.holds_released)
        self.assertEqual(restored, ['UNUSED'])
        creation.assert_not_called()

    async def test_without_cancel_never_consults_creation_or_releases(self):
        result, restored, _, creation, _ = await self.finish_case(
            {'schema_version': 2, 'workflow': 'existing_billing_cloud_v1'},
            {'original_usage_status': 'HISTORY_UNRECONCILED'}, cancelled=False)
        self.assertFalse(result.holds_released)
        self.assertEqual(restored, [])
        creation.assert_not_called()


class CompletionPinnedPlanTests(unittest.TestCase):
    """Released-task replay still pins the original config, without claiming a lease."""
    def identity_case(self, *, change_plan=None, change_config=None, legacy=False):
        from onboarding import live_config
        from test_onboarding_live_config import FIELDS, LEGACY
        from test_onboarding_executor_stop_proof import StopProofTests
        m = module(self)
        executor = StopProofTests().executor()
        worker = live_runtime._register(replace(executor._worker, config_revision='pool-' + str(uuid4())))
        fields = LEGACY.copy() if legacy else FIELDS.copy()
        plan = executor._plan if legacy else live_config.plan_for(fields, worker.actor.operator_id, worker.task_id)
        stored = plan if change_plan is None else replace(plan, **change_plan)
        config = [UUID(worker.config_id), worker.config_revision, fields, {},
                  UUID(worker.actor.operator_id), datetime.now(timezone.utc), 'pool']
        if change_config:
            change_config(config)
        commands = []
        def execute(query, params=()):
            commands.append((query, params))
            cursor = Mock()
            if query.startswith('SELECT t.execution_mode'):
                row = ('live', 'pool', worker.generation, UUID(worker.config_id), UUID(worker.actor.operator_id))
            elif 'FROM live_run_plans' in query:
                row = (worker.generation, worker.config_revision, UUID(worker.actor.operator_id),
                       asdict(stored), stored.effect_digest('project.create'))
            elif 'FROM global_configs' in query:
                self.assertIn('WHERE id=%s', query)
                self.assertEqual(params, (worker.config_id,))
                row = tuple(config)
            else:
                raise AssertionError('Unexpected offline SQL: ' + query)
            cursor.fetchone.return_value = row
            return cursor
        conn = Mock(); conn.execute.side_effect = execute
        with patch.object(m.security, 'revalidate'), patch.object(live_runtime, '_worker_graph',
                side_effect=AssertionError('released-task replay cannot reclaim an old lease')):
            result = m._read_identity(conn, worker)
        return result, plan, commands

    def test_replay_rejects_self_consistent_plan_with_unpinned_billing(self):
        with self.assertRaises(ServiceError):
            self.identity_case(change_plan={'billing_account': 'DDDDDD-EEEEEE-FFFFFF'})

    def test_replay_rejects_changed_pinned_configuration_revision(self):
        with self.assertRaises(ServiceError):
            self.identity_case(change_config=lambda row: row.__setitem__(1, 'pool-' + str(uuid4())))

    def test_replay_reads_exact_config_id_not_current_head(self):
        result, plan, commands = self.identity_case()
        self.assertEqual(result, plan)
        self.assertEqual(sum('FROM global_configs' in query for query, _ in commands), 1)

    def test_legacy_plan_identity_remains_readable(self):
        result, plan, _ = self.identity_case(legacy=True)
        self.assertEqual(result, plan)


class CompletionControlReceiptTests(unittest.TestCase):
    def related(self, action, phase):
        from test_onboarding_executor_stop_proof import StopProofTests
        m = module(self)
        worker = StopProofTests().executor()._worker
        def execute(query, params):
            # Model only an existing task-control receipt. Every other related
            # resource/history query is empty; no DB is used by this SQL check.
            row = None
            if 'FROM operation_receipts' in query:
                row = (1,) if "'" + action + "'" not in query or phase != 'SUCCEEDED' else None
            return SimpleNamespace(fetchone=lambda: row)
        return m._related(SimpleNamespace(execute=execute), worker)

    def test_exact_succeeded_live_controls_are_not_external_effect_history(self):
        for action in ('live.command.pause', 'live.command.cancel'):
            with self.subTest(action=action):
                self.assertFalse(self.related(action, 'SUCCEEDED'))

    def test_unknown_live_control_and_unrecognized_action_block_release(self):
        for action, phase in (('live.command.cancel', 'UNKNOWN'), ('live.command.cancel', 'INTENT'),
                              ('live.command.recheck', 'SUCCEEDED'), ('live.command.cancel.extra', 'SUCCEEDED')):
            with self.subTest(action=action, phase=phase):
                self.assertTrue(self.related(action, phase))


class CompletionDBTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = module(self)
        from test_onboarding_live_executor import ExecutorTests
        self.case = ExecutorTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.db = self.case.fixture_case
        self.settings, self.worker = self.case.settings, self.case.worker
        self.tid = self.case.tid

    def read(self, sql, args=()):
        return self.case.read(sql, args)

    def change(self, sql, args=()):
        self.db.change(sql, args)

    def cancel(self):
        version = self.read('SELECT version FROM onboarding_tasks WHERE id=%s', (self.tid,))[0][0]
        return live_commands.cancel(self.settings, self.case.actor, self.tid, version, str(uuid4()),
                                    policy=self.db.live_policy, mac=self.db.mac)

    async def stop_without_effect(self, *, cancel=True):
        if cancel:
            self.cancel()
        else:
            async def reject(request):
                return httpx.Response(403, json={})
            self.case.cloud_override = reject
        executor = self.case.executor()
        await executor.run()
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal')[0][0], 0)
        return executor, executor.stop_proof()

    def finish(self, proof):
        return self.m.finish_stopped(self.settings, self.worker, proof)

    def held(self):
        row = self.read('SELECT task_id,owner_id,fence,hold_reason,stopped_evidence_ref FROM resource_leases')[0]
        self.assertEqual(row[:3], (UUID(self.tid), self.worker.binding.owner_id, self.worker.binding.fence))
        self.assertIsNotNone(row[3])
        self.assertIsNone(row[4])
        self.assertEqual(self.read("SELECT usage_status,last_task_id FROM mailbox_platform_states WHERE platform='google'")[0],
                         ('RESERVED', UUID(self.tid)))

    async def test_cancelled_stopped_no_effect_releases_atomically_and_replays_readonly(self):
        executor, proof = await self.stop_without_effect()
        result = self.finish(proof)
        self.assertEqual((result.code, result.status, result.holds_released),
                         ('CANCELLED_SAFE_RELEASED', 'CANCELLED_SAFE', True))
        self.assertEqual(self.read('SELECT status,cancel_requested FROM onboarding_tasks')[0], ('CANCELLED_SAFE', True))
        self.assertEqual(self.read('SELECT task_id,owner_id,fence,lease_until,hold_reason FROM resource_leases')[0],
                         (None, None, self.worker.binding.fence+1, None, None))
        self.assertEqual(self.read("SELECT usage_status,last_task_id FROM mailbox_platform_states WHERE platform='google'")[0],
                         ('HISTORY_UNRECONCILED', None))
        version = self.read('SELECT version FROM onboarding_tasks')[0][0]
        self.assertEqual(self.finish(proof), result)
        self.assertEqual(self.m.read_stop_result(self.settings, self.worker), result)
        self.assertEqual(self.read('SELECT version FROM onboarding_tasks')[0][0], version)
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'")[0][0], 1)

    async def test_stopped_without_cancel_is_not_release_permission(self):
        executor, proof = await self.stop_without_effect(cancel=False)
        result = self.finish(proof)
        self.assertEqual((result.status, result.code, result.holds_released),
                         ('PAUSED', 'LIVE_RECONCILIATION_REQUIRED', False))
        self.held()

    async def test_old_stop_receipt_never_changes_a_new_batch_hold(self):
        executor, proof = await self.stop_without_effect()
        original = self.finish(proof)
        next_batch = self.db.call(self.db.cfg, [self.db.resource], request_key='fixture:next-batch')
        next_tid = next_batch['task_ids'][0]
        self.assertNotEqual(next_tid, self.tid)
        before = (self.read('SELECT * FROM resource_leases'),
                  self.read('SELECT * FROM mailbox_platform_states'),
                  self.read('SELECT * FROM onboarding_tasks WHERE id=%s', (next_tid,)))
        self.assertEqual(self.read('SELECT task_id FROM resource_leases')[0][0], UUID(next_tid))
        self.assertEqual(self.finish(proof), original)
        self.assertEqual(self.m.read_stop_result(self.settings, self.worker), original)
        after = (self.read('SELECT * FROM resource_leases'),
                 self.read('SELECT * FROM mailbox_platform_states'),
                 self.read('SELECT * FROM onboarding_tasks WHERE id=%s', (next_tid,)))
        self.assertEqual(after, before)

    async def test_seven_canonical_successes_are_only_cloud_verified_and_keep_hold(self):
        executor = self.case.executor()
        result = await executor.run()
        self.assertEqual(result.code, 'CHAIN_VERIFIED')
        stopped = self.finish(executor.stop_proof())
        self.assertEqual((stopped.status, stopped.code, stopped.holds_released),
                         ('PAUSED', 'CLOUD_CHAIN_VERIFIED', False))
        self.held()
        summary = self.read("SELECT result_summary FROM operation_receipts WHERE action='live.run.stop'")[0][0]
        self.assertEqual((summary['first_trial'], summary['payment'], summary['credits']),
                         ('NOT_VERIFIED',)*3)

    async def test_seven_successes_do_not_hide_another_unknown_operation(self):
        executor = self.case.executor()
        self.assertEqual((await executor.run()).code, 'CHAIN_VERIFIED')
        self.change('INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) '
                    "VALUES(%s,%s,'fixture.legacy','fixture','fixture',%s,'UNKNOWN',1,1)",
                    (str(uuid4()), self.tid, 'a'*64))
        result = self.finish(executor.stop_proof())
        self.assertEqual(result.code, 'LIVE_RECONCILIATION_REQUIRED')
        self.held()

    async def test_concurrent_finalizers_serialize_and_release_once(self):
        executor, proof = await self.stop_without_effect()
        original, replay = self.db.race_original('WHERE t.id=%s FOR UPDATE OF t',
                                                lambda: self.finish(proof), lambda: self.finish(proof))
        self.assertEqual((original[0], replay[0]), ('OK', 'OK'))
        self.assertEqual(original[1], replay[1])
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'")[0][0], 1)
        self.assertEqual(self.read('SELECT fence FROM resource_leases')[0][0], self.worker.binding.fence+1)

    async def test_deadline_expiry_after_release_writes_rolls_back_everything(self):
        executor, proof = await self.stop_without_effect()
        execute = psycopg.Cursor.execute
        armed, delayed = [], []
        def timed(cursor, query, params=None, **kwargs):
            text = query if type(query) is str else str(query)
            if 'stopped_evidence_ref FROM resource_leases' in text and 'FOR UPDATE' in text and not armed:
                # Arm after task/auth/mailbox locks, not during slow setup.
                armed.append(True)
                cursor.connection.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '1200 milliseconds'")
            value = execute(cursor, query, params, **kwargs)
            if text.startswith('INSERT INTO operation_receipts') and armed and not delayed:
                delayed.append(True)
                cursor.connection.execute('SELECT pg_sleep(1.3)')
            return value
        with patch.object(psycopg.Cursor, 'execute', timed):
            with self.assertRaises(ServiceError) as caught:
                self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.STALE_FENCE)
        self.assertTrue(armed and delayed, 'test must reach actual candidate release and receipt writes')
        self.held()
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'")[0][0], 0)

    async def test_cancel_with_intent_never_releases_or_mints_permit(self):
        with self.db.uow() as conn:
            ctx = live_runtime.effect_context(conn, self.worker, 'project.create', self.case.plan.effect_digest('project.create'))
            live_effects.prepare(conn, ctx, ctx.step_key, 'held-intent')
        self.cancel()
        executor = self.case.executor()
        await executor.run()
        with patch.object(live_effects, 'prepare_committed', side_effect=AssertionError('finalizer minted permit')):
            result = self.finish(executor.stop_proof())
        self.assertEqual(result.code, 'LIVE_RECONCILIATION_REQUIRED')
        self.held()

    async def test_any_task_step_prevents_no_effect_release(self):
        executor, proof = await self.stop_without_effect()
        self.change("INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,'old',1,'FAILED_CONFIRMED')",
                    (str(uuid4()), self.tid))
        self.assertFalse(self.finish(proof).holds_released)
        self.held()

    async def test_failed_logout_is_not_cloud_verified(self):
        async def fail_logout(request):
            if request.url.path == '/api/v1/auth/logout':
                return httpx.Response(503, json={})
        self.case.import_override = fail_logout
        executor = self.case.executor()
        self.assertEqual((await executor.run()).code, 'CLEANUP_REQUIRED')
        result = self.finish(executor.stop_proof())
        self.assertEqual(result.code, 'LIVE_RECONCILIATION_REQUIRED')
        self.held()

    async def test_adapter_normalized_unknown_is_held_without_sticky_exception_code(self):
        executor = self.case.executor()
        store_type = type(self.case.keys)
        consume = store_type._for_import
        calls = []
        def unknown(store, *args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
            return consume(store, *args, **kwargs)
        with patch.object(store_type, '_for_import', unknown):
            execution = await executor.run()
        self.assertEqual(execution.code, 'UNKNOWN')
        self.assertEqual(len(calls), 2)
        self.assertFalse(live_executor.stop_observed_commit_unknown(executor.stop_proof(), self.worker))
        self.assertEqual(self.read("SELECT phase FROM live_effect_journal WHERE action='sub2api.import_disabled'")[0], ('UNKNOWN',))
        self.cancel()
        before = len(self.case.seen)
        result = self.finish(executor.stop_proof())
        self.assertEqual((result.code, result.holds_released), ('LIVE_RECONCILIATION_REQUIRED', False))
        self.assertEqual(len(self.case.seen), before, 'finalization must not call any provider')
        self.assertEqual(len(self.case.posts('/api/v1/admin/accounts')), 0)
        self.held()

    async def test_expired_lease_cannot_finalize(self):
        executor, proof = await self.stop_without_effect()
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        with self.assertRaises(ServiceError) as caught:
            self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.STALE_FENCE)
        self.held()
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'")[0][0], 0)

    async def test_unknown_hold_without_journal_is_not_release_permission(self):
        executor, proof = await self.stop_without_effect()
        self.change("UPDATE resource_leases SET hold_reason='UNKNOWN'")
        self.assertFalse(self.finish(proof).holds_released)
        self.held()

    async def test_frozen_commit_unknown_prevents_release_after_fatal_code_changes(self):
        self.cancel()
        executor = self.case.executor()
        def ambiguous():
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        async def chain():
            await executor._db(ambiguous)
        executor._chain = chain
        await executor.run()
        proof = executor.stop_proof()
        # Later heartbeat/error reporting must not wash away the first unknown.
        executor._fatal_code = 'DEPENDENCY_UNAVAILABLE'
        self.assertFalse(executor._observation_commit_unknown)
        result = self.finish(proof)
        self.assertFalse(result.holds_released)
        self.assertEqual(result.code, 'LIVE_RECONCILIATION_REQUIRED')
        self.held()

    async def test_advanced_step_without_receipt_cannot_be_declared_never_started(self):
        executor, proof = await self.stop_without_effect()
        self.change("UPDATE onboarding_tasks SET current_step='billing.link'")
        self.assertFalse(self.finish(proof).holds_released)
        self.held()

    async def test_revoked_original_session_cannot_finalize_or_read_result(self):
        executor, proof = await self.stop_without_effect()
        self.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                    (self.worker.actor.session_id,))
        for call in (lambda: self.finish(proof), lambda: self.m.read_stop_result(self.settings, self.worker)):
            with self.assertRaises(ServiceError) as caught:
                call()
            self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        self.held()

    async def test_stale_fence_cannot_release_a_later_lease(self):
        executor, proof = await self.stop_without_effect()
        self.change('UPDATE resource_leases SET fence=fence+1')
        with self.assertRaises(ServiceError) as caught:
            self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.STALE_FENCE)
        self.assertEqual(self.read('SELECT task_id FROM resource_leases')[0][0], UUID(self.tid))

    async def test_approval_history_blocks_release(self):
        executor, proof = await self.stop_without_effect()
        self.change('INSERT INTO approvals(id,task_id,action,resource_revision,config_revision,actor_id,expires_at) '
                    "VALUES(%s,%s,'test','fixture',%s,%s,clock_timestamp()+interval '1 minute')",
                    (str(uuid4()), self.tid, self.worker.config_revision, self.worker.actor.operator_id))
        self.assertFalse(self.finish(proof).holds_released)
        self.held()

    async def test_unrecognized_terminal_receipt_blocks_release(self):
        executor, proof = await self.stop_without_effect()
        self.change('INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) '
                    "VALUES(%s,%s,'fixture.legacy','fixture','fixture',%s,'SUCCEEDED',1,1)",
                    (str(uuid4()), self.tid, 'a'*64))
        self.assertFalse(self.finish(proof).holds_released)
        self.held()

    async def test_receipt_read_rejects_bool_as_schema_version(self):
        executor, proof = await self.stop_without_effect()
        self.finish(proof)
        self.change("UPDATE operation_receipts SET result_summary=jsonb_set(result_summary,'{v}','true') WHERE action='live.run.stop'")
        with self.assertRaises(ServiceError):
            self.m.read_stop_result(self.settings, self.worker)

    async def test_unobserved_commit_unknown_does_not_restart_finalizer(self):
        executor, proof = await self.stop_without_effect()
        original = storage.unit_of_work
        @contextmanager
        def rollback_unknown(settings):
            with original(settings) as conn:
                yield conn
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage, 'unit_of_work', rollback_unknown):
            with self.assertRaises(ServiceError) as caught:
                self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
        self.assertIsNone(self.m.read_stop_result(self.settings, self.worker))
        with patch.object(self.m, '_finish', side_effect=AssertionError('unknown absent receipt retried')):
            with self.assertRaises(ServiceError) as caught:
                self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.RECONCILIATION_REQUIRED)
        self.held()

    async def test_conflict_status_is_preserved(self):
        executor, proof = await self.stop_without_effect()
        self.change("UPDATE onboarding_tasks SET status='CONFLICT',reason_code='PRIOR_CONFLICT'")
        result = self.finish(proof)
        self.assertEqual(result.status, 'CONFLICT')
        self.assertFalse(result.holds_released)
        self.held()

    async def test_commit_unknown_is_never_retried_but_exact_receipt_is_readable(self):
        executor, proof = await self.stop_without_effect()
        original = storage.unit_of_work
        @contextmanager
        def lost_ack(settings):
            with original(settings) as conn:
                yield conn
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage, 'unit_of_work', lost_ack):
            with self.assertRaises(ServiceError) as caught:
                self.finish(proof)
        self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
        result = self.m.read_stop_result(self.settings, self.worker)
        self.assertTrue(result.holds_released)
        with patch.object(self.m, '_finish', side_effect=AssertionError('ambiguous finalizer retried')):
            self.assertEqual(self.finish(proof), result)
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'")[0][0], 1)
