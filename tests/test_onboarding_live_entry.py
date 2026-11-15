"""Offline entry boundaries. No provider requests or persistent database."""
import importlib
import importlib.util
import unittest
from uuid import uuid4

from onboarding.errors import ServiceError


class LiveEntryInputTests(unittest.TestCase):
    def test_invalid_dependencies_cannot_create_a_task_or_take_a_hold(self):
        from unittest.mock import patch
        from onboarding import live_entry
        with patch.object(live_entry.live_batches, 'create_one', side_effect=AssertionError('created before validation')):
            with self.assertRaises((ServiceError, ValueError)):
                live_entry.start_committed(object(), object(), str(uuid4()), 'pool-'+str(uuid4()), 'entry',
                    policy=object(), mac=object(), admission=object(), key_store=object(),
                    google_auth_config=object(), sub2api_config=object())

    def test_mutating_an_actor_alias_cannot_move_an_admission_to_another_session(self):
        from onboarding import live_admission, security
        actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        # This test signs no provider observation; it exercises registry identity
        # using an internal test-only object, never a public issuer endpoint.
        from pathlib import Path
        from onboarding.settings import Settings
        from onboarding.live_mailbox_source import OwnedMailboxSource
        import json
        import time
        settings = Settings('test', 55433, 'test', 'test', 'not-a-real-password', 'test', 'test')
        mid, revision = str(uuid4()), 'pool-' + str(uuid4())
        source = OwnedMailboxSource(Path('/tmp/test-entry-source'), actor.operator_id, 'weifashi68@gmail.com')
        grant = live_admission._register(live_admission.Admission(settings, actor, source,
            json.dumps({'mailbox_id': mid, 'config_revision': revision}), time.monotonic() + 60))
        object.__setattr__(actor, 'session_id', str(uuid4()))
        with self.assertRaises(ServiceError):
            live_admission.consume(grant, settings, actor, mid, revision)

    def test_entry_modules_exist(self):
        for name in ('live_admission', 'live_batches', 'live_entry'):
            self.assertIsNotNone(importlib.util.find_spec('onboarding.' + name), name)

    def test_foreign_admission_cannot_be_consumed(self):
        m = importlib.import_module('onboarding.live_admission')
        with self.assertRaises(ServiceError):
            m.consume(object(), object(), object(), str(uuid4()), 'pool-' + str(uuid4()))

    def test_invalid_batch_context_never_opens_database(self):
        m = importlib.import_module('onboarding.live_batches')
        with self.assertRaises(ServiceError):
            m.create_one(object(), object(), str(uuid4()), 'pool-' + str(uuid4()),
                         'entry', policy=object(), mac=object(), admission=object())

    def test_replay_never_constructs_executor(self):
        from unittest.mock import patch
        m = importlib.import_module('onboarding.live_entry')
        with patch.object(m, 'validate_dependencies'), \
                patch.object(m.live_batches, 'create_one', return_value={'replayed': True}) as create, \
                patch.object(m, 'LiveExecutor', side_effect=AssertionError('must not start')):
            result = m.start_committed(object(), object(), str(uuid4()), 'pool-' + str(uuid4()),
                'entry', policy=object(), mac=object(), admission=None,
                key_store=object(), google_auth_config=object(), sub2api_config=object())
        self.assertEqual(result.result, {'replayed': True})
        self.assertIsNone(result.executor)
        create.assert_called_once()

    def test_second_commit_finishes_with_send_guard_after_policy_check(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from onboarding import live_entry
        from test_onboarding_live_config import FIELDS
        calls = []
        actor = SimpleNamespace(operator_id=str(uuid4()))
        task_id = str(uuid4())
        worker = SimpleNamespace(config_id=str(uuid4()))
        policy = Mock(); policy._connection.side_effect = lambda conn: calls.append('policy')
        conn = Mock(); conn.execute.return_value.fetchone.return_value = (FIELDS.copy(),)
        @contextmanager
        def uow(settings):
            yield conn
            self.assertEqual(calls[-1], 'guard')
        with patch.object(live_entry, 'validate_dependencies'), \
                patch.object(live_entry.live_batches, 'create_one', return_value={'replayed': False, 'task_ids':[task_id]}), \
                patch.object(live_entry.storage, 'unit_of_work', uow), \
                patch.object(live_entry.live_runtime, 'handoff', return_value=worker), \
                patch.object(live_entry.live_run_store, 'bind_plan'), \
                patch.object(live_entry.live_runtime, 'sending_guard', side_effect=lambda *args: calls.append('guard')), \
                patch.object(live_entry, 'LiveExecutor'):
            live_entry.start_committed(object(), actor, str(uuid4()), 'pool-'+str(uuid4()), 'entry',
                policy=policy, mac=object(), admission=object(), key_store=object(),
                google_auth_config=object(), sub2api_config=object())


class AdmissionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_database_thread_without_observation_or_grant(self):
        import asyncio
        from contextlib import contextmanager
        from pathlib import Path
        import threading
        from unittest.mock import Mock, patch
        from onboarding import live_admission, security
        from onboarding.google_auth_runtime import GoogleAuthConfig
        from onboarding.live_mailbox_source import OwnedMailboxSource
        entered, release = threading.Event(), threading.Event()
        actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        source = OwnedMailboxSource(Path('/tmp/test-entry-source'), actor.operator_id, 'weifashi68@gmail.com')
        @contextmanager
        def uow(settings):
            entered.set()
            release.wait(3)
            yield object()
        policy = Mock()
        with patch.object(live_admission, 'inputs'), patch.object(live_admission.storage, 'unit_of_work', uow), \
                patch.object(live_admission, 'inspect', return_value={}), \
                patch.object(live_admission, 'GoogleCloudAdapter') as adapter, \
                patch.object(live_admission, '_register') as register:
            task = asyncio.create_task(live_admission.prepare(object(), actor, source, str(uuid4()), 'pool-'+str(uuid4()),
                policy=policy, mac=object(), google_auth_config=GoogleAuthConfig('weifashi68@gmail.com')))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0.03)
                self.assertFalse(task.done(), 'cancel left a database thread running after the caller exited')
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            adapter.assert_not_called()
            register.assert_not_called()
