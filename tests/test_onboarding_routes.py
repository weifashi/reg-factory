"""Real isolated PostgreSQL tests for the deliberately synthetic HTTP service."""
import importlib.util
import unittest
from unittest.mock import patch
from onboarding_b2_support import B2Case
from onboarding.errors import ErrorCode, ServiceError
from onboarding import security


class RouteSurfaceTests(unittest.TestCase):
    def test_synthetic_routes_exist(self):
        self.assertIsNotNone(importlib.util.find_spec('webui.onboarding_routes'))


class TaskServiceTests(B2Case):
    def module(self):
        from webui import onboarding_routes
        return onboarding_routes

    def reject(self, code, callback):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_snapshot_has_only_whitelisted_fields(self):
        task = self.task()
        result = self.module().read_task(self.settings, self.actor, task['id'])
        self.assertEqual(set(result), {'id', 'status', 'version', 'reason_code',
            'current_step', 'generation', 'cancel_requested', 'synthetic'})
        self.assertEqual(result['id'], task['id'])
        self.assertTrue(result['synthetic'])
        self.assertNotIn('fixture:', str(result))

    def test_read_revalidates_disabled_operator(self):
        task = self.task()
        with self.uow() as conn:
            security.update_operator(conn, self.actor.operator_id, disabled=True)
        self.reject(ErrorCode.UNAUTHENTICATED,
            lambda: self.module().read_task(self.settings, self.actor, task['id']))

    def test_snapshot_denies_other_owners_and_real_mailboxes(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET mailbox_ref=%s WHERE id=%s',
                         ('not-a-fixture@example.invalid', task['id']))
        self.reject(ErrorCode.FORBIDDEN,
            lambda: self.module().read_task(self.settings, self.actor, task['id']))

    def test_fixture_actor_is_not_a_web_identity(self):
        task = self.task()
        self.reject(ErrorCode.FORBIDDEN,
            lambda: self.module().read_task(self.settings, self.fixture_actor, task['id']))

    def test_pause_replay_is_one_receipt_not_second_command(self):
        task = self.task()
        first = self.module().command_task(self.settings, self.actor, task['id'],
                                           'pause', 1, 'route-pause')
        again = self.module().command_task(self.settings, self.actor, task['id'],
                                           'pause', 1, 'route-pause')
        self.assertEqual(first, again)
        self.assertEqual(first['task']['status'], 'PAUSED')
        self.assertEqual(first['task']['version'], 2)
        self.assertEqual(set(first), {'accepted', 'synthetic', 'receipt_id', 'task'})
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 1)

    def test_version_conflict_does_not_retry(self):
        task = self.task()
        self.reject(ErrorCode.VERSION_CONFLICT, lambda: self.module().command_task(
            self.settings, self.actor, task['id'], 'pause', 2, 'stale-version'))
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)

    def test_recheck_is_only_not_sent_marker(self):
        task = self.task()
        result = self.module().command_task(self.settings, self.actor, task['id'],
                                            'recheck', 1, 'readonly')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['task']['status'], 'QUEUED')
        self.assertEqual(self.read('SELECT step_key,state FROM task_steps'),
                         [('fixture.recheck', 'NOT_SENT')])
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)

    def test_dangerous_or_unknown_actions_rejected(self):
        task = self.task()
        for action in ('verify', 'enable', 'download', 'create', 'resume', 'google'):
            self.reject(ErrorCode.INVALID_INPUT, lambda: self.module().command_task(
                self.settings, self.actor, task['id'], action, 1, 'blocked'))

    def test_diagnostics_exact_schema_no_paths(self):
        result = self.module().diagnostics(self.settings, self.actor)
        self.assertEqual(result, {'mode': 'protected', 'scope': 'offline_foundation',
            'schema_version': 1, 'ready': True, 'real_flows_connected': False})

    def test_diagnostics_cannot_bypass_revalidation(self):
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                         (self.actor.session_id,))
        self.reject(ErrorCode.UNAUTHENTICATED,
            lambda: self.module().diagnostics(self.settings, self.actor))

    def test_diagnostics_accepts_reviewed_pool_schema_without_enabling_real_flows(self):
        from onboarding.migrate import apply_all
        task = self.task()
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=2)
        result = self.module().diagnostics(self.settings, self.actor)
        self.assertEqual(result['schema_version'], 2)
        self.assertTrue(result['ready'])
        self.assertFalse(result['real_flows_connected'])
        self.assertEqual(self.module().command_task(self.settings, self.actor, task['id'],
                         'pause', 1, 'fixture-upgraded-pause')['task']['status'], 'PAUSED')

    def test_diagnostics_rejects_unknown_version(self):
        with self.fixture.migrator() as conn:
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES(3,repeat('0',64))")
        self.reject(ErrorCode.DEPENDENCY_UNAVAILABLE,
                    lambda: self.module().diagnostics(self.settings, self.actor))

    def test_diagnostics_rejects_drift_or_missing_prefix(self):
        from onboarding.migrate import apply_all
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=2)
            conn.execute("UPDATE schema_migrations SET checksum=repeat('0',64) WHERE version=2")
        self.reject(ErrorCode.DEPENDENCY_UNAVAILABLE,
                    lambda: self.module().diagnostics(self.settings, self.actor))
        with self.fixture.migrator() as conn:
            from onboarding.migration_catalog import expected_schema_versions
            conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=2',
                         (expected_schema_versions(2)[-1][1],))
            conn.execute('DELETE FROM schema_migrations WHERE version=1')
        self.reject(ErrorCode.DEPENDENCY_UNAVAILABLE,
                    lambda: self.module().diagnostics(self.settings, self.actor))

    def test_manage_only_actor_can_act_without_read_permission(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',
                         (['tasks:manage'], self.actor.operator_id))
        result = self.module().command_task(self.settings, self.actor, task['id'],
                                           'cancel', 1, 'safe-cancel')
        self.assertEqual(result['task']['status'], 'CANCELLED_SAFE')

    def test_diagnostic_rechecks_authority_after_schema_query(self):
        with patch.object(security, 'revalidate', wraps=security.revalidate) as checked:
            self.module().diagnostics(self.settings, self.actor)
        self.assertEqual(checked.call_count, 2)


