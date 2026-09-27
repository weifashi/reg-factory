"""Historical management only: real local PG and typed synthetic Vault seeds.

Cursor observation never replaces results except the explicitly named malformed
row seams (types impossible to store through PostgreSQL constraints/decoders).
"""
import copy
import importlib
import importlib.util
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from onboarding import pool_config, repository, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_secret_types import MailboxCredential, PlatformCredential, PoolResource
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding_pool_support import PoolCase

FIELDS = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)
KEYS = set(('id,batch_id,config_id,execution_scope,mailbox_id,mailbox_ref,platform,platform_plan,'
            'credential_version,mailbox_credential_ref,platform_credential_pins,status,reason_code,'
            'current_step,generation,version,cancel_requested,created_at,updated_at,created_by,'
            'batch_config_id,selected_mailbox_refs,requested_count,selection_mode,config_revision,'
            'config_scope,nonsecret_config,mailbox_owner_id').split(','))


def module():
    return importlib.import_module('onboarding.pool_repository')


@contextmanager
def observe(hook=None, seam=None):
    """Wrap real psycopg cursors. seam is explicitly malformed decoded-row input."""
    original_execute, original_fetch = psycopg.Cursor.execute, psycopg.Cursor.fetchone
    commands, queries = [], {}

    def execute(cursor, query, params=None, **kwargs):
        text = query if type(query) is str else str(query)
        commands.append((cursor.connection.info.backend_pid, text, params))
        queries[id(cursor)] = text
        result = original_execute(cursor, query, params, **kwargs)
        if hook is not None:
            hook(cursor, text, params)
        return result

    def fetch(cursor):
        row = original_fetch(cursor)
        return row if seam is None else seam(queries.get(id(cursor), ''), row)

    with patch.object(psycopg.Cursor, 'execute', execute), patch.object(psycopg.Cursor, 'fetchone', fetch):
        yield commands


class PoolRepositoryInputTests(unittest.TestCase):
    def test_local_invalid_input_executes_zero_sql(self):
        # No usable connection or settings: every error must precede policy/SQL.
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        policy = object.__new__(SyntheticPoolPolicy)
        valid = dict(conn=object(), actor=actor, task_id=str(uuid4()), policy=policy)
        class Text(str): pass
        class ActorSubclass(security.Actor): pass
        actors = [object(), repository.FixtureActor(actor.operator_id),
                  ActorSubclass(actor.operator_id, frozenset(), actor.session_id, 1)]
        for field, value in [('operator_id', Text(actor.operator_id)), ('session_id', 'bad'),
                             ('permissions', set()), ('permissions', frozenset({Text('tasks:manage')})),
                             ('auth_epoch', True), ('auth_epoch', 2**63)]:
            forged = copy.copy(actor)
            object.__setattr__(forged, field, value)
            actors.append(forged)
        for kind in ('extra', 'missing'):
            forged = copy.copy(actor)
            if kind == 'extra': object.__setattr__(forged, 'extra', 1)
            else: object.__delattr__(forged, 'session_id')
            actors.append(forged)
        cases = [('actor', value, ErrorCode.UNAUTHENTICATED) for value in actors]
        cases += [('task_id', value, ErrorCode.INVALID_INPUT) for value in
                  (None, True, uuid4(), 'bad', uuid4().hex, Text(valid['task_id']))]
        cases += [('expected_version', value, ErrorCode.INVALID_INPUT) for value in
                  (True, False, 0, -1, 2**63, '1', 1.0)]
        cases += [('permission', value, ErrorCode.FORBIDDEN) for value in
                  ('config:manage', None, [], Text('tasks:manage'))]
        cases += [('policy', object(), ErrorCode.INVALID_INPUT)]
        with patch.object(psycopg.Cursor, 'execute', side_effect=AssertionError('SQL before validation')) as sql:
            for name, value, code in cases:
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaises(ServiceError) as caught:
                        module().lock_task(**{**valid, name: value})
                    self.assertEqual(caught.exception.code, code)
            sql.assert_not_called()


