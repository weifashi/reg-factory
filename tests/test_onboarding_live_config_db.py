"""Disposable PostgreSQL v4 global head/CAS checks; never cloud operations."""
from dataclasses import replace as dataclass_replace
import json
import secrets
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg.types.json import Jsonb
from onboarding import audit, migrate, pool_config
from onboarding.errors import ErrorCode, ServiceError
from onboarding.request_mac import RequestMac
from onboarding_pool_support import PoolCase
from test_onboarding_live_config import FIELDS, LEGACY, module


class LiveConfigDBTests(PoolCase):
    def setUp(self):
        super().setUp()
        with self.fixture.migrator() as conn:
            migrate.apply_all(conn, self.fixture.schema, target_version=4)
        from onboarding.live_input_vault import LivePoolPolicy
        self.policy = LivePoolPolicy.from_settings(self.settings)
        self.m = module(self)

    def replace_config(self, expected=None, fields=None, key='live:config', **overrides):
        params = dict(settings=self.settings, actor=self.actor, expected_revision=expected,
                      fields=FIELDS.copy() if fields is None else fields, request_key=key,
                      policy=self.policy, mac=self.mac)
        params.update(overrides)
        return self.m.replace(**params)

    def current(self, **overrides):
        return self.m.get_current(**(dict(settings=self.settings, actor=self.actor,
                                          policy=self.policy) | overrides))

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('canary', str(caught.exception))
        return caught.exception

    def test_single_head_old_history_live_replacement_and_legacy_reader(self):
        self.assertIsNone(self.current())
        old = pool_config.replace(self.settings, self.actor, None, LEGACY, 'same-key',
                                   policy=self.vault._policy, mac=self.mac)
        self.assertEqual(self.current()['nonsecret_config'], LEGACY)
        first = self.replace_config(old['revision'], key='same-key')
        current = self.current()
        self.assertEqual(set(current), {'id','revision','scope','nonsecret_config','secrets_configured','created_at'})
        self.assertEqual(current['id'], first['config_id'])
        self.assertEqual(current['nonsecret_config'], FIELDS)
        self.assertIs(current['secrets_configured'], False)
        self.assertEqual(pool_config.get_current(self.settings, self.actor,
                         policy=self.vault._policy), current)
        self.assertEqual(self.read("SELECT scope,secret_refs FROM global_configs WHERE scope='pool'"),
                         [('pool', {}), ('pool', {})])
        self.assertEqual(pool_config.replace(self.settings, self.actor, None, LEGACY, 'same-key',
                         policy=self.vault._policy, mac=self.mac), old)
        self.assertEqual(self.replace_config(old['revision'], key='same-key'), first)
        self.assertEqual(self.read('SELECT nonsecret_config FROM global_configs WHERE id=%s',
                                  (old['config_id'],))[0][0], LEGACY)

    def test_replay_precedes_cas_append_only_audit_and_conflict(self):
        before = self.business_snapshot()
        first = self.replace_config()
        snapshot = self.business_snapshot()
        self.assertEqual(self.replace_config(fields=dict(reversed(list(FIELDS.items())))), first)
        self.assertEqual(snapshot, self.business_snapshot())
        second = self.replace_config(first['revision'], key='next')
        self.assertEqual(self.replace_config(), first)
        self.assertEqual(self.current()['revision'], second['revision'])
        self.assertFalse(self.reject(lambda: self.replace_config(first['revision'], key='stale'),
                                    ErrorCode.VERSION_CONFLICT).not_committed)
        self.reject(lambda: self.replace_config(fields={**FIELDS, 'project_prefix':'changed'}),
                    ErrorCode.IDEMPOTENCY_CONFLICT)
        self.reject(lambda: self.replace_config(first['revision']), ErrorCode.IDEMPOTENCY_CONFLICT)
        for table in ('onboarding_tasks', 'onboarding_batches', 'mailbox_registry',
                      'mailbox_platform_states', 'secret_objects', 'resource_leases', 'task_steps'):
            self.assertEqual(before[table], self.business_snapshot()[table])
        rows = self.read("SELECT a.after_summary,r.result_summary,a.object_ref,a.correlation_id,r.id "
                         "FROM audit_events a JOIN operation_receipts r ON r.id::text=a.correlation_id "
                         "WHERE r.action='pool.live_config.replace'")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[0], {'version': 2})
            self.assertEqual(row[1]['config_id'], row[2])
            self.assertEqual(row[3], str(row[4]))

    def test_policy_context_actor_input_rejected_before_writes(self):
        before = self.business_snapshot()
        for overrides in ({'policy': self.vault._policy}, {'policy': object()},
                          {'settings': dataclass_replace(self.settings, schema='rf_onboarding')},
                          {'mac': object()}, {'fields': LEGACY},
                          {'fields': {**FIELDS, 'url': 'private-canary'}},
                          {'fields': {**FIELDS, 'concurrency': True}}):
            self.reject(lambda: self.replace_config(**overrides))
        for actor in (object(), self.fixture_actor):
            self.reject(lambda: self.replace_config(actor=actor), ErrorCode.UNAUTHENTICATED)
            self.reject(lambda: self.current(actor=actor), ErrorCode.UNAUTHENTICATED)
        self.reject(lambda: self.current(permission='tasks:run'), ErrorCode.FORBIDDEN)
        self.reject(lambda: self.replace_config('pool-' + str(uuid4())), ErrorCode.VERSION_CONFLICT)
        self.assertEqual(before, self.business_snapshot())

    def test_session_permissions_revocation_and_no_permission_cache(self):
        first = self.replace_config()
        for mutation, restore, code in (
            ("UPDATE operators SET permissions='{}'", "UPDATE operators SET permissions=ARRAY['config:manage','onboarding:read','tasks:create','mailboxes:manage','cards:manage']", ErrorCode.FORBIDDEN),
            ('UPDATE operators SET disabled=true', 'UPDATE operators SET disabled=false', ErrorCode.UNAUTHENTICATED),
            ('UPDATE operators SET auth_epoch=2', 'UPDATE operators SET auth_epoch=1', ErrorCode.UNAUTHENTICATED),
            ('UPDATE operator_sessions SET revoked_at=clock_timestamp()', 'UPDATE operator_sessions SET revoked_at=NULL', ErrorCode.UNAUTHENTICATED),
            ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second'", "UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes'", ErrorCode.UNAUTHENTICATED)):
            with self.uow() as conn: conn.execute(mutation)
            self.reject(self.current, code)
            self.reject(self.replace_config, code)
            with self.uow() as conn: conn.execute(restore)
        self.assertEqual(self.current()['id'], first['config_id'])
        with self.uow() as conn: conn.execute("UPDATE operators SET permissions=ARRAY['config:manage']")
        self.reject(self.current, ErrorCode.FORBIDDEN)
        self.assertEqual(self.current(permission='config:manage')['id'], first['config_id'])
        self.replace_config(first['revision'], key='write-without-read')

    def test_monotonic_created_at_future_and_overflow_rollback(self):
        first = self.replace_config()
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET created_at=clock_timestamp()+interval '10 years' WHERE id=%s", (first['config_id'],))
        previous = self.current()
        second = self.replace_config(first['revision'], key='future')
        self.assertGreater(self.current()['created_at'], previous['created_at'])
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET created_at='9999-12-31 23:59:59.999999+00' WHERE id=%s", (second['config_id'],))
        before = self.business_snapshot()
        self.reject(lambda: self.replace_config(second['revision'], key='overflow'), ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(before, self.business_snapshot())

    def test_live_mac_domain_scoped_canonical_body_no_old_replay(self):
        calls = []; original = RequestMac.request_digest
        def observe(instance, action, owner, payload):
            calls.append((action, owner, payload)); return original(instance, action, owner, payload)
        with patch.object(RequestMac, 'request_digest', observe): self.replace_config()
        body = json.dumps(dict(v=2, schema=self.settings.schema, instance_marker=self.settings.instance_marker,
             expected_revision=None, fields=FIELDS), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        self.assertIn(('pool.live_config.replace.v2', self.actor.operator_id, body), calls)
        self.assertNotIn('pool.config.replace.v1', {v[0] for v in calls})
        self.assertEqual(self.read("SELECT action FROM operation_receipts WHERE idempotency_key='live:config'"),
                         [('pool.live_config.replace',)])

    def test_no_auth_profile_or_provider_key_read_and_get_needs_no_mac(self):
        (self.key_directory / 'v1.key').unlink()
        with patch('onboarding.google_auth_runtime.GoogleTokenProvider',
                   side_effect=AssertionError('no profile access')), \
             patch('onboarding.keyring.Keyring', side_effect=AssertionError('no vault access')):
            first = self.replace_config()
        sessions = self.read('SELECT * FROM operator_sessions')
        (self.key_directory / 'request-mac.key').unlink()
        self.assertEqual(self.current()['id'], first['config_id'])
        self.assertEqual(self.read('SELECT * FROM operator_sessions'), sessions)

    def test_corrupt_history_receipt_and_digest_precedence(self):
        first = self.replace_config()
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                         (Jsonb({**FIELDS, 'password':'private-canary'}), first['config_id']))
        self.reject(self.current, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.reject(self.replace_config, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.reject(lambda: self.replace_config(fields={**FIELDS, 'project_prefix':'different'}),
                    ErrorCode.IDEMPOTENCY_CONFLICT)
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s', (Jsonb(FIELDS), first['config_id']))
        for bad in ({**first, 'extra':'private-canary'}, {**first, 'revision':'pool-'+str(uuid4())},
                    {**first, 'request_key':'wrong'}):
            with self.fixture.migrator() as conn:
                conn.execute("UPDATE operation_receipts SET result_summary=%s WHERE action='pool.live_config.replace'", (Jsonb(bad),))
            self.reject(self.replace_config, ErrorCode.DEPENDENCY_UNAVAILABLE)
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE operation_receipts SET result_summary=%s,generation=2 WHERE action='pool.live_config.replace'", (Jsonb(first),))
        self.reject(self.replace_config, ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_final_policy_mac_auth_and_audit_failures_rollback(self):
        original = audit.append
        for mode, code in (('audit', ErrorCode.DEPENDENCY_UNAVAILABLE), ('path', ErrorCode.FORBIDDEN),
                           ('mac', ErrorCode.SECRET_UNAVAILABLE), ('auth', ErrorCode.UNAUTHENTICATED)):
            before = self.business_snapshot(); reached = []
            def failure(conn, *args, **kwargs):
                reached.append(True)
                if mode == 'audit': raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                result = original(conn, *args, **kwargs)
                if mode == 'path': conn.execute('SET LOCAL search_path TO pg_catalog')
                if mode == 'auth': conn.execute('UPDATE operators SET disabled=true WHERE id=%s', (self.actor.operator_id,))
                if mode == 'mac': (self.key_directory / 'request-mac.key').write_bytes(secrets.token_bytes(32))
                return result
            with patch.object(audit, 'append', failure): self.reject(self.replace_config, code)
            self.assertEqual(reached, [True]); self.assertEqual(before, self.business_snapshot())

    def test_checksum_drift_fail_closed(self):
        first = self.replace_config()
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE schema_migrations SET checksum=%s WHERE version=4", ('0'*64,))
        before = self.business_snapshot()
        self.reject(self.current, ErrorCode.VERSION_CONFLICT)
        self.reject(lambda: self.replace_config(first['revision'], key='drift'), ErrorCode.VERSION_CONFLICT)
        self.assertEqual(before, self.business_snapshot())

    def test_same_key_parallel_replay_is_one_config_and_one_receipt(self):
        second_actor = self.second_session()
        first, replay = self.race_original("428701", self.replace_config,
                   lambda: self.replace_config(actor=second_actor))
        self.assertEqual(first[0], 'OK'); self.assertEqual(replay, first)
        self.assertEqual(self.read("SELECT count(*) FROM global_configs WHERE scope='pool'")[0][0], 1)
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='pool.live_config.replace'")[0][0], 1)

    def test_old_and_live_writers_serialize_on_the_same_head_guard(self):
        actor = self.second_session()
        first, other = self.race_original('428701', self.replace_config,
            lambda: pool_config.replace(self.settings, actor, None, LEGACY,
                         'old-concurrent', policy=self.vault._policy, mac=self.mac))
        self.assertEqual(first[0], 'OK')
        self.assertEqual(other[0], ErrorCode.VERSION_CONFLICT.value)
        self.assertEqual(self.current()['id'], first[1]['config_id'])
        self.assertEqual(self.read("SELECT count(*) FROM global_configs WHERE scope='pool'")[0][0], 1)

    def test_real_commit_then_lost_ack_remains_unknown_and_replay_is_read_only(self):
        from contextlib import contextmanager
        import psycopg
        original = psycopg.Connection.transaction; committed = []
        @contextmanager
        def lost_ack(conn, *args, **kwargs):
            with original(conn, *args, **kwargs) as transaction:
                yield transaction
            committed.append(conn.info.backend_pid)
            raise psycopg.OperationalError('fixture:lost-ack-after-real-commit')
        with patch.object(psycopg.Connection, 'transaction', lost_ack):
            error = self.reject(self.replace_config, ErrorCode.COMMIT_UNKNOWN)
        self.assertFalse(error.not_committed)
        self.assertEqual(len(committed), 1)
        self.assertEqual(self.read("SELECT count(*) FROM global_configs WHERE scope='pool'"), [(1,)])
        before = self.business_snapshot()
        receipt = self.replace_config()
        self.assertEqual(receipt['config_id'], self.current()['id'])
        self.assertEqual(before, self.business_snapshot())
