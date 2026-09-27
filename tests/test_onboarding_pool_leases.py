"""Synthetic logical lease tests; never provider or execution authorization."""
import importlib.util
from onboarding_pool_support import PoolCase


class PoolLeaseTests(PoolCase):
    def test_entry_after_real_pool_setup(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.pool_leases'),
                             'missing approved pool logical lease entry')

import copy
import importlib
import hashlib
import multiprocessing
import secrets
import threading
import time
import unittest
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from onboarding import audit, repository, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.contracts import LeaseToken as FixtureLeaseToken
from onboarding.secret_store import FixtureConsumer, SecretStore
from onboarding.coordinator import FixtureExecutor
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from onboarding.pool_contracts import Platform, PoolLeaseToken, PoolExecutionContext
from onboarding.pool_secret_types import MailboxCredential, PlatformCredential, PoolResource
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.settings import BASE, load_settings

FIELDS = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)
MAX = 2**63 - 1


def module():
    return importlib.import_module('onboarding.pool_leases')


@contextmanager
def observe(hook=None, seam=None):
    """Real SQL observation; named seams only alter impossible decoded values."""
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
    """Observe real existing fixture/decrypt boundaries, not a fictitious pool API."""
    boundaries = ((FixtureConsumer, 'consume'), (SecretStore, 'use'),
                  (SecretStore, '_decrypt'), (AESGCM, 'decrypt'),
                  (FixtureExecutor, '_execute'))
    with ExitStack() as stack:
        spies = [stack.enter_context(patch.object(owner, name,
                 side_effect=AssertionError('logical lease crossed consumption boundary')))
                 for owner, name in boundaries]
        try:
            yield spies
        finally:
            for spy in spies:
                spy.assert_not_called()


def spawned_claim(schema, actor_fields, seed, barrier, output):
    """Settings loaded locally; no credentials/DSN cross the process boundary."""
    from dataclasses import replace
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    actor = security.Actor(*actor_fields)
    policy = SyntheticPoolPolicy.from_settings(settings)
    try:
        with storage.unit_of_work(settings) as conn:
            pid = conn.info.backend_pid
            barrier.wait(10)
            token = module().claim(conn, actor, seed['id'], 'mailbox', seed['mailbox'],
                                   'spawn-' + actor.session_id, 1, policy=policy)
        output.put(('OK', pid, token.owner_id))
    except ServiceError as error:
        output.put((error.code.value, pid, None))


class PoolLeaseInputTests(unittest.TestCase):
    def test_claim_local_shapes_before_policy_or_sql(self):
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        args = dict(conn=object(), actor=actor, task_id=str(uuid4()), resource_kind='mailbox',
                    resource_id=str(uuid4()), owner_id='fixture:worker', expected_version=1,
                    policy=object.__new__(SyntheticPoolPolicy))
        class Text(str): pass
        class SubActor(security.Actor): pass
        actors = [object(), repository.FixtureActor(actor.operator_id),
                  SubActor(actor.operator_id, actor.permissions, actor.session_id, 1)]
        for field, value in [('operator_id', Text(actor.operator_id)), ('session_id', 'bad'),
                             ('auth_epoch', True), ('permissions', set()),
                             ('permissions', frozenset({Text('tasks:manage')})), ('extra', 1)]:
            forged = copy.copy(actor); object.__setattr__(forged, field, value); actors.append(forged)
        forged = copy.copy(actor); object.__delattr__(forged, 'session_id'); actors.append(forged)
        cases = [('actor', x, ErrorCode.UNAUTHENTICATED) for x in actors]
        cases += [(field, value, ErrorCode.INVALID_INPUT) for field, values in {
            'task_id': [None, uuid4(), 'bad', uuid4().hex, Text(args['task_id'])],
            'resource_id': [None, 'bad', uuid4()], 'resource_kind': ['card', 'fixture', Text('mailbox')],
            'owner_id': ['', 'x y', '中', 'a'*129, Text('worker')],
            'expected_version': [None, True, 0, -1, 2**63, '1'], 'policy': [object()]}.items() for value in values]
        with (patch.object(SyntheticPoolPolicy, '_connection', side_effect=AssertionError('policy')) as connection,
              patch.object(psycopg.Cursor, 'execute', side_effect=AssertionError('SQL')) as sql):
            for field, value, code in cases:
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaises(ServiceError) as caught:
                        module().claim(**{**args, field: value})
                    self.assertEqual(caught.exception.code, code)
            connection.assert_not_called(); sql.assert_not_called()

    def test_context_complete_shape_and_ttl_before_sql(self):
        actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
        token = PoolLeaseToken(str(uuid4()), 'mailbox', str(uuid4()), 'fixture:owner', 1, 2)
        context = PoolExecutionContext(actor, token.task_id, Platform.GOOGLE, token)
        class SubContext(PoolExecutionContext): pass
        class SubToken(PoolLeaseToken): pass
        bad = [object(), {}]
        # Bypass construction explicitly: trusted validator must reject subclasses.
        child = object.__new__(SubContext); child.__dict__.update(context.__dict__); bad.append(child)
        for field, value in [('platform', 'google'), ('extra', 1), ('actor', repository.FixtureActor(actor.operator_id))]:
            forged = copy.copy(context); object.__setattr__(forged, field, value); bad.append(forged)
        forged = copy.copy(context); object.__delattr__(forged, 'lease'); bad.append(forged)
        for field, value in [('fence', True), ('credential_version', 0), ('owner_id', 'bad owner'), ('extra', 1)]:
            lease = copy.copy(token); object.__setattr__(lease, field, value)
            forged = copy.copy(context); object.__setattr__(forged, 'lease', lease); bad.append(forged)
        child_token = object.__new__(SubToken); child_token.__dict__.update(token.__dict__)
        forged = copy.copy(context); object.__setattr__(forged, 'lease', child_token); bad.append(forged)
        old = FixtureLeaseToken('mailbox', token.resource_id, token.task_id, token.owner_id, token.fence)
        forged = copy.copy(context); object.__setattr__(forged, 'lease', old); bad.append(forged)
        wrong_enum = str.__new__(Platform, 'google')
        forged = copy.copy(context); object.__setattr__(forged, 'platform', wrong_enum); bad.append(forged)
        with (patch.object(SyntheticPoolPolicy, '_connection', side_effect=AssertionError('policy')) as connection,
              patch.object(psycopg.Cursor, 'execute', side_effect=AssertionError('SQL')) as sql):
            for value in bad:
                for method in (module().assert_current, module().renew):
                    with self.assertRaises(ServiceError) as caught:
                        method(object(), value, policy=object.__new__(SyntheticPoolPolicy))
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for ttl in (True, 4, 61, '30', None):
                with self.assertRaises(ServiceError) as caught:
                    module().renew(object(), context, ttl, policy=object.__new__(SyntheticPoolPolicy))
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            connection.assert_not_called(); sql.assert_not_called()

    def test_existing_consumption_guard_installation_offline(self):
        # Installing these spies must not need settings, PostgreSQL or secret data.
        # This helper check is NOT evidence of the still-pending real-PG matrix.
        with no_consumption() as spies:
            self.assertEqual(len(spies), 5)


