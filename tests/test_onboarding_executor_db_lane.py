"""Real fixture PG task lock; no provider I/O or relaxed DB timeouts.

The counterfactual red run replaces only executor._worker_db_lane with
contextlib.nullcontext in the test process. It must fail the assertion that no
heartbeat DB transaction starts while the first task lock remains held.
"""
import asyncio
import threading
import time
import unittest

from onboarding import live_executor, live_runtime, storage
from onboarding.errors import ServiceError


class ExecutorDatabaseLaneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_onboarding_live_executor import ExecutorTests
        self.case = ExecutorTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    async def test_long_owned_task_lock_does_not_start_competing_heartbeat_transaction(self):
        executor = self.case.executor()
        entered, release, heartbeat_started = threading.Event(), threading.Event(), threading.Event()
        heartbeat_attempting = asyncio.Event()
        metadata = {'held_ms': None, 'heartbeat_sqlstate': None, 'heartbeat_transactions': 0}
        settings, worker = self.case.settings, self.case.worker

        def held_transaction():
            with storage.unit_of_work(settings) as conn:
                task, _, secrets = live_runtime._worker_graph(conn, worker)
                live_runtime._final(conn, worker.actor, task, worker.binding, secrets)
                self.assertEqual(conn.execute('SHOW lock_timeout').fetchone(), ('2s',))
                start = time.monotonic()
                entered.set()
                if not release.wait(6):
                    raise AssertionError('fixture task lock was not released')
                metadata['held_ms'] = round((time.monotonic()-start)*1000, 3)
                # Keep the actual session/lease/credential deadline checks.
                live_runtime._final(conn, worker.actor, task, worker.binding, secrets)

        def heartbeat_transaction():
            metadata['heartbeat_transactions'] += 1
            heartbeat_started.set()
            try:
                executor._heartbeat_sync()
            except ServiceError as exc:
                item, seen = exc, set()
                while item is not None and id(item) not in seen:
                    seen.add(id(item))
                    if getattr(item, 'sqlstate', None) is not None:
                        metadata['heartbeat_sqlstate'] = item.sqlstate
                    item = item.__cause__ or item.__context__
                raise

        async def heartbeat():
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            heartbeat_attempting.set()
            try:
                await executor._db(heartbeat_transaction)
            except ServiceError:
                pass  # Match the real heartbeat's stop-on-error behavior.

        async def chain():
            await executor._db(held_transaction)
            return executor._result('CHAIN_VERIFIED')

        executor._chain, executor._heartbeat = chain, heartbeat
        running = asyncio.create_task(executor.run())
        try:
            await asyncio.wait_for(heartbeat_attempting.wait(), 4)
            # Controlled fixture delay, greater than the unchanged 2s timeout.
            await asyncio.sleep(2.3)
            self.assertFalse(heartbeat_started.is_set(), metadata)
            self.assertFalse(running.done())
            release.set()
            result = await running
            self.assertEqual(result.code, 'CHAIN_VERIFIED', metadata)
            self.assertGreater(metadata['held_ms'], 2000)
            self.assertEqual(metadata['heartbeat_transactions'], 1)
            self.assertIsNone(metadata['heartbeat_sqlstate'])
            self.assertIs(live_executor.require_stopped(executor.stop_proof(), worker), True)
            self.assertEqual(self.case.seen, [])
            self.assertEqual(self.case.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
