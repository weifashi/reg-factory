"""Read-only task audit projection: offline contracts and disposable PostgreSQL."""
import importlib
import importlib.util
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from onboarding import security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_vault import SyntheticPoolPolicy
from test_onboarding_pool_preflight import context
import test_onboarding_pool_api as http_tests
ID = http_tests.ID
import test_onboarding_task_read as task_tests
import onboarding_pool_support


def api():
    return importlib.import_module('onboarding.task_events')


class EventInputTests(unittest.TestCase):
    def test_live_stop_outcomes_are_not_mistaken_for_completed_onboarding(self):
        row = (1, datetime(2026, 9, 29, tzinfo=timezone.utc), 'task.pause', 'CLOUD_CHAIN_VERIFIED', 10)
        self.assertEqual(api()._event(row, mode='live')['outcome'], 'CLOUD_CHAIN_VERIFIED')
        with self.assertRaises(ServiceError):
            api()._event(row)
        with self.assertRaises(ServiceError):
            api()._event((*row[:2], 'task.create', *row[3:]), mode='live')

    def test_live_effect_projection_is_closed_and_never_returns_provider_fields(self):
        from uuid import UUID
        self.assertTrue(callable(getattr(api(), '_effect', None)), 'live effect projection missing')
        row = (UUID(ID), 'project.create', 'INTENT', None, 1, datetime(2026, 9, 29, tzinfo=timezone.utc))
        result = api()._effect(row)
        self.assertEqual(result, dict(receipt_id=ID, action='project.create', phase='INTENT',
            result_code=None, version=1, updated_at='2026-09-29T00:00:00.000000Z'))
        conflict = (row[0], row[1], 'CONFLICT', 'CONFLICT', 2, row[5])
        self.assertEqual(api()._effect(conflict)['result_code'], 'CONFLICT')
        for index, value in ((0, ID), (1, 'payment.charge'), (2, 'PENDING'),
                             (3, 'SECRET_CANARY'), (3, 'DISABLED_IMPORT_VERIFIED'), (4, True),
                             (5, datetime.now())):
            bad = list(row); bad[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ServiceError) as caught:
                api()._effect(tuple(bad))
            self.assertEqual(str(caught.exception), 'DEPENDENCY_UNAVAILABLE')

    def test_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.task_events'))

    def test_inputs_fail_before_dependencies(self):
        args = context(); args.pop('mac')
        with patch.object(storage, 'open_app', side_effect=AssertionError('SQL')) as sql, \
             patch.object(SyntheticPoolPolicy, '_validate', side_effect=AssertionError('policy')) as policy:
            for tid in (None, True, uuid4(), 'bad', ID.upper()):
                with self.assertRaises(ServiceError) as caught:
                    api().read(**args, task_id=tid)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            with self.assertRaises(ServiceError) as caught:
                api().read(**(args | {'actor': object()}), task_id=ID)
            self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
            sql.assert_not_called(); policy.assert_not_called()

    def test_projection_exact_fields_and_bigint_string(self):
        value = api()._event((9007199254740993, datetime(2026, 9, 28, tzinfo=timezone.utc),
                              'task.create', 'CREATED', 1))
        self.assertEqual(value, dict(id='9007199254740993', occurred_at='2026-09-28T00:00:00.000000Z',
                                    action='task.create', outcome='CREATED', version=1))
        self.assertIsNone(api()._event((1, datetime.now(timezone.utc), 'task.pause', 'ACCEPTED', None))['version'])

    def test_malformed_projection_never_exposes_raw_data(self):
        good = (1, datetime.now(timezone.utc), 'task.pause', 'ACCEPTED', 2)
        for index, value in ((0, True), (0, 0), (0, '1'), (0, 2**63), (1, 'secret-date'),
                (1, datetime.now()), (2, 'step.observe'), (2, ['task.pause']),
                (3, 'SECRET_CANARY'), (3, 'CREATED'), (4, True), (4, 0), (4, '2'), (4, {'secret': 'canary'})):
            row = list(good); row[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ServiceError) as caught:
                api()._event(tuple(row))
            self.assertEqual(str(caught.exception), 'DEPENDENCY_UNAVAILABLE')


