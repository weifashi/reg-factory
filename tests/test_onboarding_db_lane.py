"""Deterministic thread barriers, real async callers; no PG, tokens or HTTP."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from onboarding import async_runtime, live_effects, live_executor as m
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_key_store import ServiceAccountKeyStore


class DatabaseLaneTests(unittest.IsolatedAsyncioTestCase):
    def executor(self):
        from test_onboarding_executor_stop_proof import StopProofTests
        return StopProofTests().executor()

    async def serial_case(self, entry, *, cancel=False):
        executor = self.executor()
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        heartbeat_attempting = asyncio.Event()
        heartbeat_entered = threading.Event()
        calls = []

        def database(*args, **kwargs):
            calls.append(True)
            if entry == 'key-disclosure' and len(calls) == 1:
                return b'fixture-only'
            entered.set()
            if not release.wait(4):
                raise AssertionError('fixture DB thread not released')
            ended.set()
            if cancel:
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
            return b'fixture-only'

        async def heartbeat():
            if not await asyncio.to_thread(entered.wait, 2):
                raise AssertionError('fixture chain never entered DB')
            heartbeat_attempting.set()
            await executor._db(heartbeat_entered.set)

        async def chain():
            if entry == 'executor':
                await executor._db(database)
            elif entry == 'permit':
                from test_onboarding_live_effects import LiveEffectTests
                context, permit, _ = LiveEffectTests().prepared(live_effects)
                self.assertIs(await permit.consume(context.step_key), True)
            else:
                class Consumer:
                    async def import_disabled(self, raw, permit, *, disclosure_guard):
                        self_outer.assertEqual(raw, b'fixture-only')
                        if entry == 'key-disclosure':
                            self_outer.assertIs(await disclosure_guard(), True)
                        return None
                self_outer = self
                await executor._keys.import_disabled(context=None, secret_id=None,
                    source_receipt_id=None, plan=None, importer=Consumer(), permit=None)
            return executor._result('CHAIN_VERIFIED')

        executor._heartbeat, executor._chain = heartbeat, chain
        with patch.object(live_effects, '_dispatch_guard', side_effect=database), \
                patch.object(ServiceAccountKeyStore, '_for_import', side_effect=database):
            task = asyncio.create_task(executor.run())
            try:
                await asyncio.wait_for(heartbeat_attempting.wait(), 2)
                if cancel:
                    for _ in range(3):
                        task.cancel()
                        await asyncio.sleep(0)
                    self.assertFalse(task.done(), 'cancel abandoned the DB owner')
                    with self.assertRaises(ServiceError):
                        executor.stop_proof()
                self.assertFalse(await asyncio.to_thread(heartbeat_entered.wait, .1),
                    'heartbeat started a competing DB transaction before the chain commit/join ended')
                release.set()
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIs(m.stop_observed_commit_unknown(executor.stop_proof(), executor._worker), True)
                else:
                    self.assertEqual((await task).code, 'CHAIN_VERIFIED')
                self.assertTrue(ended.is_set())
                self.assertTrue(heartbeat_entered.is_set())
                self.assertIs(m.require_stopped(executor.stop_proof(), executor._worker), True)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_main_database_and_heartbeat_never_overlap(self):
        await self.serial_case('executor')

    async def test_actual_permit_consume_shares_the_executor_lane(self):
        await self.serial_case('permit')

    async def test_actual_key_consumer_initial_read_shares_the_executor_lane(self):
        await self.serial_case('key-initial')

    async def test_actual_key_consumer_disclosure_read_shares_the_executor_lane(self):
        await self.serial_case('key-disclosure')

    async def test_repeated_cancel_keeps_lane_until_commit_unknown_thread_joins(self):
        await self.serial_case('executor', cancel=True)

    async def test_cancelled_lane_waiter_never_starts_a_thread_or_consumes_secret(self):
        executor = self.executor()
        entered, release = threading.Event(), threading.Event()
        effects = []

        def held():
            entered.set()
            if not release.wait(4):
                raise AssertionError('fixture DB thread not released')

        async def chain():
            owner = asyncio.create_task(executor._db(held))
            waiter = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                waiter = asyncio.create_task(async_runtime.settled_thread(effects.append, 'forbidden'))
                await asyncio.sleep(.03)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertEqual(effects, [])
            finally:
                release.set()
                await asyncio.gather(owner, *([] if waiter is None else [waiter]), return_exceptions=True)
            return executor._result('CHAIN_VERIFIED')

        executor._chain = chain
        self.assertEqual((await executor.run()).code, 'CHAIN_VERIFIED')
        self.assertEqual(effects, [])

    async def test_network_wait_does_not_hold_the_database_lane(self):
        executor = self.executor()
        network_waiting, release_network, heartbeat_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def chain():
            await executor._db(lambda: None)
            network_waiting.set()
            await release_network.wait()
            return executor._result('CHAIN_VERIFIED')
        async def heartbeat():
            await network_waiting.wait()
            await executor._db(lambda: None)
            heartbeat_done.set()
        executor._chain, executor._heartbeat = chain, heartbeat
        task = asyncio.create_task(executor.run())
        try:
            await asyncio.wait_for(heartbeat_done.wait(), 2)
            self.assertFalse(task.done())
        finally:
            release_network.set()
            await task

    async def test_distinct_executors_do_not_share_one_global_lane(self):
        first, second = self.executor(), self.executor()
        entered, release = threading.Event(), threading.Event()
        def held():
            entered.set()
            if not release.wait(4):
                raise AssertionError('fixture DB thread not released')
        async def first_chain():
            await first._db(held)
            return first._result('CHAIN_VERIFIED')
        async def second_chain():
            await second._db(lambda: None)
            return second._result('CHAIN_VERIFIED')
        first._chain, second._chain = first_chain, second_chain
        task = asyncio.create_task(first.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual((await asyncio.wait_for(second.run(), 2)).code, 'CHAIN_VERIFIED')
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_run_restores_parent_context_after_success_error_and_cancel(self):
        for outcome in ('success', 'error', 'cancel'):
            with self.subTest(outcome=outcome):
                executor = self.executor()
                async def chain():
                    self.assertIsNotNone(async_runtime._current_db_lane.get())
                    if outcome == 'error':
                        raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
                    if outcome == 'cancel':
                        raise asyncio.CancelledError()
                    return executor._result('CHAIN_VERIFIED')
                executor._chain = chain
                self.assertIsNone(async_runtime._current_db_lane.get())
                if outcome == 'cancel':
                    with self.assertRaises(asyncio.CancelledError):
                        await executor.run()
                else:
                    await executor.run()
                self.assertIsNone(async_runtime._current_db_lane.get())
