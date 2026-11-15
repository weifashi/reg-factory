"""Offline lifecycle proofs: no PG connection, credential lookup or HTTP."""
import asyncio
import copy
import gc
import pickle
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import weakref

from onboarding import live_effects, live_executor as m, live_runtime, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.google_cloud import GooglePlan
from onboarding.live_key_store import ServiceAccountKeyStore
from onboarding.sub2api_probe import ProbeConfig


class StopProofTests(unittest.IsolatedAsyncioTestCase):
    def executor(self):
        actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        worker = live_runtime._register(live_runtime.LiveWorker(
            actor, str(uuid4()), 1, str(uuid4()), 'fixture-revision', str(uuid4()),
            (str(uuid4()), None, 1, 'UNKNOWN'),
            live_effects.FenceBinding('mailbox', str(uuid4()), 'fixture-worker', 1, 1),
            'fixture-worker', 3))
        plan = GooglePlan('rf-stopped-123', 'AAAAAA-BBBBBB-CCCCCC', 'rf-stopped-sa',
                          actor.operator_id, worker.task_id, 'us-central1', 'roles/aiplatform.user')
        settings = object()
        keys = object.__new__(ServiceAccountKeyStore)
        keys._settings = settings
        # This intentionally incomplete key store isolates stop/join evidence.
        # Readiness is exercised with real key stores in the entry DB tests;
        # no DB/provider path is invoked by this lifecycle-only chain.
        with patch.object(m, 'validate_dependencies'):
            executor = m.LiveExecutor(settings, worker, plan, key_store=keys,
                google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
                sub2api_config=ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'fixture'))
        async def complete():
            return executor._result('CHAIN_VERIFIED')
        executor._chain = complete
        return executor

    def unavailable(self, executor):
        with self.assertRaises(ServiceError) as caught:
            executor.stop_proof()
        self.assertEqual(caught.exception.code, ErrorCode.RECONCILIATION_REQUIRED)

    async def test_result_and_closed_flag_are_not_stopping_evidence(self):
        executor = self.executor()
        self.unavailable(executor)
        executor._closed = True
        self.unavailable(executor)
        for supplied in (True, executor._result('CHAIN_VERIFIED'), object()):
            with self.assertRaises(ServiceError):
                m.require_stopped(supplied, executor._worker)

    async def test_direct_await_can_verify_proof_without_ending_parent_task(self):
        executor = self.executor()
        result = await executor.run()
        proof = executor.stop_proof()
        self.assertIs(m.require_stopped(proof, executor._worker), True)
        self.assertFalse(asyncio.current_task().done())
        self.assertFalse(result.task_finalized)
        self.assertFalse(result.holds_released)
        with self.assertRaises(ValueError):
            await executor.run()
        self.assertIs(executor.stop_proof(), proof)

    async def test_proof_rejects_construction_copy_pickle_wrong_worker_and_pid(self):
        executor, other = self.executor(), self.executor()
        await executor.run()
        proof = executor.stop_proof()
        for clone in (lambda: m.ExecutorStopProof(), lambda: copy.copy(proof),
                      lambda: copy.deepcopy(proof), lambda: pickle.dumps(proof)):
            with self.assertRaises(TypeError):
                clone()
        for supplied, worker in ((object.__new__(m.ExecutorStopProof), executor._worker),
                                 (proof, other._worker)):
            with self.assertRaises(ServiceError):
                m.require_stopped(supplied, worker)
        with patch.object(m.os, 'getpid', return_value=-1):
            with self.assertRaises(ServiceError):
                m.require_stopped(proof, executor._worker)
        self.assertNotIn(executor._worker.task_id, repr(proof))

    async def test_proof_retains_executor_but_weak_registry_does_not_leak_it(self):
        executor = self.executor()
        await executor.run()
        worker, proof = executor._worker, executor.stop_proof()
        reference = weakref.ref(executor)
        del executor
        gc.collect()
        self.assertIsNotNone(reference())
        self.assertIs(m.require_stopped(proof, worker), True)
        del proof
        gc.collect()
        self.assertIsNone(reference())

    async def test_no_proof_until_repeated_cancel_joins_commit_unknown_thread(self):
        executor = self.executor()
        entered, release = threading.Event(), threading.Event()
        def ambiguous():
            entered.set()
            release.wait(5)
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        async def chain():
            await executor._db(ambiguous)
            self.fail('cancelled worker resumed its chain')
        executor._chain = chain
        task = asyncio.create_task(executor.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                self.unavailable(executor)
                self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(executor._fatal_code, 'COMMIT_UNKNOWN')
        self.assertFalse(executor._db_tasks)
        self.assertTrue(executor._heartbeat_task.done())
        self.assertIs(m.require_stopped(executor.stop_proof(), executor._worker), True)
        self.assertIs(m.stop_observed_commit_unknown(executor.stop_proof(), executor._worker), True)

    async def test_finished_chain_waits_for_heartbeat_database_thread(self):
        executor = self.executor()
        entered, release = threading.Event(), threading.Event()
        def renew():
            entered.set()
            release.wait(5)
        async def heartbeat():
            await executor._db(renew)
        async def chain():
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            return executor._result('CHAIN_VERIFIED')
        executor._heartbeat, executor._chain = heartbeat, chain
        task = asyncio.create_task(executor.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                self.unavailable(executor)
                self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(executor._heartbeat_task.done())
        self.assertFalse(executor._db_tasks)
        self.assertIs(m.require_stopped(executor.stop_proof(), executor._worker), True)

    async def test_unknown_and_cleanup_failure_prove_only_local_stop(self):
        for code in ('UNKNOWN', 'COMMIT_UNKNOWN', 'CLEANUP_REQUIRED'):
            with self.subTest(code=code):
                executor = self.executor()
                async def chain():
                    raise m._Stop(code)
                executor._chain = chain
                result = await executor.run()
                self.assertEqual(result.code, code)
                proof = executor.stop_proof()
                self.assertIs(m.require_stopped(proof, executor._worker), True)
                self.assertFalse(hasattr(proof, 'code'))
                self.assertFalse(result.task_finalized)
                self.assertFalse(result.holds_released)

    async def test_cancel_during_unknown_recording_waits_for_its_thread(self):
        executor = self.executor()
        entered, release = threading.Event(), threading.Event()
        def record():
            entered.set()
            release.wait(5)
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        async def chain():
            raise m._Stop('UNKNOWN')
        async def preserve():
            await executor._db(record)
        executor._chain, executor._preserve_unknown = chain, preserve
        task = asyncio.create_task(executor.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            task.cancel()
            await asyncio.sleep(0)
            self.unavailable(executor)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(m.require_stopped(executor.stop_proof(), executor._worker), True)

    async def test_observed_commit_unknown_survives_later_heartbeat_error_and_field_changes(self):
        executor = self.executor()
        def fail(code):
            raise ServiceError(code)
        async def chain():
            await executor._db(fail, ErrorCode.COMMIT_UNKNOWN)
        async def heartbeat():
            await executor._heartbeat_stop.wait()
            try:
                await executor._db(fail, ErrorCode.DEPENDENCY_UNAVAILABLE)
            except ServiceError:
                pass
        executor._chain, executor._heartbeat = chain, heartbeat
        result = await executor.run()
        self.assertEqual(result.code, 'COMMIT_UNKNOWN')
        self.assertEqual(executor._fatal_code, 'DEPENDENCY_UNAVAILABLE')
        proof = executor.stop_proof()
        self.assertIs(m.stop_observed_commit_unknown(proof, executor._worker), True)
        executor._fatal_code = None
        executor._observation_commit_unknown = False
        executor._observed_commit_unknown = False
        self.assertIs(m.stop_observed_commit_unknown(proof, executor._worker), True)

    async def test_observed_commit_unknown_is_frozen_not_a_caller_assertion(self):
        executor = self.executor()
        await executor.run()
        proof = executor.stop_proof()
        executor._fatal_code = 'COMMIT_UNKNOWN'
        executor._observation_commit_unknown = True
        executor._observed_commit_unknown = True
        self.assertIs(m.stop_observed_commit_unknown(proof, executor._worker), False)
        with self.assertRaises(ServiceError):
            m.stop_observed_commit_unknown(True, executor._worker)
        with self.assertRaises(ServiceError):
            m.stop_observed_commit_unknown(proof, self.executor()._worker)

    async def test_key_sink_nested_database_unknown_is_frozen_in_proof(self):
        executor = self.executor()
        async def token():
            self.fail('offline sink test must not request a token')
        async def chain():
            adapter = executor._cloud(SimpleNamespace(step_key='service_account.key.create'), str(uuid4()))
            await adapter._sink(b'fixture-never-decoded', 'fixture-external-ref')
        executor._chain = chain
        with patch.object(m, '_make_token_provider', return_value=token), \
                patch.object(ServiceAccountKeyStore, 'save', side_effect=ServiceError(ErrorCode.COMMIT_UNKNOWN)):
            result = await executor.run()
        self.assertEqual(result.code, 'COMMIT_UNKNOWN')
        self.assertIs(m.stop_observed_commit_unknown(executor.stop_proof(), executor._worker), True)

    async def test_unwrapped_consumer_commit_unknown_is_frozen_on_run_boundary(self):
        executor = self.executor()
        async def chain():
            # The key consumer's first settled_thread error escapes directly;
            # it need not have passed through executor._db to veto completion.
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        executor._chain = chain
        await executor.run()
        self.assertIs(m.stop_observed_commit_unknown(executor.stop_proof(), executor._worker), True)
