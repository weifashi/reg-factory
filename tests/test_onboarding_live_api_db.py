"""Real isolated PG + ASGI session/CSRF + live manager composition.

The lifecycle cases compose WebConfig from the disposable fixture. The third
case imports the actual server using private BOOT files and real install. No
BOOT validation is mocked. Production registrations and real auth/services run.
Only SDK paths/token and Billing GET are external doubles; _chain is parked
before its first effect in the lifecycle case. No real cloud/Sub2API is called.
"""
import asyncio
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import psycopg
from psycopg.pq import TransactionStatus

from onboarding import live_admission, live_completion, live_entry, live_executor
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding.live_key_store import ServiceAccountKeyStore
from onboarding.live_manager import LiveRunManager
from onboarding.sub2api_probe import ProbeConfig
from onboarding_live_support import populate
from onboarding_pool_support import PoolCase, after_query
from test_onboarding_live_config import FIELDS
from webui import onboarding_auth, onboarding_pool_routes, onboarding_routes

ORIGIN = 'https://fixture.local'


class LiveApiDBTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = PoolCase()
        self.addCleanup(self.db.doCleanups)
        self.db.setUp()
        populate(self.db, create=False)
        self.assertEqual(self.db.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])
        self.requests = []
        self.body = dict(mailbox_id=self.db.resource['id'],
                         expected_config_revision=self.db.cfg['revision'], request_key='http:start')
        self.headers = {'Origin': ORIGIN, 'X-CSRF-Token': self.db.csrf}

    def client_and_manager(self):
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
        self.addAsyncCleanup(manager.shutdown)
        config = onboarding_auth.WebConfig(mode='protected', expected_origin=ORIGIN,
            settings=db.settings, ready=True, pool_mode='live', pool_ready=True,
            pool_keyring=db.keyring, pool_mac=db.mac, live_policy=db.live_policy,
            live_vault=db.live_vault, live_source=db.source, live_manager=manager,
            live_mailbox_id=db.resource['id'])
        app = FastAPI()
        app.state.onboarding_config = config
        onboarding_auth._install_auth_routes(app, config)
        app.add_middleware(onboarding_auth.ProtectionMiddleware, config=config)
        # Use the actual old-URL helper dispatch; do not register shadow routes.
        onboarding_routes.register_routes(app, config)
        onboarding_pool_routes.register_routes(app, config)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
            cookies={onboarding_auth.SESSION_COOKIE: db.token, onboarding_auth.CSRF_COOKIE: db.csrf})
        self.addAsyncCleanup(client.aclose)
        return client, manager

    @contextmanager
    def authorized_sdk(self):
        with patch('onboarding.google_auth_runtime._verify_paths'), \
                patch('onboarding.live_admission.GoogleTokenProvider.__call__',
                      new=AsyncMock(return_value='unit-test-authorized-token')):
            yield

    def snapshot(self):
        result = self.db.business_snapshot()
        for table in ('live_run_plans', 'live_effect_journal', 'live_effect_observations',
                      'approvals', 'billing_identities', 'payment_cards', 'card_reservations', 'card_account_links',
                      'operators', 'auth_throttles', 'download_grants'):
            result[table] = self.db.read('SELECT * FROM ' + table + ' ORDER BY 1')
        # Read real token/CSRF hashes too, but never put their values in assertion
        # output. HTTP authentication may touch activity, never rotate credentials
        # or extend either deadline merely because a receipt/page was observed.
        result['operator_sessions'] = self.db.read('SELECT id,operator_id,token_hash,csrf_hash,auth_epoch,'
            'expires_at,idle_expires_at,revoked_at,created_at,version,updated_at '
            'FROM operator_sessions ORDER BY id')
        return result

    def assert_observation_only(self, before, *, touches):
        """Permit exactly one audited session touch per successful HTTP guard.

        All business tables and existing audit rows remain byte-for-byte equal.
        New audit rows must be ONLY this actor/session's auth.session_touch with
        the exact version transition; no secret/token/effect/other audit changes
        are hidden by filtering an entire table or broadly excluding auth.*.
        """
        after = self.snapshot()
        self.assertTrue(after.keys() == before.keys(), 'Snapshot table set changed')
        for table in before:
            if table not in ('audit_events', 'operator_sessions'):
                self.assertTrue(after[table] == before[table], 'Unexpected mutation in ' + table)
        sessions = before['operator_sessions']
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(after['operator_sessions']), 1)
        previous, current = sessions[0], after['operator_sessions'][0]
        self.assertTrue(previous[:2] == (UUID(self.db.actor.session_id), UUID(self.db.actor.operator_id)),
                        'Unexpected fixture session identity')
        self.assertTrue(current[:9] == previous[:9], 'Session credential/identity/deadline changed')
        self.assertEqual(current[9], previous[9] + touches)
        self.assertTrue(current[10] >= previous[10], 'Session activity timestamp regressed')
        if touches == 0:
            self.assertTrue(current == previous, 'Non-HTTP shutdown changed session activity')

        old_events, events = before['audit_events'], after['audit_events']
        self.assertTrue(events[:len(old_events)] == old_events, 'Existing audit rows changed or disappeared')
        added = events[len(old_events):]
        self.assertEqual(len(added), touches, 'Unexpected number of HTTP authentication audits')
        last_id = old_events[-1][0] if old_events else 0
        for offset, row in enumerate(added):
            self.assertTrue(type(row[0]) is int and row[0] > last_id, 'Auth audit ID is not append-only')
            last_id = row[0]
            self.assertTrue(row[1:6] == (UUID(self.db.actor.operator_id), None, 'auth.session_touch',
                self.db.actor.session_id, 'OK'), 'Unexpected actor/task/action/object/outcome in new audit')
            self.assertTrue(type(row[6]) is str and str(UUID(row[6])) == row[6],
                            'Auth audit correlation is not a canonical UUID')
            for summary in (row[7], row[8]):
                self.assertTrue(type(summary) is dict and set(summary) == {'version'}
                    and type(summary['version']) is int, 'Auth summary type/shape changed')
            self.assertTrue(row[7] == {'version': previous[9] + offset}, 'Auth before-summary changed')
            self.assertTrue(row[8] == {'version': previous[9] + offset + 1}, 'Auth after-summary changed')
            self.assertTrue(row[9] >= previous[10], 'Auth audit timestamp predates activity snapshot')

    async def assert_observe_only(self, tid):
        before, requests = self.snapshot(), list(self.requests)
        client, manager = self.client_and_manager()
        with patch.object(live_admission, 'prepare', new=AsyncMock(
                side_effect=AssertionError('HTTP replay prepared new admission'))) as prepare, \
                patch.object(live_entry, 'LiveExecutor',
                    side_effect=AssertionError('HTTP replay constructed executor')) as construct, \
                patch.object(live_completion, 'run_managed',
                    side_effect=AssertionError('HTTP replay scheduled executor')) as run:
            response = await client.post('/api/onboarding/live/start', headers=self.headers, json=self.body)
            self.assertEqual(response.status_code, 202, response.text)
            result = response.json()
            self.assertEqual(result['task_ids'], [tid])
            self.assertTrue(result['replayed'])
            self.assertFalse(result['scheduled'])
            self.assertFalse(manager.busy)
            prepare.assert_not_awaited(); construct.assert_not_called(); run.assert_not_called()
            await manager.shutdown()
        self.assert_observation_only(before, touches=1)
        self.assertEqual(self.requests, requests)
        return client, result

    async def test_http_start_formal_cancel_shutdown_and_original_receipts(self):
        client, manager = self.client_and_manager()
        entered, gate, executors = asyncio.Event(), asyncio.Event(), []
        async def before_first_effect(executor):
            executors.append(executor)
            entered.set()
            await gate.wait()  # Real executor cancellation/join/finalizer below.
            raise AssertionError('fixture chain gate must exit only by cancellation')

        # Real CSRF/session denial occurs before any admission or task creation.
        rejected = await client.post('/api/onboarding/live/start', headers={'Origin': ORIGIN}, json=self.body)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.db.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])
        with self.authorized_sdk(), patch.object(live_executor.LiveExecutor, '_chain', before_first_effect):
            started = await client.post('/api/onboarding/live/start', headers=self.headers, json=self.body)
            self.assertEqual(started.status_code, 202, started.text)
            public = started.json()
            self.assertTrue(public['scheduled'])
            self.assertFalse(public['replayed'])
            self.assertEqual(set(public), {'accepted', 'synthetic', 'execution_mode', 'scheduled',
                'batch_id', 'task_ids', 'mailbox_ids', 'config_id', 'config_revision', 'receipt_id', 'replayed'})
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(len(executors), 1)
            tid = public['task_ids'][0]
            self.assertEqual(self.db.read('SELECT status,version FROM onboarding_tasks'), [('RUNNING', 3)])
            self.assertEqual(self.db.read('SELECT count(*) FROM live_run_plans'), [(1,)])
            self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
            control = {'expected_version': 3, 'request_key': 'http:cancel'}
            cancelled = await client.post('/api/onboarding/tasks/' + tid + '/cancel',
                                           headers=self.headers, json=control)
            self.assertEqual(cancelled.status_code, 202, cancelled.text)
            original_cancel = cancelled.json()
            self.assertFalse(original_cancel['synthetic'])
            self.assertTrue(original_cancel['snapshot_pending'])
            self.assertEqual(self.db.read('SELECT status,cancel_requested,version FROM onboarding_tasks'),
                             [('PAUSED', True, 4)])
            self.assertEqual(self.db.read("SELECT usage_status FROM mailbox_platform_states WHERE platform='google'"),
                             [('RESERVED',)])
            await manager.shutdown()

        executor = executors[0]
        self.assertTrue(live_executor.require_stopped(executor.stop_proof(), executor._worker))
        self.assertFalse(manager.busy)
        self.assertEqual(self.db.read('SELECT status,version FROM onboarding_tasks'), [('CANCELLED_SAFE', 5)])
        self.assertEqual(self.db.read("SELECT identity_status,usage_status,last_task_id FROM mailbox_platform_states "
                                     "WHERE platform='google'"), [('UNKNOWN', 'HISTORY_UNRECONCILED', None)])
        self.assertEqual(self.db.read('SELECT task_id,owner_id,lease_until,hold_reason FROM resource_leases'),
                         [(None, None, None, None)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_observations'), [(0,)])
        self.assertEqual(len(self.requests), 1)
        observed_client, replay = await self.assert_observe_only(tid)
        self.assertEqual(replay['receipt_id'], public['receipt_id'])
        before = self.snapshot()
        repeated = await observed_client.post('/api/onboarding/tasks/' + tid + '/cancel',
                                              headers=self.headers, json=control)
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json(), original_cancel)
        detail = await observed_client.get('/api/onboarding/tasks/' + tid)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()['execution_mode'], 'live')
        self.assertEqual(detail.json()['status'], 'CANCELLED_SAFE')
        events = await observed_client.get('/api/onboarding/tasks/' + tid + '/events')
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()['effect_events'], [])
        self.assert_observation_only(before, touches=3)

    async def test_http_tx2_real_commit_lost_ack_is_503_then_only_receipt_observation(self):
        client, manager = self.client_and_manager()
        transaction, marked, committed = psycopg.Connection.transaction, [], []
        def select_transaction(cursor, query):
            if query.startswith("UPDATE onboarding_tasks SET status='RUNNING'"):
                marked.append(cursor.connection)
        @contextmanager
        def lost_ack(conn, *args, **kwargs):
            with transaction(conn, *args, **kwargs) as current:
                yield current
            if any(conn is selected for selected in marked):
                self.assertEqual(conn.info.transaction_status, TransactionStatus.IDLE)
                committed.append(True)
                raise psycopg.OperationalError('fixture-http-real-commit-lost-ack')

        with self.authorized_sdk(), after_query(select_transaction), \
                patch.object(psycopg.Connection, 'transaction', lost_ack), \
                patch.object(live_entry, 'LiveExecutor',
                    side_effect=AssertionError('unknown COMMIT constructed executor')) as construct, \
                patch.object(live_completion, 'run_managed',
                    side_effect=AssertionError('unknown COMMIT scheduled executor')) as run:
            response = await client.post('/api/onboarding/live/start', headers=self.headers, json=self.body)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()['code'], 'COMMIT_UNKNOWN')
            self.assertNotIn('not_committed', response.json())
            self.assertNotIn('accepted', response.json())
            construct.assert_not_called(); run.assert_not_called()
        self.assertEqual(committed, [True])
        self.assertFalse(manager.busy)
        rows = self.db.read('SELECT id,status,version FROM onboarding_tasks')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:], ('RUNNING', 3))
        tid = str(rows[0][0])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_run_plans'), [(1,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.db.read('SELECT task_id,hold_reason FROM resource_leases'), [(rows[0][0], 'HELD')])
        self.assertEqual(self.db.read("SELECT count(*) FROM operation_receipts WHERE action='live.run.stop'"), [(0,)])
        before = self.snapshot()
        await manager.shutdown()  # No managed executor: retain uncertain hold.
        self.assert_observation_only(before, touches=0)
        await self.assert_observe_only(tid)

    async def test_real_private_boot_original_root_and_live_reads_never_start_a_task(self):
        before = self.snapshot()
        directory = tempfile.TemporaryDirectory(prefix='rf-live-http-boot-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        root.chmod(0o700)
        target, binding = root / 'sub2api.json', root / 'binding.json'
        target.write_text(json.dumps(dict(url='http://127.0.0.1:8800',
            email='admin@fixture.invalid', password='fixture-private-admin-canary')))
        target.chmod(0o600)
        binding.write_text(json.dumps(dict(owner_id=self.db.actor.operator_id,
            data_root=str(self.db.source.data_root), mailbox_id=self.db.resource['id'],
            sub2api_config_file=str(target))))
        binding.chmod(0o600)
        # Settings loader permits only owner-private files beneath its fixed
        # BASE. PoolCase's key directory already satisfies that real boundary.
        settings_file = self.db.key_directory / 'web-settings.json'
        settings_file.write_text(json.dumps(asdict(self.db.settings)))
        settings_file.chmod(0o600)
        environment = dict(ONBOARDING_MODE='protected', ONBOARDING_POOL_MODE='live',
            ONBOARDING_ORIGIN=ORIGIN, ONBOARDING_SETTINGS_FILE=str(settings_file),
            ONBOARDING_POOL_KEYRING_DIR=str(self.db.key_directory),
            ONBOARDING_POOL_KEY_VERSION=self.db.keyring.active_version,
            ONBOARDING_LIVE_BINDING_FILE=str(binding), REG_FACTORY_DATA_DIR=str(root),
            REG_FACTORY_ENV_FILE=str(root / 'unused.env'))
        # scripts.py's two legacy presentation defaults must not import the
        # checkout's ambient config/.env. This is NOT a live BOOT/auth seam.
        legacy_defaults = ModuleType('config')
        legacy_defaults.CLAUDE_PROTOCOL_VERSION = 'fixture'
        legacy_defaults.CLAUDE_REGISTRATION_PROTOCOL = 'browser'
        source = Path(__file__).resolve().parents[1] / 'webui' / 'server.py'
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            stack.enter_context(patch.dict(sys.modules, {'config': legacy_defaults}))
            old_path = list(sys.path)
            stack.callback(lambda: sys.path.__setitem__(slice(None), old_path))
            # Assertion guards, not replacements for any permitted live work:
            # importing/reading the UI must never reach an external transport.
            for name in ('subprocess.run', 'subprocess.Popen', 'socket.create_connection',
                         'socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
                stack.enter_context(patch(name, side_effect=AssertionError('BOOT/read performed forbidden external I/O')))
            stack.enter_context(patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                side_effect=AssertionError('BOOT/read reached external async HTTP')))
            stack.enter_context(patch.object(httpx.HTTPTransport, 'handle_request',
                side_effect=AssertionError('BOOT/read reached external HTTP')))
            spec = importlib.util.spec_from_file_location('fixture_live_boot_' + uuid4().hex, source)
            server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(server)
            config = server.app.state.onboarding_config
            self.assertTrue(config.ready)
            self.assertTrue(config.pool_ready)
            self.assertEqual(config.settings, self.db.settings)
            self.assertEqual(config.live_mailbox_id, self.db.resource['id'])
            self.assertIs(type(config.live_manager), LiveRunManager)
            self.assertEqual(config.live_source.data_root, self.db.source.data_root)
            self.assertEqual(config.live_source.owner_id, self.db.actor.operator_id)
            self.addAsyncCleanup(config.live_manager.shutdown)
            # ASGITransport alone does not run application lifespan hooks.
            # Enter the real server lifespan so the installed shutdown handler
            # must close the real manager (legacy protected hooks remain inert).
            async with server.app.router.lifespan_context(server.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                        base_url=ORIGIN, cookies={onboarding_auth.SESSION_COOKIE: self.db.token,
                        onboarding_auth.CSRF_COOKIE: self.db.csrf}) as client:
                    capabilities = await client.get('/api/onboarding/live/capabilities')
                    self.assertEqual(capabilities.status_code, 200, capabilities.text)
                    self.assertEqual(capabilities.json(), dict(mode='live', workflow='existing_billing_cloud_v1',
                        source=dict(available=True, mailbox_id=self.db.resource['id'],
                                    ref='local-authorized-mailbox'), start_available=True))
                    current = await client.get('/api/onboarding/config')
                    self.assertEqual(current.status_code, 200, current.text)
                    self.assertEqual(current.json()['revision'], self.db.cfg['revision'])
                    self.assertEqual(current.json()['nonsecret_config'], FIELDS)
                    mailboxes = await client.get('/api/onboarding/mailboxes')
                    self.assertEqual(mailboxes.status_code, 200, mailboxes.text)
                    self.assertEqual([item['id'] for item in mailboxes.json()['items']], [self.db.resource['id']])
                    page = await client.get('/')
                    self.assertEqual(page.status_code, 200, page.text[:200])
                    self.assertIn('data-console-mode="protected"', page.text)
                    self.assertIn('data-pool-mode="live"', page.text)
                    self.assertEqual(page.text.count('id="main"'), 1)
                    for response in (capabilities, current, mailboxes, page):
                        self.assertEqual(response.headers['cache-control'], 'no-store')
                        self.assertNotIn(str(binding), response.text)
                        self.assertNotIn(str(target), response.text)
                        self.assertNotIn('fixture-private-admin-canary', response.text)
                    self.assertFalse(config.live_manager.busy)
                    self.assertFalse(config.live_manager.closed)
            self.assertTrue(config.live_manager.closed)
            self.assertFalse(config.live_manager.busy)
        self.assert_observation_only(before, touches=4)
        self.assertEqual(self.db.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_run_plans'), [(0,)])
        self.assertEqual(self.db.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        self.assertEqual(self.requests, [])


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LiveApiDBTests)