class TaskHttpTests(B2Case):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from webui.onboarding_auth import install, SESSION_COOKIE, CSRF_COOKIE
        from webui.onboarding_routes import register_routes
        app = FastAPI()
        config = install(app, mode='protected', expected_origin='https://fixture.local',
                         settings=self.settings)
        register_routes(app, config)
        self.client = TestClient(app, base_url='https://fixture.local',
                                 client=('127.0.0.1', 42000))
        self.addCleanup(self.client.close)
        self.client.cookies.set(SESSION_COOKIE, self.token)
        self.client.cookies.set(CSRF_COOKIE, self.csrf)
        self.headers = {'Origin':'https://fixture.local', 'X-CSRF-Token':self.csrf}

    def url(self, task, action=''):
        return '/api/onboarding/tasks/' + task['id'] + ('/' + action if action else '')

    def test_http_pause_and_replay_are_202_and_same_receipt(self):
        task = self.task()
        payload = {'expected_version':1, 'request_key':'http-pause'}
        first = self.client.post(self.url(task,'pause'), json=payload, headers=self.headers)
        again = self.client.post(self.url(task,'pause'), json=payload, headers=self.headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(first.json()['task']['version'], 2)
        self.assertEqual(self.client.get(self.url(task)).json()['status'], 'PAUSED')
        self.assertEqual(first.headers['cache-control'], 'no-store')

    def test_http_unsafe_origin_and_csrf_cannot_create_receipt(self):
        task = self.task()
        for headers in ({}, {'Origin':'https://evil.invalid','X-CSRF-Token':self.csrf},
                        {'Origin':'https://fixture.local','X-CSRF-Token':'bad'}):
            response = self.client.post(self.url(task,'pause'), json={
                'expected_version':1,'request_key':'csrf-denied'}, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)

    def test_http_unknown_routes_and_input_are_never_echoed(self):
        task = self.task()
        for action in ('create','enable','verify','download','resume'):
            response = self.client.post(self.url(task,action), json={}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
        for payload in ({'expected_version':True,'request_key':'x'},
                        {'expected_version':1,'request_key':'x','password':'fixture-canary-do-not-leak'},
                        {'expected_version':1}, {'expected_version':0,'request_key':'x'}):
            response = self.client.post(self.url(task,'pause'), json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(set(response.json()), {'code','correlation_id'})
            self.assertNotIn('fixture-canary', response.text)
        response = self.client.post(self.url(task,'pause'),
            content='{"expected_version":1,"expected_version":2,"request_key":"x"}',
            headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(response.status_code, 422)

    def test_http_actor_cannot_read_foreign_task(self):
        import uuid
        task = self.task()
        other = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,%s,%s)',
                         (other,'fixture-other','fixture-only',['onboarding:read']))
            conn.execute('UPDATE onboarding_batches SET created_by=%s WHERE id=%s',
                         (other,task['batch_id']))
        self.assertEqual(self.client.get(self.url(task)).status_code,403)
        self.assertEqual(self.client.post(self.url(task,'pause'),json={
            'expected_version':1,'request_key':'foreign'},headers=self.headers).status_code,403)

    def test_http_invalid_uuid_and_service_error_are_fixed_codes(self):
        response = self.client.get('/api/onboarding/tasks/not-a-uuid')
        self.assertEqual(response.status_code,422)
        self.assertEqual(set(response.json()), {'code','correlation_id'})
        with patch('webui.onboarding_routes.read_task',side_effect=RuntimeError('fixture-no-leak')):
            response = self.client.get(self.url(self.task()))
        self.assertEqual(response.status_code,503)
        self.assertNotIn('fixture-no-leak',response.text)

    def test_http_read_permission_is_not_manage(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',
                         (['onboarding:read'], self.actor.operator_id))
        self.assertEqual(self.client.get(self.url(task)).status_code,200)
        self.assertEqual(self.client.post(self.url(task,'pause'),json={
            'expected_version':1,'request_key':'no-manage'},headers=self.headers).status_code,403)

    def test_http_dependency_down_is_503_not_anonymous_fallback(self):
        with patch('onboarding.storage.open_app',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)):
            response = self.client.get('/api/onboarding/diagnostics')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['code'],'DEPENDENCY_UNAVAILABLE')

    def test_http_guard_actor_is_revalidated_again_before_mutation(self):
        task = self.task()
        with self.uow() as conn:
            security.update_operator(conn,self.actor.operator_id,disabled=True)
        # Simulate identity becoming stale after a completed guard transaction.
        with patch('webui.onboarding_auth._authenticate_request',return_value=self.actor):
            response = self.client.post(self.url(task,'pause'),json={
                'expected_version':1,'request_key':'stale-actor'},headers=self.headers)
        self.assertEqual(response.status_code,401)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0],0)

    def test_http_fixture_command_conflict_never_carries_proof(self):
        task = self.task()
        response = self.client.post(self.url(task, 'pause'), json={'expected_version': 1, 'request_key': 'fixture:a'},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 202)
        response = self.client.post(self.url(task, 'cancel'), json={'expected_version': 1, 'request_key': 'fixture:b'},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(response.json()), {'code', 'correlation_id'})