class EventHttpTests(unittest.TestCase):
    settings = http_tests.PoolApiOfflineTests.settings
    client = http_tests.PoolApiOfflineTests.client

    def test_live_http_projection_drops_raw_provider_and_secret_values(self):
        safe = dict(receipt_id=ID, action='project.create', phase='CONFLICT',
                    result_code='CONFLICT', version=2, updated_at='2026-09-29T00:00:00.000000Z')
        value = dict(task_id=ID, mode='live', execution_connected=False, has_more=False,
                     events=[], effects_has_more=False, effect_events=[dict(safe, secret='CANARY')])
        with patch('onboarding.task_events.read', return_value=dict(value, provider_raw='CANARY')):
            with self.client() as (client, _, _):
                response = client.get('/api/onboarding/tasks/' + ID + '/events')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), dict(value, effect_events=[safe]))
        self.assertNotIn('CANARY', response.text)

    def test_exact_get_auth_and_projection(self):
        response_value = dict(task_id=ID, mode='synthetic', execution_connected=False,
                              events=[dict(id='1', occurred_at='2026-09-28T00:00:00.000000Z',
                                           action='task.create', outcome='CREATED', version=1)], has_more=False)
        with patch('onboarding.task_events.read', return_value=response_value | {'secret': 'canary'}) as service:
            with self.client() as (client, auth, config):
                response = client.get('/api/onboarding/tasks/' + ID + '/events')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), response_value)
        self.assertEqual(auth.call_args.args[-1], 'onboarding:read')
        self.assertEqual(service.call_args.kwargs, {'policy': config.pool_policy})

    def test_only_exact_read_allowed_and_no_queries(self):
        with patch('onboarding.task_events.read') as service:
            with self.client() as (client, _, _):
                base = '/api/onboarding/tasks/' + ID + '/events'
                for suffix in ('?limit=1', '?cursor=x', '?actor=x', '?a=1&a=2'):
                    self.assertEqual(client.get(base + suffix).status_code, 422)
                for path in (base + '/raw', base + '/', base.replace(ID, ID.upper()), base.replace(ID, 'bad')):
                    self.assertEqual(client.get(path).status_code, 403)
                for method in ('POST', 'HEAD', 'OPTIONS', 'DELETE'):
                    self.assertEqual(client.request(method, base).status_code, 403)
        service.assert_not_called()

    def test_auth_dependency_and_pool_mode_fail_closed(self):
        base = '/api/onboarding/tasks/' + ID + '/events'
        with patch('onboarding.task_events.read') as service:
            with self.client(authenticate=False) as (client, _, _):
                self.assertEqual(client.get(base).status_code, 401)
            with self.client(ready=False) as (client, _, _):
                self.assertEqual(client.get(base).status_code, 503)
            with self.client(mode='off') as (client, _, _):
                self.assertEqual(client.get(base).status_code, 403)
        service.assert_not_called()

    def test_service_failure_is_sanitized(self):
        with patch('onboarding.task_events.read', side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)):
            with self.client() as (client, _, _):
                response = client.get('/api/onboarding/tasks/' + ID + '/events')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(set(response.json()), {'code', 'correlation_id'})


