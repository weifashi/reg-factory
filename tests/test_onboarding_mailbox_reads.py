"""Real app transactions for synthetic owner-only mailbox observations."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import importlib
import json
import secrets
import threading
import time
from uuid import uuid4
from unittest.mock import patch
import psycopg
from psycopg.types.json import Jsonb

from onboarding import mailboxes, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding_pool_support import PoolCase

FIELDS = {'id','email','source_type','group_ref','health','disabled','ever_registration_attempted',
          'sale_eligibility','pool_status','occupied','version','created_at','updated_at','last_used_at','platforms'}
PLATFORMS = ('google','claude','chatgpt','grok','kiro','github','k12')


class MailboxReadTests(PoolCase):
    def setUp(self):
        super().setUp()
        self.api=importlib.import_module('onboarding.mailbox_read')
        self.policy=self.vault._policy

    def page(self, **kwargs):
        self.assertTrue(hasattr(self.api,'list_page'),'missing approved list_page')
        return self.api.list_page(self.settings,self.actor,policy=self.policy,mac=self.mac,**kwargs)

    def seed(self, *names, group='fixture:group'):
        text=json.dumps([{'email':name+'@fixture.invalid','password':'fixture:mailbox-read-canary','provider':'outlook'} for name in names])
        preview=mailboxes.preview_import(self.settings,self.actor,text,group,vault=self.vault,mac=self.mac)
        return mailboxes.import_text(self.settings,self.actor,text,group,preview['preview_digest'],
                                     'fixture:'+uuid4().hex,vault=self.vault,mac=self.mac).created_ids

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code,code)

    def counts(self):
        return [self.read('SELECT count(*) FROM '+table) for table in ('mailbox_registry',
            'mailbox_platform_states','secret_objects','audit_events','operation_receipts','resource_leases')]

    def test_empty_page_frozen_no_writes_or_session_refresh(self):
        before=self.counts()
        session=self.read('SELECT version,idle_expires_at,updated_at FROM operator_sessions')
        page=self.page()
        self.assertEqual((page.items,page.next_cursor),((),None))
        self.assertIs(type(page),self.api.Page)
        with self.assertRaises(FrozenInstanceError):
            page.next_cursor='bad'
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.read('SELECT version,idle_expires_at,updated_at FROM operator_sessions'),session)

    def test_tied_created_at_keyset_desc_once_each_and_real_summary(self):
        ids=self.seed('one','two','three')
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET created_at='2026-09-24 01:02:03.123456+00'")
        seen=[]
        page=self.page(limit=1)
        while True:
            self.assertEqual(len(page.items),1)
            row=page.items[0]
            seen.append(row['id'])
            self.assertEqual(set(row),FIELDS)
            self.assertEqual(row['created_at'],'2026-09-24T01:02:03.123456Z')
            self.assertEqual(tuple(p['platform'] for p in row['platforms']),PLATFORMS)
            self.assertEqual(type(row['platforms']),tuple)
            for state in row['platforms']:
                self.assertEqual(set(state),{'platform','identity_status','usage_status','version','checked_at'})
                self.assertEqual(state['usage_status'],'HISTORY_UNRECONCILED')
                self.assertEqual(state['identity_status'],'UNKNOWN')
            if page.next_cursor is None:
                break
            page=self.page(limit=1,cursor=page.next_cursor)
        self.assertEqual(seen,sorted(ids,reverse=True))
        self.assertEqual(len(set(seen)),3)

    def test_filters_are_literal_preserve_spaces_and_owner_only(self):
        ids=self.seed('one%_','onetwo','other')
        other=str(uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,'fixture')",(other,'fixture-'+other))
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s',(other,ids[2]))
            conn.execute("UPDATE mailbox_registry SET group_ref='',health='HEALTHY',disabled=true,ever_registration_attempted=true,sale_eligibility='INELIGIBLE' WHERE id=%s",(ids[0],))
        self.assertEqual({r['id'] for r in self.page().items},set(ids[:2]))
        self.assertEqual([r['id'] for r in self.page(search='%_').items],[ids[0]])
        self.assertEqual(len(self.page(search='ONE').items),2)
        self.assertEqual(self.page(search=' one').items,())
        row=self.page(group_ref='',health='HEALTHY',occupied=False).items[0]
        self.assertTrue(row['disabled'])
        self.assertTrue(row['ever_registration_attempted'])
        self.assertEqual(row['sale_eligibility'],'INELIGIBLE')
        self.assertEqual(row['health'],'HEALTHY')
        self.assertEqual(self.page(group_ref='none').items,())

    def test_platform_missing_is_not_fabricated_or_written_no_unused_filter(self):
        ids=self.seed('one','two')
        with self.fixture.migrator() as conn:
            conn.execute('DELETE FROM mailbox_platform_states WHERE mailbox_id=%s',(ids[1],))
            conn.execute("UPDATE mailbox_platform_states SET identity_status='EXISTING',usage_status='SUCCEEDED',version=9,checked_at='2026-09-24 01:02:03.123456+00' WHERE platform='google'")
        before=self.counts()
        allrows={r['id']:r for r in self.page().items}
        self.assertEqual(allrows[ids[1]]['platforms'],())
        page=self.page(platform='google')
        self.assertEqual(len(page.items),1)
        self.assertEqual(page.items[0]['platforms'],({'platform':'google','identity_status':'EXISTING','usage_status':'SUCCEEDED','version':9,'checked_at':'2026-09-24T01:02:03.123456Z'},))
        self.assertEqual(self.counts(),before)

    def test_occupied_expired_lease_with_terminal_task_still_held_null_task_not_held(self):
        ids=self.seed('one','two')
        task=self.task()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='SUCCEEDED' WHERE id=%s",(task['id'],))
            conn.execute("INSERT INTO resource_leases(resource_kind,resource_id,task_id,owner_id,lease_until) VALUES('mailbox',%s,%s,NULL,clock_timestamp()-interval '1 hour')",(ids[0],task['id']))
            conn.execute("INSERT INTO resource_leases(resource_kind,resource_id,task_id) VALUES('mailbox',%s,NULL)",(ids[1],))
        self.assertEqual([r['id'] for r in self.page(occupied=True).items],[ids[0]])
        self.assertEqual([r['id'] for r in self.page(occupied=False).items],[ids[1]])

    def test_occupied_pool_task_fallback_and_terminal_release(self):
        identity=self.seed('one')[0]
        task=self.task()
        ref=self.read('SELECT credential_ref FROM mailbox_registry WHERE id=%s',(identity,))[0][0]
        state=self.read("SELECT id::text FROM mailbox_platform_states WHERE mailbox_id=%s AND platform='google'",(identity,))[0][0]
        pins={'google':{'state_id':state,'secret_ref':None,'revision':1,'identity_status':'UNKNOWN'}}
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET execution_scope='pool',mailbox_id=%s,platform='google',platform_plan=%s,credential_version=1,mailbox_credential_ref=%s,platform_credential_pins=%s WHERE id=%s",(identity,Jsonb(['google']),ref,Jsonb(pins),task['id']))
        self.assertTrue(self.page().items[0]['occupied'])
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='CANCELLED_SAFE' WHERE id=%s",(task['id'],))
        self.assertFalse(self.page().items[0]['occupied'])

    def test_no_active_aes_needed_and_no_vault_construction(self):
        self.seed('one')
        (self.key_directory/'v1.key').unlink()
        with patch('onboarding.pool_vault.PoolVault',side_effect=AssertionError('no vault')):
            self.assertEqual(len(self.page().items),1)

    def test_exact_dependency_types_and_fixture_actor_rejected(self):
        for name,value in (('settings',object()),('policy',object()),('mac',object())):
            kw=dict(settings=self.settings,actor=self.actor,policy=self.policy,mac=self.mac)
            kw[name]=value
            self.reject(lambda:self.api.list_page(**kw))
        self.reject(lambda:self.api.list_page(replace(self.settings,schema='rf_onboarding'),self.actor,policy=self.policy,mac=self.mac))
        self.reject(lambda:self.api.list_page(self.settings,self.fixture_actor,policy=self.policy,mac=self.mac),ErrorCode.UNAUTHENTICATED)

    def test_database_permissions_not_actor_cache(self):
        actor=self.actor
        self.actor=replace(actor,permissions=frozenset())
        self.assertEqual(self.page().items,())
        self.actor=actor
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'onboarding:read') WHERE id=%s",(actor.operator_id,))
        self.reject(self.page,ErrorCode.FORBIDDEN)

    def test_disabled_epoch_revocation_and_expiry(self):
        for mutation,restore in (("UPDATE operators SET disabled=true","UPDATE operators SET disabled=false"),
                ("UPDATE operators SET auth_epoch=2","UPDATE operators SET auth_epoch=1"),
                ("UPDATE operator_sessions SET revoked_at=clock_timestamp()","UPDATE operator_sessions SET revoked_at=NULL"),
                ("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second'","UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '30 minutes'")):
            with self.uow() as conn:
                conn.execute(mutation)
            self.reject(self.page,ErrorCode.UNAUTHENTICATED)
            with self.uow() as conn:
                conn.execute(restore)

    def test_one_safe_data_select_and_zero_writes(self):
        self.seed('one','two')
        observed=[]
        execute=psycopg.Connection.execute
        def trace(conn, query, *args, **kwargs):
            if isinstance(query,str):
                observed.append(query)
            return execute(conn,query,*args,**kwargs)
        before=self.counts()
        with patch.object(psycopg.Connection,'execute',trace):
            page=self.page(limit=1)
        self.assertEqual(self.counts(),before)
        data=[q for q in observed if 'mailbox_registry' in q]
        self.assertEqual(len(data),1)
        for forbidden in ('SELECT *','row_to_json','credential_ref','source_fingerprint','secret_objects','evidence_ref','last_task_id'):
            self.assertNotIn(forbidden,data[0])
        for q in observed:
            self.assertFalse(q.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')))
        self.assertNotIn('fixture:mailbox-read-canary',repr(page))

    def test_final_mac_key_replacement_fails_closed(self):
        self.seed('one')
        original=SyntheticPoolPolicy._connection
        calls=[]
        def change(policy,conn):
            result=original(policy,conn)
            calls.append(1)
            if len(calls)==2:
                (self.key_directory/'request-mac.key').write_bytes(secrets.token_bytes(32))
            return result
        with patch.object(SyntheticPoolPolicy,'_connection',change):
            self.reject(self.page,ErrorCode.SECRET_UNAVAILABLE)
        self.assertEqual(len(calls),2)

    def test_bad_database_control_text_and_infinite_time_fail_closed(self):
        identity=self.seed('one')[0]
        for query in ("UPDATE mailbox_registry SET group_ref='fixture:'||chr(8205)",
                      "UPDATE mailbox_registry SET group_ref='',created_at='infinity'"):
            with self.uow() as conn:
                conn.execute(query)
            self.reject(self.page,ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_commit_unknown_propagates_once(self):
        original=storage.unit_of_work
        calls=[]
        @contextmanager
        def unknown(settings):
            calls.append(1)
            with original(settings) as conn:
                yield conn
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage,'unit_of_work',unknown):
            self.reject(self.page,ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(calls,[1])

    def test_actual_table_read_wait_crosses_ttl_and_never_returns_stale_page(self):
        self.seed('one')
        outcomes=[]
        started=threading.Event()
        reader_pid=[]
        execute=psycopg.Connection.execute
        def observe(conn,query,*args,**kwargs):
            if isinstance(query,str) and 'mailbox_registry' in query and threading.current_thread().name=='fixture-mailbox-reader':
                reader_pid.append(conn.info.backend_pid)
                started.set()
            return execute(conn,query,*args,**kwargs)
        def reader():
            try:
                self.page()
                outcomes.append('RETURNED')
            except ServiceError as exc:
                outcomes.append(exc.code)
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            blocker.execute('LOCK TABLE mailbox_registry IN ACCESS EXCLUSIVE MODE')
            with self.uow() as conn:
                conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '900 milliseconds'")
            with patch.object(psycopg.Connection,'execute',observe):
                worker=threading.Thread(target=reader,name='fixture-mailbox-reader')
                worker.start()
                try:
                    self.assertTrue(started.wait(1),'reader did not reach real data SELECT')
                    deadline=time.monotonic()+0.5
                    blocked=False
                    while time.monotonic()<deadline:
                        wait=blocker.execute("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE pid=%s AND NOT granted AND relation='mailbox_registry'::regclass)",(reader_pid[0],)).fetchone()
                        if wait and wait[0] is True:
                            blocked=True
                            break
                        time.sleep(0.01)
                    self.assertTrue(blocked,'PostgreSQL must confirm a real Lock wait')
                    time.sleep(1.05)
                finally:
                    blocker.execute('ROLLBACK')
                    worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes,[ErrorCode.UNAUTHENTICATED])

    def test_strict_filters_and_lowercase_expansion(self):
        self.assertEqual(self.page(search='İ'*320).items,())
        for field,values in {'platform':['combined','GOOGLE',1], 'search':[None,'x'*321,'a\n','\u200b'],
                             'group_ref':['x'*129,1], 'health':['healthy'], 'occupied':[1],
                             'limit':[True,0,101], 'cursor':[True,'x'*513]}.items():
            for value in values:
                self.reject(lambda:self.page(**{field:value}))
        with self.assertRaises(TypeError):
            self.page(owner_id=self.actor.operator_id)

    def test_cursor_filter_change_rejected_real_page(self):
        self.seed('one','two')
        first=self.page(limit=1)
        self.reject(lambda:self.page(limit=2,cursor=first.next_cursor))
        self.reject(lambda:self.page(limit=1,platform='google',cursor=first.next_cursor))
        self.assertEqual(len(self.page(limit=1,cursor=first.next_cursor).items),1)
