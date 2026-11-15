"""One task, real isolated PG bookkeeping and adapters, HTTP mocks only.

No Google credential lookup or Sub2API secret files: private factories replace
only the token subprocess/target-proc inspection. No synthetic task is promoted
outside the manifest-scoped fixture. Completion is not task success/release.
"""
import asyncio
import base64
from contextlib import ExitStack
from dataclasses import replace
import importlib
import importlib.util
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onboarding import live_commands, live_runtime, live_effects
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.google_cloud import GoogleObservation, GooglePlan
from onboarding.live_key_store import ServiceAccountKeyStore
from onboarding.migrate import apply_all
from onboarding.sub2api_probe import ProbeConfig


class Availability(unittest.TestCase):
    def test_composer_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_executor'))


class ThreadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_journal_record_is_read_only_without_a_new_permit(self):
        m = importlib.import_module('onboarding.live_executor')
        executor = object.__new__(m.LiveExecutor)
        executor._has_new_permit = False
        executor._observation_commit_unknown = False
        executor._last_stored = None
        executor._current_context = SimpleNamespace(step_key='project.create')
        executor._record = AsyncMock()
        executor._prepared(live_effects.JournalRecord('fixture-existing', 'INTENT'))
        await executor._preserve_unknown()
        executor._record.assert_not_awaited()
        # A fresh committed capability, including one returned just as cancel
        # arrives, still needs the ordinary conservative UNKNOWN bookkeeping.
        executor._prepared(live_effects.PreparedEffect('fixture-new', 'INTENT', 'a' * 64))
        await executor._preserve_unknown()
        executor._record.assert_awaited_once()

    async def test_record_commit_unknown_racing_cancel_remains_no_retry(self):
        m = importlib.import_module('onboarding.live_executor')
        executor = object.__new__(m.LiveExecutor)
        executor._fatal_code = None
        executor._db_tasks = set()
        executor._closed = executor._observation_commit_unknown = False
        executor._current_context, executor._receipt_id = object(), 'fixture-only'
        executor._last_stored = None
        entered, release = threading.Event(), threading.Event()
        def ambiguous(*args):
            entered.set()
            release.wait(2)
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        executor._record_sync = ambiguous
        task = asyncio.create_task(executor._record(GoogleObservation('UNKNOWN', 'EXECUTOR_STOPPED')))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(executor._observation_commit_unknown,
                        'Cancelled COMMIT_UNKNOWN must never retry recording an observation')


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = importlib.import_module('onboarding.live_executor')
        self.assertTrue(hasattr(self.m, 'LiveExecutor'), 'closed composition is missing')
        from test_onboarding_live_runtime import LiveRuntimeDBTests
        from onboarding import live_run_store
        self.store_module = live_run_store
        self.fixture_case = LiveRuntimeDBTests()
        self.addCleanup(self.fixture_case.doCleanups)
        self.fixture_case.setUp()
        case = self.fixture_case
        self.settings, self.actor, self.tid = case.settings, case.actor, case.tid
        with case.fixture.migrator() as conn:
            apply_all(conn, case.fixture.schema, target_version=4)
        self.plan = case.plan
        with case.uow() as conn:
            self.worker = live_runtime.handoff(conn, self.actor, self.tid, 2, 'executor-fixture')
            live_run_store.bind_plan(conn, self.worker, self.plan)
        self.keys = ServiceAccountKeyStore(self.settings, case.keyring)
        self.config = ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'fixture-password')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.material = dict(type='service_account', project_id=self.plan.project_id,
            client_email=self.plan.sa_email, private_key_id='a' * 40,
            token_uri='https://oauth2.googleapis.com/token', private_key=key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode())
        self.seen, self.tokens, self.target_checks = [], [], []
        self.cloud_override = self.import_override = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.m, '_make_token_provider', self.fake_token))
        self.stack.enter_context(patch.object(self.m, '_make_target_guard', self.fake_target))
        self.stack.enter_context(patch.object(self.m, '_POLL_SECONDS', .001))

    def read(self, query, values=()):
        return self.fixture_case.read(query, values)

    def fake_token(self, config, *, guard):
        self.assertEqual(config.account, 'weifashi68@gmail.com')
        async def token():
            for action in ('auth.identity', 'auth.identity', 'auth.token', 'auth.token'):
                self.tokens.append(action)
                if await guard(action) is not True:
                    raise ValueError('fixture-token-source-denied')
            return 'fixture-access-token'
        return token

    def fake_target(self, *, task_guard):
        owner = self
        class Target:
            async def __call__(self, action):
                owner.target_checks.append(action)
                return await task_guard(action) is True
            async def cleanup(self, action):
                owner.target_checks.append(action)
                return action == 'sub2api.logout'
        return Target()

    async def cloud_http(self, request):
        self.seen.append(request)
        if self.cloud_override:
            value = await self.cloud_override(request)
            if value is not None:
                return value
        path, method = request.url.path, request.method
        if path.startswith('/v1/billingAccounts/'):
            body = {'name': 'billingAccounts/' + self.plan.billing_account, 'open': True}
        elif method == 'POST' and path == '/v3/projects':
            body = {'name': 'operations/project-fixture'}
        elif '/operations/' in path:
            body = {'done': True}
        elif path.endswith('/billingInfo'):
            body = {'billingAccountName': 'billingAccounts/' + self.plan.billing_account
                    if method == 'PUT' else '', 'billingEnabled': method == 'PUT'}
        elif path.endswith('aiplatform.googleapis.com:enable'):
            body = {'name': 'operations/service-fixture'}
        elif path.endswith('aiplatform.googleapis.com'):
            body = {'name': 'projects/12345/services/aiplatform.googleapis.com', 'state': 'ENABLED'}
        elif path.endswith(':getIamPolicy'):
            body = {'etag': 'fixture-etag', 'version': 3, 'bindings': []}
        elif path.endswith(':setIamPolicy'):
            body = json.loads(request.content)['policy']
        elif path.endswith('/keys'):
            body = ({'keys': []} if method == 'GET' else {
                'name': 'projects/' + self.plan.project_id + '/serviceAccounts/' + self.plan.sa_email
                        + '/keys/' + self.material['private_key_id'],
                'privateKeyData': base64.b64encode(json.dumps(self.material).encode()).decode()})
        elif '/serviceAccounts' in path:
            body = {'projectId': self.plan.project_id, 'email': self.plan.sa_email,
                    'description': 'rf-task:' + self.tid, 'uniqueId': '98765'}
        elif path == '/v3/projects/' + self.plan.project_id:
            body = {'projectId': self.plan.project_id, 'state': 'ACTIVE', 'name': 'projects/12345',
                    'labels': {'rf-task': self.tid, 'rf-owner': self.actor.operator_id}}
        else:
            raise AssertionError('Unexpected fixture cloud route')
        return httpx.Response(200, json=body)

    async def import_http(self, request):
        self.seen.append(request)
        if self.import_override:
            value = await self.import_override(request)
            if value is not None:
                return value
        responses = {
            '/api/v1/auth/login': {'access_token': 'fixture-import-token', 'refresh_token': 'fixture-refresh'},
            '/api/v1/admin/compliance': {'required': False, 'version': 'fixture'},
            '/api/v1/admin/groups/all': [{'id': 2, 'name': 'regfactory-gemini', 'platform': 'gemini', 'status': 'active'}],
            '/api/v1/admin/accounts': {'id': 17},
            '/api/v1/admin/accounts/17': {'id': 17, 'name': 'rf-google-' + self.tid,
                'platform': 'gemini', 'type': 'service_account', 'status': 'inactive', 'schedulable': False,
                'group_ids': [2], 'credentials': {'project_id': self.plan.project_id,
                    'location': self.plan.region, 'client_email': self.plan.sa_email},
                'credentials_status': {'has_service_account_json': True}},
            '/api/v1/auth/logout': {},
        }
        return httpx.Response(200, json={'code': 0, 'data': responses[request.url.path]})

    def executor(self, **changes):
        args = dict(key_store=self.keys, google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
                    sub2api_config=self.config, google_transport=httpx.MockTransport(self.cloud_http),
                    sub2api_transport=httpx.MockTransport(self.import_http))
        args.update(changes)
        return self.m.LiveExecutor(self.settings, self.worker, self.plan, **args)

    def posts(self, path):
        return [r for r in self.seen if r.method == 'POST' and r.url.path == path]

    async def test_full_chain_uses_real_committed_receipts_encryption_and_disabled_import(self):
        executor = self.executor()
        result = await executor.run()
        self.assertEqual(result.code, 'CHAIN_VERIFIED', repr(result) + repr(
            self.read('SELECT action,phase,result_code FROM live_effect_journal ORDER BY created_at')))
        self.assertFalse(result.task_finalized)
        self.assertFalse(result.holds_released)
        self.assertIs(self.m.require_stopped(executor.stop_proof(), self.worker), True)
        rows = self.read('SELECT action,phase FROM live_effect_journal ORDER BY created_at')
        self.assertEqual(rows, [(action, 'SUCCEEDED') for action in live_runtime.ACTION_SEQUENCE])
        self.assertEqual(self.read('SELECT status,current_step FROM onboarding_tasks')[0],
                         ('RUNNING', 'sub2api.import_disabled'))
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0], ('HELD',))
        self.assertEqual(self.read("SELECT usage_status FROM mailbox_platform_states WHERE platform='google'"),
                         [('RESERVED',)])
        self.assertEqual(self.read("SELECT DISTINCT usage_status FROM mailbox_platform_states WHERE platform<>'google'"),
                         [('HISTORY_UNRECONCILED',)])
        self.assertEqual(len(self.posts('/v3/projects')), 1)
        imports = self.posts('/api/v1/admin/accounts')
        self.assertEqual(len(imports), 1)
        self.assertIs(json.loads(imports[0].content)['create_disabled'], True)
        self.assertEqual(imports[0].headers['idempotency-key'], result.receipt_id)
        evidence = self.read('SELECT row_to_json(live_effect_observations) FROM live_effect_observations')
        self.assertGreaterEqual(len(evidence), 9)
        snapshot = repr(evidence) + repr(executor) + repr(result)
        for private in ('PRIVATE KEY', 'fixture-password', 'fixture-access-token'):
            self.assertNotIn(private, snapshot)
        secret = self.read("SELECT ciphertext FROM secret_objects WHERE kind='service_account_json'")
        self.assertEqual(len(secret), 1)
        self.assertNotIn(b'PRIVATE KEY', secret[0][0])
        with self.assertRaises(ValueError):
            await executor.run()

    async def test_mismatched_mailbox_account_prevents_every_provider_request(self):
        with self.fixture_case.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET email_norm='other@fixture.invalid'")
        result = await self.executor().run()
        self.assertNotEqual(result.code, 'CHAIN_VERIFIED')
        self.assertEqual(self.seen, [])
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal')[0][0], 0)

    async def test_preflight_without_open_billing_creates_no_intent_or_cloud_resource(self):
        async def closed(request):
            if request.url.path.startswith('/v1/billingAccounts/'):
                return httpx.Response(200, json={'name': 'billingAccounts/' + self.plan.billing_account, 'open': False})
        self.cloud_override = closed
        result = await self.executor().run()
        self.assertEqual(result.code, 'BLOCKED')
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal')[0][0], 0)
        self.assertEqual(self.posts('/v3/projects'), [])

    async def test_unknown_write_is_recorded_held_and_never_retried(self):
        async def lost(request):
            if request.method == 'POST' and request.url.path == '/v3/projects':
                raise httpx.ReadTimeout('fixture-access-token')
        self.cloud_override = lost
        result = await self.executor().run()
        self.assertEqual(result.code, 'UNKNOWN')
        self.assertEqual(len(self.posts('/v3/projects')), 1)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('UNKNOWN',)])
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases'), [('UNKNOWN',)])
        self.assertNotIn('fixture-access-token', repr(result))

    async def test_conflict_is_not_reinterpreted_as_success(self):
        async def conflict(request):
            if request.method == 'POST' and request.url.path == '/v3/projects':
                return httpx.Response(409, json={})
        self.cloud_override = conflict
        result = await self.executor().run()
        self.assertEqual(result.code, 'CONFLICT')
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('UNKNOWN',)])

    async def test_lro_polling_is_bounded_read_only_and_keeps_unknown(self):
        async def pending(request):
            if '/operations/' in request.url.path:
                return httpx.Response(200, json={'done': False})
        self.cloud_override = pending
        result = await self.executor().run()
        self.assertEqual(result.code, 'POLL_EXHAUSTED')
        self.assertEqual(len(self.posts('/v3/projects')), 1)
        polls = [r for r in self.seen if '/operations/' in r.url.path]
        self.assertEqual(len(polls), self.m._MAX_POLLS)
        self.assertTrue(all(r.method == 'GET' for r in polls))
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('UNKNOWN',)])

    async def test_task_cancel_after_send_records_observation_but_does_not_poll_or_advance(self):
        async def cancel(request):
            if request.method == 'POST' and request.url.path == '/v3/projects':
                case = self.fixture_case
                version = case.read('SELECT version FROM onboarding_tasks WHERE id=%s', (self.tid,))[0][0]
                await asyncio.to_thread(live_commands.cancel, self.settings, self.actor, self.tid,
                    version, 'executor:cancel', policy=case.live_policy, mac=case.mac)
        self.cloud_override = cancel
        result = await self.executor().run()
        self.assertNotEqual(result.code, 'CHAIN_VERIFIED')
        self.assertFalse(any('/operations/' in r.url.path for r in self.seen))
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('UNKNOWN',)])

    async def test_async_cancellation_after_send_preserves_unknown_and_cleans_heartbeat(self):
        started = asyncio.Event()
        async def hang(request):
            if request.method == 'POST' and request.url.path == '/v3/projects':
                started.set()
                await asyncio.Event().wait()
        self.cloud_override = hang
        executor = self.executor()
        task = asyncio.create_task(executor.run())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('UNKNOWN',)])
        self.assertTrue(executor._heartbeat_task.done())
        self.assertIs(self.m.require_stopped(executor.stop_proof(), self.worker), True)
        self.assertEqual(len(self.posts('/v3/projects')), 1)

    async def test_unknown_prepare_commit_does_not_send_or_reconstruct_permit(self):
        prepare = live_effects.prepare_committed
        def ambiguous(*args):
            prepare(*args)
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(live_effects, 'prepare_committed', ambiguous):
            result = await self.executor().run()
        self.assertEqual(result.code, 'COMMIT_UNKNOWN')
        self.assertEqual(self.posts('/v3/projects'), [])
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal'), [('INTENT',)])

    async def test_unknown_observation_commit_stops_before_any_poll(self):
        executor = self.executor()
        record = executor._record_sync
        def ambiguous(*args):
            record(*args)
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(executor, '_record_sync', ambiguous):
            result = await executor.run()
        self.assertEqual(result.code, 'COMMIT_UNKNOWN')
        self.assertFalse(any('/operations/' in r.url.path for r in self.seen))
        self.assertEqual(len(self.posts('/v3/projects')), 1)

    async def test_key_storage_unknown_never_creates_second_key_or_imports(self):
        with patch.object(ServiceAccountKeyStore, 'save', side_effect=ServiceError(ErrorCode.COMMIT_UNKNOWN)):
            result = await self.executor().run()
        self.assertEqual(result.code, 'COMMIT_UNKNOWN')
        key_posts = [r for r in self.seen if r.method == 'POST' and r.url.path.endswith('/keys')]
        self.assertEqual(len(key_posts), 1)
        self.assertEqual(self.posts('/api/v1/admin/accounts'), [])
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal ORDER BY created_at')[-1], ('UNKNOWN',))

    async def test_import_unknown_stops_without_reimport_or_plaintext_recovery(self):
        async def lost(request):
            if request.method == 'POST' and request.url.path == '/api/v1/admin/accounts':
                raise httpx.ReadTimeout('fixture-import-token')
        self.import_override = lost
        result = await self.executor().run()
        self.assertEqual(result.code, 'UNKNOWN')
        self.assertEqual(len(self.posts('/api/v1/admin/accounts')), 1)
        self.assertEqual(len(self.posts('/api/v1/auth/logout')), 1)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal ORDER BY created_at')[-1], ('UNKNOWN',))

    async def test_cancel_waits_for_key_consumer_database_thread_and_never_imports(self):
        entered, release = threading.Event(), threading.Event()
        consume = ServiceAccountKeyStore._for_import
        def paused(store, *args):
            entered.set()
            if not release.wait(8):
                raise AssertionError('fixture consumer was not released')
            return consume(store, *args)
        executor = self.executor()
        with patch.object(ServiceAccountKeyStore, '_for_import', paused):
            task = asyncio.create_task(executor.run())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 60))
                task.cancel()
                await asyncio.sleep(.03)
                self.assertFalse(task.done(), 'cancel returned while key-consumer DB thread was still running')
                with self.assertRaises(ServiceError):
                    executor.stop_proof()
            finally:
                release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.assertEqual(self.posts('/api/v1/admin/accounts'), [])
        self.assertIs(self.m.require_stopped(executor.stop_proof(), self.worker), True)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal ORDER BY created_at')[-1], ('UNKNOWN',))

    async def test_logout_failure_never_claims_chain_verified(self):
        async def lost(request):
            if request.url.path == '/api/v1/auth/logout':
                raise httpx.ReadTimeout('fixture-refresh')
        self.import_override = lost
        executor = self.executor()
        result = await executor.run()
        self.assertEqual(result.code, 'CLEANUP_REQUIRED')
        self.assertIs(self.m.require_stopped(executor.stop_proof(), self.worker), True)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal ORDER BY created_at')[-1], ('SUCCEEDED',))

    async def test_heartbeat_failure_closes_send_gate(self):
        async def slow(request):
            if request.url.path.startswith('/v1/billingAccounts/'):
                await asyncio.sleep(.04)
        self.cloud_override = slow
        with patch.object(self.m, '_HEARTBEAT_SECONDS', .01), \
                patch.object(live_runtime, 'heartbeat', side_effect=ServiceError(ErrorCode.STALE_FENCE)):
            result = await self.executor().run()
        self.assertEqual(result.code, 'STALE_FENCE')
        self.assertEqual(self.posts('/v3/projects'), [])

    async def test_heartbeat_failure_during_disclosure_wait_prevents_key_post(self):
        entered, release = threading.Event(), threading.Event()
        heartbeat_queued = asyncio.Event()
        consume, renew = ServiceAccountKeyStore._for_import, live_runtime.heartbeat
        count = 0
        def delayed(store, *args):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
                if not release.wait(8):
                    raise AssertionError('fixture disclosure was not released')
            return consume(store, *args)
        def heartbeat(*args):
            if entered.is_set():
                raise ServiceError(ErrorCode.STALE_FENCE)
            return renew(*args)
        executor = self.executor()
        database = executor._db
        async def queued_database(function, *args, **kwargs):
            if function == executor._heartbeat_sync and entered.is_set():
                heartbeat_queued.set()
            return await database(function, *args, **kwargs)
        with patch.object(ServiceAccountKeyStore, '_for_import', delayed), \
                patch.object(live_runtime, 'heartbeat', heartbeat), \
                patch.object(executor, '_db', queued_database), \
                patch.object(self.m, '_HEARTBEAT_SECONDS', .05):
            task = asyncio.create_task(executor.run())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 60))
                # The disclosure DB thread now owns the short DB lane. Wait
                # for heartbeat to queue, not to run through that owned lane.
                await asyncio.wait_for(heartbeat_queued.wait(), 3)
                self.assertIsNone(executor._fatal_code)
            finally:
                release.set()
                # Always join the executor, even when a fixture assertion fails.
                result = await task
        self.assertEqual(result.code, 'STALE_FENCE')
        self.assertEqual(self.posts('/api/v1/admin/accounts'), [])
        self.assertEqual(len(self.posts('/api/v1/auth/logout')), 1)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal ORDER BY created_at')[-1], ('UNKNOWN',))

    async def test_public_constructor_has_no_factory_or_allow_guard_escape_hatch(self):
        for changes in ({'token_factory': self.fake_token}, {'guard': lambda _: True},
                        {'target_factory': self.fake_target}, {'key_store': object()}):
            with self.assertRaises((TypeError, ValueError, ServiceError)):
                self.executor(**changes)
        self.assertEqual(self.seen, [])