class EventDatabaseTests(onboarding_pool_support.PoolCase):
    config = task_tests.TaskReadTests.config
    seed = task_tests.TaskReadTests.seed
    create = task_tests.TaskReadTests.create
    change = task_tests.TaskReadTests.change
    other_owner = task_tests.TaskReadTests.other_owner

    def events(self, tid, actor=None):
        return api().read(self.settings, actor or self.actor, tid, policy=self.vault._policy)

    def test_live_mode_and_effect_receipts_are_read_only_and_owner_scoped(self):
        from onboarding.migrate import apply_all
        tid = self.create()['task_ids'][0]
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=3)
        rid = str(uuid4())
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET execution_mode='live' WHERE id=%s", (tid,))
            conn.execute("INSERT INTO live_effect_journal(id,task_id,step_key,action,generation,config_revision,"
                "resource_revision,effect_digest,request_hash,request_key,fence_bindings) "
                "SELECT %s,t.id,'project.create','project.create',generation,c.revision,%s,%s,%s,'fixture:observe','[]'::jsonb "
                "FROM onboarding_tasks t JOIN global_configs c ON c.id=t.config_id WHERE t.id=%s",
                (rid, 'a'*64, 'b'*64, 'c'*64, tid))
        before = self.business_snapshot()
        value = self.events(tid)
        self.assertEqual(value['mode'], 'live')
        self.assertFalse(value['execution_connected'], 'historical live rows do not prove an attached worker')
        self.assertEqual(value['effect_events'][0]['receipt_id'], rid)
        self.assertEqual(value['effect_events'][0]['phase'], 'INTENT')
        self.assertFalse(value['effects_has_more'])
        self.assertEqual(before, self.business_snapshot())
        with self.assertRaises(ServiceError) as caught:
            self.events(tid, self.other_owner())
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_pool_and_fixture_owner_reads_no_business_side_effects(self):
        from onboarding import repository
        with self.uow() as conn:
            fixture = repository.create_fixture_task(conn, self.actor, 'fixture:' + uuid4().hex, self.config_id)
        pool = self.create()
        before = self.business_snapshot()
        for tid, outcome in ((fixture['id'], 'OK'), (pool['task_ids'][0], 'CREATED')):
            result = self.events(tid)
            self.assertEqual(set(result), {'task_id', 'mode', 'execution_connected', 'events', 'has_more'})
            self.assertEqual(result['task_id'], tid)
            self.assertEqual(result['mode'], 'synthetic'); self.assertFalse(result['execution_connected'])
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(result['events'][0]['outcome'], outcome)
        self.assertEqual(before, self.business_snapshot())

    def test_foreign_and_missing_tasks_rejected(self):
        tid = self.create()['task_ids'][0]; other = self.other_owner()
        for task, actor in ((tid, other), (str(uuid4()), self.actor)):
            with self.assertRaises(ServiceError) as caught:
                self.events(task, actor)
            self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_commands_are_actual_persistent_records_not_worker_output(self):
        from onboarding import pool_commands
        tid = self.create()['task_ids'][0]
        for version, name in enumerate(('pause', 'cancel', 'recheck'), 2):
            getattr(pool_commands, name)(self.settings, self.actor, tid, version, 'fixture:' + name,
                                        policy=self.vault._policy, mac=self.mac)
        events = self.events(tid)['events']
        self.assertEqual([e['action'] for e in events], ['task.create', 'task.pause', 'task.cancel', 'task.recheck'])
        self.assertEqual([e['version'] for e in events], [2, 3, 4, 5])

    def test_latest_100_allowlisted_records_ordered_and_projected(self):
        tid = self.create()['task_ids'][0]
        with self.uow() as conn:
            for index in range(105):
                conn.execute("INSERT INTO audit_events(actor_id,task_id,action,object_ref,outcome_code,correlation_id,after_summary) "
                             "VALUES(%s,%s,'task.pause','secret-reference','ACCEPTED','secret-correlation', "
                             "jsonb_build_object('version',%s::integer,'secret','CANARY'))", (self.actor.operator_id, tid, index + 2))
            conn.execute("INSERT INTO audit_events(actor_id,task_id,action,object_ref,outcome_code,correlation_id) "
                         "VALUES(%s,%s,'secret.use','secret-ref','CANARY','secret-correlation')", (self.actor.operator_id, tid))
        result = self.events(tid)
        self.assertTrue(result['has_more']); self.assertEqual(len(result['events']), 100)
        self.assertEqual([e['version'] for e in result['events']], list(range(7, 107)))
        ids = [int(e['id']) for e in result['events']]
        self.assertEqual(ids, sorted(ids)); self.assertNotIn('CANARY', str(result)); self.assertNotIn('secret', str(result))

    def test_permission_revocation_after_read_denies_response(self):
        tid = self.create()['task_ids'][0]; original = security.revalidate; calls = []
        def revoke(conn, actor, permission):
            result = original(conn, actor, permission); calls.append(permission)
            if len(calls) == 1:
                conn.execute("UPDATE operators SET permissions=ARRAY[]::text[] WHERE id=%s", (actor.operator_id,))
            return result
        with patch.object(security, 'revalidate', revoke), self.assertRaises(ServiceError) as caught:
            self.events(tid)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        self.assertEqual(calls, ['onboarding:read'])

    def test_bad_stored_outcome_and_missing_dependency_never_leak(self):
        tid = self.create()['task_ids'][0]
        with self.uow() as conn:
            conn.execute("INSERT INTO audit_events(actor_id,task_id,action,object_ref,outcome_code,correlation_id) "
                         "VALUES(%s,%s,'task.pause','secret-ref','SECRET_CANARY','secret-correlation')", (self.actor.operator_id, tid))
        with self.assertRaises(ServiceError) as caught:
            self.events(tid)
        self.assertEqual(str(caught.exception), 'DEPENDENCY_UNAVAILABLE')
        with patch.object(storage, 'open_app', side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)), \
             self.assertRaises(ServiceError) as caught:
            self.events(tid)
        self.assertEqual(str(caught.exception), 'DEPENDENCY_UNAVAILABLE')
