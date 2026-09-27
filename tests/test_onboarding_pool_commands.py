"""Command acceptance contract: real synthetic PG, never external execution.

Decoded-row seams explicitly cover types PostgreSQL cannot persist. Spies wrap
real boundaries; transaction fault seams distinguish rollback from lost ACK.
"""
import copy
import hashlib
import importlib
import importlib.util
import json
import multiprocessing
import secrets
import threading
import time
import unittest
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from onboarding import audit, repository, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.coordinator import FixtureExecutor
from onboarding.secret_store import FixtureConsumer, SecretStore
from onboarding.pool_secret_types import MailboxCredential, PlatformCredential, PoolResource
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.settings import BASE, Settings, load_settings
from onboarding_pool_support import PoolCase, after_query

FIELDS = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)
MAX = 2**63 - 1
COMMANDS = ('pause', 'cancel', 'recheck')
TERMINAL = {'SUCCEEDED', 'FAILED_CONFIRMED', 'CANCELLED_SAFE'}
STEP_STATES = ('NOT_SENT', 'RUNNING', 'INTENT', 'UNKNOWN', 'SUCCEEDED',
               'FAILED_CONFIRMED', 'CONFLICT', 'CANCELLED_SAFE')


def module():
    return importlib.import_module('onboarding.pool_commands')


@contextmanager
def observe(hook=None, seam=None):
    """Actual cursors; seam means explicit malformed decoded-row fault only."""
    execute, fetch = psycopg.Cursor.execute, psycopg.Cursor.fetchone
    commands, queries = [], {}
    def run(cursor, query, params=None, **kwargs):
        text = query if type(query) is str else str(query)
        commands.append((cursor.connection.info.backend_pid, text, params))
        queries[id(cursor)] = text
        result = execute(cursor, query, params, **kwargs)
        if hook:
            hook(cursor, text, params)
        return result
    def one(cursor):
        row = fetch(cursor)
        return seam(queries.get(id(cursor), ''), row) if seam else row
    with patch.object(psycopg.Cursor, 'execute', run), patch.object(psycopg.Cursor, 'fetchone', one):
        yield commands


@contextmanager
def no_consumption():
    with ExitStack() as stack:
        spies = [stack.enter_context(patch.object(owner, name,
                 side_effect=AssertionError('command crossed consumption boundary')))
                 for owner, name in ((FixtureConsumer, 'consume'), (SecretStore, 'use'),
                    (SecretStore, '_decrypt'), (AESGCM, 'decrypt'), (FixtureExecutor, '_execute'))]
        try:
            yield
        finally:
            for spy in spies:
                spy.assert_not_called()


def spawned_command(schema, actor_fields, directory, task_id, command, expected, key, barrier, output):
    """Two independent APP backends; barrier before any task/session lock."""
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    actor = security.Actor(*actor_fields)
    policy, mac = SyntheticPoolPolicy.from_settings(settings), RequestMac(directory)
    original = storage.open_app
    pids = []
    def opened(value):
        conn = original(value)
        pids.append(conn.info.backend_pid)
        barrier.wait(10)
        return conn
    try:
        with patch.object(storage, 'open_app', opened):
            result = getattr(module(), command)(settings, actor, task_id, expected, key, policy=policy, mac=mac)
        output.put(('OK', pids[0], result))
    except ServiceError as exc:
        output.put((exc.code.value, pids[0] if pids else None, None))
    except Exception as exc:
        output.put((type(exc).__name__, pids[0] if pids else None, None))


def spawned_ordered_body(schema, actor_fields, directory, task_id, expected, key, first, release, output):
    """First actor pauses while holding the real task lock, before live auth.

    Second actor uses a different session and must block in the actual service;
    the parent releases the first only after observing pg_blocking_pids.
    """
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    actor = security.Actor(*actor_fields)
    policy, mac = SyntheticPoolPolicy.from_settings(settings), RequestMac(directory)
    original = storage.open_app
    pids = []
    def opened(value):
        conn = original(value); pids.append(conn.info.backend_pid)
        if not first: output.put(('OPEN', pids[0], None))
        return conn
    def hold(cursor, query, params):
        if first and 'FROM onboarding_tasks t' in query:
            output.put(('LOCKED', cursor.connection.info.backend_pid, None))
            if not release.wait(8):
                raise RuntimeError('fixture:parent-did-not-release-task')
    try:
        with patch.object(storage, 'open_app', opened), observe(hook=hold):
            result = module().pause(settings, actor, task_id, expected, key, policy=policy, mac=mac)
        output.put(('OK', pids[0], result))
    except ServiceError as exc:
        output.put((exc.code.value, pids[0] if pids else None, None))
    except Exception as exc:
        output.put((type(exc).__name__, pids[0] if pids else None, None))


class PoolCommandInputTests(unittest.TestCase):
    def test_bad_local_inputs_zero_policy_mac_connection_sql(self):
        settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
        policy = object.__new__(SyntheticPoolPolicy)
        object.__setattr__(policy, 'settings', settings)
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        args = dict(settings=settings, actor=actor, task_id=str(uuid4()), expected_version=1,
                    request_key='fixture:key', policy=policy, mac=object.__new__(RequestMac))
        class Text(str): pass
        class Number(int): pass
        class SubActor(security.Actor): pass
        actors = [object(), repository.FixtureActor(actor.operator_id),
                  SubActor(actor.operator_id, frozenset(), actor.session_id, 1)]
        for field, value in [('operator_id', Text(actor.operator_id)), ('session_id', 'bad'),
                             ('auth_epoch', True), ('auth_epoch', Number(1)),
                             ('permissions', set()), ('permissions', frozenset({Text('tasks:manage')})),
                             ('extra', 1)]:
            forged = copy.copy(actor); object.__setattr__(forged, field, value); actors.append(forged)
        forged = copy.copy(actor); object.__delattr__(forged, 'session_id'); actors.append(forged)
        cases = [('actor', x, ErrorCode.UNAUTHENTICATED) for x in actors]
        cases += [(name, value, ErrorCode.INVALID_INPUT) for name, values in {
            'settings': [object(), replace(settings, schema='different')],
            'policy': [object()], 'mac': [object()],
            'task_id': [None, uuid4(), 'bad', uuid4().hex, Text(args['task_id'])],
            'expected_version': [None, True, 0, -1, MAX+1, Number(1), '1', 1.0],
            'request_key': ['', 'has space', '\n', 'a'*129, Text('key'), None, '中文']
        }.items() for value in values]
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                     for owner, name in ((SyntheticPoolPolicy, '_validate'), (RequestMac, 'request_digest'),
                                         (storage, 'open_app'), (psycopg.Cursor, 'execute'))]
            for command in COMMANDS:
                for field, value, code in cases:
                    with self.subTest(command=command, field=field, value_type=type(value).__name__):
                        with self.assertRaises(ServiceError) as caught:
                            getattr(module(), command)(**{**args, field: value})
                        self.assertEqual(caught.exception.code, code)
                with self.assertRaises(TypeError):
                    getattr(module(), command)(**args, consumer=object())
            for spy in spies: spy.assert_not_called()


