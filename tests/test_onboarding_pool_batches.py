"""Atomic synthetic batch contracts. Real PG tests are never silently skipped.

All health/UNUSED evidence below is test-only trusted fixture data. SQL spies
observe actual statements; named decoded-row seams are not concurrency proof.
"""
import copy
import hashlib
import importlib
import importlib.util
import json
import multiprocessing
import queue
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
import onboarding_pool_support
from onboarding_pool_support import after_query
from onboarding import audit, mailboxes, pool_config, pool_leases, repository, security, storage
from onboarding.coordinator import FixtureExecutor
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_secret_types import MailboxCredential, PlatformCredential, PoolResource
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.secret_store import FixtureConsumer, SecretStore
from onboarding.settings import BASE, Settings, load_settings

FIELDS = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)
MAX = 2**63 - 1
TABLES = ('onboarding_batches', 'onboarding_tasks', 'mailbox_registry', 'mailbox_platform_states',
          'secret_objects', 'resource_leases', 'operation_receipts', 'audit_events', 'task_steps',
          'operator_sessions', 'payment_cards', 'card_reservations', 'card_account_links')


def module():
    return importlib.import_module('onboarding.pool_batches')


@contextmanager
def observe(hook=None, seam=None, before=None):
    execute, fetch = psycopg.Cursor.execute, psycopg.Cursor.fetchone
    queries, commands = {}, []
    def run(cursor, query, params=None, **kwargs):
        text = query if type(query) is str else str(query)
        commands.append((cursor.connection.info.backend_pid, text, params))
        queries[id(cursor)] = text
        if before:
            before(cursor, text, params)
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
        spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError('consumer reached')))
                 for owner, name in ((FixtureConsumer, 'consume'), (SecretStore, 'use'),
                                     (SecretStore, '_decrypt'), (AESGCM, 'decrypt'), (FixtureExecutor, '_execute'),
                                     (pool_leases, 'claim'), (pool_leases, 'renew'))]
        try:
            yield spies
        finally:
            for spy in spies:
                spy.assert_not_called()


def spawned_batch(schema, actor_fields, directory, revision, ids, selection, count, key,
                  ready, start, hold_marker, release, output, config_replace=False):
    """Preopen APP before synchronization; real lock acquisition in child service."""
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    actor = security.Actor(*actor_fields)
    conn = None
    try:
        policy, mac = SyntheticPoolPolicy.from_settings(settings), RequestMac(directory)
        conn = storage.open_app(settings)
        pid = conn.info.backend_pid
        ready.put(('READY', pid, None))
        if not start.wait(10):
            raise RuntimeError('fixture:start-timeout')
        held = []
        def hold(cursor, query, params):
            if hold_marker and hold_marker in query and not held:
                held.append(True)
                output.put(('LOCKED', pid, None))
                if not release.wait(8):
                    raise RuntimeError('fixture:release-timeout')
        with patch.object(storage, 'open_app', return_value=conn), observe(hook=hold), no_consumption():
            if config_replace:
                result = pool_config.replace(settings, actor, revision, dict(FIELDS, concurrency=2), key,
                                             policy=policy, mac=mac)
            else:
                result = module().create(settings, actor, selection, count, ids, revision, key, policy=policy, mac=mac)
        output.put(('OK', pid, result))
    except ServiceError as exc:
        output.put((exc.code.value, conn.info.backend_pid if conn and not conn.closed else locals().get('pid'), None))
    except Exception as exc:
        output.put((type(exc).__name__, locals().get('pid'), None))
    finally:
        if conn is not None:
            conn.close()


class PoolBatchInputTests(unittest.TestCase):
    def test_local_exact_inputs_zero_policy_mac_sql(self):
        settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
        policy = object.__new__(SyntheticPoolPolicy)
        object.__setattr__(policy, 'settings', settings)
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        mid = str(uuid4())
        args = dict(settings=settings, actor=actor, selection='specified', requested_count=1,
                    mailbox_ids=[mid], expected_config_revision='pool-' + str(uuid4()),
                    request_key='fixture:key', policy=policy, mac=object.__new__(RequestMac))
        class Text(str): pass
        class Number(int): pass
        class Items(list): pass
        class SubActor(security.Actor): pass
        actors = [object(), repository.FixtureActor(actor.operator_id),
                  SubActor(actor.operator_id, actor.permissions, actor.session_id, 1)]
        for field, value in [('operator_id', Text(actor.operator_id)), ('session_id', 'bad'),
                             ('permissions', set()), ('permissions', frozenset({Text('tasks:manage')})),
                             ('auth_epoch', True), ('extra', 1)]:
            other = copy.copy(actor); object.__setattr__(other, field, value); actors.append(other)
        other = copy.copy(actor); object.__delattr__(other, 'session_id'); actors.append(other)
        cases = [('actor', value, ErrorCode.UNAUTHENTICATED) for value in actors]
        cases += [(name, value, ErrorCode.INVALID_INPUT) for name, values in {
            'settings': [object(), replace(settings, schema='different')], 'policy': [object()], 'mac': [object()],
            'selection': ['combined', '', None, Text('specified')],
            'requested_count': [True, 0, -1, 101, MAX, '1', 1.0, Number(1), None],
            'mailbox_ids': [None, (), Items([mid]), [], [mid, mid], [uuid4()], ['bad'], [uuid4().hex], [Text(mid)], [mid, str(uuid4())]],
            'expected_config_revision': [None, '', 'pool-bad', str(uuid4()), Text(args['expected_config_revision'])],
            'request_key': ['', 'space key', 'a'*129, None, Text('key'), '中文', 'key\n']
        }.items() for value in values]
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                     for owner, name in ((SyntheticPoolPolicy, '_validate'), (RequestMac, 'request_digest'),
                                         (storage, 'open_app'), (psycopg.Cursor, 'execute'))]
            for field, value, code in cases:
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaises(ServiceError) as caught:
                        module().create(**{**args, field: value})
                    self.assertEqual(caught.exception.code, code)
            with self.assertRaises(ServiceError) as caught:
                module().create(**{**args, 'selection': 'automatic'})
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for extra in ('platform', 'fields', 'model', 'region', 'target', 'timeout', 'worker_id', 'health'):
                with self.assertRaises(TypeError):
                    module().create(**args, **{extra: object()})
            for spy in spies:
                spy.assert_not_called()

    def test_shape_snapshot_and_boundary_counts(self):
        settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
        policy = object.__new__(SyntheticPoolPolicy); object.__setattr__(policy, 'settings', settings)
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        values = [str(uuid4()) for _ in range(100)]
        args = (settings, actor, 'specified', 100, values, 'pool-'+str(uuid4()), 'fixture:key', policy, object.__new__(RequestMac))
        saved = module()._inputs(*args)
        values.clear()
        self.assertEqual(len(saved), 100)
        self.assertEqual(saved, sorted(saved))
        self.assertEqual(module()._inputs(settings, actor, 'automatic', 100, [], args[5], args[6], policy, args[8]), [])

    def test_offline_consumption_guard_installs(self):
        with no_consumption() as spies:
            self.assertEqual(len(spies), 7)