class PoolRepositoryTests(PoolCase):
    def test_entry_after_real_pool_setup(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.pool_repository'),
                             'missing approved historical pool repository')
        self.assertTrue(callable(module().lock_task))

    def seed(self, plan=('google',), *, editor=None, missing_platform_secret=False):
        """Test-only historical SQL fixture, NOT a production task creation API."""
        mailbox, batch, task, config = (str(uuid4()) for _ in range(4))
        email = 'fixture-' + uuid4().hex + '@fixture.invalid'
        pins = {}
        with self.uow() as conn:
            secret = self.vault.put_locked(conn, self.actor, PoolResource('mailbox', mailbox),
                                          MailboxCredential(email, password='fixture:mailbox'))
            conn.execute('INSERT INTO mailbox_registry '
                         '(id,owner_operator_id,email_norm,source_type,credential_ref,credential_version) '
                         "VALUES(%s,%s,%s,'outlook',%s,2)",
                         (mailbox, self.actor.operator_id, email, secret.id))
            for name in plan:
                state = str(uuid4())
                conn.execute('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version) '
                             'VALUES(%s,%s,%s,3)', (state, mailbox, name))
                credential = None if missing_platform_secret else self.vault.put_locked(
                    conn, self.actor, PoolResource('platform', state),
                    PlatformCredential(email, name, 'fixture:platform')).id
                conn.execute("UPDATE mailbox_platform_states SET credential_ref=%s,identity_status='EXISTING' WHERE id=%s",
                             (credential, state))
                pins[name] = dict(state_id=state, secret_ref=credential, revision=3, identity_status='EXISTING')
            conn.execute('INSERT INTO global_configs(id,revision,nonsecret_config,changed_by,scope) '
                         "VALUES(%s,%s,%s,%s,'pool')", (config, 'pool-' + str(uuid4()), Jsonb(FIELDS),
                                                     editor or self.actor.operator_id))
            conn.execute('INSERT INTO onboarding_batches '
                         '(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) '
                         "VALUES(%s,'specified',1,%s,%s,%s)",
                         (batch, Jsonb([mailbox]), config, self.actor.operator_id))
            conn.execute('INSERT INTO onboarding_tasks '
                         '(id,batch_id,config_id,execution_scope,mailbox_id,mailbox_ref,platform,platform_plan,'
                         'credential_version,mailbox_credential_ref,platform_credential_pins) '
                         "VALUES(%s,%s,%s,'pool',%s,%s,%s,%s,2,%s,%s)",
                         (task, batch, config, mailbox, mailbox, plan[0] if len(plan) == 1 else 'combined',
                          Jsonb(list(plan)), secret.id, Jsonb(pins)))
        return dict(id=task, batch=batch, config=config, mailbox=mailbox, secret=secret.id, pins=pins, email=email)

    def lock(self, conn, seed, **kwargs):
        return module().lock_task(conn, self.actor, seed['id'], policy=self.vault._policy, **kwargs)

    def reject(self, call, code=ErrorCode.FORBIDDEN):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('fixture:mailbox', str(caught.exception))

    def snapshot(self):
        return {name: self.read('SELECT * FROM ' + name + ' ORDER BY 1') for name in
                ('onboarding_tasks', 'onboarding_batches', 'mailbox_registry', 'mailbox_platform_states',
                 'global_configs', 'secret_objects', 'resource_leases', 'operator_sessions',
                 'operation_receipts', 'audit_events')}

    def test_google_combined_exact_history_projection_and_no_writes(self):
        other = str(uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture-only','{}')",
                         (other, 'fixture-' + other))
        for plan in (('google',), ('k12', 'github', 'claude', 'chatgpt', 'grok', 'kiro')):
            seed = self.seed(plan, editor=other)
            before = self.snapshot()
            with self.uow() as conn, observe() as commands:
                pid = conn.info.backend_pid
                result = self.lock(conn, seed, expected_version=1)
                self.assertEqual(set(result), KEYS)
                self.assertEqual(result['platform_plan'], plan)
                self.assertEqual(result['platform_credential_pins'], seed['pins'])
                self.assertEqual(result['selected_mailbox_refs'], (seed['mailbox'],))
                self.assertEqual(result['credential_version'], 2)
                self.assertEqual(result['created_by'], self.actor.operator_id)
                self.assertEqual(result['mailbox_owner_id'], self.actor.operator_id)
                self.assertEqual(result['created_at'].tzinfo, timezone.utc)
                result['nonsecret_config']['model'] = 'mutated'
                result['platform_credential_pins'][plan[0]]['revision'] = 999
                again = self.lock(conn, seed)
                self.assertEqual(again['nonsecret_config'], FIELDS)
                self.assertEqual(again['platform_credential_pins'], seed['pins'])
            # Filter own UoW SELECTs; no COMMIT, write or secret-body query in entry.
            texts = [query for backend, query, _ in commands if backend == pid]
            self.assertTrue(all(query.startswith('SELECT ') for query in texts))
            secrets = [query for query in texts if 'FROM secret_objects' in query]
            self.assertEqual(len(secrets), 2 * (1 + len(plan)))
            for query in secrets:
                self.assertNotIn('*', query)
                self.assertNotIn('ciphertext', query)
                self.assertNotIn('nonce', query)
                self.assertNotIn('FOR ', query)
            self.assertFalse(any('advisory' in query or 'ORDER BY created_at' in query for query in texts))
            self.assertEqual(self.snapshot(), before)

    def test_new_head_does_not_replace_old_config(self):
        seed = self.seed()
        revision = self.read('SELECT revision FROM global_configs WHERE id=%s', (seed['config'],))[0][0]
        latest = pool_config.replace(self.settings, self.actor, revision,
                                     {**FIELDS, 'model': 'fixture-new-head'}, 'fixture:new-head',
                                     policy=self.vault._policy, mac=self.mac)
        with self.uow() as conn:
            result = self.lock(conn, seed)
        self.assertNotEqual(result['config_id'], latest['config_id'])
        self.assertEqual(result['config_id'], seed['config'])
        self.assertEqual(result['nonsecret_config'], FIELDS)

    def test_history_drift_disabled_states_expired_revoked_and_terminal_remain_manageable(self):
        seed = self.seed(('claude', 'github'))
        with self.uow() as conn:
            replacement = self.vault.put_locked(conn, self.actor, PoolResource('mailbox', seed['mailbox']),
                                               MailboxCredential(seed['email'], password='fixture:replacement'))
            conn.execute('UPDATE mailbox_registry SET credential_ref=%s,credential_version=8,version=9,'
                         "disabled=true,health='DISABLED',pool_status='EXPORTED' WHERE id=%s",
                         (replacement.id, seed['mailbox']))
            conn.execute("UPDATE mailbox_platform_states SET credential_ref=NULL,credential_version=9,version=10,"
                         "identity_status='NEW_CONFIRMED',usage_status='CONFLICT' WHERE mailbox_id=%s", (seed['mailbox'],))
            conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()-interval '1 day',"
                         "revoked_at=clock_timestamp()-interval '1 hour' WHERE id=%s OR access_policy LIKE %s",
                         (seed['secret'], 'pool:' + self.actor.operator_id + ':platform:%'))
        with self.uow() as conn:
            conn.execute('INSERT INTO resource_leases(resource_kind,resource_id,task_id,owner_id,fence,lease_until,hold_reason) '
                         "VALUES('mailbox',%s,%s,'fixture:old-owner',9,clock_timestamp()-interval '1 day','UNKNOWN')",
                         (seed['mailbox'], seed['id']))
        lease_before = self.read('SELECT * FROM resource_leases')
        for status in sorted(repository.STATES):
            with self.uow() as conn:
                conn.execute('UPDATE onboarding_tasks SET status=%s,cancel_requested=true,'
                             "reason_code='legacy free text',current_step='future.step:untrusted' WHERE id=%s",
                             (status, seed['id']))
                result = self.lock(conn, seed)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['mailbox_credential_ref'], seed['secret'])
                self.assertEqual(result['platform_credential_pins'], seed['pins'])
                self.assertTrue(result['cancel_requested'])
                self.assertEqual(result['current_step'], 'future.step:untrusted')
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET health='UNKNOWN',pool_status='QUARANTINED' WHERE id=%s", (seed['mailbox'],))
            for identity in ('UNKNOWN', 'EXISTING', 'NEW_CONFIRMED'):
                for usage in ('UNUSED', 'RESERVED', 'SUCCEEDED', 'FAILED_CONFIRMED', 'UNKNOWN', 'CONFLICT', 'HISTORY_UNRECONCILED'):
                    conn.execute('UPDATE mailbox_platform_states SET identity_status=%s,usage_status=%s WHERE mailbox_id=%s',
                                 (identity, usage, seed['mailbox']))
                    self.assertEqual(self.lock(conn, seed)['platform_credential_pins'], seed['pins'])
        self.assertEqual(self.read('SELECT * FROM resource_leases'), lease_before)
        missing = self.seed(('google',), missing_platform_secret=True)
        with self.uow() as conn, observe() as commands:
            result = self.lock(conn, missing)
        self.assertIsNone(result['platform_credential_pins']['google']['secret_ref'])
        self.assertEqual(sum('FROM secret_objects' in query for _, query, _ in commands), 1)

    def test_fixture_owner_and_graph_isolation_cas_last(self):
        seed, other = self.seed(), self.seed(('claude',))
        fixture = self.task()
        with self.uow() as conn:
            self.reject(lambda: self.lock(conn, fixture))
            self.reject(lambda: self.lock(conn, {'id': str(uuid4())}))
            self.reject(lambda: repository.lock_task(conn, self.actor, seed['id']))
            self.reject(lambda: self.lock(conn, seed, expected_version=2), ErrorCode.VERSION_CONFLICT)
            patches = [
                ('UPDATE onboarding_batches SET config_id=%s WHERE id=%s', (self.config_id, seed['batch'])),
                ('UPDATE onboarding_batches SET selected_mailbox_refs=%s WHERE id=%s', (Jsonb([other['mailbox']]), seed['batch'])),
                ('UPDATE onboarding_tasks SET mailbox_ref=%s WHERE id=%s', (other['mailbox'], seed['id'])),
                ('UPDATE onboarding_tasks SET mailbox_credential_ref=%s WHERE id=%s', (other['secret'], seed['id'])),
                ('UPDATE secret_objects SET kind=%s WHERE id=%s', ('platform_credential', seed['secret'])),
                ('UPDATE secret_objects SET access_policy=%s WHERE id=%s', ('fixture:wrong-policy', seed['secret'])),
            ]
            wrong_pin = copy.deepcopy(seed['pins'])
            wrong_pin['google']['state_id'] = other['pins']['claude']['state_id']
            patches.append(('UPDATE onboarding_tasks SET platform_credential_pins=%s WHERE id=%s', (Jsonb(wrong_pin), seed['id'])))
            for query, params in patches:
                with self.subTest(query=query), conn.transaction(force_rollback=True):
                    conn.execute(query, params)
                    self.reject(lambda: self.lock(conn, seed, expected_version=2))
            other_owner = str(uuid4())
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture-only','{}')",
                         (other_owner, 'fixture-' + other_owner))
            for table, field, identity in (('onboarding_batches', 'created_by', seed['batch']),
                                            ('mailbox_registry', 'owner_operator_id', seed['mailbox'])):
                with conn.transaction(force_rollback=True):
                    conn.execute('UPDATE ' + table + ' SET ' + field + '=%s WHERE id=%s', (other_owner, identity))
                    self.reject(lambda: self.lock(conn, seed, expected_version=2))
        # Real scope identity error versus remaining malformed config schema.
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET scope='fixture' WHERE id=%s", (seed['config'],))
        with self.uow() as conn:
            self.reject(lambda: self.lock(conn, seed))
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET scope='pool',nonsecret_config=%s WHERE id=%s",
                         (Jsonb({**FIELDS, 'unknown': 'fixture:bad'}), seed['config']))
        with self.uow() as conn:
            self.reject(lambda: self.lock(conn, seed), ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_live_auth_revocation_epoch_permissions_and_no_followup_graph_reads(self):
        seed = self.seed()
        with self.uow() as conn:
            for query in (
                'UPDATE operators SET disabled=true WHERE id=%s',
                'UPDATE operators SET auth_epoch=auth_epoch+1 WHERE id=%s',
                "UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE operator_id=%s",
                "UPDATE operator_sessions SET expires_at=statement_timestamp()-interval '1 second',"
                "idle_expires_at=statement_timestamp()-interval '1 second' WHERE operator_id=%s",
                "UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE operator_id=%s",
                "UPDATE operators SET permissions='{}' WHERE id=%s",
            ):
                with self.subTest(query=query), conn.transaction(force_rollback=True):
                    conn.execute(query, (self.actor.operator_id,))
                    with observe() as commands:
                        self.reject(lambda: self.lock(conn, seed, expected_version=2),
                                    ErrorCode.FORBIDDEN if "permissions='{}'" in query else ErrorCode.UNAUTHENTICATED)
                    for _, text, _ in commands:
                        self.assertFalse(any('FROM ' + table in text for table in
                            ('onboarding_batches', 'mailbox_registry', 'mailbox_platform_states', 'global_configs', 'secret_objects')))
            conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read'] WHERE id=%s", (self.actor.operator_id,))
            self.assertEqual(self.lock(conn, seed, permission='onboarding:read')['id'], seed['id'])
            self.reject(lambda: self.lock(conn, seed))
            empty_cache = security.Actor(self.actor.operator_id, frozenset(), self.actor.session_id, 1)
            self.assertEqual(module().lock_task(conn, empty_cache, seed['id'], 'onboarding:read',
                                               policy=self.vault._policy)['id'], seed['id'])
        with self.fixture.app() as conn:
            self.reject(lambda: self.lock(conn, seed), ErrorCode.INVALID_INPUT)

    def test_explicit_malformed_decoded_row_seams(self):
        seed = self.seed()
        class Text(str): pass
        task_fields = module()._TASK_FIELDS
        changes = []
        def task_change(field, value):
            return ('FROM onboarding_tasks t', task_fields.index(field), value)
        for field, value in [('id', str(uuid4())), ('version', True), ('generation', 2**63),
                             ('cancel_requested', 1), ('created_at', datetime.now()),
                             ('updated_at', 'infinity'), ('status', 'NEW_UNKNOWN'),
                             ('reason_code', []), ('current_step', {}), ('platform_plan', ('google',)),
                             ('platform_plan', ['google', 'google']), ('platform', Text('google')),
                             ('mailbox_ref', 'bad')]:
            changes.append(task_change(field, value))
        for value in ({Text('google'): seed['pins']['google']}, {'google': {Text(k): v for k, v in seed['pins']['google'].items()}},
                      {'google': {**seed['pins']['google'], 'revision': True}},
                      {'google': {**seed['pins']['google'], 'secret_ref': uuid4().hex}},
                      {'google': {**seed['pins']['google'], 'identity_status': 'BAD'}}, {}):
            changes.append(task_change('platform_credential_pins', value))
        changes += [('FROM onboarding_batches', 2, [seed['mailbox'], seed['mailbox']]),
                    ('FROM onboarding_batches', 3, True), ('FROM onboarding_batches', 3, 2**31),
                    ('FROM onboarding_batches', 4, 'random'), ('FROM global_configs', 0, seed['config']),
                    ('FROM global_configs', 2, {Text(k): v for k, v in FIELDS.items()}),
                    ('FROM global_configs', 6, None), ('FROM global_configs', 1, 'not-pool-revision'),
                    ('FROM global_configs', 3, {'secret': str(uuid4())}),
                    ('FROM secret_objects', 0, seed['secret']), ('FROM secret_objects', 2, True),
                    ('FROM secret_objects', 3, ''), ('FROM secret_objects', 3, 'x'*129),
                    ('FROM secret_objects', 4, []), ('FROM secret_objects', 5, datetime.now()),
                    ('FROM secret_objects', 6, 'infinity'), ('FROM secret_objects', 7, 0)]
        with self.uow() as conn:
            for marker, index, value in changes:
                def seam(query, row):
                    if marker in query and row is not None:
                        result = list(row); result[index] = value; return tuple(result)
                    return row
                with self.subTest(marker=marker, index=index, value_type=type(value).__name__), observe(seam=seam):
                    self.reject(lambda: self.lock(conn, seed, expected_version=2), ErrorCode.DEPENDENCY_UNAVAILABLE)
            for marker in ('FROM onboarding_batches', 'FROM mailbox_registry', 'FROM mailbox_platform_states',
                           'FROM global_configs', 'FROM secret_objects'):
                with self.subTest(missing=marker), observe(seam=lambda query, row: None if marker in query else row):
                    self.reject(lambda: self.lock(conn, seed, expected_version=2))

    def test_caller_transaction_rollback_lock_release_and_final_search_path(self):
        seed = self.seed()
        before = self.snapshot()
        with self.uow() as conn:
            pid = conn.info.backend_pid
            conn.execute('SAVEPOINT history_read')
            self.lock(conn, seed)
            with self.fixture.app() as other, other.transaction():
                self.assertNotEqual(pid, other.info.backend_pid)
                with self.assertRaises(psycopg.errors.LockNotAvailable), other.transaction():
                    other.execute('SELECT id FROM onboarding_tasks WHERE id=%s FOR UPDATE NOWAIT', (seed['id'],))
            conn.execute('ROLLBACK TO SAVEPOINT history_read')
        with self.uow() as other:
            other.execute('SELECT id FROM onboarding_tasks WHERE id=%s FOR UPDATE NOWAIT', (seed['id'],))
        self.assertEqual(self.snapshot(), before)
        with self.uow() as conn:
            changed = False
            def hook(cursor, query, params):
                nonlocal changed
                if not changed and 'FROM secret_objects' in query:
                    changed = True
                    # Real SQL, no policy-result mock. Keep business schema first.
                    cursor.connection.execute('SET LOCAL search_path TO ' + self.fixture.schema + ',pg_catalog,public')
            with observe(hook=hook):
                self.reject(lambda: self.lock(conn, seed, expected_version=2))
            self.assertTrue(changed)

    def test_real_backend_waits_auth_ttl_precedes_graph_and_cas(self):
        seed = self.seed(('github', 'claude'))
        state = min(pin['state_id'] for pin in seed['pins'].values())
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_batches SET config_id=%s WHERE id=%s', (self.config_id, seed['batch']))
        # Three independent waits in the same fixture: no timeout setting changes.
        for table, identity in (('onboarding_tasks', seed['id']), ('mailbox_registry', seed['mailbox']),
                                ('mailbox_platform_states', state)):
            with self.subTest(table=table):
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '650 milliseconds' "
                                 'WHERE id=%s RETURNING idle_expires_at', (self.actor.session_id,))
                    deadline = conn.execute('SELECT idle_expires_at FROM operator_sessions WHERE id=%s', (self.actor.session_id,)).fetchone()[0]
                started, done = threading.Event(), threading.Event()
                result, pids = [], []
                def worker():
                    try:
                        with self.uow() as conn:
                            pids.append(conn.info.backend_pid); started.set()
                            self.lock(conn, seed, expected_version=2)
                        result.append('unexpected_success')
                    except ServiceError as exc:
                        result.append(exc.code)
                    except Exception as exc:
                        result.append(type(exc).__name__)
                    finally:
                        done.set()
                with self.uow() as locker, self.fixture.app() as observer, observe() as commands:
                    locker.execute('SAVEPOINT history_wait')
                    locker.execute('SELECT id FROM ' + table + ' WHERE id=%s FOR UPDATE', (identity,))
                    thread = threading.Thread(target=worker)
                    thread.start()
                    try:
                        self.assertTrue(started.wait(1), 'worker did not connect')
                        self.assertNotEqual(pids[0], locker.info.backend_pid)
                        observed = False
                        stop = time.monotonic() + 1.3
                        while time.monotonic() < stop:
                            blockers, now = observer.execute('SELECT pg_blocking_pids(%s),clock_timestamp()', (pids[0],)).fetchone()
                            if locker.info.backend_pid in blockers:
                                observed = True
                            if observed and now > deadline:
                                break
                            done.wait(.01)
                        self.assertTrue(observed, 'no actual backend lock wait observed')
                        self.assertGreater(now, deadline)
                        self.assertFalse(done.is_set(), 'must still wait on held lock')
                    finally:
                        locker.execute('ROLLBACK TO SAVEPOINT history_wait')
                        thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result, [ErrorCode.UNAUTHENTICATED])
                    queries = [query for backend, query, _ in commands if backend == pids[0]]
                    self.assertFalse(any('FROM global_configs' in query or 'FROM secret_objects' in query for query in queries))
                    if table == 'onboarding_tasks':
                        self.assertFalse(any('FROM mailbox_registry' in query for query in queries))
                    if table == 'mailbox_registry':
                        self.assertFalse(any('FROM mailbox_platform_states' in query for query in queries))
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.actor.session_id,))

    def test_observed_task_auth_mailbox_sorted_states_and_no_extra_locks(self):
        seed = self.seed(('github', 'claude', 'k12'))
        with self.uow() as conn, observe() as commands:
            self.lock(conn, seed)
        queries = [(query, params) for _, query, params in commands if 'FOR UPDATE' in query or 'FOR SHARE' in query]
        self.assertIn('FROM onboarding_tasks t', queries[0][0])
        self.assertIn('FROM operators', queries[1][0])
        self.assertIn('FROM operator_sessions', queries[2][0])
        self.assertIn('FROM mailbox_registry', queries[3][0])
        states = [(query, params) for query, params in queries if 'FROM mailbox_platform_states' in query]
        self.assertEqual([params[0] for _, params in states], sorted(pin['state_id'] for pin in seed['pins'].values()))
        for query, params in states:
            self.assertIn('WHERE id=%s AND mailbox_id=%s AND platform=%s', query)
            self.assertEqual(params[1], seed['mailbox'])
        self.assertFalse(any(any('FROM ' + name in query for name in ('global_configs', 'secret_objects', 'onboarding_batches'))
                             for query, _ in queries))
