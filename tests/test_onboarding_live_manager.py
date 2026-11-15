"""Offline server-owned lifecycle tests. No PG, cloud, SDK or real key files."""
import asyncio
from contextlib import contextmanager
import importlib
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from onboarding import live_entry, security
from onboarding.async_runtime import settled_thread
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.live_input_vault import LivePoolPolicy
from onboarding.live_mailbox_source import OwnedMailboxSource
from onboarding.request_mac import RequestMac
from onboarding.settings import Settings, SOCKET


class LiveManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_manager'),
                             'server-owned lifecycle manager is missing')
        self.m = importlib.import_module('onboarding.live_manager')
        self.settings = Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
            'offline-password', 'rf-onboarding-p1b-v1:offline', 'rf_onboarding')
        self.actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        self.source = OwnedMailboxSource(Path('/tmp/offline-live-manager'), self.actor.operator_id, 'weifashi68@gmail.com')
        self.policy, self.mac = LivePoolPolicy(self.settings), object.__new__(RequestMac)
        self.mid, self.revision = str(uuid4()), 'pool-' + str(uuid4())
        self.result = {'replayed': False, 'task_ids': [str(uuid4())], 'receipt_id': str(uuid4())}
        self.executor = object()
        self.real_start = live_entry.start_committed
        self.run_entered, self.run_release = asyncio.Event(), asyncio.Event()
        async def run(executor):
            self.assertIs(executor, self.executor)
            self.run_entered.set()
            await self.run_release.wait()
        patches = [patch.object(self.m.live_batches, 'observe_request', return_value=None),
                   patch.object(self.m.live_admission, 'prepare', new_callable=AsyncMock, return_value=object()),
                   patch.object(self.m.live_entry, 'validate_dependencies'),
                   patch.object(self.m.live_entry, 'start_committed', return_value=live_entry.PreparedRun(self.result, self.executor)),
                   patch.object(self.m.live_completion, 'run_managed', new=AsyncMock(side_effect=run))]
        self.observe, self.prepare, self.validate, self.start, self.run = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.managers = []

    async def asyncTearDown(self):
        for manager in getattr(self, 'managers', []):
            await manager.shutdown()

    def manager(self):
        result = self.m.LiveRunManager(self.settings, self.mid, source=self.source,
            policy=self.policy, mac=self.mac, key_store=object(),
            google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'), sub2api_config=object())
        self.managers.append(result)
        return result

    async def submit(self, manager, key='fixture-key'):
        return await manager.submit(self.actor, self.revision, key)

    async def entered(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 2))

    async def test_replay_is_observed_before_any_admission_or_dependency_probe(self):
        manager = self.manager()
        expected = {**self.result, 'replayed': True}
        self.observe.return_value = expected
        self.validate.side_effect = AssertionError('replay must not probe new dependencies')
        result = await self.submit(manager)
        self.assertFalse(result.scheduled)
        self.assertEqual(result.result, expected)
        self.prepare.assert_not_awaited()
        self.start.assert_not_called()
        self.run.assert_not_awaited()
        self.assertFalse(manager.busy)
        args, kw = self.observe.call_args
        self.assertEqual(args, (self.settings, self.actor, self.mid, self.revision, 'fixture-key'))
        self.assertEqual(kw, {'policy': self.policy, 'mac': self.mac})

    async def test_fresh_request_orders_observe_validation_admission_committed_start_then_run(self):
        manager, order = self.manager(), []
        self.observe.side_effect = lambda *a, **k: order.append('observe')
        self.validate.side_effect = lambda *a, **k: order.append('dependencies')
        async def prepare(*a, **k):
            order.append('prepare')
            return 'fixture-admission'
        self.prepare.side_effect = prepare
        def start(*a, **k):
            self.assertEqual(k['admission'], 'fixture-admission')
            order.extend(('commit-one', 'commit-two'))
            return live_entry.PreparedRun(self.result, self.executor)
        self.start.side_effect = start
        result = await self.submit(manager)
        await asyncio.wait_for(self.run_entered.wait(), 2)
        self.assertTrue(result.scheduled)
        self.assertEqual(order, ['observe', 'dependencies', 'prepare', 'commit-one', 'commit-two'])
        self.assertTrue(manager.busy)
        self.run.assert_awaited_once_with(self.executor)

    async def test_dependency_failure_never_preflights_or_enters_creation(self):
        manager = self.manager()
        self.validate.side_effect = ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        with self.assertRaises(ServiceError) as error:
            await self.submit(manager)
        self.assertEqual(error.exception.code, ErrorCode.SECRET_UNAVAILABLE)
        self.prepare.assert_not_awaited(); self.start.assert_not_called(); self.run.assert_not_awaited()
        self.assertFalse(manager.busy)

    async def test_observation_error_never_prepares(self):
        manager = self.manager()
        self.observe.side_effect = ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        with self.assertRaises(ServiceError):
            await self.submit(manager)
        self.prepare.assert_not_awaited(); self.start.assert_not_called()

    async def test_lost_commit_ack_never_schedules_or_invents_stop_proof(self):
        manager = self.manager()
        self.start.side_effect = ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with self.assertRaises(ServiceError) as error:
            await self.submit(manager)
        self.assertEqual(error.exception.code, ErrorCode.COMMIT_UNKNOWN)
        self.run.assert_not_awaited(); self.assertFalse(manager.busy)

    async def test_raced_replay_from_start_never_schedules(self):
        manager = self.manager()
        self.start.return_value = live_entry.PreparedRun({**self.result, 'replayed': True}, None)
        result = await self.submit(manager)
        self.assertFalse(result.scheduled); self.run.assert_not_awaited()

    async def test_two_managers_share_one_startup_slot(self):
        first, second = self.manager(), self.manager()
        preparing, release = asyncio.Event(), asyncio.Event()
        async def prepare(*a, **k):
            preparing.set(); await release.wait(); return object()
        self.prepare.side_effect = prepare
        task = asyncio.create_task(self.submit(first))
        try:
            await asyncio.wait_for(preparing.wait(), 2)
            with self.assertRaises(ServiceError) as error:
                await self.submit(second, 'other-key')
            self.assertEqual(error.exception.code, ErrorCode.RESOURCE_HELD)
            self.assertEqual(self.prepare.await_count, 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(first.busy)

    async def test_existing_receipt_observation_is_allowed_while_a_run_is_owned(self):
        first, second = self.manager(), self.manager()
        await self.submit(first)
        await asyncio.wait_for(self.run_entered.wait(), 2)
        self.observe.return_value = {**self.result, 'replayed': True}
        replay = await self.submit(second)
        self.assertFalse(replay.scheduled)
        self.assertEqual(self.run.await_count, 1)
        self.assertEqual(self.prepare.await_count, 1)

    async def test_cancellation_before_coroutine_first_step_starts_nothing(self):
        manager = self.manager()
        task = asyncio.create_task(self.submit(manager))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.observe.assert_not_called(); self.prepare.assert_not_awaited()

    async def test_already_cancelled_submitter_creates_no_owned_entry(self):
        manager = self.manager()
        async def cancelled():
            asyncio.current_task().cancel()
            await self.submit(manager)
        task = asyncio.create_task(cancelled())
        with self.assertRaises(asyncio.CancelledError): await task
        self.observe.assert_not_called()

    async def test_repeated_request_cancellation_joins_observer_thread(self):
        manager = self.manager()
        entered, release = threading.Event(), threading.Event()
        def observe(*a, **k):
            entered.set(); release.wait(3); return None
        self.observe.side_effect = observe
        task = asyncio.create_task(self.submit(manager))
        try:
            await self.entered(entered)
            task.cancel(); await asyncio.sleep(.02); task.cancel(); await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.prepare.assert_not_awaited()
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.start.assert_not_called()

    async def test_cancelled_two_commit_start_is_joined_but_not_scheduled(self):
        manager = self.manager()
        entered, release = threading.Event(), threading.Event()
        def start(*a, **k):
            entered.set(); release.wait(3)
            return live_entry.PreparedRun(self.result, self.executor)
        self.start.side_effect = start
        task = asyncio.create_task(self.submit(manager))
        try:
            await self.entered(entered)
            task.cancel(); await asyncio.sleep(.02); task.cancel(); await asyncio.sleep(.02)
            self.assertFalse(task.done()); self.assertTrue(manager.busy)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.run.assert_not_awaited(); self.assertFalse(manager.busy)

    async def test_real_entry_confirms_both_commits_before_constructor_and_cancel_never_runs_it(self):
        manager = self.manager()
        constructed, release = threading.Event(), threading.Event()
        order = []
        conn = Mock(); conn.execute.return_value.fetchone.return_value = ({},)
        worker, plan = SimpleNamespace(config_id=str(uuid4())), Mock()
        def created(*a, **k):
            order.append('tx-one-returned')
            return self.result
        @contextmanager
        def uow(settings):
            yield conn
            order.append('tx-two-committed')
        def constructor(*a, **k):
            self.assertEqual(order, ['tx-one-returned', 'tx-two-committed'])
            order.append('executor-constructed')
            constructed.set(); release.wait(3)
            return self.executor
        self.start.side_effect = self.real_start
        with patch.object(live_entry.live_batches, 'create_one', side_effect=created), \
                patch.object(live_entry.storage, 'unit_of_work', uow), \
                patch.object(LivePoolPolicy, '_connection'), \
                patch.object(live_entry.live_runtime, 'handoff', return_value=worker), \
                patch.object(live_entry.live_config, 'plan_for', return_value=plan), \
                patch.object(live_entry.live_run_store, 'bind_plan'), \
                patch.object(live_entry.live_runtime, 'sending_guard'), \
                patch.object(live_entry, 'LiveExecutor', side_effect=constructor):
            task = asyncio.create_task(self.submit(manager))
            try:
                await self.entered(constructed)
                task.cancel(); await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(order, ['tx-one-returned', 'tx-two-committed', 'executor-constructed'])
        self.run.assert_not_awaited(); self.assertFalse(manager.busy)

    async def test_repeated_http_cancel_joins_admission_cleanup_database_thread(self):
        manager = self.manager()
        preparing = asyncio.Event()
        cleanup, release = threading.Event(), threading.Event()
        async def prepare(*a, **k):
            try:
                preparing.set(); await asyncio.Event().wait()
            finally:
                def read(): cleanup.set(); release.wait(3)
                await settled_thread(read)
        self.prepare.side_effect = prepare
        task = asyncio.create_task(self.submit(manager))
        try:
            await asyncio.wait_for(preparing.wait(), 2)
            task.cancel(); await self.entered(cleanup)
            task.cancel(); await asyncio.sleep(.02)
            self.assertFalse(task.done()); self.assertTrue(manager.busy)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.start.assert_not_called(); self.run.assert_not_awaited()

    async def test_request_cancel_after_handoff_does_not_drop_or_cancel_managed_run(self):
        manager = self.manager()
        handed, unrelated = asyncio.Event(), asyncio.Event()
        async def handler():
            await self.submit(manager)
            handed.set(); await unrelated.wait()
        request = asyncio.create_task(handler())
        await asyncio.wait_for(handed.wait(), 2)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError): await request
        await asyncio.wait_for(self.run_entered.wait(), 2)
        self.assertTrue(manager.busy)
        self.assertFalse(manager._running.done())

    async def test_shutdown_joins_owned_entry_not_unrelated_cancel_catching_http_handler(self):
        manager = self.manager()
        entered, release = threading.Event(), threading.Event()
        caught, unrelated = asyncio.Event(), asyncio.Event()
        def observe(*a, **k):
            entered.set(); release.wait(3)
        self.observe.side_effect = observe
        async def handler():
            try: await self.submit(manager)
            except asyncio.CancelledError:
                caught.set(); await unrelated.wait()
        request = asyncio.create_task(handler())
        await self.entered(entered)
        closing = asyncio.create_task(manager.shutdown())
        try:
            await asyncio.sleep(.02)
            self.assertFalse(closing.done())
            closing.cancel(); await asyncio.sleep(.02); closing.cancel()
            self.assertFalse(closing.done())
            with self.assertRaises(ServiceError): await self.submit(manager, 'closed')
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError): await closing
            await asyncio.wait_for(caught.wait(), 2)
            self.assertFalse(request.done(), 'shutdown must not await unrelated handler work')
            unrelated.set(); await request
        self.prepare.assert_not_awaited(); self.start.assert_not_called()
        self.assertTrue(manager.closed)

    async def test_shutdown_repeated_cancel_waits_for_managed_finalizer_thread(self):
        manager = self.manager()
        entered, release = threading.Event(), threading.Event()
        async def run(executor):
            self.run_entered.set()
            try: await asyncio.Event().wait()
            finally:
                def finalizer(): entered.set(); release.wait(3)
                await settled_thread(finalizer)
        self.run.side_effect = run
        await self.submit(manager)
        await asyncio.wait_for(self.run_entered.wait(), 2)
        closing = asyncio.create_task(manager.shutdown())
        try:
            await self.entered(entered)
            closing.cancel(); await asyncio.sleep(.02); closing.cancel(); await asyncio.sleep(.02)
            self.assertFalse(closing.done()); self.assertTrue(manager.busy)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError): await closing
        self.assertFalse(manager.busy)
        await manager.shutdown()

    async def test_shutdown_cancels_ready_admission_before_yielding_to_the_loop(self):
        manager = self.manager()
        preparing, ready = asyncio.Event(), asyncio.Event()
        provider_calls = []
        async def prepare(*a, **k):
            preparing.set()
            await ready.wait()
            provider_calls.append('would-send')
            return object()
        self.prepare.side_effect = prepare
        request = asyncio.create_task(self.submit(manager))
        await asyncio.wait_for(preparing.wait(), 2)
        ready.set()  # Entry is already ahead of a newly scheduled drain task.
        await manager.shutdown()
        await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(provider_calls, [])
        self.start.assert_not_called()

    async def test_background_failure_is_retrieved_and_releases_only_the_local_slot(self):
        manager = self.manager()
        errors = []
        loop = asyncio.get_running_loop(); original = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        self.run.side_effect = RuntimeError('fixture-private-error')
        try:
            await self.submit(manager)
            for _ in range(10):
                if not manager.busy: break
                await asyncio.sleep(0)
            self.assertFalse(manager.busy)
            self.assertEqual(errors, [])
        finally: loop.set_exception_handler(original)

    async def test_invalid_request_and_foreign_owner_cannot_observe(self):
        manager = self.manager()
        for revision, key in ((True, 'key'), (self.revision, ''), (self.revision, {'path': '/tmp/anything'})):
            with self.assertRaises(ServiceError):
                await manager.submit(self.actor, revision, key)
        foreign = security.Actor(str(uuid4()), self.actor.permissions, str(uuid4()), 1)
        with self.assertRaises(ServiceError):
            await manager.submit(foreign, self.revision, 'key')
        self.observe.assert_not_called()


if __name__ == '__main__': unittest.main()