class PoolBatchEntryTests(onboarding_pool_support.PoolCase):
    def test_entry_after_real_pool_setup(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.pool_batches'),
                             'onboarding.pool_batches is not implemented')

    def config(self, actor=None):
        current = pool_config.get_current(self.settings, self.actor, policy=self.vault._policy)
        return pool_config.replace(self.settings, actor or self.actor, current['revision'] if current else None,
                                   FIELDS, 'fixture:config:'+uuid4().hex, policy=self.vault._policy, mac=self.mac)

    def seed(self, *, identity='UNKNOWN', platform_secret=False, health='HEALTHY', usage='UNUSED'):
        """Trusted synthetic observer evidence, not an import/health API."""
        mid, sid = str(uuid4()), str(uuid4())
        email = 'fixture-'+uuid4().hex+'@fixture.invalid'
        with self.uow() as conn:
            ref = self.vault.put_locked(conn, self.actor, PoolResource('mailbox', mid),
                                       MailboxCredential(email, password='fixture:mailbox')).id
            conn.execute('INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref,'
                         "credential_version,health) VALUES(%s,%s,%s,'outlook',%s,2,%s)",
                         (mid, self.actor.operator_id, email, ref, health))
            conn.execute('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version,identity_status,'
                         "usage_status,evidence_ref,checked_at) VALUES(%s,%s,'google',3,%s,%s,'fixture:observer',clock_timestamp())",
                         (sid, mid, identity, usage))
            pref = self.vault.put_locked(conn, self.actor, PoolResource('platform', sid),
                        PlatformCredential(email, 'google', 'fixture:platform')).id if platform_secret else None
            conn.execute('UPDATE mailbox_platform_states SET credential_ref=%s WHERE id=%s', (pref, sid))
        return dict(mailbox=mid, state=sid, secret=ref, platform_secret=pref, identity=identity)

    def call(self, config, seeds=(), **overrides):
        args = dict(settings=self.settings, actor=self.actor, selection='specified', requested_count=len(seeds),
                    mailbox_ids=[seed['mailbox'] for seed in seeds], expected_config_revision=config['revision'],
                    request_key='fixture:batch', policy=self.vault._policy, mac=self.mac)
        args.update(overrides)
        with no_consumption():
            return module().create(**args)

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def change(self, query, args=()):
        with self.uow() as conn:
            conn.execute(query, args)

    def snapshot(self):
        with storage.open_app(self.settings) as conn:
            return {name: conn.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in TABLES}

    def unchanged(self, call, code):
        before = self.snapshot()
        self.reject(call, code)
        self.assertEqual(before, self.snapshot())

    def second_actor(self):
        sid = str(uuid4())
        self.change('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                    "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                    (sid, self.actor.operator_id, hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                     hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        return security.Actor(self.actor.operator_id, self.actor.permissions, sid, 1)

    def test_specified_and_automatic_exact_counts_pins_and_audits(self):
        config = self.config()
        for mode in ('specified', 'automatic'):
            seeds = [self.seed(identity='EXISTING', platform_secret=True), self.seed(identity='NEW_CONFIRMED')]
            before = self.snapshot()
            args = dict(selection=mode, request_key='fixture:'+mode)
            if mode == 'automatic': args['mailbox_ids'] = []
            result = self.call(config, list(reversed(seeds)), **args)
            self.assertEqual(set(result), {'batch_id','task_ids','mailbox_ids','config_id','config_revision','receipt_id','phase'})
            self.assertEqual(result['phase'], 'SUCCEEDED')
            self.assertEqual(result['mailbox_ids'], sorted(seed['mailbox'] for seed in seeds))
            self.assertEqual(result['config_id'], config['config_id'])
            after = self.snapshot()
            for table, delta in [('onboarding_batches',1),('onboarding_tasks',2),('resource_leases',2),('operation_receipts',1),('audit_events',4)]:
                self.assertEqual(len(after[table])-len(before[table]), delta)
            for table in ('mailbox_registry','secret_objects','task_steps','operator_sessions','payment_cards','card_reservations','card_account_links'):
                self.assertEqual(before[table], after[table])
            for mid, tid in zip(result['mailbox_ids'], result['task_ids']):
                seed = next(s for s in seeds if s['mailbox'] == mid)
                row = self.read('SELECT batch_id::text,config_id::text,mailbox_id::text,mailbox_ref,platform,platform_plan,'
                                'credential_version,mailbox_credential_ref::text,platform_credential_pins,status,generation,version,'
                                'cancel_requested,current_step,reason_code FROM onboarding_tasks WHERE id=%s', (tid,))[0]
                pin = dict(state_id=seed['state'],secret_ref=seed['platform_secret'],revision=3,identity_status=seed['identity'])
                self.assertEqual(row, (result['batch_id'],config['config_id'],mid,mid,'google',['google'],2,seed['secret'],
                                       {'google':pin},'QUEUED',1,2,False,None,None))
                self.assertEqual(self.read('SELECT usage_status,last_task_id::text,version FROM mailbox_platform_states WHERE id=%s',
                                          (seed['state'],)), [('RESERVED',tid,2)])
                self.assertEqual(self.read('SELECT task_id::text,owner_id,fence,hold_reason,version,'
                                          "lease_until>clock_timestamp(),lease_until<=updated_at+interval '31 seconds' "
                                          'FROM resource_leases WHERE resource_id=%s',(mid,)),
                                 [(tid,'batch:'+result['batch_id'],1,'HELD',2,True,True)])
            rows = self.read('SELECT action,correlation_id FROM audit_events WHERE task_id=ANY(%s::uuid[]) ORDER BY action',
                             (result['task_ids'],))
            self.assertCountEqual(rows, [('task.create',result['receipt_id'])]*2+[('lease.claim',result['receipt_id'])]*2)
            stable = self.snapshot()
            self.assertEqual(self.call(config, seeds, **args), result)
            self.assertEqual(stable, self.snapshot())

    def test_real_import_defaults_are_never_automatic_qualification(self):
        config = self.config()
        text = json.dumps(dict(email='fixture-import@fixture.invalid',password='fixture:mailbox',provider='outlook'))
        preview = mailboxes.preview_import(self.settings,self.actor,text,'',vault=self.vault,mac=self.mac)
        imported = mailboxes.import_text(self.settings,self.actor,text,'',preview['preview_digest'],'fixture:import',vault=self.vault,mac=self.mac)
        mid = imported.created_ids[0]
        self.assertEqual(self.read('SELECT health FROM mailbox_registry WHERE id=%s',(mid,)), [('UNKNOWN',)])
        self.assertEqual(self.read('SELECT DISTINCT usage_status FROM mailbox_platform_states WHERE mailbox_id=%s',(mid,)), [('HISTORY_UNRECONCILED',)])
        self.unchanged(lambda:self.call(config,selection='automatic',requested_count=1,mailbox_ids=[]), ErrorCode.RESOURCE_HELD)
        self.unchanged(lambda:self.call(config,[{'mailbox':mid}]), ErrorCode.RECONCILIATION_REQUIRED)

    def test_identity_null_and_distinct_secret_policy_matrix(self):
        config = self.config()
        for identity in ('UNKNOWN','NEW_CONFIRMED','EXISTING'):
            seed = self.seed(identity=identity)
            if identity == 'EXISTING':
                self.unchanged(lambda:self.call(config,[seed]), ErrorCode.RECONCILIATION_REQUIRED)
                self.unchanged(lambda:self.call(config,selection='automatic',requested_count=1,mailbox_ids=[]), ErrorCode.RESOURCE_HELD)
            else:
                self.call(config,[seed],request_key='fixture:'+identity)
        seed = self.seed(identity='EXISTING',platform_secret=True)
        self.assertNotEqual(seed['secret'], seed['platform_secret'])
        self.change('UPDATE mailbox_platform_states SET credential_ref=%s WHERE id=%s',(seed['secret'],seed['state']))
        self.unchanged(lambda:self.call(config,[seed]), ErrorCode.FORBIDDEN)
        self.change('UPDATE mailbox_platform_states SET credential_ref=%s WHERE id=%s',(seed['platform_secret'],seed['state']))
        self.call(config,[seed])

    def test_qualification_matrix_second_bad_resource_rolls_back_everything(self):
        config = self.config()
        good, bad = self.seed(), self.seed()
        cases = [('mailbox_registry','disabled',True), ('mailbox_registry','health','UNKNOWN'),
                 ('mailbox_registry','health','NEEDS_REVIEW'), ('mailbox_registry','health','DISABLED'),
                 ('mailbox_registry','pool_status','EXPORTED'), ('mailbox_registry','pool_status','QUARANTINED')]
        cases += [('mailbox_platform_states','usage_status',value) for value in
                  ('RESERVED','SUCCEEDED','FAILED_CONFIRMED','UNKNOWN','CONFLICT','HISTORY_UNRECONCILED')]
        for table, field, value in cases:
            target = bad['mailbox'] if table == 'mailbox_registry' else bad['state']
            old = self.read('SELECT '+field+' FROM '+table+' WHERE id=%s',(target,))[0][0]
            self.change('UPDATE '+table+' SET '+field+'=%s WHERE id=%s',(value,target))
            self.unchanged(lambda:self.call(config,[good,bad]), ErrorCode.RECONCILIATION_REQUIRED)
            self.unchanged(lambda:self.call(config,selection='automatic',requested_count=2,mailbox_ids=[]), ErrorCode.RESOURCE_HELD)
            self.change('UPDATE '+table+' SET '+field+'=%s WHERE id=%s',(old,target))
        self.unchanged(lambda:self.call(config,selection='automatic',requested_count=3,mailbox_ids=[]), ErrorCode.RESOURCE_HELD)

    def test_owner_precedes_eligibility_and_config_cas_precedes_owner(self):
        config = self.config()
        seed = self.seed(health='UNKNOWN')
        absent = dict(mailbox=str(uuid4()))
        self.unchanged(lambda:self.call(config,[seed,absent]), ErrorCode.FORBIDDEN)
        self.unchanged(lambda:self.call(config,[absent],expected_config_revision='pool-'+str(uuid4())), ErrorCode.VERSION_CONFLICT)
        other = str(uuid4())
        self.change("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture',ARRAY['tasks:manage'])",
                    (other,'fixture-'+uuid4().hex))
        self.change('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s',(other,seed['mailbox']))
        self.unchanged(lambda:self.call(config,[seed]), ErrorCode.FORBIDDEN)
        self.unchanged(lambda:self.call(config,selection='automatic',requested_count=1,mailbox_ids=[]), ErrorCode.RESOURCE_HELD)

    def test_secret_kind_policy_expired_revoked_and_independent_revisions(self):
        config = self.config()
        seed = self.seed(identity='EXISTING',platform_secret=True)
        for ref in (seed['secret'], seed['platform_secret']):
            for field, value, code in [('kind','fixture',ErrorCode.FORBIDDEN),('access_policy','fixture:wrong',ErrorCode.FORBIDDEN),
                                       ('expires_at',datetime(2000,1,1,tzinfo=timezone.utc),ErrorCode.SECRET_UNAVAILABLE),
                                       ('revoked_at',datetime(2000,1,1,tzinfo=timezone.utc),ErrorCode.SECRET_UNAVAILABLE)]:
                old = self.read('SELECT '+field+' FROM secret_objects WHERE id=%s',(ref,))[0][0]
                self.change('UPDATE secret_objects SET '+field+'=%s WHERE id=%s',(value,ref))
                self.unchanged(lambda:self.call(config,[seed]),code)
                self.unchanged(lambda:self.call(config,selection='automatic',requested_count=1,mailbox_ids=[]),ErrorCode.RESOURCE_HELD)
                self.change('UPDATE secret_objects SET '+field+'=%s WHERE id=%s',(old,ref))
        self.change('UPDATE secret_objects SET revision=41 WHERE id=ANY(%s::uuid[])',([seed['secret'],seed['platform_secret']],))
        result=self.call(config,[seed])
        self.assertEqual(self.read('SELECT credential_version,platform_credential_pins FROM onboarding_tasks WHERE id=%s',
                                  (result['task_ids'][0],))[0][0],2)

    def test_free_lease_preserves_counters_and_max_overflow_rolls_back(self):
        config = self.config(); seed = self.seed()
        self.change("INSERT INTO resource_leases(resource_kind,resource_id,fence,version) VALUES('mailbox',%s,7,9)",(seed['mailbox'],))
        for table, field, target_field, target in [('resource_leases','fence','resource_id',seed['mailbox']),
                                                  ('resource_leases','version','resource_id',seed['mailbox']),
                                                  ('mailbox_platform_states','version','id',seed['state'])]:
            old=self.read('SELECT '+field+' FROM '+table+' WHERE '+target_field+'=%s',(target,))[0][0]
            self.change('UPDATE '+table+' SET '+field+'=%s WHERE '+target_field+'=%s',(MAX,target))
            self.unchanged(lambda:self.call(config,[seed]),ErrorCode.VERSION_CONFLICT)
            self.change('UPDATE '+table+' SET '+field+'=%s WHERE '+target_field+'=%s',(old,target))
        self.call(config,[seed])
        self.assertEqual(self.read('SELECT fence,version FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(8,10)])

    def test_expired_unknown_holds_and_cross_platform_active_tasks_never_stolen(self):
        config=self.config(); seed=self.seed()
        result=self.call(config,[seed]); tid=result['task_ids'][0]
        self.change("UPDATE onboarding_tasks SET platform='claude',platform_plan='[\"claude\"]',"
                    "platform_credential_pins=jsonb_build_object('claude',platform_credential_pins->'google') WHERE id=%s",(tid,))
        self.change("UPDATE mailbox_platform_states SET usage_status='UNUSED',last_task_id=NULL WHERE id=%s",(seed['state'],))
        self.unchanged(lambda:self.call(config,[seed],request_key='fixture:other'),ErrorCode.RESOURCE_HELD)
        self.change("UPDATE onboarding_tasks SET status='SUCCEEDED' WHERE id=%s",(tid,))
        for hold in ('HELD','UNKNOWN','INTENT','CONFLICT'):
            self.change("UPDATE resource_leases SET hold_reason=%s,lease_until=clock_timestamp()-interval '1 second' WHERE resource_id=%s",(hold,seed['mailbox']))
            self.unchanged(lambda:self.call(config,[seed],request_key='fixture:other'),ErrorCode.RESOURCE_HELD)
        self.change('UPDATE resource_leases SET task_id=NULL,owner_id=NULL,hold_reason=NULL WHERE resource_id=%s',(seed['mailbox'],))
        self.unchanged(lambda:self.call(config,[seed],request_key='fixture:other'),ErrorCode.RECONCILIATION_REQUIRED)
        self.change('UPDATE resource_leases SET lease_until=NULL WHERE resource_id=%s',(seed['mailbox'],))
        self.change('UPDATE mailbox_platform_states SET last_task_id=%s WHERE id=%s',(tid,seed['state']))
        self.unchanged(lambda:self.call(config,[seed],request_key='fixture:other'),ErrorCode.RECONCILIATION_REQUIRED)

    def test_old_revision_replay_ignores_current_health_head_and_lease(self):
        config=self.config(); seed=self.seed(); result=self.call(config,[seed])
        newer=self.config()
        self.change("UPDATE mailbox_registry SET health='DISABLED',pool_status='QUARANTINED' WHERE id=%s",(seed['mailbox'],))
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second',hold_reason='UNKNOWN' WHERE resource_id=%s",(seed['mailbox'],))
        self.change('UPDATE secret_objects SET revoked_at=clock_timestamp() WHERE id=%s',(seed['secret'],))
        before=self.snapshot()
        self.assertEqual(self.call(config,[seed]),result)
        self.assertEqual(before,self.snapshot())
        self.reject(lambda:self.call(newer,[seed]),ErrorCode.IDEMPOTENCY_CONFLICT)
        self.reject(lambda:self.call(config,[seed],request_key='fixture:stale'),ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.read('SELECT config_id::text FROM onboarding_tasks WHERE id=%s',(result['task_ids'][0],)),[(config['config_id'],)])

    def test_live_permission_not_cached_and_replay_auth_required(self):
        config=self.config(); seed=self.seed()
        empty=security.Actor(self.actor.operator_id,frozenset(),self.actor.session_id,1)
        result=self.call(config,[seed],actor=empty)
        self.change("UPDATE operators SET permissions=ARRAY['onboarding:read','config:manage'] WHERE id=%s",(self.actor.operator_id,))
        self.unchanged(lambda:self.call(config,[seed]),ErrorCode.FORBIDDEN)
        self.change("UPDATE operators SET permissions=ARRAY['tasks:manage'] WHERE id=%s",(self.actor.operator_id,))
        self.assertEqual(self.call(config,[seed],actor=empty),result)
        self.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
        self.unchanged(lambda:self.call(config,[seed]),ErrorCode.UNAUTHENTICATED)

    def test_receipt_header_and_summary_corruption_hash_conflict_priority(self):
        config=self.config(); seed=self.seed(); result=self.call(config,[seed]); before=self.snapshot()
        def receipt_query(query):
            return 'FROM operation_receipts ' in query and "action='pool.batch.create'" in query
        header_faults=[(0,str(uuid4())),(1,uuid4()),(2,uuid4()),(3,'pool.other'),(4,'pool-admin:wrong'),
                       (5,True),(5,2),(6,False),(6,1),(7,True),(8,'wrong'),(9,'not-hash')]
        result_faults=[(7,'FAILED_CONFIRMED'),(10,'WRONG'),(11,'fixture:external'),(12,{}),
                       (12,{'extra':True})]
        for index,value in header_faults+result_faults:
            reached=[]
            def seam(query,row):
                if receipt_query(query):
                    reached.append(1); values=list(row); values[index]=value; return tuple(values)
                return row
            with self.subTest(index=index,value_type=type(value).__name__),observe(seam=seam):
                self.reject(lambda:self.call(config,[seed]),ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(reached,[1])
        saved=self.read('SELECT result_summary FROM operation_receipts WHERE id=%s',(result['receipt_id'],))[0][0]
        for field,value in [('task_ids',[str(uuid4())]),('task_ids',[True]),('mailbox_ids',[str(uuid4())]),
                            ('requested_count',True),('selection','automatic'),('config_revision','pool-'+str(uuid4())),
                            ('batch_id',str(uuid4())),('config_id',str(uuid4()))]:
            def seam(query,row):
                if receipt_query(query):
                    values=list(row); values[12]={**saved,field:value}; return tuple(values)
                return row
            with observe(seam=seam):
                self.reject(lambda:self.call(config,[seed]),ErrorCode.DEPENDENCY_UNAVAILABLE)
        def corrupt_result(query,row):
            if receipt_query(query):
                values=list(row); values[12]={}; return tuple(values)
            return row
        with observe(seam=corrupt_result):
            self.reject(lambda:self.call(config,[seed],expected_config_revision='pool-'+str(uuid4())),ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(before,self.snapshot())

    def test_history_graph_decoded_corruption_never_replays(self):
        config=self.config(); seed=self.seed(); self.call(config,[seed]); before=self.snapshot()
        cases=[('FROM onboarding_batches WHERE',0,uuid4()),('FROM onboarding_batches WHERE',1,uuid4()),
               ('FROM onboarding_batches WHERE',4,True),('FROM global_configs WHERE id=',6,'fixture'),
               ('FROM mailbox_registry WHERE',1,uuid4())]
        for marker,index,value in cases:
            def seam(query,row):
                if marker in query:
                    changed=list(row); changed[index]=value; return tuple(changed)
                return row
            with observe(seam=seam):
                self.reject(lambda:self.call(config,[seed]),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(before,self.snapshot())

    def test_resource_decoded_types_and_returning_faults_roll_back(self):
        config=self.config(); seed=self.seed(identity='EXISTING',platform_secret=True); before=self.snapshot()
        cases=[('FROM mailbox_registry WHERE id=%s FOR UPDATE NOWAIT',3,True),
               ('FROM mailbox_registry WHERE id=%s FOR UPDATE NOWAIT',4,1),
               ('FROM mailbox_platform_states WHERE',4,True),('FROM mailbox_platform_states WHERE',8,True),
               ('FROM secret_objects WHERE',2,True),('FROM secret_objects WHERE',3,'bad key'),
               ('FROM secret_objects WHERE',5,datetime(2020,1,1)),('FROM secret_objects WHERE',7,False),
               ('UPDATE onboarding_tasks SET',1,True),('UPDATE mailbox_platform_states SET',8,True),
               ('UPDATE resource_leases SET',4,True),('UPDATE resource_leases SET',1,str(uuid4())),
               ('INSERT INTO audit_events',0,True)]
        for marker,index,value in cases:
            reached=[]
            def seam(query,row):
                if marker in query:
                    reached.append(1); values=list(row); values[index]=value; return tuple(values)
                return row
            with self.subTest(marker=marker,index=index),observe(seam=seam):
                self.reject(lambda:self.call(config,[seed]),ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertTrue(reached)
            self.assertEqual(before,self.snapshot())
        for marker,code in [('UPDATE onboarding_tasks SET',ErrorCode.VERSION_CONFLICT),
                            ('UPDATE resource_leases SET',ErrorCode.VERSION_CONFLICT),
                            ('UPDATE mailbox_platform_states SET',ErrorCode.VERSION_CONFLICT),
                            ('INSERT INTO operation_receipts',ErrorCode.DEPENDENCY_UNAVAILABLE),
                            ('INSERT INTO onboarding_batches',ErrorCode.DEPENDENCY_UNAVAILABLE)]:
            reached=[]
            def seam(query,row):
                if marker in query: reached.append(1); return None
                return row
            with observe(seam=seam):
                self.reject(lambda:self.call(config,[seed]),code)
            self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())

    def test_mac_canonical_body_and_actual_final_key_change_rolls_back(self):
        config=self.config(); seeds=[self.seed(),self.seed()]; calls=[]
        original=RequestMac.request_digest
        def capture(mac,domain,owner,body):
            calls.append((domain,owner,body)); return original(mac,domain,owner,body)
        with patch.object(RequestMac,'request_digest',capture): result=self.call(config,seeds)
        body=json.dumps(dict(v=1,schema=self.settings.schema,instance_marker=self.settings.instance_marker,
                             selection='specified',requested_count=2,mailbox_ids=sorted(s['mailbox'] for s in seeds),
                             expected_config_revision=config['revision'],request_key='fixture:batch'),
                        sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('utf-8')
        self.assertEqual([item for item in calls if item[0]=='pool.batch.create.v1'],
                         [('pool.batch.create.v1',self.actor.operator_id,body)])
        self.assertEqual([item for item in calls if item[0]=='pool.batch.create.keycheck.v1'],
                         [('pool.batch.create.keycheck.v1',self.actor.operator_id,b'fixture:stable-key')]*3)
        self.assertEqual(self.read('SELECT request_hash FROM operation_receipts WHERE id=%s',(result['receipt_id'],)),
                         [(original(self.mac,'pool.batch.create.v1',self.actor.operator_id,body),)])
        seed=self.seed(); path=self.key_directory/'request-mac.key'; saved=path.read_bytes(); append=audit.append
        before=self.snapshot(); changed=[]
        def fault(*args,**kwargs):
            value=append(*args,**kwargs)
            if not changed: path.write_bytes(secrets.token_bytes(32)); changed.append(True)
            return value
        try:
            with patch.object(audit,'append',fault):
                self.reject(lambda:self.call(config,[seed],request_key='fixture:mac'),ErrorCode.SECRET_UNAVAILABLE)
        finally: path.write_bytes(saved)
        self.assertEqual(changed,[True]); self.assertEqual(before,self.snapshot())

    def test_audit_precommit_fault_rollback_and_real_commit_ack_loss_replay(self):
        config=self.config(); seeds=[self.seed(),self.seed()]; before=self.snapshot()
        with patch.object(audit,'append',side_effect=psycopg.OperationalError('fixture:audit')):
            self.reject(lambda:self.call(config,seeds),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(before,self.snapshot())
        original=psycopg.Connection.transaction; reached=[]
        @contextmanager
        def precommit(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as tx:
                yield tx; reached.append('before'); raise RuntimeError('fixture:before-commit')
        with patch.object(psycopg.Connection,'transaction',precommit):
            self.reject(lambda:self.call(config,seeds),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,['before']); self.assertEqual(before,self.snapshot())
        @contextmanager
        def ackloss(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as tx: yield tx
            reached.append('durable'); raise psycopg.OperationalError('fixture:after-real-commit')
        with patch.object(psycopg.Connection,'transaction',ackloss):
            self.reject(lambda:self.call(config,seeds),ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(reached,['before','durable'])
        durable=self.snapshot(); self.assertEqual(len(durable['onboarding_tasks'])-len(before['onboarding_tasks']),2)
        result=self.call(config,seeds)
        self.assertEqual(set(result['task_ids']),{str(row[0]) for row in durable['onboarding_tasks']})
        self.assertEqual(durable,self.snapshot())

    def test_lock_order_last_clock_commit_and_no_secret_body_or_old_task_lock(self):
        config=self.config(); seeds=[self.seed(platform_secret=True),self.seed(platform_secret=True)]
        phases=[]; original=psycopg.Connection.transaction
        @contextmanager
        def commit(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as tx: yield tx
            phases.append('commit')
        with observe() as statements,patch.object(psycopg.Connection,'transaction',commit):
            result=self.call(config,seeds); phases.append('return')
        self.assertEqual(phases,['commit','return'])
        sql=[q for _,q,_ in statements]
        self.assertEqual(sql[-1],'SELECT clock_timestamp()')
        self.assertFalse(any('SKIP LOCKED' in q or 'ciphertext' in q or 'nonce' in q or 'payment_cards' in q or 'card_reservations' in q for q in sql))
        self.assertFalse(any('FROM onboarding_tasks' in q and ('FOR UPDATE' in q or 'FOR SHARE' in q) for q in sql))
        mailbox=[p[0] for _,q,p in statements if 'FROM mailbox_registry' in q and 'FOR UPDATE NOWAIT' in q]
        state=[p[0] for _,q,p in statements if 'FROM mailbox_platform_states' in q and 'FOR UPDATE' in q]
        secret=[p[0] for _,q,p in statements if 'FROM secret_objects' in q and 'FOR SHARE' in q]
        self.assertEqual(mailbox,sorted(s['mailbox'] for s in seeds)); self.assertEqual(state,sorted(s['state'] for s in seeds))
        self.assertEqual(secret,sorted(secret)); self.assertEqual(len(secret),4)
        locate=lambda marker:next(i for i,q in enumerate(sql) if marker in q)
        self.assertLess(locate('FROM operator_sessions'),locate('428702'))
        self.assertLess(locate('428702'),locate('FROM operation_receipts'))
        self.assertLess(locate('FROM operation_receipts'),locate('428701'))
        self.assertLess(locate('428701'),locate('FOR UPDATE NOWAIT'))
        self.assertEqual(result['phase'],'SUCCEEDED')

    def wait_blocked(self, control, waiter, blocker, seconds=1.3):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            blockers=control.execute('SELECT pg_blocking_pids(%s)',(waiter,)).fetchone()[0]
            if blocker in blockers:
                return blockers
            time.sleep(.01)
        self.fail('actual backend never showed expected pg_blocking_pids edge')

    def run_spawn_pair(self, config, seeds, mode):
        ctx=multiprocessing.get_context('spawn'); actors=(self.actor,self.second_actor())
        ready,output=ctx.Queue(),ctx.Queue(); start=[ctx.Event(),ctx.Event()]; release=ctx.Event()
        ids=[s['mailbox'] for s in seeds]
        specs=[('specified',len(ids),ids,'fixture:race:'+mode,False),
               ('specified',len(ids),ids,'fixture:race:'+mode,False)]
        marker='pg_advisory_xact_lock(hashtext(%s),428702)'
        if mode=='different-body': specs[1]=('automatic',len(ids),[],'fixture:race:'+mode,False)
        if mode=='mailbox':
            specs[1]=('specified',len(ids),ids,'fixture:other:'+mode,False)
            marker='FOR UPDATE NOWAIT'
        if mode=='config':
            specs[1]=('specified',len(ids),ids,'fixture:config-race',True)
            marker='pg_advisory_xact_lock_shared'
        processes=[]; pids=[]; outcomes=[]
        with storage.open_app(self.settings) as control:
            try:
                for index,(actor,spec) in enumerate(zip(actors,specs)):
                    selection,count,chosen,key,config_replace=spec
                    fields=(actor.operator_id,actor.permissions,actor.session_id,actor.auth_epoch)
                    process=ctx.Process(target=spawned_batch,args=(self.settings.schema,fields,str(self.key_directory),
                        config['revision'],chosen,selection,count,key,ready,start[index],marker if index==0 else None,
                        release,output,config_replace))
                    processes.append(process); process.start()
                    message=ready.get(timeout=12); self.assertEqual(message[0],'READY'); pids.append(message[1])
                self.assertNotEqual(*pids)
                start[0].set(); held=output.get(timeout=8); self.assertEqual(held[:2],('LOCKED',pids[0]))
                start[1].set()
                if mode=='mailbox':
                    outcomes.append(output.get(timeout=4))
                    self.assertEqual(outcomes[0][0],ErrorCode.RESOURCE_HELD.value)
                else:
                    self.wait_blocked(control,pids[1],pids[0])
                release.set()
                while len(outcomes)<2: outcomes.append(output.get(timeout=8))
                for process in processes:
                    process.join(5); self.assertEqual(process.exitcode,0)
            finally:
                release.set()
                for event in start: event.set()
                for process in processes:
                    if process.is_alive(): process.terminate(); process.join(5)
                ready.close(); ready.join_thread(); output.close(); output.join_thread()
        self.assertEqual(len({item[1] for item in outcomes}),2)
        return outcomes

    def test_two_spawn_same_key_same_body_returns_one_complete_batch(self):
        config=self.config(); seeds=[self.seed(),self.seed()]
        before=self.snapshot(); outcomes=self.run_spawn_pair(config,seeds,'same-body')
        self.assertEqual([item[0] for item in outcomes],['OK','OK'])
        self.assertEqual(outcomes[0][2],outcomes[1][2])
        after=self.snapshot()
        for table,delta in [('onboarding_batches',1),('onboarding_tasks',2),('resource_leases',2),('operation_receipts',1),('audit_events',4)]:
            self.assertEqual(len(after[table])-len(before[table]),delta)

    def test_two_spawn_same_key_different_body_hash_conflict_after_guard(self):
        config=self.config(); seeds=[self.seed(),self.seed()]
        outcomes=self.run_spawn_pair(config,seeds,'different-body')
        self.assertCountEqual([item[0] for item in outcomes],['OK',ErrorCode.IDEMPOTENCY_CONFLICT.value])
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='pool.batch.create'"),[(1,)])
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks'),[(2,)])

    def test_two_spawn_different_keys_same_mailboxes_nowait_all_or_nothing(self):
        config=self.config(); seeds=[self.seed(),self.seed()]
        outcomes=self.run_spawn_pair(config,seeds,'mailbox')
        self.assertCountEqual([item[0] for item in outcomes],['OK',ErrorCode.RESOURCE_HELD.value])
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks'),[(2,)])
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases'),[(2,)])

    def test_two_spawn_shared_config_guard_keeps_one_exact_snapshot(self):
        config=self.config(); seeds=[self.seed()]
        outcomes=self.run_spawn_pair(config,seeds,'config')
        self.assertEqual([item[0] for item in outcomes],['OK','OK'])
        batch=next(item[2] for item in outcomes if 'batch_id' in item[2])
        new=next(item[2] for item in outcomes if 'request_key' in item[2])
        self.assertNotEqual(new['config_id'],config['config_id'])
        self.assertEqual(batch['config_id'],config['config_id'])
        self.assertEqual(self.read('SELECT config_id::text FROM onboarding_tasks'),[(config['config_id'],)])

    def test_second_mailbox_nowait_busy_leaves_no_partial_batch(self):
        config=self.config(); seeds=sorted([self.seed(),self.seed()],key=lambda s:s['mailbox'])
        before=self.snapshot()
        with storage.open_app(self.settings) as blocker:
            with blocker.transaction():
                blocker.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE',(seeds[1]['mailbox'],))
                self.reject(lambda:self.call(config,seeds),ErrorCode.RESOURCE_HELD)
        self.assertEqual(before,self.snapshot())

    def test_old_task_waits_same_session_batch_never_locks_old_task(self):
        config=self.config(); seed=self.seed(); accepted=self.call(config,[seed]); tid=accepted['task_ids'][0]
        before=self.snapshot(); held=threading.Event(); release=threading.Event(); outcomes=queue.Queue()
        batch_conn=storage.open_app(self.settings); old_conn=storage.open_app(self.settings)
        batch_pid,old_pid=batch_conn.info.backend_pid,old_conn.info.backend_pid
        batch_thread=None; old_thread=None; real_open=storage.open_app; stopped=[]
        def opened(settings):
            if threading.current_thread().name=='fixture:batch': return batch_conn
            if threading.current_thread().name=='fixture:old': return old_conn
            return real_open(settings)
        def hook(cursor,query,params):
            if cursor.connection.info.backend_pid==batch_pid and 'FROM operator_sessions' in query and not stopped:
                stopped.append(True); held.set()
                if not release.wait(6): raise RuntimeError('fixture:batch-release')
        def old_run():
            try:
                with storage.unit_of_work(self.settings) as conn:
                    pool_leases.claim(conn,self.actor,tid,'mailbox',seed['mailbox'],'fixture:old',2,policy=self.vault._policy)
                outcomes.put(('old','OK'))
            except ServiceError as exc: outcomes.put(('old',exc.code.value))
            except Exception as exc: outcomes.put(('old',type(exc).__name__))
        with storage.open_app(self.settings) as control:
            try:
                # This intentionally calls the actual old claim; do not install
                # no_consumption's claim prohibition around the old thread.
                def new_batch_run():
                    try:
                        module().create(self.settings,self.actor,'specified',1,[seed['mailbox']],config['revision'],
                                        'fixture:inversion',policy=self.vault._policy,mac=self.mac)
                        outcomes.put(('batch','OK'))
                    except ServiceError as exc: outcomes.put(('batch',exc.code.value))
                    except Exception as exc: outcomes.put(('batch',type(exc).__name__))
                with patch.object(storage,'open_app',opened),observe(hook=hook) as statements:
                    batch_thread=threading.Thread(target=new_batch_run,name='fixture:batch'); batch_thread.start()
                    self.assertTrue(held.wait(4))
                    old_thread=threading.Thread(target=old_run,name='fixture:old'); old_thread.start()
                    self.wait_blocked(control,old_pid,batch_pid)
                    release.set(); batch_thread.join(5); old_thread.join(5)
                    self.assertFalse(batch_thread.is_alive()); self.assertFalse(old_thread.is_alive())
                self.assertCountEqual([outcomes.get(timeout=1),outcomes.get(timeout=1)],
                                      [('batch',ErrorCode.RESOURCE_HELD.value),('old',ErrorCode.RESOURCE_HELD.value)])
                self.assertFalse(any(pid==batch_pid and 'FROM onboarding_tasks' in q and 'FOR UPDATE' in q
                                     for pid,q,_ in statements))
            finally:
                release.set()
                for thread,conn in ((batch_thread,batch_conn),(old_thread,old_conn)):
                    if thread is not None and thread.is_alive(): conn.cancel(); thread.join(5)
                    conn.close()
                self.assertFalse(any(t is not None and t.is_alive() for t in (batch_thread,old_thread)))
        self.assertEqual(before,self.snapshot())

    def test_old_task_already_holds_mailbox_nowait_breaks_other_inversion(self):
        config=self.config(); seed=self.seed(); accepted=self.call(config,[seed]); tid=accepted['task_ids'][0]
        before=self.snapshot()
        with storage.open_app(self.settings) as old:
            with old.transaction():
                old.execute('SELECT id FROM onboarding_tasks WHERE id=%s FOR UPDATE',(tid,))
                old.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE',(seed['mailbox'],))
                self.reject(lambda:self.call(config,[seed],request_key='fixture:held-mailbox'),ErrorCode.RESOURCE_HELD)
        self.assertEqual(before,self.snapshot())

    def waited_expiry(self, boundary, deadline_kind, *, automatic=False):
        """Prepare before arming; prove a real wait straddles one DB deadline.

        For secret-row contention only, arm/COMMIT the expiry immediately before
        taking its row lock: the worker sees that committed deadline, never an
        old uncommitted snapshot. No clock seam or widened timeout is used.
        """
        table='operator_sessions' if deadline_kind=='session' else 'secret_objects'
        evidence=dict(table=table,boundary=boundary,worker_pid=None,blocker_pid=None,
                      deadline=None,first_wait_at=None,last_db_now=None,results=[],
                      thread_alive=False,waiting=False)
        self.batch_wait_evidence=evidence
        ready=threading.Event(); start=threading.Event(); abort=threading.Event(); done=threading.Event()
        worker=control=blocker=thread=transaction=None
        real_open=storage.open_app
        def opened(settings):
            return worker if threading.current_thread().name=='fixture:expiry' else real_open(settings)
        def run():
            try:
                kwargs = dict(selection='automatic',mailbox_ids=[]) if automatic else {}
                ready.set()
                if not start.wait(8):
                    evidence['results'].append('START_TIMEOUT'); return
                if abort.is_set():
                    evidence['results'].append('ABORTED'); return
                self.call(config,[seed],**kwargs); evidence['results'].append('OK')
            except ServiceError as exc: evidence['results'].append(exc.code.value)
            except Exception as exc: evidence['results'].append(type(exc).__name__)
            finally: done.set()
        try:
            config=self.config(); seed=self.seed(platform_secret=True)
            accepted=self.call(config,[seed]) if boundary=='receipt' else None
            if boundary=='lease':
                # This is durable fixture preparation, before snapshot or timer.
                self.change("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s)",
                            (seed['mailbox'],))
            original=self.snapshot()
            worker=storage.open_app(self.settings); control=storage.open_app(self.settings)
            blocker=self.fixture.migrator() if boundary=='audit' else storage.open_app(self.settings)
            pid,bpid=worker.info.backend_pid,blocker.info.backend_pid
            evidence.update(worker_pid=pid,blocker_pid=bpid)
            transaction=blocker.transaction(); transaction.__enter__()
            if boundary=='config': pool_config._guard_locked(blocker)
            elif boundary=='state': blocker.execute('SELECT id FROM mailbox_platform_states WHERE id=%s FOR UPDATE',(seed['state'],))
            elif boundary=='lease':
                blocker.execute("SELECT resource_id FROM resource_leases WHERE resource_kind='mailbox' AND resource_id=%s FOR UPDATE",(seed['mailbox'],))
            elif boundary=='receipt': blocker.execute('SELECT id FROM operation_receipts WHERE id=%s FOR UPDATE',(accepted['receipt_id'],))
            elif boundary=='audit': blocker.execute('LOCK TABLE audit_events IN SHARE MODE')
            elif boundary!='secret': self.fail('unrecognized fixture boundary')
            identity=self.actor.session_id if deadline_kind=='session' else seed['platform_secret']
            field='idle_expires_at' if deadline_kind=='session' else 'expires_at'
            with patch.object(storage,'open_app',opened):
                thread=threading.Thread(target=run,name='fixture:expiry'); thread.start()
                self.assertTrue(ready.wait(4))
                # All connections, snapshot, empty lease and worker are ready.
                # Only secret's lock must follow the committed deadline UPDATE.
                cursor=control.execute('UPDATE '+table+' SET '+field+"=clock_timestamp()+interval '1200 milliseconds' "
                                       'WHERE id=%s RETURNING *',(identity,))
                row=cursor.fetchone()
                expires=row[[column.name for column in cursor.description].index(field)]
                evidence['deadline']=expires.isoformat()
                original[table]=[row if str(value[0])==identity else value for value in original[table]]
                if boundary=='secret':
                    blocker.execute('SELECT id FROM secret_objects WHERE id=%s FOR UPDATE',(seed['platform_secret'],))
                start.set()
                finish=time.monotonic()+2
                first_wait_at=None
                while True:
                    blockers,now=control.execute('SELECT pg_blocking_pids(%s),clock_timestamp()',(pid,)).fetchone()
                    waiting=bpid in blockers
                    evidence.update(last_db_now=now.isoformat(),waiting=waiting,thread_alive=thread.is_alive())
                    if waiting and first_wait_at is None:
                        first_wait_at=now
                        evidence['first_wait_at']=now.isoformat()
                        self.assertLess(first_wait_at,expires)
                    self.assertFalse(done.is_set())
                    self.assertTrue(thread.is_alive())
                    if first_wait_at is not None and now>expires:
                        self.assertTrue(waiting)
                        self.assertLess(first_wait_at,expires)
                        self.assertLess(expires,now)
                        break
                    self.assertLess(time.monotonic(),finish)
                    time.sleep(.01)
                transaction.__exit__(None,None,None); transaction=None
                thread.join(5); self.assertFalse(thread.is_alive())
            code=ErrorCode.UNAUTHENTICATED if deadline_kind=='session' else ErrorCode.SECRET_UNAVAILABLE
            self.assertEqual(evidence['results'],[code.value])
        finally:
            abort.set(); start.set()
            try:
                if transaction is not None: transaction.__exit__(None,None,None)
            finally:
                if blocker is not None: blocker.close()
                try:
                    if thread is not None and thread.is_alive():
                        if worker is not None and not worker.closed: worker.cancel()
                        thread.join(5)
                finally:
                    if worker is not None: worker.close()
                    if control is not None: control.close()
                    evidence['thread_alive']=thread is not None and thread.is_alive()
                    # Last observation is kept even on failure; no exception text,
                    # email, credential, SQL or unapproved fields are recorded.
                    self.batch_wait_evidence=evidence
                self.assertFalse(evidence['thread_alive'])
        self.assertEqual(original,self.snapshot())

    def test_actual_config_wait_then_session_expiry(self):
        self.waited_expiry('config','session')

    def test_actual_state_wait_then_session_expiry(self):
        self.waited_expiry('state','session')

    def test_actual_lease_wait_then_session_expiry(self):
        self.waited_expiry('lease','session')

    def test_actual_secret_wait_then_platform_secret_expiry(self):
        self.waited_expiry('secret','secret',automatic=True)

    def test_actual_receipt_wait_then_session_expiry(self):
        self.waited_expiry('receipt','session')

    def test_actual_audit_wait_then_secret_expiry_rolls_back_all(self):
        self.waited_expiry('audit','secret')

    def test_automatic_busy_candidate_never_replaced_by_spare(self):
        config=self.config(); seeds=sorted([self.seed(),self.seed(),self.seed()],key=lambda s:s['mailbox'])
        before=self.snapshot()
        with storage.open_app(self.settings) as blocker:
            with blocker.transaction():
                blocker.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE',(seeds[1]['mailbox'],))
                self.reject(lambda:self.call(config,selection='automatic',requested_count=2,mailbox_ids=[]),ErrorCode.RESOURCE_HELD)
        self.assertEqual(before,self.snapshot())

    def test_automatic_unknown_null_only_logical_queue(self):
        config=self.config(); seed=self.seed(identity='UNKNOWN')
        result=self.call(config,selection='automatic',requested_count=1,mailbox_ids=[])
        self.assertEqual(result['mailbox_ids'],[seed['mailbox']])
        self.assertEqual(self.read('SELECT platform_credential_pins FROM onboarding_tasks WHERE id=%s',(result['task_ids'][0],)),
                         [({'google':dict(state_id=seed['state'],secret_ref=None,revision=3,identity_status='UNKNOWN')},)])
        self.assertEqual(self.read('SELECT ever_registration_attempted,sale_eligibility,last_used_at FROM mailbox_registry WHERE id=%s',
                                  (seed['mailbox'],)),[(False,'UNVERIFIED',None)])

    def create(self, config, seeds, key, actor=None, **overrides):
        args = dict(settings=self.settings, actor=actor or self.actor, selection='specified',
                    requested_count=len(seeds), mailbox_ids=[seed['mailbox'] for seed in seeds],
                    expected_config_revision=config['revision'], request_key=key,
                    policy=self.vault._policy, mac=self.mac)
        args.update(overrides)
        return module().create(**args)

    def test_not_committed_proof_when_config_revision_displaced(self):
        stale, seed = self.config(), self.seed()
        self.config()                                                    # revision moves on
        before = self.business_snapshot()
        self.assertTrue(self.proof(lambda: self.create(stale, [seed], 'fixture:stale')))
        self.assertTrue(self.proof(lambda: self.create(stale, [], 'fixture:auto', selection='automatic',
                                                       requested_count=1, mailbox_ids=[])))
        self.assertEqual(before, self.business_snapshot())

    def test_no_proof_on_resource_rejection(self):
        config, seed = self.config(), self.seed(usage='SUCCEEDED')
        with self.assertRaises(ServiceError) as caught:
            self.create(config, [seed], 'fixture:busy')
        self.assertFalse(caught.exception.not_committed)
        with self.assertRaises(ServiceError) as caught:
            self.create(config, [], 'fixture:none', selection='automatic', requested_count=5, mailbox_ids=[])
        self.assertFalse(caught.exception.not_committed)

    def test_committed_original_replays_its_result(self):
        config, seed = self.config(), self.seed()
        result = self.create(config, [seed], 'fixture:done')
        self.assertEqual(self.create(config, [seed], 'fixture:done'), result)

    def test_no_proof_under_migration_drift(self):
        stale, seed = self.config(), self.seed()
        self.config()
        checksum = self.drift_migrations()
        try:
            self.assertFalse(self.proof(lambda: self.create(stale, [seed], 'fixture:drift')))
        finally:
            self.set_migration_checksum(checksum)

    def test_no_proof_when_migration_commits_before_tail_checks(self):
        stale, seed = self.config(), self.seed()
        self.config()
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.set_migration_checksum('0' * 64)
        try:
            with after_query(hook):
                flagged = self.proof(lambda: self.create(stale, [seed], 'fixture:race'))
        finally:
            self.set_migration_checksum(checksum)
        self.assertTrue(fired)
        self.assertFalse(flagged)

    def test_mac_rotation_after_commit_is_idempotency_conflict_without_proof(self):
        config, seed = self.config(), self.seed()
        self.create(config, [seed], 'fixture:mac')
        self.mac = self.rotate_request_mac()
        with self.assertRaises(ServiceError) as caught:
            self.create(config, [seed], 'fixture:mac')
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertFalse(caught.exception.not_committed)

    def test_queued_original_never_commits_after_proof(self):
        stale, seed = self.config(), self.seed()
        self.config()
        outcome = self.queued_original_never_commits(lambda: self.create(stale, [seed], 'fixture:r0'),
                                                     lambda: self.create(stale, [seed], 'fixture:r0'))
        self.assertEqual(outcome[0], ErrorCode.VERSION_CONFLICT.value)

    def test_replay_behind_committing_original_returns_it_without_proof(self):
        config, seed, other = self.config(), self.seed(), self.second_session()
        original, replay = self.race_original('428702', lambda: self.create(config, [seed], 'fixture:lock'),
                                              lambda: self.create(config, [seed], 'fixture:lock', actor=other))
        self.assertEqual(original[0], 'OK')
        self.assertEqual(replay, original)

    def test_replay_behind_rolled_back_original_applies_normally(self):
        config, seed, other = self.config(), self.seed(), self.second_session()
        original, replay = self.race_original('428702', lambda: self.create(config, [seed], 'fixture:rollback'),
                                              lambda: self.create(config, [seed], 'fixture:rollback', actor=other),
                                              rollback=True)
        self.assertNotEqual(original[0], 'OK')
        self.assertEqual(replay[0], 'OK')
        # B1Case pre-seeds a fixture batch; count this race's pool writes only (Task 4 ruling).
        self.assertEqual(self.read("SELECT count(*) FROM onboarding_tasks WHERE execution_scope='pool'")[0][0], 1)
