"""Offline protected HTTP adapters: no DB, files, SDK or provider is opened.

The real ASGI guard/JSON parser/DTO projections run. Authentication and business
services are explicit test seams; this suite is not a PG or Google integration.
"""
import asyncio
from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from onboarding import live_config, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_manager import Submission
from onboarding.mailbox_read import Page
from onboarding.settings import Settings, SOCKET
from test_onboarding_live_config import FIELDS, LEGACY
from webui import onboarding_auth

ORIGIN = 'https://fixture.local'
HEADERS = {'Origin': ORIGIN, 'X-CSRF-Token': 'c' * 43}
ID = '12345678-1234-4234-9234-123456789abc'
OTHER = '22345678-1234-4234-9234-123456789abc'
REVISION = 'pool-' + ID
CANARY = 'private-secret-path-owner-canary'


def module(case):
    case.assertIsNotNone(importlib.util.find_spec('webui.onboarding_live_routes'),
                         'protected live HTTP adapters are missing')
    return importlib.import_module('webui.onboarding_live_routes')


def config_fixture():
    settings = Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
                        'offline-placeholder', 'rf-onboarding-p1b-v1:fixture', 'rf_onboarding')
    return SimpleNamespace(mode='protected', pool_mode='live', ready=True, pool_ready=True,
        expected_origin=ORIGIN, settings=settings, pool_mac=object(), live_policy=object(),
        live_vault=object(), live_source=SimpleNamespace(owner_id=ID, data_root=CANARY, email=CANARY),
        live_mailbox_id=ID, live_manager=SimpleNamespace(submit=AsyncMock()))


def task_fixture(*, listed=False, mode='live'):
    result = dict(id=ID, execution_scope='pool', batch_id=ID, config_id=ID,
        config_revision=REVISION, status='RUNNING', version=3, reason_code=None,
        current_step='project.create', generation=1, cancel_requested=False,
        synthetic=mode == 'synthetic', execution_mode=mode, credential_ref=CANARY)
    if listed:
        result['created_at'] = '2026-09-30T00:00:00+00:00'
    return result


