"""No network/PG: cancellation must settle DB threads before leaving a consumer."""
import asyncio
import importlib
import importlib.util
import threading
import unittest


class SettledThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.async_runtime'), 'cancel-safe thread boundary missing')
        self.call = importlib.import_module('onboarding.async_runtime').settled_thread

    async def test_success_and_exception_are_not_rewritten(self):
        self.assertEqual(await self.call(lambda a, *, b: a+b, 2, b=3), 5)
        def fail():
            raise ValueError('fixed-fixture-error')
        with self.assertRaises(ValueError):
            await self.call(fail)

    async def test_cancellation_waits_for_thread_and_does_not_run_consumer(self):
        started, release, ended = threading.Event(), threading.Event(), threading.Event()
        consumer = []
        def blocked():
            started.set()
            if not release.wait(3):
                raise RuntimeError('fixture not released')
            ended.set()
            return 'not-to-be-returned'
        async def use():
            value = await self.call(blocked)
            consumer.append(value)
        task = asyncio.create_task(use())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done(), 'consumer returned while DB thread still running')
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done(), 'repeated cancellation abandoned DB thread')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(ended.is_set())
            self.assertEqual(consumer, [])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_thread_failure_after_cancel_still_propagates_cancellation(self):
        started, release, ended = threading.Event(), threading.Event(), threading.Event()
        def blocked():
            started.set(); release.wait(3); ended.set()
            raise ValueError('private-thread-error-canary')
        task = asyncio.create_task(self.call(blocked))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(ended.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