class PoolLeaseIntegrationTests(PoolCase):
    def seed_single_task(self, platform='google', identity='UNKNOWN', platform_secret=False, status='QUEUED'):
        """Explicit reconciled synthetic baseline, never production import defaults."""
        mailbox, state, batch, task, config = (str(uuid4()) for _ in range(5))
        email = 'fixture-' + uuid4().hex + '@fixture.invalid'
        with self.uow() as conn:
            secret = self.vault.put_locked(conn, self.actor, PoolResource('mailbox', mailbox),
                                          MailboxCredential(email, password='fixture:mailbox'))
            conn.execute('INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,'
                         'credential_ref,credential_version,health) VALUES(%s,%s,%s,\'outlook\',%s,2,\'HEALTHY\')',
                         (mailbox, self.actor.operator_id, email, secret.id))
            conn.execute('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version,identity_status) '
                         'VALUES(%s,%s,%s,3,%s)', (state, mailbox, platform, identity))
            platform_ref = self.vault.put_locked(conn, self.actor, PoolResource('platform', state),
                            PlatformCredential(email, platform, 'fixture:platform')).id if platform_secret else None
            conn.execute('UPDATE mailbox_platform_states SET credential_ref=%s WHERE id=%s', (platform_ref, state))
            pins = {platform: dict(state_id=state, secret_ref=platform_ref, revision=3, identity_status=identity)}
            conn.execute('INSERT INTO global_configs(id,revision,nonsecret_config,changed_by,scope) '
                         "VALUES(%s,%s,%s,%s,'pool')", (config, 'pool-'+str(uuid4()), Jsonb(FIELDS), self.actor.operator_id))
            conn.execute('INSERT INTO onboarding_batches(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) '
                         "VALUES(%s,'specified',1,%s,%s,%s)", (batch, Jsonb([mailbox]), config, self.actor.operator_id))
            conn.execute('INSERT INTO onboarding_tasks(id,batch_id,config_id,execution_scope,mailbox_id,mailbox_ref,platform,'
                         'platform_plan,credential_version,mailbox_credential_ref,platform_credential_pins,status) '
                         "VALUES(%s,%s,%s,'pool',%s,%s,%s,%s,2,%s,%s,%s)",
                         (task,batch,config,mailbox,mailbox,platform,Jsonb([platform]),secret.id,Jsonb(pins),status))
        return dict(id=task, mailbox=mailbox, state=state, secret=secret.id, platform=platform,
                    platform_secret=platform_ref, pins=pins, email=email, batch=batch, config=config)

    def claim(self, seed, **overrides):
        with no_consumption(), self.uow() as conn:
            return module().claim(conn, overrides.pop('actor', self.actor), seed['id'],
                   overrides.pop('resource_kind', 'mailbox'), overrides.pop('resource_id', seed['mailbox']),
                   overrides.pop('owner_id', 'fixture:owner'), overrides.pop('expected_version', 1),
                   policy=self.vault._policy, **overrides)

    def context(self, token, actor=None, platform='google'):
        return PoolExecutionContext(actor or self.actor, token.task_id, Platform(platform), token)

    def check(self, context, renew=False, ttl=30):
        with no_consumption(), self.uow() as conn:
            if renew: return module().renew(conn, context, ttl, policy=self.vault._policy)
            return module().assert_current(conn, context, policy=self.vault._policy)

    def reject(self, call, code):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code, code)

    def snapshot(self):
        return {name: self.read('SELECT * FROM '+name+' ORDER BY 1') for name in
                ('onboarding_tasks','mailbox_registry','mailbox_platform_states','secret_objects',
                 'resource_leases','operation_receipts','task_steps','audit_events','operator_sessions')}

    def change(self, sql, params=()):
        with self.uow() as conn: conn.execute(sql, params)

    def test_claim_seven_platforms_null_matrix_and_exact_write_set(self):
        for platform in Platform:
            for identity in ('UNKNOWN','NEW_CONFIRMED','EXISTING'):
                with self.subTest(platform=platform.value,identity=identity):
                    seed=self.seed_single_task(platform.value,identity,identity=='EXISTING')
                    before=self.snapshot()
                    with observe() as commands: token=self.claim(seed)
                    self.assertIs(type(token),PoolLeaseToken);self.assertEqual(token.credential_version,2)
                    self.assertEqual(token.fence,1)
                    after=self.snapshot()
                    for name in ('mailbox_registry','mailbox_platform_states','secret_objects','operation_receipts','task_steps','operator_sessions'):
                        self.assertEqual(before[name],after[name])
                    self.assertEqual(self.read('SELECT version,status,generation FROM onboarding_tasks WHERE id=%s',(seed['id'],)),[(2,'QUEUED',1)])
                    self.assertEqual(self.read('SELECT fence,version,hold_reason FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(1,2,'HELD')])
                    self.assertEqual(len(after['audit_events'])-len(before['audit_events']),1)
                    lease_index=next(i for i,(_,q,_) in enumerate(commands) if 'resource_leases' in q and 'FOR UPDATE' in q)
                    secret_index=next(i for i,(_,q,_) in enumerate(commands) if 'secret_objects' in q and 'FOR SHARE' in q)
                    self.assertLess(lease_index,secret_index)
                    self.assertFalse(any('ciphertext' in q or 'nonce' in q for _,q,_ in commands))

    def test_claim_rejections_rollback_and_precedence(self):
        cases=[("UPDATE mailbox_registry SET health='UNKNOWN' WHERE id=%s",'mailbox'),
               ("UPDATE mailbox_registry SET disabled=true WHERE id=%s",'mailbox'),
               ("UPDATE mailbox_registry SET pool_status='EXPORTED' WHERE id=%s",'mailbox')]
        cases += [("UPDATE mailbox_platform_states SET usage_status='%s' WHERE id=%%s"%value,'state')
                  for value in ('RESERVED','SUCCEEDED','FAILED_CONFIRMED','UNKNOWN','CONFLICT','HISTORY_UNRECONCILED')]
        for sql,key in cases:
            seed=self.seed_single_task();self.change(sql,(seed[key],));before=self.snapshot()
            self.reject(lambda:self.claim(seed,expected_version=77),ErrorCode.RECONCILIATION_REQUIRED)
            self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task(identity='EXISTING');before=self.snapshot()
        self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED);self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();before=self.snapshot()
        self.reject(lambda:self.claim(seed,expected_version=2),ErrorCode.VERSION_CONFLICT);self.assertEqual(before,self.snapshot())
        for column in ('expires_at','revoked_at'):
            seed=self.seed_single_task();self.change('UPDATE secret_objects SET '+column+"=clock_timestamp()-interval '1 second' WHERE id=%s",(seed['secret'],));before=self.snapshot()
            self.reject(lambda:self.claim(seed,expected_version=2),ErrorCode.SECRET_UNAVAILABLE);self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();self.change("UPDATE secret_objects SET revoked_at=clock_timestamp()+interval '1 day' WHERE id=%s",(seed['secret'],))
        self.reject(lambda:self.claim(seed),ErrorCode.SECRET_UNAVAILABLE)
        for phase in ('RUNNING','INTENT','UNKNOWN','CONFLICT'):
            seed=self.seed_single_task();self.change('INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,\'fixture\',2,%s)',(str(uuid4()),seed['id'],phase))
            self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED)
        for phase in ('INTENT','UNKNOWN','CONFLICT'):
            seed=self.seed_single_task();self.change('INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) VALUES(%s,%s,\'fixture\',\'fixture\',%s,%s,%s,0,2)',(str(uuid4()),seed['id'],str(uuid4()),'a'*64,phase))
            self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED)

    def test_claim_state_cancel_matrix_and_nonpending_receipt(self):
        for status in repository.STATES:
            for cancel in (False,True):
                seed=self.seed_single_task(status=status)
                self.change('UPDATE onboarding_tasks SET cancel_requested=%s WHERE id=%s',(cancel,seed['id']))
                if status in ('QUEUED','PREFLIGHT') and not cancel: self.claim(seed)
                else: self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED)
        seed=self.seed_single_task()
        self.change("INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,'fixture',1,'NOT_SENT')",(str(uuid4()),seed['id']))
        self.claim(seed)

    def test_empty_released_counter_limits_duplicate_expired_holds(self):
        for column in ('fence','version'):
            seed=self.seed_single_task();self.change('INSERT INTO resource_leases(resource_kind,resource_id,'+column+") VALUES('mailbox',%s,%s)",(seed['mailbox'],MAX))
            before=self.snapshot();self.reject(lambda:self.claim(seed),ErrorCode.VERSION_CONFLICT);self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();self.change('UPDATE onboarding_tasks SET version=%s WHERE id=%s',(MAX,seed['id']))
        self.reject(lambda:self.claim(seed,expected_version=MAX),ErrorCode.VERSION_CONFLICT)
        seed=self.seed_single_task();self.change("INSERT INTO resource_leases(resource_kind,resource_id,fence,version) VALUES('mailbox',%s,4,9)",(seed['mailbox'],))
        token=self.claim(seed);self.assertEqual(token.fence,5)
        self.reject(lambda:self.claim(seed,expected_version=2),ErrorCode.RESOURCE_HELD)
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE resource_id=%s",(seed['mailbox'],))
        before=self.snapshot();self.reject(lambda:self.claim(seed),ErrorCode.RESOURCE_HELD)
        for renew in (False,True):self.reject(lambda:self.check(self.context(token),renew),ErrorCode.STALE_FENCE)
        self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();self.change("INSERT INTO resource_leases(resource_kind,resource_id,lease_until) VALUES('mailbox',%s,clock_timestamp())",(seed['mailbox'],))
        self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED)

    def test_live_status_cancel_hold_resource_and_secret_drift_matrix(self):
        seed=self.seed_single_task(platform_secret=True);token=self.claim(seed);context=self.context(token)
        self.change("UPDATE mailbox_registry SET disabled=true,health='DISABLED',pool_status='QUARANTINED' WHERE id=%s",(seed['mailbox'],))
        self.change("UPDATE mailbox_platform_states SET usage_status='CONFLICT' WHERE id=%s",(seed['state'],))
        self.change("UPDATE secret_objects SET revoked_at=clock_timestamp(),expires_at=clock_timestamp()-interval '1 day'")
        for status in repository.STATES:
            for cancel in (False,True):
                self.change('UPDATE onboarding_tasks SET status=%s,cancel_requested=%s WHERE id=%s',(status,cancel,seed['id']))
                for hold in ('HELD','INTENT','UNKNOWN','CONFLICT'):
                    self.change('UPDATE resource_leases SET hold_reason=%s WHERE resource_id=%s',(hold,seed['mailbox']))
                    if status in ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE'):
                        before=self.snapshot()
                        for renew in (False,True):self.reject(lambda:self.check(context,renew),ErrorCode.RECONCILIATION_REQUIRED)
                        self.assertEqual(before,self.snapshot())
                    else:
                        before=self.snapshot();self.assertIsNone(self.check(context));self.assertEqual(before,self.snapshot())
                        self.assertIsNone(self.check(context,True))
                        self.assertEqual(self.read('SELECT version,generation FROM onboarding_tasks WHERE id=%s',(seed['id'],)),[(2,1)])
                        self.assertEqual(self.read('SELECT fence,hold_reason FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(1,hold)])
        # Existing/null can maintain an already-held binding, not start a new claim.
        self.change("UPDATE onboarding_tasks SET status='WAIT_ADMIN',platform_credential_pins=jsonb_set(jsonb_set(platform_credential_pins,'{google,secret_ref}','null'),'{google,identity_status}','\"EXISTING\"') WHERE id=%s",(seed['id'],))
        self.change("UPDATE mailbox_platform_states SET credential_ref=NULL,identity_status='EXISTING' WHERE id=%s",(seed['state'],))
        self.check(context,True)
        for pool_status in ('EXPORTED', 'QUARANTINED'):
            for usage in ('UNKNOWN', 'CONFLICT'):
                self.change('UPDATE mailbox_registry SET pool_status=%s WHERE id=%s',
                            (pool_status,seed['mailbox']))
                self.change('UPDATE mailbox_platform_states SET usage_status=%s WHERE id=%s',
                            (usage,seed['state']))
                before=self.snapshot();self.check(context);self.assertEqual(before,self.snapshot())
                self.check(context,True)

    def test_current_pin_drift_and_wrong_binding(self):
        changes=[('mailbox_registry','credential_version=4','mailbox'),
                 ('mailbox_registry','credential_ref=OTHER_SECRET','mailbox'),
                 ('mailbox_platform_states','credential_version=4','state'),
                 ('mailbox_platform_states','credential_ref=NULL','state'),
                 ('mailbox_platform_states',"identity_status='EXISTING'",'state')]
        for table,assignment,key in changes:
            seed=self.seed_single_task(platform_secret=True);token=self.claim(seed)
            assignment=assignment.replace('OTHER_SECRET', "'"+seed['platform_secret']+"'")
            self.change('UPDATE '+table+' SET '+assignment+' WHERE id=%s',(seed[key],))
            before=self.snapshot()
            for renew in (False,True):self.reject(lambda:self.check(self.context(token),renew),ErrorCode.VERSION_CONFLICT)
            self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();token=self.claim(seed)
        for field,value,code in [('resource_id',str(uuid4()),ErrorCode.FORBIDDEN),('owner_id','different',ErrorCode.STALE_FENCE),('fence',2,ErrorCode.STALE_FENCE),('credential_version',3,ErrorCode.VERSION_CONFLICT)]:
            altered=copy.copy(token);object.__setattr__(altered,field,value)
            self.reject(lambda:self.check(self.context(altered)),code)
        self.reject(lambda:self.check(self.context(token,platform='claude')),ErrorCode.FORBIDDEN)
        other=self.seed_single_task()
        self.change('UPDATE mailbox_platform_states SET last_task_id=%s WHERE id=%s',(other['id'],seed['state']))
        self.reject(lambda:self.check(self.context(token),True),ErrorCode.RECONCILIATION_REQUIRED)

    def test_renew_ttl_write_set_and_sql_no_secret_lock(self):
        seed=self.seed_single_task();context=self.context(self.claim(seed))
        self.change("UPDATE resource_leases SET stopped_evidence_ref='fixture:preserved' WHERE resource_id=%s",(seed['mailbox'],))
        with self.uow() as conn, observe() as assertions:
            self.assertIsNone(module().assert_current(conn,context,policy=self.vault._policy))
            self.assertTrue(all(q.startswith('SELECT ') for _,q,_ in assertions))
            self.assertEqual(assertions[-1][1],'SELECT clock_timestamp()')
            self.assertFalse(any('secret_objects' in q and 'FOR ' in q for _,q,_ in assertions))
        for ttl in (5,30,60):
            before=self.snapshot()
            with self.uow() as conn,observe() as commands:
                start=conn.execute('SELECT clock_timestamp()').fetchone()[0]
                module().renew(conn,context,ttl,policy=self.vault._policy)
                # Record entry statements before caller's extra clock query.
                entry=list(commands)
                end=conn.execute('SELECT clock_timestamp()').fetchone()[0]
            until=self.read('SELECT lease_until FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],))[0][0]
            self.assertGreaterEqual(until,start+timedelta(seconds=ttl));self.assertLessEqual(until,end+timedelta(seconds=ttl))
            after=self.snapshot()
            for name in before:
                if name not in ('resource_leases','audit_events'):self.assertEqual(before[name],after[name])
            self.assertEqual(len(after['audit_events'])-len(before['audit_events']),1)
            old=next(row for row in before['resource_leases'] if row[1]==seed['mailbox'])
            new=next(row for row in after['resource_leases'] if row[1]==seed['mailbox'])
            self.assertEqual([v for i,v in enumerate(old) if i not in (5,8,10)],
                             [v for i,v in enumerate(new) if i not in (5,8,10)])
            self.assertEqual(entry[-1][1],'SELECT clock_timestamp()')
            self.assertFalse(any('secret_objects' in q and 'FOR SHARE' in q for _,q,_ in entry))
        self.change('UPDATE resource_leases SET version=%s WHERE resource_id=%s',(MAX,seed['mailbox']))
        self.reject(lambda:self.check(context,True),ErrorCode.VERSION_CONFLICT)

    def test_live_auth_not_cached_permissions(self):
        seed=self.seed_single_task();token=self.claim(seed);context=self.context(token)
        original=self.read('SELECT permissions FROM operators WHERE id=%s',(self.actor.operator_id,))[0][0]
        original_deadlines=self.read('SELECT expires_at,idle_expires_at FROM operator_sessions WHERE id=%s',
                                     (self.actor.session_id,))[0]
        cases=[("UPDATE operators SET permissions='{}' WHERE id=%s",(self.actor.operator_id,),ErrorCode.FORBIDDEN,
                'UPDATE operators SET permissions=%s WHERE id=%s',(original,self.actor.operator_id)),
               ('UPDATE operators SET auth_epoch=2 WHERE id=%s',(self.actor.operator_id,),ErrorCode.UNAUTHENTICATED,
                'UPDATE operators SET auth_epoch=1 WHERE id=%s',(self.actor.operator_id,)),
               ('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,),ErrorCode.UNAUTHENTICATED,
                'UPDATE operator_sessions SET revoked_at=NULL WHERE id=%s',(self.actor.session_id,)),
               ("UPDATE operator_sessions SET expires_at=statement_timestamp()-interval '1 second',"
                "idle_expires_at=statement_timestamp()-interval '1 second' WHERE id=%s",
                (self.actor.session_id,),ErrorCode.UNAUTHENTICATED,
                'UPDATE operator_sessions SET expires_at=%s,idle_expires_at=%s WHERE id=%s',
                (*original_deadlines,self.actor.session_id))]
        for sql,params,code,restore,rparams in cases:
            self.change(sql,params)
            try:
                before=self.snapshot()
                for call in (lambda:self.claim(seed,expected_version=77),lambda:self.check(context),lambda:self.check(context,True)):
                    self.reject(call,code)
                self.assertEqual(before,self.snapshot())
            finally:
                self.change(restore,rparams)

    def add_other_task(self,seed,platform='claude'):
        state,task=str(uuid4()),str(uuid4())
        pins={platform:dict(state_id=state,secret_ref=None,revision=3,identity_status='UNKNOWN')}
        self.change('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version) VALUES(%s,%s,%s,3)',(state,seed['mailbox'],platform))
        self.change('INSERT INTO onboarding_tasks(id,batch_id,config_id,execution_scope,mailbox_id,mailbox_ref,platform,platform_plan,credential_version,mailbox_credential_ref,platform_credential_pins) '
                    "VALUES(%s,%s,%s,'pool',%s,%s,%s,%s,2,%s,%s)",
                    (task,seed['batch'],seed['config'],seed['mailbox'],seed['mailbox'],platform,Jsonb([platform]),seed['secret'],Jsonb(pins)))
        return {**seed,'id':task,'state':state,'platform':platform,'pins':pins}

    def test_cross_platform_incumbent_and_two_committed_active_fail_closed(self):
        seed=self.seed_single_task();self.claim(seed);other=self.add_other_task(seed)
        self.reject(lambda:self.claim(other),ErrorCode.RESOURCE_HELD)
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 day' WHERE resource_id=%s",(seed['mailbox'],))
        self.reject(lambda:self.claim(other),ErrorCode.RESOURCE_HELD)
        seed=self.seed_single_task();other=self.add_other_task(seed);before=self.snapshot()
        for task in (seed,other):self.reject(lambda:self.claim(task),ErrorCode.RESOURCE_HELD)
        self.assertEqual(before,self.snapshot())

    def test_same_task_two_spawn_sessions_one_winner(self):
        seed=self.seed_single_task();actors=[]
        for _ in range(2):
            session=str(uuid4())
            self.change('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                        "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '1 hour',clock_timestamp()+interval '10 minutes')",
                        (session,self.actor.operator_id,hashlib.sha256(secrets.token_bytes(32)).hexdigest(),hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
            actors.append((self.actor.operator_id,self.actor.permissions,session,1))
        ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(2);queue=ctx.Queue()
        children=[ctx.Process(target=spawned_claim,args=(self.fixture.schema,actor,seed,barrier,queue)) for actor in actors]
        try:
            for child in children:child.start()
            results=[queue.get(timeout=15) for _ in children]
            for child in children:child.join(5);self.assertEqual(child.exitcode,0)
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join(3)
            queue.close();queue.join_thread()
        self.assertEqual(sorted(row[0] for row in results),['OK','RESOURCE_HELD'])
        self.assertEqual(len({row[1] for row in results}),2)
        print('POOL_LEASE_SPAWN_BACKENDS',sorted(row[1] for row in results))
        winner=next(row for row in results if row[0]=='OK')
        self.assertEqual(self.read('SELECT owner_id,fence,version FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(winner[2],1,2)])
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='lease.claim' AND task_id=%s",(seed['id'],)),[(1,)])

    @contextmanager
    def audit_wait_locker(self):
        # Only the test-owned audit blocker uses the existing schema owner.
        # APP remains append-only; worker/observer never use this connection.
        with self.fixture.migrator() as conn, conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            yield conn

    def wait_across_deadline(self,seed,lock_sql,params,deadline,call,expected,*,audit_lock=False):
        started,done=threading.Event(),threading.Event();result=[];pids=[]
        def worker():
            try:
                with self.uow() as conn:
                    pids.append(conn.info.backend_pid);started.set();call(conn)
                result.append('unexpected_success')
            except ServiceError as error:result.append(error.code)
            except Exception as error:result.append(type(error).__name__)
            finally:done.set()
        if audit_lock:
            self.assertEqual(lock_sql,'LOCK TABLE audit_events IN SHARE MODE')
            self.assertEqual(params,())
        locker_context=self.audit_wait_locker() if audit_lock else self.uow()
        with locker_context as locker,self.fixture.app() as observer:
            locker.execute('SAVEPOINT blocked');locker.execute(lock_sql,params)
            thread=threading.Thread(target=worker);thread.start()
            try:
                self.assertTrue(started.wait(1));observed=False;stop=time.monotonic()+1.4
                while time.monotonic()<stop:
                    blockers,now=observer.execute('SELECT pg_blocking_pids(%s),clock_timestamp()',(pids[0],)).fetchone()
                    observed=observed or locker.info.backend_pid in blockers
                    if observed and now>deadline:break
                    done.wait(.01)
                self.assertTrue(observed,'backend wait not observed');self.assertGreater(now,deadline)
                self.assertFalse(done.is_set());self.assertNotEqual(pids[0],locker.info.backend_pid)
                print('POOL_LEASE_WAIT_BACKENDS',locker.info.backend_pid,pids[0])
            finally:
                locker.execute('ROLLBACK TO SAVEPOINT blocked');thread.join(4)
            self.assertFalse(thread.is_alive());self.assertEqual(result,[expected])

    def test_real_task_mailbox_state_lease_secret_audit_waits_expire_session(self):
        # All six backend waits retain existing 2s lock/5s statement limits.
        for target in ('task','mailbox','state','lease','secret','audit'):
            seed=self.seed_single_task()
            self.change("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s)",(seed['mailbox'],))
            self.change("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '650 milliseconds' WHERE id=%s",(self.actor.session_id,))
            deadline=self.read('SELECT idle_expires_at FROM operator_sessions WHERE id=%s',(self.actor.session_id,))[0][0]
            table,key={'task':('onboarding_tasks','id'),'mailbox':('mailbox_registry','mailbox'),
                       'state':('mailbox_platform_states','state'),'lease':('resource_leases','mailbox'),
                       'secret':('secret_objects','secret'),'audit':('audit_events','id')}[target]
            sql=('LOCK TABLE audit_events IN SHARE MODE' if target=='audit' else
                 'SELECT '+('resource_id' if target=='lease' else 'id')+' FROM '+table+' WHERE '+('resource_id' if target=='lease' else 'id')+'=%s FOR UPDATE')
            params=() if target=='audit' else (seed[key],)
            before=self.snapshot()
            self.wait_across_deadline(seed,sql,params,deadline,
                lambda conn:module().claim(conn,self.actor,seed['id'],'mailbox',seed['mailbox'],'fixture:owner',77,policy=self.vault._policy),
                ErrorCode.UNAUTHENTICATED) if target!='audit' else self.wait_across_deadline(seed,sql,params,deadline,
                lambda conn:module().claim(conn,self.actor,seed['id'],'mailbox',seed['mailbox'],'fixture:owner',1,policy=self.vault._policy),ErrorCode.UNAUTHENTICATED,audit_lock=True)
            self.assertEqual(before,self.snapshot())
            self.change("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s",(self.actor.session_id,))

    def test_real_lease_and_secret_deadlines_after_wait(self):
        seed=self.seed_single_task();token=self.claim(seed);context=self.context(token)
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '650 milliseconds' WHERE resource_id=%s",(seed['mailbox'],))
        deadline=self.read('SELECT lease_until FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],))[0][0]
        self.wait_across_deadline(seed,'SELECT resource_id FROM resource_leases WHERE resource_id=%s FOR UPDATE',(seed['mailbox'],),deadline,
                                 lambda conn:module().renew(conn,context,policy=self.vault._policy),ErrorCode.STALE_FENCE)
        for target in ('secret','audit'):
            seed=self.seed_single_task()
            self.change("UPDATE secret_objects SET expires_at=clock_timestamp()+interval '650 milliseconds' WHERE id=%s",(seed['secret'],))
            deadline=self.read('SELECT expires_at FROM secret_objects WHERE id=%s',(seed['secret'],))[0][0]
            sql='SELECT id FROM secret_objects WHERE id=%s FOR UPDATE' if target=='secret' else 'LOCK TABLE audit_events IN SHARE MODE'
            before=self.snapshot()
            self.wait_across_deadline(seed,sql,(seed['secret'],) if target=='secret' else (),deadline,
                lambda conn:module().claim(conn,self.actor,seed['id'],'mailbox',seed['mailbox'],'fixture:owner',1,policy=self.vault._policy),ErrorCode.SECRET_UNAVAILABLE,audit_lock=(target=='audit'))
            self.assertEqual(before,self.snapshot())

    def test_audit_and_commit_failures_and_actual_commit_unknown_not_replayed(self):
        seed=self.seed_single_task();before=self.snapshot()
        with patch.object(audit,'append',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)) as failed:
            self.reject(lambda:self.claim(seed),ErrorCode.DEPENDENCY_UNAVAILABLE);failed.assert_called_once()
        self.assertEqual(before,self.snapshot())
        # Real transaction COMMIT boundary seam, not a mocked DB/body.
        transaction=psycopg.Connection.transaction
        for durable in (False,True):
            hits=[];responses=[]
            @contextmanager
            def fault(conn,*args,**kwargs):
                with transaction(conn,*args,**kwargs):
                    yield
                    if not durable:
                        hits.append('pre_commit');raise psycopg.IntegrityError('synthetic confirmed rollback')
                if durable:
                    hits.append('post_commit');raise psycopg.OperationalError('synthetic lost reply')
            with patch.object(psycopg.Connection,'transaction',fault):
                try:responses.append(self.claim(seed))
                except ServiceError as error:
                    self.assertEqual(error.code,ErrorCode.COMMIT_UNKNOWN if durable else ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(responses,[]);self.assertEqual(hits,['post_commit' if durable else 'pre_commit'])
            if not durable:self.assertEqual(before,self.snapshot())
            else:
                self.assertEqual(self.read('SELECT fence FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(1,)])
                self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE task_id=%s AND action='lease.claim'",(seed['id'],)),[(1,)])
        # Renew uncertain outcome may be durable too: never claim rollback/replay.
        token=PoolLeaseToken(seed['id'],'mailbox',seed['mailbox'],'fixture:owner',1,2);context=self.context(token)
        version=self.read('SELECT version FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],))[0][0]
        hits.clear()
        with patch.object(psycopg.Connection,'transaction',fault):self.reject(lambda:self.check(context,True),ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(hits,['post_commit'])
        self.assertEqual(self.read('SELECT version FROM resource_leases WHERE resource_id=%s',(seed['mailbox'],)),[(version+1,)])

    def test_final_clock_seams_and_guarded_update_loss_rollback(self):
        # Explicit clock/decoded-row seams, NOT a claim to wait 30 seconds.
        for target in ('session','lease','secret'):
            seed=self.seed_single_task();before=self.snapshot();hit=[];audited=[False]
            def hook(cursor,query,params):
                if query.startswith('INSERT INTO audit_events'):audited[0]=True
            def seam(query,row):
                if not audited[0]:return row
                if target=='session' and 'FROM operator_sessions' in query and row is not None:
                    altered=dict(row);altered['idle_expires_at']=datetime.now(timezone.utc)+timedelta(hours=1)
                    # Internal auth sees still-live snapshot; final clock is moved beyond it.
                    hit.append(altered['idle_expires_at']);return altered
                if query=='SELECT clock_timestamp()' and target=='session' and hit:
                    # First clock belongs to _authenticate, second to final.
                    hit.append('clock')
                    if hit.count('clock')>=2:return (hit[0],)
                if target=='lease' and 'FROM resource_leases' in query and row is not None:
                    altered=list(row);altered[5]=datetime.now(timezone.utc)-timedelta(seconds=1)
                    hit.append('lease');return tuple(altered)
                if target=='secret' and 'FROM secret_objects' in query and row is not None:
                    altered=list(row);altered[6]=datetime.now(timezone.utc)+timedelta(days=1)
                    hit.append('secret');return tuple(altered)
                return row
            with observe(hook=hook,seam=seam):
                self.reject(lambda:self.claim(seed),{'session':ErrorCode.UNAUTHENTICATED,'lease':ErrorCode.STALE_FENCE,'secret':ErrorCode.SECRET_UNAVAILABLE}[target])
            self.assertTrue(hit);self.assertEqual(before,self.snapshot())
        seed=self.seed_single_task();context=self.context(self.claim(seed));before=self.snapshot();hits=[]
        def no_update(query,row):
            if query.startswith('UPDATE resource_leases'):
                hits.append('guarded');return None
            return row
        with observe(seam=no_update):self.reject(lambda:self.check(context,True),ErrorCode.STALE_FENCE)
        self.assertEqual(hits,['guarded']);self.assertEqual(before,self.snapshot())

    def test_malformed_lease_current_and_secret_decoded_rows_fail_closed(self):
        seed=self.seed_single_task()
        self.change("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s)",(seed['mailbox'],))
        mutations=[('SELECT id,owner_operator_id,credential_ref',3,True),
                   ('SELECT id,owner_operator_id,credential_ref',5,'INVALID'),
                   ('SELECT id,mailbox_id,platform,credential_ref',4,False),
                   ('SELECT id,mailbox_id,platform,credential_ref',6,'INVALID'),
                   ('SELECT resource_kind,resource_id',4,True),
                   ('SELECT resource_kind,resource_id',6,'INVALID'),
                   ('SELECT resource_kind,resource_id',7,0),
                   ('SELECT id,kind,revision,key_version,access_policy',2,True)]
        for prefix,index,value in mutations:
            def seam(query,row):
                if query.startswith(prefix) and row is not None:
                    changed=list(row);changed[index]=value;return tuple(changed)
                return row
            before=self.snapshot()
            with observe(seam=seam):self.reject(lambda:self.claim(seed),ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(before,self.snapshot())

    def test_relationship_combined_and_platform_secret_liveness_not_permission(self):
        seed=self.seed_single_task(platform='claude',platform_secret=True)
        self.change("UPDATE secret_objects SET revoked_at=clock_timestamp(),expires_at=clock_timestamp()-interval '1 day' WHERE id=%s",(seed['platform_secret'],))
        token=self.claim(seed);self.check(self.context(token,platform='claude'),True)
        # Real single-task graph with two child pins, not malformed combined JSON.
        seed=self.seed_single_task(platform='claude');child=str(uuid4())
        self.change("INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version) VALUES(%s,%s,'github',3)",(child,seed['mailbox']))
        pins={**seed['pins'],'github':dict(state_id=child,secret_ref=None,revision=3,identity_status='UNKNOWN')}
        self.change("UPDATE onboarding_tasks SET platform='combined',platform_plan=%s,platform_credential_pins=%s WHERE id=%s",
                    (Jsonb(['claude','github']),Jsonb(pins),seed['id']))
        self.reject(lambda:self.claim(seed),ErrorCode.FORBIDDEN)
        seed=self.seed_single_task();self.reject(lambda:self.claim(seed,resource_id=str(uuid4())),ErrorCode.FORBIDDEN)
        self.change('UPDATE onboarding_batches SET created_by=%s WHERE id=%s',(self.actor.operator_id,seed['batch']))
        outsider=str(uuid4())
        self.change("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture','{}')",(outsider,'fixture-'+outsider))
        self.change('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s',(outsider,seed['mailbox']))
        self.reject(lambda:self.claim(seed),ErrorCode.FORBIDDEN)
        seed=self.seed_single_task();self.change('UPDATE mailbox_platform_states SET last_task_id=%s WHERE id=%s',(seed['id'],seed['state']))
        self.change("INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) VALUES(%s,%s,'fixture','fixture',%s,%s,'NOT_SENT',0,1)",
                    (str(uuid4()),seed['id'],str(uuid4()),'b'*64))
        self.claim(seed)

    def test_renew_audit_failure_confirmed_rollback_and_final_policy(self):
        seed=self.seed_single_task();context=self.context(self.claim(seed));before=self.snapshot()
        with patch.object(audit,'append',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)) as failed:
            self.reject(lambda:self.check(context,True),ErrorCode.DEPENDENCY_UNAVAILABLE);failed.assert_called_once()
        self.assertEqual(before,self.snapshot())
        transaction=psycopg.Connection.transaction;hits=[]
        @contextmanager
        def fail_commit(conn,*args,**kwargs):
            with transaction(conn,*args,**kwargs):
                yield
                hits.append('before_commit');raise psycopg.IntegrityError('synthetic rollback')
        with patch.object(psycopg.Connection,'transaction',fail_commit):
            self.reject(lambda:self.check(context,True),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(hits,['before_commit']);self.assertEqual(before,self.snapshot())
        hit=[]
        def redirect(cursor,query,params):
            if query.startswith('INSERT INTO audit_events'):
                hit.append('audit');cursor.connection.execute('SET LOCAL search_path TO '+self.fixture.schema+',pg_catalog,public')
        with observe(hook=redirect):self.reject(lambda:self.check(context,True),ErrorCode.FORBIDDEN)
        self.assertEqual(hit,['audit']);self.assertEqual(before,self.snapshot())

    def test_exact_final_lease_clock_equality_is_expired_seam(self):
        """Synthetic final-clock equality, NOT a real 30-second lock wait."""
        for action in ('claim', 'assert_current', 'renew'):
            with self.subTest(action=action):
                seed=self.seed_single_task()
                context=None if action=='claim' else self.context(self.claim(seed))
                boundary=self.read('SELECT clock_timestamp()')[0][0]+timedelta(minutes=1)
                # Both real session deadlines exceed this synthetic boundary, so
                # the expected STALE_FENCE cannot be an incidental auth failure.
                deadlines=self.read('SELECT expires_at,idle_expires_at FROM operator_sessions WHERE id=%s',
                                    (self.actor.session_id,))[0]
                self.assertTrue(all(deadline>boundary for deadline in deadlines))
                before=self.snapshot();hits={'final_lease':0,'auth_clock':0,'final_clock':0}
                def seam(query,row):
                    if (query.startswith('SELECT resource_kind,resource_id')
                            and 'FROM resource_leases' in query and 'FOR UPDATE' not in query):
                        self.assertIsNotNone(row)
                        changed=list(row);changed[5]=boundary
                        hits['final_lease']+=1
                        return tuple(changed)
                    if query=='SELECT clock_timestamp()' and hits['final_lease']:
                        if hits['auth_clock']==0:
                            hits['auth_clock']+=1
                        else:
                            hits['final_clock']+=1
                            return (boundary,)
                    return row
                with observe(seam=seam):
                    call=(lambda:self.claim(seed)) if action=='claim' else (
                          lambda:self.check(context,action=='renew'))
                    self.reject(call,ErrorCode.STALE_FENCE)
                self.assertEqual(hits,{'final_lease':1,'auth_clock':1,'final_clock':1})
                self.assertEqual(before,self.snapshot())

    def test_missing_live_lease_rejects_assert_and_renew_without_writes(self):
        seed=self.seed_single_task()
        context=self.context(PoolLeaseToken(seed['id'],'mailbox',seed['mailbox'],'fixture:owner',1,2))
        before=self.snapshot()
        for renew in (False,True):
            self.reject(lambda:self.check(context,renew),ErrorCode.STALE_FENCE)
            self.assertEqual(before,self.snapshot())

    def test_claim_last_other_and_combined_heartbeat_reject_without_writes(self):
        # last_task refers to another mailbox's task, so no active-task conflict
        # masks the intended first-claim eligibility gate.
        seed=self.seed_single_task();other=self.seed_single_task()
        self.change('UPDATE mailbox_platform_states SET last_task_id=%s WHERE id=%s',
                    (other['id'],seed['state']))
        before=self.snapshot()
        self.reject(lambda:self.claim(seed),ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(before,self.snapshot())
        # Preserve a genuine held lease while expanding only the synthetic
        # historical graph; no terminal task revival or reverse lock order.
        seed=self.seed_single_task(platform='claude')
        context=self.context(self.claim(seed),platform='claude')
        child=str(uuid4())
        self.change("INSERT INTO mailbox_platform_states(id,mailbox_id,platform,credential_version) "
                    "VALUES(%s,%s,'github',3)",(child,seed['mailbox']))
        pins={**seed['pins'],'github':dict(state_id=child,secret_ref=None,revision=3,identity_status='UNKNOWN')}
        self.change("UPDATE onboarding_tasks SET platform='combined',platform_plan=%s,platform_credential_pins=%s WHERE id=%s",
                    (Jsonb(['claude','github']),Jsonb(pins),seed['id']))
        before=self.snapshot()
        for renew in (False,True):
            self.reject(lambda:self.check(context,renew),ErrorCode.FORBIDDEN)
            self.assertEqual(before,self.snapshot())


    def test_audit_wait_locker_owner_only_app_permissions_unchanged(self):
        # This separate permission test adds no queries to the 650ms wait path.
        with self.uow() as app:
            self.assertEqual(app.info.user,'rf_onboarding_app')
            privileges=app.execute(
                "SELECT has_table_privilege(current_user,'audit_events','SELECT'),"
                "has_table_privilege(current_user,'audit_events','INSERT'),"
                "has_table_privilege(current_user,'audit_events','UPDATE'),"
                "has_table_privilege(current_user,'audit_events','DELETE'),"
                "has_table_privilege(current_user,'audit_events','TRUNCATE')").fetchone()
            self.assertEqual(privileges,(True,True,False,False,False))
            # Nested savepoint rolls back the genuine 42501, not the outer UoW.
            with self.assertRaises(psycopg.errors.InsufficientPrivilege) as denied, app.transaction():
                app.execute('LOCK TABLE audit_events IN SHARE MODE')
            self.assertEqual(denied.exception.sqlstate,'42501')
        with self.audit_wait_locker() as locker,self.fixture.app() as observer,self.uow() as worker:
            self.assertEqual(locker.info.user,'rf_onboarding_migrator')
            self.assertEqual(observer.info.user,'rf_onboarding_app')
            self.assertEqual(worker.info.user,'rf_onboarding_app')
            self.assertEqual(len({locker.info.backend_pid,observer.info.backend_pid,worker.info.backend_pid}),3)
            limits=locker.execute("SELECT current_setting('transaction_isolation'),"
                "current_setting('lock_timeout'),current_setting('statement_timeout'),"
                "current_setting('idle_in_transaction_session_timeout')").fetchone()
            self.assertEqual(limits,('read committed','2s','5s','10s'))
            locker.execute('LOCK TABLE audit_events IN SHARE MODE')