class LiveApiTests(unittest.TestCase):
    @contextmanager
    def client(self, *, changes=None, actor_id=ID, authenticated=True):
        m, config = module(self), config_fixture()
        for name, value in (changes or {}).items():
            setattr(config, name, value)
        actor = security.Actor(actor_id, frozenset({'onboarding:read', 'tasks:manage',
            'config:manage', 'mailboxes:manage'}), ID, 1)
        app = FastAPI()
        onboarding_auth._install_auth_routes(app, config)
        app.add_middleware(onboarding_auth.ProtectionMiddleware, config=config)
        m.register_routes(app, config)

        # Production existing-URL dispatch belongs to onboarding_routes and
        # onboarding_pool_routes; mount the exact helpers once in this fixture.
        @app.get('/api/onboarding/config')
        async def config_get(request: Request):
            return await m.config_get(request, config)
        @app.put('/api/onboarding/config')
        async def config_put(request: Request):
            return await m.config_put(request, config)
        @app.get('/api/onboarding/tasks')
        async def tasks_list(request: Request):
            return await m.tasks_list(request, config)
        @app.get('/api/onboarding/tasks/{task_id}')
        async def task_detail(request: Request, task_id: str):
            return await m.task_detail(request, config, task_id)
        @app.get('/api/onboarding/tasks/{task_id}/events')
        async def task_events(request: Request, task_id: str):
            return await m.task_events(request, config, task_id)
        @app.post('/api/onboarding/tasks/{task_id}/pause')
        async def pause(request: Request, task_id: str):
            return await m.task_command(request, config, task_id, 'pause')
        @app.post('/api/onboarding/tasks/{task_id}/cancel')
        async def cancel(request: Request, task_id: str):
            return await m.task_command(request, config, task_id, 'cancel')
        @app.post('/api/onboarding/tasks/{task_id}/recheck')
        async def recheck(request: Request, task_id: str):
            return await m.task_command(request, config, task_id, 'recheck')
        with ExitStack() as stack:
            auth = stack.enter_context(patch.object(onboarding_auth, '_authenticate_request',
                return_value=actor, side_effect=None if authenticated else ServiceError(ErrorCode.UNAUTHENTICATED)))
            stack.enter_context(patch('onboarding.storage.open_app', side_effect=AssertionError('offline HTTP opened PG')))
            client = stack.enter_context(TestClient(app, base_url=ORIGIN))
            yield client, config, auth

    def test_register_only_four_new_routes_and_only_explicit_live_mode(self):
        m = module(self)
        for mode, expected in (('live', 4), ('synthetic', 0), ('off', 0)):
            app, config = FastAPI(), config_fixture()
            config.pool_mode = mode
            m.register_routes(app, config)
            # FastAPI may keep included routers nested; inspect its resolved
            # public method/path contract rather than counting wrapper objects.
            new = list(app.openapi()['paths'])
            self.assertEqual(len(new), expected)
            self.assertTrue(all(path.startswith('/api/onboarding/live/') for path in new))

    def test_capabilities_are_owner_scoped_nonsecret_and_separate_source_from_start(self):
        with self.client() as (client, config, auth):
            response = client.get('/api/onboarding/live/capabilities')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'mode': 'live', 'workflow': 'existing_billing_cloud_v1',
                'source': {'available': True, 'mailbox_id': ID, 'ref': 'local-authorized-mailbox'},
                'start_available': True})
            self.assertEqual(auth.call_args.args[-1], 'onboarding:read')
            self.assertNotIn(CANARY, response.text)
            config.live_manager = None
            self.assertFalse(client.get('/api/onboarding/live/capabilities').json()['start_available'])
            config.live_mailbox_id = None
            self.assertIsNone(client.get('/api/onboarding/live/capabilities').json()['source']['mailbox_id'])
        with self.client(actor_id=OTHER) as (client, _, _):
            value = client.get('/api/onboarding/live/capabilities').json()
            self.assertFalse(value['source']['available'])
            self.assertIsNone(value['source']['mailbox_id'])
            self.assertFalse(value['start_available'])

    def test_guard_rejects_missing_auth_wrong_origin_wrong_methods_unknown_paths(self):
        with self.client(authenticated=False) as (client, _, _):
            self.assertEqual(client.get('/api/onboarding/live/capabilities').status_code, 401)
        with self.client() as (client, config, _):
            for method, path in (('HEAD', 'capabilities'), ('GET', 'start'), ('POST', 'future')):
                self.assertEqual(client.request(method, '/api/onboarding/live/' + path,
                    headers=HEADERS, json={}).status_code, 403)
            self.assertEqual(client.post('/api/onboarding/live/start', json={},
                headers={**HEADERS, 'Origin': 'https://evil.invalid'}).status_code, 403)
            self.assertEqual(client.post('/api/onboarding/live/start', json={}).status_code, 403)
            config.live_manager.submit.assert_not_awaited()

    def test_start_projects_internal_summary_and_calls_only_fixed_manager(self):
        result = dict(batch_id=ID, task_ids=[ID], mailbox_ids=[ID], config_id=ID,
            config_revision=REVISION, receipt_id=ID, replayed=False, credential_ref=CANARY,
            mailbox_credential_ref=CANARY, source_fingerprint=CANARY, admission={'secret': CANARY})
        with self.client() as (client, config, auth):
            config.live_manager.submit.return_value = Submission(result, True)
            response = client.post('/api/onboarding/live/start', headers=HEADERS,
                json=dict(mailbox_id=ID, expected_config_revision=REVISION, request_key='start:1'))
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {key: result[key] for key in (
                'batch_id', 'task_ids', 'mailbox_ids', 'config_id', 'config_revision', 'receipt_id', 'replayed')} |
                {'accepted': True, 'scheduled': True, 'execution_mode': 'live', 'synthetic': False})
            self.assertNotIn(CANARY, response.text)
            self.assertEqual(auth.call_args.args[-1], 'tasks:manage')
            args = config.live_manager.submit.await_args.args
            self.assertEqual((args[0].operator_id, *args[1:]), (ID, REVISION, 'start:1'))

    def test_start_replay_never_claims_scheduled_and_unknown_commit_is_not_retried(self):
        result = dict(batch_id=ID, task_ids=[ID], mailbox_ids=[ID], config_id=ID,
            config_revision=REVISION, receipt_id=ID, replayed=True)
        body = dict(mailbox_id=ID, expected_config_revision=REVISION, request_key='start:1')
        with self.client() as (client, config, _):
            config.live_manager.submit.return_value = Submission(result, False)
            self.assertFalse(client.post('/api/onboarding/live/start', headers=HEADERS, json=body).json()['scheduled'])
            config.live_manager.submit.reset_mock()
            config.live_manager.submit.side_effect = ServiceError(ErrorCode.COMMIT_UNKNOWN)
            response = client.post('/api/onboarding/live/start', headers=HEADERS, json=body)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['code'], 'COMMIT_UNKNOWN')
            self.assertNotIn('not_committed', response.json())
            config.live_manager.submit.assert_awaited_once()

    def test_start_invalid_internal_arrays_and_replay_schedule_are_not_serialized(self):
        result = dict(batch_id=ID, task_ids=[ID], mailbox_ids=[ID], config_id=ID,
            config_revision=REVISION, receipt_id=ID, replayed=False)
        body = dict(mailbox_id=ID, expected_config_revision=REVISION, request_key='start:1')
        with self.client() as (client, config, _):
            for value, scheduled in (({**result, 'task_ids': [{'secret': CANARY}]}, True),
                                     ({**result, 'mailbox_ids': [ID, OTHER]}, True),
                                     ({**result, 'replayed': True}, True)):
                config.live_manager.submit.return_value = Submission(value, scheduled)
                response = client.post('/api/onboarding/live/start', headers=HEADERS, json=body)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(CANARY, response.text)

    def test_start_rejects_unknown_fields_noncanonical_ids_and_client_targets(self):
        body = dict(mailbox_id=ID, expected_config_revision=REVISION, request_key='start:1')
        with self.client() as (client, config, _):
            for extra in ({'mailbox_id': ID.upper()}, {'mailbox_id': True}, {'mailbox_id': [ID]},
                          {'expected_config_revision': None}, {'request_key': True},
                          {'source': '/tmp/elsewhere'}, {'target': 'https://evil.invalid'},
                          {'owner_id': ID}, {'billing_account': FIELDS['billing_account']}):
                response = client.post('/api/onboarding/live/start', headers=HEADERS, json={**body, **extra})
                self.assertEqual(response.status_code, 422, extra)
            self.assertEqual(client.post('/api/onboarding/live/start?scope=pool', headers=HEADERS,
                json=body).status_code, 422)
            config.live_manager.submit.assert_not_awaited()

    def test_start_missing_fixed_manager_or_id_is_503_and_foreign_mailbox_is_forbidden(self):
        body = dict(mailbox_id=ID, expected_config_revision=REVISION, request_key='start:1')
        for changes in ({'live_manager': None}, {'live_mailbox_id': None}):
            with self.client(changes=changes) as (client, _, _):
                self.assertEqual(client.post('/api/onboarding/live/start', headers=HEADERS, json=body).status_code, 503)
        with self.client() as (client, config, _):
            self.assertEqual(client.post('/api/onboarding/live/start', headers=HEADERS,
                json={**body, 'mailbox_id': OTHER}).status_code, 403)
            config.live_manager.submit.assert_not_awaited()
        with self.client(actor_id=OTHER) as (client, config, _):
            self.assertEqual(client.post('/api/onboarding/live/start', headers=HEADERS, json=body).status_code, 403)
            config.live_manager.submit.assert_not_awaited()

    def test_source_preview_uses_server_source_and_renames_only_opaque_digest(self):
        result = dict(email='owned@example.com', provider='other', group_ref='owned',
                      source_digest='a' * 64, source_fingerprint=CANARY, password=CANARY)
        with patch('onboarding.live_input_vault.preview_source', return_value=result) as service:
            with self.client(changes={'live_manager': None, 'live_mailbox_id': None}) as (client, config, auth):
                response = client.post('/api/onboarding/live/source-preview', headers=HEADERS, json={'group_ref': 'owned'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(email='owned@example.com', provider='other',
                    group_ref='owned', preview_digest='a' * 64))
                self.assertEqual(auth.call_args.args[-1], 'mailboxes:manage')
                self.assertIs(service.call_args.args[2], config.live_source)
                self.assertEqual(service.call_args.kwargs, {'vault': config.live_vault, 'mac': config.pool_mac})

    def test_source_import_returns_id_without_mutating_fixed_manager_or_config(self):
        body = dict(group_ref='owned', preview_digest='a' * 64, request_key='import:1')
        with patch('onboarding.live_input_vault.import_source',
                   return_value=dict(mailbox_id=ID, created=True, request_key='import:1', private=CANARY)) as service:
            with self.client(changes={'live_manager': None, 'live_mailbox_id': None}) as (client, config, _):
                response = client.post('/api/onboarding/live/source-import', headers=HEADERS, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(mailbox_id=ID, created=True, request_key='import:1'))
                self.assertEqual(service.call_args.args[3:], ('owned', 'a' * 64, 'import:1'))
                self.assertIsNone(config.live_manager)
                self.assertIsNone(config.live_mailbox_id)

    def test_source_input_is_exact_and_nonowner_cannot_reach_service(self):
        body = dict(group_ref='owned', preview_digest='a' * 64, request_key='import:1')
        with patch('onboarding.live_input_vault.import_source') as service:
            with self.client() as (client, _, _):
                for change in ({'group_ref': '\n'}, {'group_ref': 'x' * 129}, {'group_ref': True},
                               {'preview_digest': 'A' * 64}, {'preview_digest': None}, {'request_key': False},
                               {'path': CANARY}, {'text': CANARY}, {'source_digest': 'a' * 64}):
                    self.assertEqual(client.post('/api/onboarding/live/source-import', headers=HEADERS,
                        json={**body, **change}).status_code, 422, change)
            with self.client(actor_id=OTHER) as (client, _, _):
                self.assertEqual(client.post('/api/onboarding/live/source-import', headers=HEADERS,
                    json=body).status_code, 403)
            service.assert_not_called()

    def test_json_duplicate_unknown_keys_and_oversize_never_call_service(self):
        with patch('onboarding.live_input_vault.preview_source') as service:
            with self.client() as (client, _, _):
                for raw in ('{}', '[]', '{"group_ref":"a","group_ref":"b"}',
                            '{"group_ref":NaN}', '{"group_ref":"a","secret":"b"}',
                            '{"group_ref":"' + 'x' * 8192 + '"}'):
                    response = client.post('/api/onboarding/live/source-preview', content=raw,
                        headers={**HEADERS, 'Content-Type': 'application/json'})
                    self.assertEqual(response.status_code, 422)
            service.assert_not_called()

    def test_v2_and_legacy_config_get_are_readonly_whitelisted_without_promotion(self):
        for fields in (FIELDS, LEGACY):
            result = dict(id=ID, revision=REVISION, scope='pool', secrets_configured=False,
                created_at='2026-09-30T00:00:00+00:00', nonsecret_config=fields.copy(), secret_refs=CANARY)
            with patch('onboarding.live_config.get_current', return_value=result) as service:
                with self.client() as (client, _, _):
                    response = client.get('/api/onboarding/config')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['nonsecret_config'], fields)
                    self.assertNotIn(CANARY, response.text)
                    self.assertEqual(service.call_args.kwargs['permission'], 'config:manage')

    def test_config_get_null_and_corrupt_or_expanded_fields_fail_closed(self):
        with patch('onboarding.live_config.get_current', return_value=None):
            with self.client() as (client, _, _):
                self.assertIsNone(client.get('/api/onboarding/config').json())
        with patch('onboarding.live_config.get_current', return_value=dict(id=ID, revision=REVISION,
                scope='pool', secrets_configured=False, created_at='time',
                nonsecret_config={**FIELDS, 'private_key': CANARY})):
            with self.client() as (client, _, _):
                response = client.get('/api/onboarding/config')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(CANARY, response.text)

    def test_config_put_accepts_only_complete_v2_and_projects_committed_receipt(self):
        body = dict(expected_revision=REVISION, fields=FIELDS, request_key='config:1')
        with patch('onboarding.live_config.replace', return_value=dict(config_id=ID,
                   revision=REVISION, request_key='config:1', private=CANARY)) as service:
            with self.client() as (client, config, _):
                response = client.put('/api/onboarding/config', headers=HEADERS, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(config_id=ID, revision=REVISION, request_key='config:1'))
                self.assertIs(service.call_args.kwargs['policy'], config.live_policy)
                for fields in (LEGACY, {**FIELDS, 'concurrency': True}, {**FIELDS, 'private_key': CANARY}):
                    self.assertEqual(client.put('/api/onboarding/config', headers=HEADERS,
                        json={**body, 'fields': fields}).status_code, 422)
            service.assert_called_once()

    def test_task_list_and_detail_use_live_read_and_allowlist_for_historical_modes(self):
        for mode in ('live', 'synthetic'):
            with patch('onboarding.live_read.list_page', return_value=Page((task_fixture(listed=True, mode=mode),), None)) as listed, \
                    patch('onboarding.live_read.read_task', return_value=task_fixture(mode=mode)) as detail:
                with self.client() as (client, config, _):
                    response = client.get('/api/onboarding/tasks?scope=pool&limit=5')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['items'][0]['execution_mode'], mode)
                    self.assertNotIn(CANARY, response.text)
                    self.assertIs(listed.call_args.kwargs['policy'], config.live_policy)
                    response = client.get('/api/onboarding/tasks/' + ID)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(CANARY, response.text)
                    detail.assert_called_once()

    def test_task_events_project_each_nested_allowlist(self):
        result = dict(task_id=ID, mode='live', execution_connected=False, has_more=False,
            events=[dict(id='1', occurred_at='time', action='task.cancel', outcome='ACCEPTED', version=4,
                         before_summary=CANARY)], effects_has_more=False,
            effect_events=[dict(receipt_id=ID, action='project.create', phase='INTENT', result_code=None,
                                version=1, updated_at='time', fence_bindings=CANARY)], private=CANARY)
        with patch('onboarding.live_read.events', return_value=result):
            with self.client() as (client, _, _):
                response = client.get('/api/onboarding/tasks/' + ID + '/events')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['effect_events']), 1)
                self.assertNotIn(CANARY, response.text)

    def test_pause_cancel_return_committed_receipt_without_followup_read(self):
        for action in ('pause', 'cancel'):
            with patch('onboarding.live_read.locate', return_value={'execution_scope': 'pool', 'execution_mode': 'live'}) as locate, \
                    patch('onboarding.live_commands.' + action, return_value={'receipt_id': ID, 'phase': 'SUCCEEDED', 'private': CANARY}) as command, \
                    patch('onboarding.live_read.read_task', side_effect=AssertionError('post-commit read erased receipt')) as read:
                with self.client() as (client, config, _):
                    response = client.post('/api/onboarding/tasks/' + ID + '/' + action, headers=HEADERS,
                        json={'expected_version': 3, 'request_key': 'control:1'})
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json(), {'accepted': True, 'synthetic': False,
                        'execution_scope': 'pool', 'execution_mode': 'live', 'receipt_id': ID,
                        'phase': 'SUCCEEDED', 'task': None, 'snapshot_pending': True})
                    self.assertEqual(locate.call_args.kwargs['permission'], 'tasks:manage')
                    self.assertIs(command.call_args.kwargs['policy'], config.live_policy)
                    self.assertNotIn(CANARY, response.text)
                read.assert_not_called()

    def test_legacy_task_controls_and_recheck_are_disabled_without_fallback(self):
        with patch('onboarding.live_read.locate', return_value={'execution_scope': 'pool', 'execution_mode': 'synthetic'}) as locate, \
                patch('onboarding.live_commands.cancel') as cancel, patch('onboarding.pool_commands.cancel') as old:
            with self.client() as (client, _, _):
                for action in ('cancel', 'recheck'):
                    response = client.post('/api/onboarding/tasks/' + ID + '/' + action, headers=HEADERS,
                        json={'expected_version': 3, 'request_key': 'control:1'})
                    self.assertEqual(response.status_code, 403)
            self.assertEqual(locate.call_count, 1)
            cancel.assert_not_called(); old.assert_not_called()

    def test_command_or_locator_failure_is_not_fallback_or_retry(self):
        for code in (ErrorCode.FORBIDDEN, ErrorCode.COMMIT_UNKNOWN, ErrorCode.VERSION_CONFLICT):
            with patch('onboarding.live_read.locate', return_value={'execution_scope': 'pool', 'execution_mode': 'live'}), \
                    patch('onboarding.live_commands.cancel', side_effect=ServiceError(code)) as command, \
                    patch('onboarding.pool_commands.cancel') as old:
                with self.client() as (client, _, _):
                    response = client.post('/api/onboarding/tasks/' + ID + '/cancel', headers=HEADERS,
                        json={'expected_version': 3, 'request_key': 'control:1'})
                    self.assertEqual(response.status_code, {ErrorCode.FORBIDDEN: 403,
                        ErrorCode.COMMIT_UNKNOWN: 503, ErrorCode.VERSION_CONFLICT: 409}[code])
                    self.assertNotIn('not_committed', response.json())
                command.assert_called_once(); old.assert_not_called()
        with patch('onboarding.live_read.locate', side_effect=ServiceError(ErrorCode.FORBIDDEN)), \
                patch('onboarding.live_commands.cancel') as command:
            with self.client() as (client, _, _):
                self.assertEqual(client.post('/api/onboarding/tasks/' + ID + '/cancel', headers=HEADERS,
                    json={'expected_version': 3, 'request_key': 'control:1'}).status_code, 403)
            command.assert_not_called()

    def test_query_and_command_validation_precede_any_business_read_or_write(self):
        with patch('onboarding.live_read.locate') as locate, patch('onboarding.live_read.list_page') as listed, \
                patch('onboarding.live_read.read_task') as detail:
            with self.client() as (client, _, _):
                for suffix in ('limit=1&limit=2', 'limit=true', 'limit=101', 'mode=live', 'scope=evil'):
                    self.assertEqual(client.get('/api/onboarding/tasks?' + suffix).status_code, 422)
                self.assertEqual(client.get('/api/onboarding/tasks/' + ID + '?mode=live').status_code, 422)
                for body in ({'expected_version': True, 'request_key': 'x'},
                             {'expected_version': 3, 'request_key': 'x', 'mode': 'live'}):
                    self.assertEqual(client.post('/api/onboarding/tasks/' + ID + '/cancel',
                        headers=HEADERS, json=body).status_code, 422)
            locate.assert_not_called(); listed.assert_not_called(); detail.assert_not_called()


class LiveApiCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_request_cancel_joins_running_config_transaction_thread(self):
        m, config = module(self), config_fixture()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def write(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture release timed out')
            done.set()
            return dict(config_id=ID, revision=REVISION, request_key='config:1')
        raw = json.dumps(dict(expected_revision=REVISION, fields=FIELDS, request_key='config:1')).encode()
        async def receive():
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        request = Request({'type': 'http', 'method': 'PUT', 'path': '/api/onboarding/config',
            'query_string': b'', 'headers': [], 'state': {'onboarding_actor': security.Actor(
                ID, frozenset({'config:manage'}), ID, 1)}}, receive)
        with patch.object(live_config, 'replace', side_effect=write):
            task = asyncio.create_task(m.config_put(request, config))
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
        self.assertTrue(done.is_set())


if __name__ == '__main__':
    unittest.main()