class TestCase(PoolCase):
    def test_entry_after_real_pool_setup(self):
        self.assertIsNotNone(
            importlib.util.find_spec('onboarding.pool_commands'),
            'missing approved pool management commands',
        )

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

    def call(self, command, seed, version=1, key='fixture:command', actor=None):
        return getattr(module(), command)(self.settings, actor or self.actor, seed['id'], version, key,
                                          policy=self.vault._policy, mac=self.mac)

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def snapshot(self):
        return {table: self.read('SELECT * FROM ' + table + ' ORDER BY 1') for table in
                ('onboarding_tasks', 'onboarding_batches', 'global_configs', 'mailbox_registry',
                 'mailbox_platform_states', 'secret_objects', 'resource_leases', 'task_steps',
                 'operation_receipts', 'audit_events', 'operator_sessions', 'payment_cards',
                 'card_reservations')}

    def task_row(self, seed):
        return self.read('SELECT version,status,cancel_requested,generation,current_step,reason_code '
                         'FROM onboarding_tasks WHERE id=%s', (seed['id'],))[0]

    def test_all_commands_twelve_states_both_cancel_flags_and_exact_audit(self):
        for command in COMMANDS:
            for status in sorted(repository.STATES):
                for cancelled in (False, True):
                    with self.subTest(command=command, status=status, cancelled=cancelled):
                        seed = self.seed()
                        with self.uow() as conn:
                            conn.execute('UPDATE onboarding_tasks SET status=%s,cancel_requested=%s,'
                                         "current_step='fixture:old-step',reason_code='fixture:reason' WHERE id=%s",
                                         (status, cancelled, seed['id']))
                        if command != 'recheck' and status in TERMINAL:
                            before = self.snapshot()
                            self.reject(lambda: self.call(command, seed), ErrorCode.RECONCILIATION_REQUIRED)
                            self.assertEqual(before, self.snapshot())
                            continue
                        with no_consumption():
                            result = self.call(command, seed)
                        expected_status = status if command == 'recheck' or status == 'CONFLICT' else 'PAUSED'
                        expected_cancel = cancelled or command == 'cancel'
                        self.assertEqual(self.task_row(seed),
                            (2, expected_status, expected_cancel, 1, 'fixture:old-step', 'fixture:reason'))
                        self.assertEqual(set(result), {'receipt_id', 'phase'})
                        self.assertEqual(result['phase'], 'SUCCEEDED')
                        receipt = self.read('SELECT task_id,scope_operator_id,action,resource_revision,generation,'
                            'fence,phase,result_code,external_ref,result_summary FROM operation_receipts WHERE id=%s',
                            (result['receipt_id'],))[0]
                        self.assertEqual(receipt[:9], (UUID(seed['id']), None, 'pool.command.'+command,
                            'pool-command:'+seed['id'], 1, 0, 'SUCCEEDED', 'COMMAND_ACCEPTED', None))
                        summary = receipt[9]
                        self.assertEqual(set(summary), {'task_id', 'command', 'accepted_version', 'status',
                                                       'cancel_requested', 'generation', 'inspect_step_id'})
                        self.assertEqual(summary['accepted_version'], 2)
                        events = self.read('SELECT action,outcome_code,correlation_id,before_summary,after_summary '
                                           'FROM audit_events WHERE task_id=%s', (seed['id'],))
                        self.assertEqual(events, [('task.'+command, 'ACCEPTED', result['receipt_id'],
                            {'status': status, 'version': 1}, {'status': expected_status, 'version': 2})])
                        before = self.snapshot()
                        self.assertEqual(self.call(command, seed), result)
                        self.assertEqual(self.snapshot(), before)

    def test_cancel_clean_task_is_request_not_safe_termination(self):
        seed = self.seed()
        self.call('cancel', seed)
        self.assertEqual(self.task_row(seed)[:3], (2, 'PAUSED', True))
        self.call('pause', seed, 2, 'fixture:pause-after-cancel')
        self.assertEqual(self.task_row(seed)[:3], (3, 'PAUSED', True))
        self.call('cancel', seed, 3, 'fixture:repeat-cancel')
        self.assertEqual(self.task_row(seed)[:3], (4, 'PAUSED', True))

    def test_replay_historical_state_generation_and_conflict_precedes_cas(self):
        seed = self.seed(('claude', 'github'))
        for command in COMMANDS:
            version = self.task_row(seed)[0]
            result = self.call(command, seed, version, 'fixture:'+command)
            with self.uow() as conn:
                conn.execute("UPDATE onboarding_tasks SET status='SUCCEEDED',cancel_requested=false,"
                             'generation=generation+1,version=version+1 WHERE id=%s', (seed['id'],))
            before = self.snapshot()
            self.assertEqual(self.call(command, seed, version, 'fixture:'+command), result)
            self.assertEqual(self.snapshot(), before)
            self.reject(lambda: self.call(command, seed, version+1, 'fixture:'+command),
                        ErrorCode.IDEMPOTENCY_CONFLICT)
            self.reject(lambda: self.call(command, seed, version, 'fixture:new-key'), ErrorCode.VERSION_CONFLICT)
            with self.uow() as conn:
                conn.execute("UPDATE onboarding_tasks SET status='QUEUED' WHERE id=%s", (seed['id'],))
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET version=%s WHERE id=%s', (MAX, seed['id']))
        for command in COMMANDS:
            self.reject(lambda: self.call(command, seed, MAX, 'fixture:max'), ErrorCode.VERSION_CONFLICT)

    def test_scopes_commands_tasks_and_old_fixture_action_do_not_alias(self):
        one, two = self.seed(), self.seed()
        results = [self.call('pause', one), self.call('pause', two), self.call('cancel', one, 2),
                   self.call('recheck', one, 3)]
        self.assertEqual(len({r['receipt_id'] for r in results}), 4)
        seed = self.seed()
        with self.uow() as conn:
            for task_id, owner, action in ((None, self.actor.operator_id, 'pool.command.pause'),
                                          (seed['id'], None, 'command:pause')):
                conn.execute('INSERT INTO operation_receipts(id,task_id,scope_operator_id,action,resource_revision,'
                             'generation,fence,phase,idempotency_key,request_hash) '
                             "VALUES(%s,%s,%s,%s,'fixture:other',1,0,'SUCCEEDED','fixture:command',%s)",
                             (str(uuid4()), task_id, owner, action, 'a'*64))
        self.call('pause', seed)
        self.assertEqual(self.task_row(seed)[0], 2)

    def test_history_drift_combined_null_secret_and_other_owner(self):
        for plan, null_secret in ((('google',), True), (('claude', 'github'), False)):
            seed = self.seed(plan, missing_platform_secret=null_secret)
            with self.uow() as conn:
                replacement = self.vault.put_locked(conn, self.actor, PoolResource('mailbox', seed['mailbox']),
                    MailboxCredential(seed['email'], password='fixture:replacement'))
                conn.execute("UPDATE mailbox_registry SET credential_ref=%s,credential_version=9,disabled=true,"
                             "health='UNKNOWN',pool_status='EXPORTED' WHERE id=%s", (replacement.id, seed['mailbox']))
                conn.execute("UPDATE mailbox_platform_states SET credential_ref=NULL,credential_version=9,"
                             "usage_status='UNKNOWN',identity_status='EXISTING' WHERE mailbox_id=%s", (seed['mailbox'],))
                conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()-interval '1 day',"
                             "revoked_at=clock_timestamp() WHERE id=%s OR access_policy LIKE %s",
                             (seed['secret'], 'pool:'+self.actor.operator_id+':platform:%'))
                conn.execute('INSERT INTO resource_leases(resource_kind,resource_id,task_id,owner_id,fence,lease_until,hold_reason) '
                             "VALUES('mailbox',%s,%s,'fixture:stale',9,clock_timestamp()-interval '1 day','UNKNOWN')",
                             (seed['mailbox'], seed['id']))
            before = self.snapshot()
            for version, command in enumerate(COMMANDS, 1):
                with no_consumption(): self.call(command, seed, version, 'fixture:'+command)
            after = self.snapshot()
            for table in set(before)-{'onboarding_tasks', 'task_steps', 'operation_receipts', 'audit_events'}:
                self.assertEqual(before[table], after[table], table)
            other = str(uuid4())
            with self.uow() as conn:
                conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture','{}')",
                             (other, 'fixture-'+other))
                conn.execute('UPDATE onboarding_batches SET created_by=%s WHERE id=%s', (other, seed['batch']))
            self.reject(lambda: self.call('pause', seed), ErrorCode.FORBIDDEN)

    def test_unresolved_receipts_steps_holds_and_neighbor_are_untouched(self):
        seed, neighbor = self.seed(), self.seed(('claude',))
        with self.uow() as conn:
            # Current generation 2 must not erase generation 1 unresolved facts.
            conn.execute('UPDATE onboarding_tasks SET generation=2 WHERE id=%s', (seed['id'],))
            for index, phase in enumerate(('INTENT', 'UNKNOWN', 'CONFLICT'), 1):
                conn.execute('INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,'
                             'request_hash,phase,fence,generation) VALUES(%s,%s,%s,%s,%s,%s,%s,9,1)',
                             (str(uuid4()), seed['id'], 'fixture:effect:'+str(index), 'fixture:revision',
                              'fixture:effect:'+str(index), 'a'*64, phase))
            for state in ('RUNNING', 'INTENT', 'UNKNOWN', 'CONFLICT'):
                conn.execute('INSERT INTO task_steps(id,task_id,step_key,generation,state,fence) '
                             'VALUES(%s,%s,%s,1,%s,9)', (str(uuid4()), seed['id'], 'fixture:'+state, state))
            for hold in ('HELD', 'INTENT', 'UNKNOWN', 'CONFLICT'):
                conn.execute('INSERT INTO resource_leases(resource_kind,resource_id,task_id,owner_id,fence,lease_until,hold_reason) '
                             "VALUES('mailbox',%s,%s,'fixture:worker',9,clock_timestamp()-interval '1 day',%s)",
                             ('fixture:'+hold, seed['id'], hold))
        before = self.snapshot()
        unresolved = self.read('SELECT * FROM operation_receipts WHERE task_id=%s ORDER BY id', (seed['id'],))
        self.assertEqual(self.task_row(seed)[3], 2)
        self.assertEqual(self.read('SELECT DISTINCT generation FROM operation_receipts WHERE task_id=%s',
                                   (seed['id'],)), [(1,)])
        with observe() as queries, no_consumption():
            for version, command in enumerate(COMMANDS, 1):
                self.call(command, seed, version, 'fixture:'+command)
                self.assertEqual(unresolved, self.read(
                    "SELECT * FROM operation_receipts WHERE task_id=%s AND phase IN ('INTENT','UNKNOWN','CONFLICT') ORDER BY id",
                    (seed['id'],)))
        after = self.snapshot()
        for table in set(before)-{'onboarding_tasks', 'task_steps', 'operation_receipts', 'audit_events'}:
            self.assertEqual(before[table], after[table], table)
        self.assertEqual([r for r in before['onboarding_tasks'] if str(r[0]) == neighbor['id']],
                         [r for r in after['onboarding_tasks'] if str(r[0]) == neighbor['id']])
        for table in ('task_steps', 'operation_receipts'):
            for row in before[table]: self.assertIn(row, after[table])
        for _, query, _ in queries:
            self.assertFalse(any(name in query for name in ('resource_leases', 'payment_cards', 'card_reservations')))
            if 'FROM secret_objects' in query:
                self.assertNotIn('FOR UPDATE', query)
                self.assertFalse(any(name in query for name in ('ciphertext', 'nonce', 'SELECT *')))

    def test_marker_eight_states_preserved_and_next_generation_separate(self):
        seed = self.seed()
        first = self.call('recheck', seed)
        row = self.read('SELECT * FROM task_steps WHERE task_id=%s', (seed['id'],))[0]
        self.assertEqual(row[4:9], ('NOT_SENT', 0, None, None, None))
        version = 2
        for state in STEP_STATES:
            with self.uow() as conn:
                conn.execute('UPDATE task_steps SET state=%s,fence=19,observation_code=%s,'
                             'started_at=clock_timestamp(),finished_at=clock_timestamp(),version=7 WHERE task_id=%s',
                             (state, 'fixture:observation', seed['id']))
            before = self.read('SELECT * FROM task_steps WHERE task_id=%s', (seed['id'],))
            result = self.call('recheck', seed, version, 'fixture:'+state)
            self.assertNotEqual(first, result)
            self.assertEqual(before, self.read('SELECT * FROM task_steps WHERE task_id=%s', (seed['id'],)))
            version += 1
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET generation=2 WHERE id=%s', (seed['id'],))
        self.call('recheck', seed, version, 'fixture:next-gen')
        rows = self.read('SELECT generation,state FROM task_steps WHERE task_id=%s ORDER BY generation', (seed['id'],))
        self.assertEqual(rows, [(1, 'CANCELLED_SAFE'), (2, 'NOT_SENT')])

    def test_receipt_corrupt_storable_fields_and_body_conflict_priority(self):
        seed = self.seed()
        result = self.call('cancel', seed)
        receipt_id = result['receipt_id']
        original = self.read('SELECT result_summary FROM operation_receipts WHERE id=%s', (receipt_id,))[0][0]
        patches = [('fence', 1), ('resource_revision', 'fixture:wrong'), ('phase', 'UNKNOWN'),
                   ('result_code', 'WRONG'), ('external_ref', 'fixture:wrong'), ('generation', 2)]
        for key, value in [('accepted_version', 3), ('accepted_version', True), ('generation', 2),
                           ('status', 'SUCCEEDED'), ('cancel_requested', False), ('inspect_step_id', str(uuid4())),
                           ('command', 'pause'), ('task_id', str(uuid4())), ('extra', 1)]:
            patches.append(('result_summary', Jsonb({**original, key: value})))
        for column, value in patches:
            with self.subTest(column=column):
                with self.fixture.migrator() as conn:
                    previous = conn.execute('SELECT '+column+' FROM operation_receipts WHERE id=%s', (receipt_id,)).fetchone()[0]
                    conn.execute('UPDATE operation_receipts SET '+column+'=%s WHERE id=%s', (value, receipt_id))
                before = self.snapshot()
                self.reject(lambda: self.call('cancel', seed), ErrorCode.DEPENDENCY_UNAVAILABLE)
                self.assertEqual(before, self.snapshot())
                if column in ('phase', 'result_code', 'external_ref', 'result_summary'):
                    self.reject(lambda: self.call('cancel', seed, 2), ErrorCode.IDEMPOTENCY_CONFLICT)
                with self.fixture.migrator() as conn:
                    conn.execute('UPDATE operation_receipts SET '+column+'=%s WHERE id=%s',
                                 (Jsonb(previous) if column == 'result_summary' else previous, receipt_id))
    def test_impossible_decoded_receipt_marker_and_returning_types_fail_closed(self):
        seed = self.seed()
        self.call('recheck', seed)
        class Text(str): pass
        receipt = self.read('SELECT '+module()._RECEIPT_COLUMNS+' FROM operation_receipts WHERE task_id=%s',
                            (seed['id'],))[0]
        summary = receipt[12]
        changes = [(' FROM operation_receipts ', index, value, 'recheck', 1, 'fixture:command')
                   for index, value in [(0, str(receipt[0])), (1, str(receipt[1])), (2, uuid4()),
                       (3, Text('pool.command.recheck')), (4, Text(receipt[4])), (5, True), (6, False),
                       (7, Text('SUCCEEDED')), (8, Text('fixture:command')), (9, Text(receipt[9])),
                       (10, Text('COMMAND_ACCEPTED')), (12, {Text(k): v for k, v in summary.items()}),
                       (12, {**summary, 'inspect_step_id': 'bad'}),
                       (12, {**summary, 'cancel_requested': 0})]]
        changes += [(' FROM task_steps ', index, value, 'recheck', 2, 'fixture:marker')
                    for index, value in [(0, str(uuid4())), (1, uuid4()), (2, Text('pool.inspect')),
                        (3, True), (4, Text('NOT_SENT')), (5, True), (5, MAX+1), (6, []),
                        (7, datetime.now()), (8, 'infinity'), (9, True), (10, datetime.now()), (11, None)]]
        changes += [('UPDATE onboarding_tasks SET ', index, value, 'pause', 2, 'fixture:returning')
                    for index, value in [(0, str(uuid4())), (1, True), (1, 2), (2, Text('PAUSED')),
                                         (3, 0), (4, True), (4, 2)]]
        before = self.snapshot()
        for marker, index, value, command, version, key in changes:
            touched = []
            def seam(query, row):
                if marker in query and row is not None:
                    touched.append(1)
                    row = list(row); row[index] = value; return tuple(row)
                return row
            with self.subTest(marker=marker, index=index), observe(seam=seam):
                self.reject(lambda: self.call(command, seed, version, key), ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertTrue(touched)
            self.assertEqual(before, self.snapshot())

    def test_task_returning_missing_and_receipt_insert_conflict_roll_back(self):
        seed = self.seed()
        before = self.snapshot()
        for marker, code in [('UPDATE onboarding_tasks SET ', ErrorCode.VERSION_CONFLICT),
                             ('INSERT INTO operation_receipts ', ErrorCode.DEPENDENCY_UNAVAILABLE)]:
            reached = []
            def seam(query, row):
                if marker in query:
                    reached.append(1)
                    return None  # Explicit impossible decoded response after actual SQL.
                return row
            with observe(seam=seam):
                self.reject(lambda: self.call('recheck', seed), code)
            self.assertEqual(reached, [1])
            self.assertEqual(before, self.snapshot())

    def test_mac_exact_body_frames_and_file_failures(self):
        seed = self.seed()
        original = RequestMac.request_digest
        calls = []
        def capture(mac, action, owner, body):
            calls.append((action, owner, body))
            return original(mac, action, owner, body)
        with patch.object(RequestMac, 'request_digest', capture):
            result = self.call('pause', seed)
        body = json.dumps(dict(v=1, schema=self.settings.schema, instance_marker=self.settings.instance_marker,
            task_id=seed['id'], command='pause', expected_version=1, request_key='fixture:command'),
            sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')
        self.assertEqual([c for c in calls if c[0] == 'pool.command.pause.v1'],
                         [('pool.command.pause.v1', self.actor.operator_id, body)])
        probes = [c for c in calls if c[0] == 'pool.command.keycheck.v1']
        self.assertEqual(probes, [('pool.command.keycheck.v1', self.actor.operator_id, b'fixture:stable-key')]*3)
        stored = self.read('SELECT request_hash FROM operation_receipts WHERE id=%s', (result['receipt_id'],))[0][0]
        self.assertEqual(stored, original(self.mac, 'pool.command.pause.v1', self.actor.operator_id, body))
        path = self.key_directory/'request-mac.key'
        saved = path.read_bytes()
        before = self.snapshot()
        try:
            for failure in ('mode', 'length', 'missing'):
                path.write_bytes(saved); path.chmod(0o600)
                if failure == 'mode': path.chmod(0o644)
                elif failure == 'length': path.write_bytes(b'fixture:short')
                else: path.unlink()
                with patch.object(storage, 'open_app', side_effect=AssertionError('opened before MAC')) as opened:
                    self.reject(lambda: self.call('pause', seed), ErrorCode.SECRET_UNAVAILABLE)
                opened.assert_not_called()
        finally:
            path.write_bytes(saved); path.chmod(0o600)
        self.assertEqual(before, self.snapshot())

    def test_final_policy_mac_auth_faults_after_actual_audit_roll_back(self):
        seed = self.seed()
        before = self.snapshot()
        append = audit.append
        path = self.key_directory/'request-mac.key'
        saved = path.read_bytes()
        cases = [('path', ErrorCode.FORBIDDEN), ('mac', ErrorCode.SECRET_UNAVAILABLE),
                 ('revoke', ErrorCode.UNAUTHENTICATED), ('epoch', ErrorCode.UNAUTHENTICATED),
                 ('expiry', ErrorCode.UNAUTHENTICATED), ('permission', ErrorCode.FORBIDDEN)]
        for mode, code in cases:
            reached = []
            def fault(conn, *args, **kwargs):
                value = append(conn, *args, **kwargs); reached.append(1)
                if mode == 'path': conn.execute('SET LOCAL search_path TO pg_catalog')
                elif mode == 'mac': path.write_bytes(secrets.token_bytes(32))
                elif mode == 'revoke':
                    conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.actor.session_id,))
                elif mode == 'expiry':
                    conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (self.actor.session_id,))
                elif mode == 'epoch': conn.execute('UPDATE operators SET auth_epoch=auth_epoch+1 WHERE id=%s', (self.actor.operator_id,))
                else: conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read','config:manage'] WHERE id=%s", (self.actor.operator_id,))
                return value
            try:
                with self.subTest(mode=mode), patch.object(audit, 'append', fault):
                    self.reject(lambda: self.call('recheck', seed), code)
            finally:
                path.write_bytes(saved)
            self.assertEqual(reached, [1])
            self.assertEqual(before, self.snapshot())

    def test_live_permission_not_actor_cache_and_replay_requires_auth(self):
        seed = self.seed()
        empty = security.Actor(self.actor.operator_id, frozenset(), self.actor.session_id, self.actor.auth_epoch)
        result = self.call('pause', seed, actor=empty)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read','config:manage'] WHERE id=%s", (self.actor.operator_id,))
        self.reject(lambda: self.call('pause', seed), ErrorCode.FORBIDDEN)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=ARRAY['tasks:manage'] WHERE id=%s", (self.actor.operator_id,))
        self.assertEqual(self.call('pause', seed, actor=empty), result)
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.actor.session_id,))
        self.reject(lambda: self.call('pause', seed), ErrorCode.UNAUTHENTICATED)

    def test_audit_failure_and_explicit_precommit_rollback_are_atomic(self):
        seed = self.seed()
        before = self.snapshot()
        with patch.object(audit, 'append', side_effect=psycopg.OperationalError('fixture:audit-fault')) as spy:
            self.reject(lambda: self.call('recheck', seed), ErrorCode.DEPENDENCY_UNAVAILABLE)
        spy.assert_called_once()
        self.assertEqual(before, self.snapshot())
        original = psycopg.Connection.transaction
        reached = []
        @contextmanager
        def rollback(conn, *args, **kwargs):
            with original(conn, *args, **kwargs) as tx:
                yield tx
                reached.append(1)
                raise RuntimeError('fixture:before-commit-rollback')
        with patch.object(psycopg.Connection, 'transaction', rollback):
            self.reject(lambda: self.call('recheck', seed), ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached, [1])
        self.assertEqual(before, self.snapshot())

    def test_commit_lost_ack_can_be_durable_then_explicit_key_replay(self):
        seed = self.seed()
        original = psycopg.Connection.transaction
        committed = []
        @contextmanager
        def lost_ack(conn, *args, **kwargs):
            with original(conn, *args, **kwargs) as tx: yield tx
            committed.append(conn.info.backend_pid)
            raise psycopg.OperationalError('fixture:after-real-commit')
        with patch.object(psycopg.Connection, 'transaction', lost_ack):
            self.reject(lambda: self.call('recheck', seed), ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(len(committed), 1)
        self.assertEqual(self.task_row(seed)[0], 2)
        before = self.snapshot()
        receipt_id = str(self.read('SELECT id FROM operation_receipts WHERE task_id=%s', (seed['id'],))[0][0])
        self.assertEqual(self.call('recheck', seed), {'receipt_id': receipt_id, 'phase': 'SUCCEEDED'})
        self.assertEqual(before, self.snapshot())

    def test_success_returns_only_after_commit_and_final_sql_order(self):
        seed = self.seed()
        original_tx, original_auth, original_policy = (psycopg.Connection.transaction,
                                                      security.revalidate, SyntheticPoolPolicy._connection)
        phases = []
        @contextmanager
        def committed(conn, *args, **kwargs):
            with original_tx(conn, *args, **kwargs) as tx: yield tx
            phases.append('commit')
        def auth(*args, **kwargs):
            result = original_auth(*args, **kwargs); phases.append('auth'); return result
        def policy(*args, **kwargs):
            result = original_policy(*args, **kwargs); phases.append('policy'); return result
        with observe() as queries, patch.object(psycopg.Connection, 'transaction', committed), \
             patch.object(security, 'revalidate', auth), patch.object(SyntheticPoolPolicy, '_connection', policy):
            result = self.call('recheck', seed)
            phases.append('return')
        self.assertEqual(phases[-4:], ['policy', 'auth', 'commit', 'return'])
        self.assertEqual(result['phase'], 'SUCCEEDED')
        texts = [q for _, q, _ in queries]
        lock_queries = [q for q in texts if 'FOR UPDATE' in q or 'FOR SHARE' in q]
        self.assertIn('FROM onboarding_tasks t', lock_queries[0])
        receipt = next(i for i, q in enumerate(texts) if 'FROM operation_receipts ' in q)
        marker = next(i for i, q in enumerate(texts) if 'FROM task_steps ' in q)
        audit_index = next(i for i, q in enumerate(texts) if 'INSERT INTO audit_events' in q)
        self.assertLess(receipt, marker); self.assertLess(marker, audit_index)
        tail = texts[audit_index+1:]
        self.assertEqual(tail[-1], 'SELECT clock_timestamp()')
        self.assertFalse(any('FROM onboarding_tasks' in q or 'FROM mailbox_' in q for q in tail))
    def second_actor(self):
        session = str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                         (session, self.actor.operator_id, hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                          hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        return security.Actor(self.actor.operator_id, self.actor.permissions, session, 1)

    def test_two_spawn_backends_same_key_conflict_and_cas(self):
        ctx = multiprocessing.get_context('spawn')
        actors = (self.actor, self.second_actor())
        for mode in ('replay', 'body-conflict', 'cas'):
            seed = self.seed()
            queue, barrier = ctx.Queue(), ctx.Barrier(2)
            specs = [('pause', 1, 'fixture:race'),
                     ('cancel' if mode == 'cas' else 'pause', 2 if mode == 'body-conflict' else 1,
                      'fixture:other' if mode == 'cas' else 'fixture:race')]
            # A different body at stale version 2 must lose only after first acceptance.
            # Race both bodies at valid CAS by binding different keys for CAS case;
            # body-conflict case preaccepts body 1 so neither contender may mutate.
            accepted = self.call('pause', seed) if mode == 'body-conflict' else None
            if mode == 'body-conflict':
                specs = [('pause', 1, 'fixture:command'), ('pause', 2, 'fixture:command')]
            processes = []
            for actor, (command, expected, key) in zip(actors, specs):
                fields = (actor.operator_id, actor.permissions, actor.session_id, actor.auth_epoch)
                process = ctx.Process(target=spawned_command, args=(self.settings.schema, fields,
                    str(self.key_directory), seed['id'], command, expected, key, barrier, queue))
                processes.append(process); process.start()
            try:
                outcomes = [queue.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
            finally:
                for process in processes:
                    if process.is_alive(): process.terminate(); process.join(5)
                queue.close(); queue.join_thread()
            self.assertEqual(len({row[1] for row in outcomes}), 2)
            expected_codes = ['OK', 'OK'] if mode == 'replay' else ['OK',
                ErrorCode.IDEMPOTENCY_CONFLICT.value if mode == 'body-conflict' else ErrorCode.VERSION_CONFLICT.value]
            self.assertCountEqual([row[0] for row in outcomes], expected_codes)
            successes = [row[2] for row in outcomes if row[0] == 'OK']
            if mode == 'replay': self.assertEqual(successes[0], successes[1])
            if accepted: self.assertEqual(successes, [accepted])
            self.assertEqual(self.task_row(seed)[0], 2)
            self.assertEqual(self.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s', (seed['id'],)), [(1,)])
            self.assertEqual(self.read('SELECT count(*) FROM audit_events WHERE task_id=%s', (seed['id'],)), [(1,)])

    def test_actual_lock_waits_expire_auth_before_replay_marker_and_commit(self):
        seed = self.seed(('claude', 'github'))
        self.call('recheck', seed)
        receipt = self.read('SELECT id FROM operation_receipts WHERE task_id=%s', (seed['id'],))[0][0]
        marker = self.read('SELECT id FROM task_steps WHERE task_id=%s', (seed['id'],))[0][0]
        targets = [('onboarding_tasks', seed['id']), ('mailbox_registry', seed['mailbox']),
                   ('mailbox_platform_states', min(p['state_id'] for p in seed['pins'].values())),
                   ('operation_receipts', receipt), ('task_steps', marker), ('audit_events', None)]
        self.command_wait_evidence = []
        for table, identity in targets:
            # All slow setup/snapshots/connections precede the unchanged 650 ms TTL.
            before = self.snapshot()
            ready, gate, abort, done = (threading.Event() for _ in range(4))
            connections, results = [], []
            original = storage.open_app
            version = 1 if table == 'operation_receipts' else 2
            key = 'fixture:command' if table == 'operation_receipts' else 'fixture:wait'
            def connection(settings):
                if threading.current_thread() is not thread:
                    return original(settings)
                conn = original(settings)
                connections.append(conn); ready.set()
                if not gate.wait(5) or abort.is_set():
                    conn.close()
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                return conn  # only now may the service enter its real UoW
            def work():
                try:
                    with patch.object(storage, 'open_app', connection):
                        self.call('recheck', seed, version, key)
                    results.append('unexpected_success')
                except ServiceError as exc: results.append(exc.code)
                except Exception as exc: results.append(type(exc).__name__)
                finally: done.set()
            thread = threading.Thread(target=work)
            pid = deadline = first_blocked = now = None
            isolated = False
            with self.subTest(table=table), self.fixture.migrator() as locker, self.fixture.app() as observer:
                old_deadline = observer.execute('SELECT idle_expires_at FROM operator_sessions WHERE id=%s',
                                               (self.actor.session_id,)).fetchone()[0]
                with locker.transaction():
                    locker.execute('SAVEPOINT command_wait')
                    try:
                        if table == 'audit_events': locker.execute('LOCK TABLE audit_events IN SHARE MODE')
                        else:
                            locked = locker.execute('SELECT id FROM '+table+' WHERE id=%s FOR UPDATE', (identity,)).fetchone()
                            self.assertEqual(locked, (UUID(str(identity)),))
                        thread.start()
                        self.assertTrue(ready.wait(3), 'worker APP connection not prepared')
                        pid = connections[0].info.backend_pid
                        self.assertNotEqual(pid, locker.info.backend_pid)
                        # Existing autocommit APP observer arms TTL before the gate opens;
                        # no new connection or snapshot is created within this interval.
                        deadline = observer.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '650 milliseconds' "
                            'WHERE id=%s RETURNING idle_expires_at', (self.actor.session_id,)).fetchone()[0]
                        gate.set()
                        first_blocked, now, blockers = None, None, []
                        stop = time.monotonic()+1.3
                        while time.monotonic() < stop:
                            blockers, now = observer.execute('SELECT pg_blocking_pids(%s),clock_timestamp()', (pid,)).fetchone()
                            if locker.info.backend_pid in blockers and first_blocked is None:
                                first_blocked = now
                            if first_blocked is not None and now > deadline: break
                            if done.is_set(): break
                            time.sleep(.01)  # never spin when done is already set
                        diagnostic = dict(table=table, pid=pid, blocker=locker.info.backend_pid,
                            blockers=blockers, first_blocked=None if first_blocked is None else first_blocked.isoformat(),
                            deadline=deadline.isoformat(), now=None if now is None else now.isoformat(),
                            done=done.is_set(), codes=[x.value if isinstance(x, ErrorCode) else x for x in results])
                        self.assertIsNotNone(first_blocked, diagnostic)
                        self.assertLess(first_blocked, deadline, diagnostic)
                        self.assertGreater(now, deadline, diagnostic)
                        self.assertFalse(done.is_set(), diagnostic)
                    finally:
                        # Cleanup runs even when a subtest assertion fails: no live
                        # patched worker or changed session deadline may leak onward.
                        abort.set(); gate.set()
                        locker.execute('ROLLBACK TO SAVEPOINT command_wait')
                        if thread.ident is not None:
                            thread.join(3)
                            if thread.is_alive() and connections:
                                connections[0].cancel()
                                thread.join(6)
                        try:
                            if not thread.is_alive():
                                observer.execute('UPDATE operator_sessions SET idle_expires_at=%s WHERE id=%s',
                                                 (old_deadline, self.actor.session_id))
                        finally:
                            self.command_wait_evidence.append(dict(table=table, worker_pid=pid,
                                locker_pid=locker.info.backend_pid,
                                deadline=None if deadline is None else deadline.isoformat(),
                                first_wait_at=None if first_blocked is None else first_blocked.isoformat(),
                                last_db_now=None if now is None else now.isoformat(),
                                results=[x.value if isinstance(x, ErrorCode) else x for x in results],
                                thread_alive=thread.is_alive(), waiting=first_blocked is not None))
                        self.assertFalse(thread.is_alive(), 'worker remains alive after release/cancel')
                        isolated = before == self.snapshot()
                        self.assertTrue(isolated, 'failed wait must not mutate shared fixtures')
                self.assertEqual(results, [ErrorCode.UNAUTHENTICATED])
            # A subTest must not swallow fatal cleanup and start another worker.
            self.assertFalse(thread.is_alive(), 'stop: live worker after subtest')
            self.assertTrue(isolated, 'stop: fixture cleanup failed; do not run subsequent targets')

    def test_real_receipt_unique_conflict_after_task_update_rolls_back(self):
        seed = self.seed()
        before = self.snapshot()
        reached = []
        def collision(cursor, query, params):
            if query.startswith('UPDATE onboarding_tasks SET '):
                reached.append(1)
                cursor.connection.execute('INSERT INTO operation_receipts(id,task_id,action,resource_revision,'
                    'generation,fence,phase,idempotency_key,request_hash,result_code,result_summary) '
                    "VALUES(%s,%s,'pool.command.recheck',%s,1,0,'SUCCEEDED','fixture:command',%s,'COMMAND_ACCEPTED','{}')",
                    (str(uuid4()), seed['id'], 'pool-command:'+seed['id'], 'a'*64))
        with observe(hook=collision) as queries:
            self.reject(lambda: self.call('recheck', seed), ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached, [1])
        self.assertTrue(any('ON CONFLICT DO NOTHING RETURNING id' in q and 'operation_receipts' in q for _, q, _ in queries))
        self.assertEqual(before, self.snapshot())

    def test_marker_conflict_path_rereads_and_preserves_actual_existing_row(self):
        seed = self.seed()
        self.call('recheck', seed)
        with self.uow() as conn:
            conn.execute("UPDATE task_steps SET state='UNKNOWN',fence=17,observation_code='fixture:observation' WHERE task_id=%s",
                         (seed['id'],))
        before = self.read('SELECT * FROM task_steps WHERE task_id=%s', (seed['id'],))
        reads = []
        def first_missing(query, row):
            if ' FROM task_steps ' in query:
                reads.append(1)
                # Explicit decoded first-read miss injects the conflict branch;
                # INSERT is real and must lose against the actual unique row.
                if len(reads) == 1: return None
            return row
        with observe(seam=first_missing) as queries:
            self.call('recheck', seed, 2, 'fixture:collision')
        self.assertEqual(reads, [1, 1])
        self.assertTrue(any('INSERT INTO task_steps' in q for _, q, _ in queries))
        self.assertEqual(before, self.read('SELECT * FROM task_steps WHERE task_id=%s', (seed['id'],)))

    def test_history_graph_wrong_owner_kind_policy_scope_denied_before_old_key(self):
        seed = self.seed()
        self.call('pause', seed)
        other = str(uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture','{}')",
                         (other, 'fixture-'+other))
        cases = [('mailbox_registry', 'owner_operator_id', seed['mailbox'], other),
                 ('secret_objects', 'kind', seed['secret'], 'platform_credential'),
                 ('secret_objects', 'access_policy', seed['secret'], 'fixture:wrong-policy'),
                 ('global_configs', 'scope', seed['config'], 'fixture')]
        for table, column, identity, value in cases:
            with self.fixture.migrator() as conn:
                previous = conn.execute('SELECT '+column+' FROM '+table+' WHERE id=%s', (identity,)).fetchone()[0]
                conn.execute('UPDATE '+table+' SET '+column+'=%s WHERE id=%s', (value, identity))
            before = self.snapshot()
            try:
                with observe() as queries:
                    self.reject(lambda: self.call('pause', seed), ErrorCode.FORBIDDEN)
                self.assertFalse(any('FROM operation_receipts ' in q for _, q, _ in queries))
                self.assertEqual(before, self.snapshot())
            finally:
                with self.fixture.migrator() as conn:
                    conn.execute('UPDATE '+table+' SET '+column+'=%s WHERE id=%s', (previous, identity))

    def test_new_key_different_body_two_processes_wait_then_conflict(self):
        seed = self.seed()
        actors = (self.actor, self.second_actor())
        key = 'fixture:new-body-race'
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s', (seed['id'],)), [(0,)])
        ctx = multiprocessing.get_context('spawn')
        release, output = ctx.Event(), ctx.Queue()
        processes = []
        for first, actor, expected in ((True, actors[0], 1), (False, actors[1], 2)):
            fields = (actor.operator_id, actor.permissions, actor.session_id, actor.auth_epoch)
            processes.append(ctx.Process(target=spawned_ordered_body, args=(self.settings.schema, fields,
                str(self.key_directory), seed['id'], expected, key, first, release, output)))
        try:
            with self.fixture.app() as observer:
                processes[0].start()
                locked = output.get(timeout=10)
                self.assertEqual(locked[0], 'LOCKED')
                processes[1].start()
                opened = output.get(timeout=10)
                self.assertEqual(opened[0], 'OPEN')
                self.assertNotEqual(locked[1], opened[1])
                waiting = False
                stop = time.monotonic()+1.3
                while time.monotonic() < stop:
                    blockers = observer.execute('SELECT pg_blocking_pids(%s)', (opened[1],)).fetchone()[0]
                    if locked[1] in blockers:
                        waiting = True; break
                    time.sleep(.01)
                self.assertTrue(waiting, 'different body must actually wait behind first valid-CAS body')
                release.set()
                outcomes = [output.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual({row[1] for row in outcomes}, {locked[1], opened[1]})
            self.assertCountEqual([row[0] for row in outcomes], ['OK', ErrorCode.IDEMPOTENCY_CONFLICT.value])
            self.assertEqual(next(row[1] for row in outcomes if row[0] == 'OK'), locked[1])
            self.assertEqual(self.task_row(seed)[:3], (2, 'PAUSED', False))
            self.assertEqual(self.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s', (seed['id'],)), [(1,)])
            self.assertEqual(self.read('SELECT count(*) FROM audit_events WHERE task_id=%s', (seed['id'],)), [(1,)])
            accepted = next(row[2] for row in outcomes if row[0] == 'OK')
            before = self.snapshot()
            self.assertEqual(self.call('pause', seed, 1, key), accepted)
            self.assertEqual(self.snapshot(), before)
        finally:
            release.set()
            for process in processes:
                if process.pid is not None:
                    process.join(3)
                    if process.is_alive(): process.terminate(); process.join(5)
            output.close(); output.join_thread()

    def test_not_committed_proof_when_version_moved_past_without_receipt(self):
        seed = self.seed()
        self.call('pause', seed, 1, 'fixture:first')                     # version 2
        before = self.business_snapshot()
        self.assertTrue(self.proof(lambda: self.call('cancel', seed, 1, 'fixture:stale')))
        self.assertTrue(self.proof(lambda: self.call('cancel', seed, 1, 'fixture:stale')), 'replay proves too')
        self.assertEqual(before, self.business_snapshot())

    def test_no_proof_when_expected_version_is_ahead_or_out_of_range(self):
        seed = self.seed()
        self.assertFalse(self.proof(lambda: self.call('pause', seed, 5, 'fixture:ahead')))
        for expected in (0, -1, 2**63):
            with self.subTest(expected=expected):
                with self.assertRaises(ServiceError) as caught:
                    self.call('pause', seed, expected, 'fixture:range')
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
                self.assertFalse(caught.exception.not_committed)

    def test_committed_original_replays_its_result(self):
        seed = self.seed()
        accepted = self.call('pause', seed, 1, 'fixture:done')
        self.assertEqual(self.call('pause', seed, 1, 'fixture:done'), accepted)

    def test_no_proof_under_migration_drift(self):
        seed = self.seed()
        self.call('pause', seed, 1, 'fixture:first')
        checksum = self.drift_migrations()
        try:
            self.assertFalse(self.proof(lambda: self.call('cancel', seed, 1, 'fixture:drift')))
        finally:
            self.set_migration_checksum(checksum)

    def test_no_proof_when_migration_commits_before_tail_checks(self):
        seed = self.seed()
        self.call('pause', seed, 1, 'fixture:first')
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.set_migration_checksum('0' * 64)
        try:
            with after_query(hook):
                flagged = self.proof(lambda: self.call('cancel', seed, 1, 'fixture:race'))
        finally:
            self.set_migration_checksum(checksum)
        self.assertTrue(fired)
        self.assertFalse(flagged, 'C4 must see the concurrent migration')

    def test_mac_rotation_after_commit_is_idempotency_conflict_without_proof(self):
        seed = self.seed()
        self.call('pause', seed, 1, 'fixture:mac')
        self.mac = self.rotate_request_mac()
        with self.assertRaises(ServiceError) as caught:
            self.call('pause', seed, 1, 'fixture:mac')
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertFalse(caught.exception.not_committed)

    def test_queued_original_never_commits_after_proof(self):
        seed = self.seed()
        self.call('pause', seed, 1, 'fixture:other')
        outcome = self.queued_original_never_commits(lambda: self.call('cancel', seed, 1, 'fixture:r0'),
                                                     lambda: self.call('cancel', seed, 1, 'fixture:r0'))
        self.assertEqual(outcome[0], ErrorCode.VERSION_CONFLICT.value)
        self.assertEqual(self.task_row(seed)[0], 2)

    def test_replay_behind_committing_original_returns_it_without_proof(self):
        seed, other = self.seed(), self.second_session()
        original, replay = self.race_original('FROM onboarding_tasks t',
            lambda: self.call('cancel', seed, 1, 'fixture:lock'),
            lambda: self.call('cancel', seed, 1, 'fixture:lock', actor=other))
        self.assertEqual(original[0], 'OK')
        self.assertEqual(replay, original)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s', (seed['id'],)), [(1,)])

    def test_replay_behind_rolled_back_original_applies_normally(self):
        seed, other = self.seed(), self.second_session()
        original, replay = self.race_original('FROM onboarding_tasks t',
            lambda: self.call('cancel', seed, 1, 'fixture:rollback'),
            lambda: self.call('cancel', seed, 1, 'fixture:rollback', actor=other), rollback=True)
        self.assertNotEqual(original[0], 'OK')
        self.assertEqual(replay[0], 'OK')
        self.assertEqual(self.task_row(seed)[:3], (2, 'PAUSED', True))

    def test_no_proof_on_non_monotonic_rejection(self):
        seed = self.seed()
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='SUCCEEDED' WHERE id=%s", (seed['id'],))
        with self.assertRaises(ServiceError) as caught:
            self.call('pause', seed, 1, 'fixture:terminal')
        self.assertEqual(caught.exception.code, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertFalse(caught.exception.not_committed)
