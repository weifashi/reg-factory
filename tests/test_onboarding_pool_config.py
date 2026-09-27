"""Approved global synthetic configuration contract, real local PostgreSQL."""
import importlib
import importlib.util
import threading
import unittest
from unittest.mock import patch
from onboarding.errors import ErrorCode, ServiceError
from onboarding_pool_support import PoolCase, after_query
from uuid import uuid4

FIELDS = dict(model='fixture-gemini', region='fixture-region', instance_ref='fixture:sub2api',
              group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
              concurrency=1, retention_days=30)


def module(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.pool_config'), 'missing approved pool configuration entry')
    return importlib.import_module('onboarding.pool_config')


class PoolConfigTests(PoolCase):
    def setUp(self):
        super().setUp()
        self.policy = self.vault._policy

    def replace_config(self, expected=None, fields=None, key='fixture:config', **overrides):
        params=dict(settings=self.settings, actor=self.actor, expected_revision=expected,
                    fields=FIELDS.copy() if fields is None else fields, request_key=key,
                    policy=self.policy, mac=self.mac)
        params.update(overrides)
        return module(self).replace(**params)

    def current(self, **overrides):
        params=dict(settings=self.settings, actor=self.actor, policy=self.policy)
        params.update(overrides)
        return module(self).get_current(**params)

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('fixture:canary', str(caught.exception))

    def snapshot(self):
        return {table:self.read('SELECT * FROM '+table+' ORDER BY 1') for table in
                ('global_configs','operation_receipts','audit_events','onboarding_tasks',
                 'onboarding_batches','secret_objects','resource_leases')}

    def test_entry_real_setup_and_read_absence(self):
        self.assertIsNone(self.current())
        result=self.replace_config()
        self.assertEqual(set(result), {'config_id','revision','request_key'})
        self.assertEqual(self.current()['nonsecret_config'], FIELDS)

    def test_public_projection_append_only_and_replay_precedes_cas(self):
        from datetime import datetime, timezone
        from uuid import UUID
        before=self.snapshot()
        first=self.replace_config()
        current=self.current()
        self.assertEqual(set(current),{'id','revision','scope','nonsecret_config','secrets_configured','created_at'})
        self.assertEqual(current['id'],first['config_id']); self.assertEqual(str(UUID(current['id'])),current['id'])
        self.assertEqual(current['scope'],'pool'); self.assertIs(current['secrets_configured'],False)
        stamp=datetime.fromisoformat(current['created_at'])
        self.assertEqual(stamp.utcoffset(),timezone.utc.utcoffset(stamp))
        self.assertEqual(current['created_at'],stamp.isoformat(timespec='microseconds'))
        stored=self.snapshot()
        self.assertEqual(self.replace_config(fields=dict(reversed(list(FIELDS.items())))),first)
        self.assertEqual(self.snapshot(),stored)
        second=self.replace_config(first['revision'],key='fixture:second')
        self.assertNotEqual(first['config_id'],second['config_id']); self.assertNotEqual(first['revision'],second['revision'])
        self.assertEqual(self.replace_config(),first)
        self.assertEqual(self.current()['revision'],second['revision'])
        self.reject(lambda:self.replace_config(key='stale'),ErrorCode.VERSION_CONFLICT)
        self.reject(lambda:self.replace_config(first['revision'],key='stale'),ErrorCode.VERSION_CONFLICT)
        for expected, fields in ((first['revision'],FIELDS), (None,{**FIELDS,'group_ref':'fixture:group-alt'})):
            self.reject(lambda:self.replace_config(expected,fields),ErrorCode.IDEMPOTENCY_CONFLICT)
        for table in ('onboarding_tasks','onboarding_batches','secret_objects','resource_leases'):
            self.assertEqual(self.snapshot()[table],before[table])
        self.assertEqual(self.read('SELECT * FROM global_configs WHERE id=%s',(self.config_id,)),before['global_configs'])
        rows=self.read("SELECT a.object_ref,a.correlation_id,a.task_id,a.outcome_code,a.before_summary,a.after_summary,r.id,r.result_summary FROM audit_events a JOIN operation_receipts r ON r.id::text=a.correlation_id WHERE r.action='pool.config.replace'")
        self.assertEqual(len(rows),2)
        for row in rows:
            self.assertEqual(row[0],row[7]['config_id']); self.assertEqual(row[1],str(row[6]))
            self.assertEqual(row[2:6],(None,'OK',{}, {'version':1}))
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn: conn.execute("UPDATE global_configs SET scope='pool'")
        self.assertEqual(caught.exception.code,ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_strict_service_inputs_and_no_side_effects(self):
        from dataclasses import replace
        from uuid import uuid4
        class Text(str): pass
        before=self.snapshot()
        bad={'expected_revision':[True,1,'pool-'+uuid4().hex,'pool-'+str(uuid4()).upper(),'fixture-old','pool-bad',Text('pool-'+str(uuid4()))],
             'request_key':['',' ', 'x'*129, 'x\n', '中文',Text('key'),None],
             'settings':[object(),replace(self.settings,schema='rf_onboarding')], 'mac':[object()], 'policy':[object()]}
        for name,values in bad.items():
            for value in values:
                with self.subTest(name=name,value=value): self.reject(lambda:self.replace_config(**{name:value}))
        for actor in (self.fixture_actor,object()):
            self.reject(lambda:self.replace_config(actor=actor),ErrorCode.UNAUTHENTICATED)
            self.reject(lambda:self.current(actor=actor),ErrorCode.UNAUTHENTICATED)
        for name in ('settings','policy'):
            self.reject(lambda:self.current(**{name:object()}))
        with self.assertRaises(TypeError): self.current(mac=self.mac)
        self.reject(lambda:self.replace_config('pool-'+str(uuid4()),key='missing'),ErrorCode.VERSION_CONFLICT)
        self.assertEqual(before,self.snapshot())

    def new_actor(self, permissions=('onboarding:read','config:manage')):
        import hashlib
        from uuid import uuid4
        from onboarding.security import Actor
        owner,session=str(uuid4()),str(uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,'fixture',%s)",(owner,'fixture-'+owner,list(permissions)))
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                (session,owner,hashlib.sha256(uuid4().bytes).hexdigest(),hashlib.sha256(uuid4().bytes).hexdigest()))
        return Actor(owner,frozenset(),session,1)

    def test_global_cross_operator_permissions_and_no_read_requirement_to_write(self):
        from dataclasses import replace
        writer=self.new_actor(('config:manage',)); reader=self.new_actor(('onboarding:read',))
        first=self.replace_config(actor=writer)
        self.assertEqual(self.current(actor=reader)['id'],first['config_id'])
        self.reject(lambda:self.current(actor=writer),ErrorCode.FORBIDDEN)
        self.reject(lambda:self.replace_config(actor=reader),ErrorCode.FORBIDDEN)
        second=self.replace_config(first['revision'],key='other-owner')
        self.assertEqual(self.current(actor=reader)['id'],second['config_id'])
        self.assertEqual(self.replace_config(actor=writer),first)
        self.reject(lambda:self.replace_config(first['revision'],actor=writer,key='stale'),ErrorCode.VERSION_CONFLICT)
        self.reject(lambda:self.current(actor=replace(reader,session_id=writer.session_id)),ErrorCode.UNAUTHENTICATED)
        with self.uow() as conn: conn.execute("UPDATE operators SET permissions='{}' WHERE id=%s",(self.actor.operator_id,))
        self.reject(lambda:self.current(),ErrorCode.FORBIDDEN)
        self.reject(lambda:self.replace_config(second['revision'],key='cached'),ErrorCode.FORBIDDEN)

    def test_disabled_epoch_revoked_expired_read_and_write(self):
        before=self.snapshot()
        mutations=(("UPDATE operators SET disabled=true","UPDATE operators SET disabled=false"),
          ("UPDATE operators SET auth_epoch=2","UPDATE operators SET auth_epoch=1"),
          ("UPDATE operator_sessions SET revoked_at=clock_timestamp()","UPDATE operator_sessions SET revoked_at=NULL"),
          ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second'","UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes'"),
          ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '2 seconds',expires_at=clock_timestamp()-interval '1 second'","UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '8 hours'"))
        for mutation,restore in mutations:
            with self.uow() as conn: conn.execute(mutation)
            self.reject(self.current,ErrorCode.UNAUTHENTICATED); self.reject(self.replace_config,ErrorCode.UNAUTHENTICATED)
            with self.uow() as conn: conn.execute(restore)
        self.assertEqual(before,self.snapshot())

    def test_no_aes_vault_read_mac_or_session_renewal(self):
        from onboarding.request_mac import RequestMac
        (self.key_directory/'v1.key').unlink()
        sessions=self.read('SELECT * FROM operator_sessions')
        with patch('onboarding.pool_vault.PoolVault',side_effect=AssertionError('no vault')),patch('onboarding.keyring.Keyring',side_effect=AssertionError('no keyring')):
            first=self.replace_config()
        before=self.snapshot()
        (self.key_directory/'request-mac.key').unlink()
        with patch.object(RequestMac,'__init__',side_effect=AssertionError('no MAC read')):
            self.assertEqual(self.current()['id'],first['config_id'])
        self.assertEqual(before,self.snapshot()); self.assertEqual(sessions,self.read('SELECT * FROM operator_sessions'))

    def test_future_head_is_strictly_monotonic_and_bad_time_rolls_back(self):
        first=self.replace_config()
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET created_at=clock_timestamp()+interval '10 years' WHERE id=%s",(first['config_id'],))
        old=self.current()
        second=self.replace_config(first['revision'],key='future')
        self.assertEqual(self.current()['id'],second['config_id'])
        self.assertGreater(self.current()['created_at'],old['created_at'])
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE global_configs SET created_at='9999-12-31 23:59:59.999999+00' WHERE id=%s",(second['config_id'],))
        before=self.snapshot()
        self.reject(lambda:self.replace_config(second['revision'],key='overflow'),ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(before,self.snapshot())
        with self.fixture.migrator() as conn: conn.execute("UPDATE global_configs SET created_at='infinity' WHERE id=%s",(second['config_id'],))
        self.reject(self.current,ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_canonical_payload_scoped_mac_and_independence(self):
        import json
        from onboarding.request_mac import RequestMac
        calls=[]; original=RequestMac.request_digest
        def observe(instance,action,owner,payload):
            calls.append((action,owner,payload)); return original(instance,action,owner,payload)
        with patch.object(RequestMac,'request_digest',observe): self.replace_config()
        body=json.dumps(dict(v=1,schema=self.settings.schema,instance_marker=self.settings.instance_marker,
                 expected_revision=None,fields=FIELDS),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self.assertIn(('pool.config.replace.v1',self.actor.operator_id,body),calls)
        (self.key_directory/'v1.key').write_bytes((self.key_directory/'request-mac.key').read_bytes())
        self.reject(self.replace_config,ErrorCode.SECRET_UNAVAILABLE)

    def test_bad_db_projection_and_receipt_historical_consistency(self):
        from psycopg.types.json import Jsonb
        from uuid import uuid4
        first=self.replace_config()
        badfields=({**FIELDS,'concurrency':True},{**FIELDS,'model':'fixture:canary'}, {**FIELDS,'extra':'fixture:canary'})
        for fields in badfields:
            with self.fixture.migrator() as conn:
                conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',(Jsonb(fields),first['config_id']))
            self.reject(self.current,ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',(Jsonb(FIELDS),first['config_id']))
        for assignment,restore in (("secret_refs='{"+'"x":"fixture:canary"'+"}'","secret_refs='{}'"),
             ("revision='broken'","revision='"+first['revision']+"'"), ("scope='fixture'","scope='pool'")):
            with self.fixture.migrator() as conn: conn.execute('UPDATE global_configs SET '+assignment+' WHERE id=%s',(first['config_id'],))
            self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
            with self.fixture.migrator() as conn: conn.execute('UPDATE global_configs SET '+restore+' WHERE id=%s',(first['config_id'],))
        other=self.new_actor()
        with self.fixture.migrator() as conn: conn.execute('UPDATE global_configs SET changed_by=%s WHERE id=%s',(other.operator_id,first['config_id']))
        self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        # Different valid hash takes precedence over broken history/summary.
        self.reject(lambda:self.replace_config(fields={**FIELDS,'concurrency':2}),ErrorCode.IDEMPOTENCY_CONFLICT)
        with self.fixture.migrator() as conn: conn.execute('UPDATE global_configs SET changed_by=%s WHERE id=%s',(self.actor.operator_id,first['config_id']))
        second=self.replace_config(first['revision'],fields={**FIELDS,'concurrency':2},key='next')
        for bad in (None,[],{**first,'extra':'fixture:canary'},{**first,'config_id':True},
                    {**first,'revision':'pool-'+str(uuid4())},{**first,'request_key':'wrong'},
                    {**first,'config_id':second['config_id'],'revision':second['revision']}):
            # JSON CHECK excludes nonobjects: these are exercised in the projection seam below.
            if type(bad) is not dict: continue
            with self.fixture.migrator() as conn:
                conn.execute("UPDATE operation_receipts SET result_summary=%s WHERE idempotency_key='fixture:config'",(Jsonb(bad),))
            self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE operation_receipts SET result_summary=%s WHERE idempotency_key='fixture:config'",(Jsonb(first),))
        for assignment,restore in (("generation=2","generation=1"),("fence=1","fence=0"),
             ("phase='FAILED_CONFIRMED'","phase='SUCCEEDED'"),("resource_revision='wrong'","resource_revision='pool-admin:"+self.actor.operator_id+"'")):
            with self.fixture.migrator() as conn: conn.execute("UPDATE operation_receipts SET "+assignment+" WHERE idempotency_key='fixture:config'")
            self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
            with self.fixture.migrator() as conn: conn.execute("UPDATE operation_receipts SET "+restore+" WHERE idempotency_key='fixture:config'")
        with self.fixture.migrator() as conn: conn.execute("UPDATE operation_receipts SET request_hash=%s WHERE idempotency_key='fixture:config'",('0'*64,))
        self.reject(self.replace_config,ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_audit_reached_and_real_sql_check_failure_roll_back(self):
        import psycopg
        from onboarding import audit
        before=self.snapshot(); reached=[]
        def fail(conn,*args,**kwargs):
            reached.append(conn.execute("SELECT count(*) FROM global_configs WHERE scope='pool'").fetchone()[0])
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        with patch.object(audit,'append',fail): self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())
        execute=psycopg.Connection.execute; reached=[]
        def sqlfail(conn,query,*args,**kwargs):
            if isinstance(query,str) and query.startswith('INSERT INTO operation_receipts'):
                reached.append(conn.execute("SELECT count(*) FROM global_configs WHERE scope='pool'").fetchone()[0])
                execute(conn,"INSERT INTO global_configs(id,revision,nonsecret_config,changed_by) VALUES(gen_random_uuid(),'fixture-check','[]',%s)",(self.actor.operator_id,))
            return execute(conn,query,*args,**kwargs)
        with patch.object(psycopg.Connection,'execute',sqlfail): self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())

    def test_final_policy_mac_auth_all_roll_back_and_checksum_preserved(self):
        import secrets
        from onboarding import audit
        from onboarding.settings import BASE
        before=self.snapshot(); append=audit.append
        for mode,code in (('path',ErrorCode.FORBIDDEN),('mac',ErrorCode.SECRET_UNAVAILABLE),('auth',ErrorCode.UNAUTHENTICATED)):
            reached=[]
            def fault(conn,*args,**kwargs):
                result=append(conn,*args,**kwargs); reached.append(1)
                if mode=='path': conn.execute('SET LOCAL search_path TO pg_catalog')
                elif mode=='mac': (self.key_directory/'request-mac.key').write_bytes(secrets.token_bytes(32))
                else: conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
                return result
            with patch.object(audit,'append',fault): self.reject(self.replace_config,code)
            self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())
        path=BASE/(self.settings.schema+'.json'); content=path.read_bytes()
        try:
            path.write_text('{}'); self.reject(self.replace_config,ErrorCode.FORBIDDEN); self.reject(self.current,ErrorCode.FORBIDDEN)
        finally: path.write_bytes(content)
        checksum=self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        try:
            with self.fixture.migrator() as conn: conn.execute("UPDATE schema_migrations SET checksum=%s WHERE version=2",('0'*64,))
            self.reject(self.replace_config,ErrorCode.VERSION_CONFLICT); self.reject(self.current,ErrorCode.VERSION_CONFLICT)
        finally:
            with self.fixture.migrator() as conn: conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=2',(checksum,))
        self.assertEqual(before,self.snapshot())

    def test_real_commit_then_lost_ack_is_unknown_and_explicit_replay_zero_writes(self):
        from contextlib import contextmanager
        import psycopg
        original=psycopg.Connection.transaction; calls=[]
        @contextmanager
        def lostack(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as transaction: yield transaction
            calls.append(conn.info.backend_pid)
            raise psycopg.OperationalError('fixture:wrapper-after-real-commit')
        with patch.object(psycopg.Connection,'transaction',lostack): self.reject(self.replace_config,ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(len(calls),1); self.assertEqual(self.read("SELECT count(*) FROM global_configs WHERE scope='pool'"),[(1,)])
        before=self.snapshot(); result=self.replace_config()
        self.assertEqual(result['config_id'],self.current()['id']); self.assertEqual(before,self.snapshot())

    def test_insert_returning_bad_timestamp_rolls_back_before_receipt(self):
        import psycopg
        execute=psycopg.Connection.execute; reached=[]; before=self.snapshot()
        def corrupt(conn,query,*args,**kwargs):
            cursor=execute(conn,query,*args,**kwargs)
            if isinstance(query,str) and query.startswith('INSERT INTO global_configs'):
                reached.append(1)
                class Cursor:
                    def fetchone(self): return (None,)
                return Cursor()
            return cursor
        with patch.object(psycopg.Connection,'execute',corrupt):
            self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())

    def test_equal_hash_impossible_conflict_after_insert_rolls_back(self):
        import psycopg
        before=self.snapshot(); execute=psycopg.Connection.execute; reached=[]
        def conceal(conn,query,*args,**kwargs):
            cursor=execute(conn,query,*args,**kwargs)
            if isinstance(query,str) and query.startswith('INSERT INTO operation_receipts'):
                self.assertIsNotNone(cursor.fetchone()); reached.append(1)
                class Cursor:
                    def fetchone(self): return None
                return Cursor()
            return cursor
        with patch.object(psycopg.Connection,'execute',conceal): self.reject(self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())

    def test_fixture_creation_ignores_pool_and_pool_shaped_fixture_cannot_lock(self):
        from uuid import uuid4
        from psycopg.types.json import Jsonb
        from onboarding import repository
        self.replace_config()
        with self.uow() as conn:
            task=repository.create_fixture_task(conn,self.actor,'fixture:old-entry',self.config_id)
            self.assertEqual(repository.lock_task(conn,self.actor,task['id'])['id'],task['id'])
            secret,mailbox,state=str(uuid4()),str(uuid4()),str(uuid4())
            conn.execute("INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) VALUES(%s,'mailbox_credential','fixture-key',%s,%s,'fixture:pool')",(secret,b'1'*12,b'fixture-ciphertext-32-byte-value'))
            conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref) VALUES(%s,%s,'shape@fixture.invalid','outlook',%s)",(mailbox,self.actor.operator_id,secret))
            conn.execute("INSERT INTO mailbox_platform_states(id,mailbox_id,platform) VALUES(%s,%s,'google')",(state,mailbox))
            pins={'google':dict(state_id=state,secret_ref=None,revision=1,identity_status='UNKNOWN')}
            conn.execute("UPDATE onboarding_tasks SET execution_scope='pool',mailbox_id=%s,platform='google',platform_plan='[\"google\"]',credential_version=1,mailbox_credential_ref=%s,platform_credential_pins=%s WHERE id=%s",(mailbox,secret,Jsonb(pins),task['id']))
        before=self.snapshot()
        def oldlock():
            with self.uow() as conn: repository.lock_task(conn,self.actor,task['id'])
        self.reject(oldlock,ErrorCode.FORBIDDEN); self.assertEqual(before,self.snapshot())
        current=self.current()
        self.replace_config(current['revision'],fields={**FIELDS,'concurrency':2},key='fixture:pins-stable')
        self.assertEqual(before['onboarding_tasks'],self.snapshot()['onboarding_tasks'])
        self.assertEqual(before['onboarding_batches'],self.snapshot()['onboarding_batches'])
        with self.uow() as conn: newer=repository.create_config(conn,self.actor,__import__('onboarding_b1_support').CONFIG)
        def oldconfig():
            with self.uow() as conn: repository.create_fixture_task(conn,self.actor,'fixture:stale',self.config_id)
        self.reject(oldconfig)
        with self.uow() as conn: repository.create_fixture_task(conn,self.actor,'fixture:new',newer['id'])


    def test_returned_dto_and_input_mutation_do_not_change_storage(self):
        fields=FIELDS.copy(); result=self.replace_config(fields=fields)
        fields.clear(); result['revision']='wrong'; result['config_id']='wrong'
        dto=self.current(); dto['nonsecret_config']['model']='wrong'; dto['changed_by']='fixture:canary'
        self.assertEqual(self.current()['nonsecret_config'],FIELDS)
        self.assertNotIn('changed_by',self.current())
        before=self.snapshot(); sessions=self.read('SELECT * FROM operator_sessions')
        self.replace_config()
        self.assertEqual(before,self.snapshot()); self.assertEqual(sessions,self.read('SELECT * FROM operator_sessions'))

    def test_sql_impossible_projections_fail_closed(self):
        from datetime import datetime
        from uuid import uuid4
        import psycopg
        self.replace_config(); execute=psycopg.Connection.execute
        cases=[('SELECT id,revision,nonsecret_config',i,bad) for i,bad in ((0,None),(1,None),(2,[]),(3,[]),(4,'bad'),(5,None),(5,datetime.now()),(6,'fixture'))]
        cases += [('SELECT id,task_id,scope_operator_id',i,bad) for i,bad in ((0,None),(1,uuid4()),(2,'bad'),(3,'wrong'),(5,True),(6,False),(9,None),(9,'fixture:canary'),(10,None),(10,[]))]
        for prefix,index,bad in cases:
            reached=[]
            def corrupt(conn,query,*args,**kwargs):
                cursor=execute(conn,query,*args,**kwargs)
                if isinstance(query,str) and query.startswith(prefix):
                    row=list(cursor.fetchone()); row[index]=bad; reached.append(1)
                    class Cursor:
                        def fetchone(self): return tuple(row)
                    return Cursor()
                return cursor
            before=self.snapshot()
            with self.subTest(prefix=prefix,index=index,bad=bad),patch.object(psycopg.Connection,'execute',corrupt):
                self.reject(self.current if 'revision,nonsecret' in prefix else self.replace_config,ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())

    def _wait_ttl(self,kind):
        import time
        import psycopg
        before=self.snapshot(); entered=threading.Event(); outcomes=[]; pids=[]
        execute=psycopg.Connection.execute
        # Session row SQL is issued via cursor, so observe the underlying cursor
        # as well as direct execute; actual waiting must be visible in pg_locks.
        cursor_execute=psycopg.Cursor.execute
        def observe_cursor(cursor,query,*args,**kwargs):
            if (kind=='session' and threading.current_thread().name=='pool-ttl-worker'
                    and isinstance(query,str) and 'FROM operator_sessions' in query and 'FOR UPDATE' in query):
                pids.append(cursor.connection.info.backend_pid); entered.set()
            return cursor_execute(cursor,query,*args,**kwargs)
        def observe(conn,query,*args,**kwargs):
            if threading.current_thread().name=='pool-ttl-worker' and isinstance(query,str):
                target=(kind=='guard' and 'pg_advisory_xact_lock(' in query or
                        kind=='audit' and query.startswith('INSERT INTO audit_events'))
                if target: pids.append(conn.info.backend_pid); entered.set()
            return execute(conn,query,*args,**kwargs)
        def worker():
            try: self.replace_config(); outcomes.append('RETURNED')
            except ServiceError as exc: outcomes.append(exc.code)
        with self.uow() as conn: conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '900 milliseconds'")
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            if kind=='session': blocker.execute('SELECT id FROM operator_sessions WHERE id=%s FOR UPDATE',(self.actor.session_id,))
            elif kind=='guard': module(self)._guard_locked(blocker)
            else: blocker.execute('LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE')
            with patch.object(psycopg.Connection,'execute',observe),patch.object(psycopg.Cursor,'execute',observe_cursor):
                thread=threading.Thread(target=worker,name='pool-ttl-worker'); thread.start()
                try:
                    self.assertTrue(entered.wait(.65),'must reach real SQL lock attempt before session expiry')
                    deadline=time.monotonic()+.3; waiting=False
                    while time.monotonic()<deadline:
                        waiting=blocker.execute('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE pid=%s AND NOT granted)',(pids[0],)).fetchone()[0]
                        if waiting: break
                        time.sleep(.01)
                    self.assertTrue(waiting,'real PostgreSQL wait required')
                    self.assertNotEqual(pids[0],blocker.info.backend_pid)
                    time.sleep(1.05)
                finally: blocker.execute('ROLLBACK'); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(outcomes,[ErrorCode.UNAUTHENTICATED]); self.assertEqual(before,self.snapshot())

    def test_real_session_lock_crosses_ttl(self): self._wait_ttl('session')
    def test_real_config_guard_crosses_ttl(self): self._wait_ttl('guard')
    def test_real_audit_lock_crosses_ttl(self): self._wait_ttl('audit')

    def test_guard_shared_exclusive_both_directions_and_strict_transaction(self):
        import time
        from onboarding import storage
        service=module(self)
        with self.fixture.app() as conn:
            self.reject(lambda:service._guard_locked(conn))
        with self.uow() as conn:
            for bad in (None,1,0,'true'): self.reject(lambda:service._guard_locked(conn,shared=bad))
        for held_shared in (False,True):
            entered=threading.Event(); acquired=threading.Event(); pids=[]; outcomes=[]
            def worker():
                try:
                    with self.uow() as conn:
                        pids.append(conn.info.backend_pid); entered.set()
                        service._guard_locked(conn,shared=not held_shared); acquired.set()
                    outcomes.append('OK')
                except ServiceError as exc: outcomes.append(exc.code)
            with self.fixture.app() as blocker:
                blocker.execute('BEGIN')
                service._guard_locked(blocker,shared=held_shared)
                thread=threading.Thread(target=worker); thread.start()
                try:
                    self.assertTrue(entered.wait(1)); deadline=time.monotonic()+.5; waiting=False
                    while time.monotonic()<deadline:
                        waiting=blocker.execute('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE pid=%s AND NOT granted)',(pids[0],)).fetchone()[0]
                        if waiting: break
                        time.sleep(.01)
                    self.assertTrue(waiting); self.assertFalse(acquired.is_set()); self.assertNotEqual(pids[0],blocker.info.backend_pid)
                finally: blocker.execute('ROLLBACK')
            thread.join(5); self.assertFalse(thread.is_alive()); self.assertTrue(acquired.is_set()); self.assertEqual(outcomes,['OK'])

    def test_guard_timeout_is_dependency_no_retry(self):
        service=module(self); before=self.snapshot(); outcomes=[]
        def worker():
            try: self.replace_config(); outcomes.append('RETURNED')
            except ServiceError as exc: outcomes.append(exc.code)
        with self.uow() as blocker:
            service._guard_locked(blocker)
            thread=threading.Thread(target=worker); thread.start(); thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes,[ErrorCode.DEPENDENCY_UNAVAILABLE]); self.assertEqual(before,self.snapshot())

    def test_final_read_policy_and_auth_and_commit_unknown(self):
        import psycopg
        from contextlib import contextmanager
        from onboarding import security
        from onboarding.pool_vault import SyntheticPoolPolicy
        service=module(self); before=self.snapshot(); current=service._current_locked
        def redirect(conn):
            result=current(conn); conn.execute('SET LOCAL search_path TO pg_catalog'); return result
        with patch.object(service,'_current_locked',redirect): self.reject(self.current,ErrorCode.FORBIDDEN)
        def revoke(conn):
            result=current(conn); conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp()'); return result
        with patch.object(service,'_current_locked',revoke): self.reject(self.current,ErrorCode.UNAUTHENTICATED)
        transaction=psycopg.Connection.transaction
        @contextmanager
        def lostack(conn,*args,**kwargs):
            with transaction(conn,*args,**kwargs) as tx: yield tx
            raise psycopg.OperationalError('fixture:read-commit-ack')
        with patch.object(psycopg.Connection,'transaction',lostack): self.reject(self.current,ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(before,self.snapshot())

    def test_digest_and_final_probe_material_change_and_final_expiry(self):
        import time
        import secrets
        from onboarding.request_mac import RequestMac
        from onboarding import audit
        original=RequestMac.request_digest; before=self.snapshot(); reached=[]
        def rotate(instance,action,owner,payload):
            result=original(instance,action,owner,payload)
            if action=='pool.config.replace.v1':
                reached.append(1); (self.key_directory/'request-mac.key').write_bytes(secrets.token_bytes(32))
            return result
        with patch.object(RequestMac,'request_digest',rotate): self.reject(self.replace_config,ErrorCode.SECRET_UNAVAILABLE)
        self.assertEqual(reached,[1]); self.assertEqual(before,self.snapshot())
        audited=[]; append=audit.append
        def expire(conn,*args,**kwargs):
            result=append(conn,*args,**kwargs)
            conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '150 milliseconds'")
            audited.append(1); return result
        def slow(instance,action,owner,payload):
            if audited and action=='pool.config.replace.keycheck.v1': time.sleep(.25)
            return original(instance,action,owner,payload)
        with patch.object(audit,'append',expire),patch.object(RequestMac,'request_digest',slow): self.reject(self.replace_config,ErrorCode.UNAUTHENTICATED)
        self.assertEqual(audited,[1]); self.assertEqual(before,self.snapshot())

    def proof_of(self, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        return caught.exception.code, caught.exception.not_committed

    def test_not_committed_proof_when_revision_displaced(self):
        first = self.replace_config(None, key='fixture:first')
        self.replace_config(first['revision'], key='fixture:second')
        before = self.business_snapshot()
        self.assertTrue(self.proof(lambda: self.replace_config(first['revision'], key='fixture:stale')))
        self.assertTrue(self.proof(lambda: self.replace_config(None, key='fixture:late-first')),
                        'a first-config request after another config exists can never apply')
        self.assertEqual(before, self.business_snapshot())

    def test_no_proof_when_expected_revision_names_no_existing_config(self):
        self.assertFalse(self.proof(lambda: self.replace_config('pool-' + str(uuid4()), key='fixture:ghost')))

    def test_committed_original_replays_its_result(self):
        result = self.replace_config(None, key='fixture:done')
        self.assertEqual(self.replace_config(None, key='fixture:done'), result)

    def test_no_proof_under_migration_drift(self):
        first = self.replace_config(None, key='fixture:first')
        self.replace_config(first['revision'], key='fixture:second')
        checksum = self.drift_migrations()
        try:
            self.assertFalse(self.proof(lambda: self.replace_config(first['revision'], key='fixture:drift')))
        finally:
            self.set_migration_checksum(checksum)

    def test_no_proof_when_migration_commits_before_tail_checks(self):
        first = self.replace_config(None, key='fixture:first')
        self.replace_config(first['revision'], key='fixture:second')
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        fired = []
        def hook(cursor, text):
            if not fired and 'idempotency_key' in text:
                fired.append(True)
                self.set_migration_checksum('0' * 64)
        try:
            with after_query(hook):
                flagged = self.proof(lambda: self.replace_config(first['revision'], key='fixture:race'))
        finally:
            self.set_migration_checksum(checksum)
        self.assertTrue(fired)
        self.assertFalse(flagged)

    def test_mac_rotation_after_commit_is_idempotency_conflict_without_proof(self):
        self.replace_config(None, key='fixture:mac')
        self.mac = self.rotate_request_mac()
        self.assertEqual(self.proof_of(lambda: self.replace_config(None, key='fixture:mac')),
                         (ErrorCode.IDEMPOTENCY_CONFLICT, False))

    def test_queued_original_never_commits_after_proof(self):
        first = self.replace_config(None, key='fixture:first')
        self.replace_config(first['revision'], key='fixture:second')
        outcome = self.queued_original_never_commits(
            lambda: self.replace_config(first['revision'], key='fixture:r0'),
            lambda: self.replace_config(first['revision'], key='fixture:r0'))
        self.assertEqual(outcome[0], ErrorCode.VERSION_CONFLICT.value)

    def test_replay_behind_committing_original_returns_it_without_proof(self):
        other = self.second_session()
        original, replay = self.race_original('pg_advisory_xact_lock(',
            lambda: self.replace_config(None, key='fixture:lock'),
            lambda: self.replace_config(None, key='fixture:lock', actor=other))
        self.assertEqual(original[0], 'OK')
        self.assertEqual(replay, original)

    def test_replay_behind_rolled_back_original_applies_normally(self):
        other = self.second_session()
        original, replay = self.race_original('pg_advisory_xact_lock(',
            lambda: self.replace_config(None, key='fixture:rollback'),
            lambda: self.replace_config(None, key='fixture:rollback', actor=other), rollback=True)
        self.assertNotEqual(original[0], 'OK')
        self.assertEqual(replay[0], 'OK')
        # B1Case pre-seeds one fixture-scope config; only pool configs are this race's writes.
        self.assertEqual(self.read("SELECT count(*) FROM global_configs WHERE scope='pool'")[0][0], 1)


class PoolConfigInputTests(unittest.TestCase):
    def test_strict_pure_fields(self):
        service=module(self)
        class Mapping(dict): pass
        for value in (None, [], Mapping(FIELDS), {}, {**FIELDS,'extra':1}, {**FIELDS,'concurrency':True},
                      {**FIELDS,'model':'real-model'}, {**FIELDS,'instance_ref':'fixture:unknown'}):
            with self.subTest(value=value), self.assertRaises(ServiceError) as caught:
                service._inputs(None, value, 'fixture:key')
            self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)

    def test_all_exact_fields_ranges_tokens(self):
        service=module(self)
        class Text(str): pass
        class Number(int): pass
        matrix={name:[None,False,1,Text(FIELDS[name]),'fixture-', 'fixture-A', 'fixture-a'+'x'*64,'fixture-a\n'] for name in ('model','region','project_prefix')}
        matrix.update(instance_ref=[None,'fixture:other',Text('fixture:sub2api')],group_ref=[None,'fixture:unknown',Text('fixture:group')])
        for name,minimum,maximum in (('timeout_seconds',30,86400),('concurrency',1,32),('retention_days',1,365)):
            matrix[name]=[True,False,1.0,'1',None,Number(minimum),minimum-1,maximum+1]
            for value in (minimum,maximum): self.assertEqual(service._inputs(None,{**FIELDS,name:value},'x')[name],value)
        for name,values in matrix.items():
            for value in values:
                with self.subTest(name=name,value=value),self.assertRaises(ServiceError) as caught:
                    service._inputs(None,{**FIELDS,name:value},'x')
                self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)
        for name in FIELDS:
            with self.assertRaises(ServiceError): service._inputs(None,{k:v for k,v in FIELDS.items() if k!=name},'x')
        self.assertEqual(service._inputs(None,{**FIELDS,'group_ref':'fixture:group-alt'},'x')['group_ref'],'fixture:group-alt')
        self.assertEqual(service._inputs(None,{**FIELDS,'model':'fixture-a'+'x'*63},'x')['model'],'fixture-a'+'x'*63)

    def test_caller_mutation_during_and_after_validation_is_not_retained(self):
        service=module(self)
        source=FIELDS.copy(); validating=threading.Event(); mutated=threading.Event(); ids=[]
        original=service._TOKEN
        class Token:
            def fullmatch(inner,value):
                if not validating.is_set():
                    validating.set(); self.assertTrue(mutated.wait(2))
                return original.fullmatch(value)
        def mutate():
            if validating.wait(2):
                ids.append(threading.get_ident()); source['concurrency']=True; source['extra']='fixture:canary'; mutated.set()
        worker=threading.Thread(target=mutate); worker.start()
        try:
            with patch.object(service,'_TOKEN',Token()): result=service._inputs(None,source,'fixture:mutation')
        finally: validating.set(); worker.join(3)
        self.assertFalse(worker.is_alive()); self.assertEqual(result,FIELDS); self.assertIsNot(result,source)
        self.assertNotEqual(ids,[threading.get_ident()])
        source=FIELDS.copy(); result=service._inputs(None,source,'fixture:after')
        worker=threading.Thread(target=source.clear); worker.start(); worker.join(3)
        self.assertEqual(result,FIELDS); self.assertEqual(source,{})
