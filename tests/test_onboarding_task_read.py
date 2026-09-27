"""Safe task observation contracts, offline input tests and separate real PG tests."""
import importlib
import importlib.util
import inspect
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from uuid import uuid4

from onboarding import pool_config, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
import onboarding_pool_support
import test_onboarding_pool_batches as batches
from test_onboarding_pool_preflight import context


class TaskReadInputTests(unittest.TestCase):
    def test_new_entry_points_exist(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.task_read'), 'task_read missing')
        api = importlib.import_module('onboarding.task_read')
        for name in ('locate_scope', 'read_pool', 'list_page'):
            self.assertTrue(callable(getattr(api, name, None)), name + ' missing')

    def test_config_permission_is_fixed_and_rejected_before_dependencies(self):
        self.assertIn('permission', inspect.signature(pool_config.get_current).parameters)
        args = context(); args.pop('mac')
        with patch.object(storage, 'open_app', side_effect=AssertionError('SQL')), \
             patch.object(SyntheticPoolPolicy, '_validate', side_effect=AssertionError('policy')):
            for permission in ('tasks:manage', None, True, '', ['onboarding:read']):
                with self.assertRaises(ServiceError) as caught:
                    pool_config.get_current(**args, permission=permission)
                self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_task_inputs_fail_before_dependencies(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.task_read'), 'task_read missing')
        api = importlib.import_module('onboarding.task_read'); args = context()
        single = {key:value for key,value in args.items() if key != 'mac'}
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                     for owner, name in ((storage, 'open_app'), (SyntheticPoolPolicy, '_validate'),
                                         (RequestMac, 'request_digest'))]
            for name in ('locate_scope', 'read_pool'):
                for task_id in ('bad', uuid4().hex, uuid4(), None, True):
                    with self.assertRaises(ServiceError) as caught:
                        getattr(api, name)(**single, task_id=task_id)
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for permission in ('config:manage', None, '', True):
                with self.assertRaises(ServiceError) as caught:
                    api.locate_scope(**single, task_id=str(uuid4()), permission=permission)
                self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
            for changes in ({'scope':'other'}, {'scope':True}, {'batch_id':'bad'}, {'limit':True},
                            {'limit':0}, {'limit':101}, {'cursor':'x'*513}):
                with self.assertRaises(ServiceError) as caught:
                    api.list_page(**args, **changes)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for spy in spies:
                spy.assert_not_called()


class TaskReadTests(onboarding_pool_support.PoolCase):
    config = batches.PoolBatchEntryTests.config
    seed = batches.PoolBatchEntryTests.seed
    change = batches.PoolBatchEntryTests.change
    reject = batches.PoolBatchEntryTests.reject
    snapshot = batches.PoolBatchEntryTests.snapshot

    def api(self):
        return importlib.import_module('onboarding.task_read')

    def create(self):
        from onboarding import pool_batches
        config = self.config(); seed = self.seed()
        return pool_batches.create(self.settings, self.actor, 'specified', 1, [seed['mailbox']],
            config['revision'], 'fixture:batch:'+uuid4().hex, policy=self.vault._policy, mac=self.mac)

    def detail(self, tid):
        return self.api().read_pool(self.settings, self.actor, tid, policy=self.vault._policy)

    def page(self, **changes):
        return self.api().list_page(self.settings, self.actor, policy=self.vault._policy, mac=self.mac, **changes)

    def test_historical_detail_exact_keys_and_no_current_head_dependency(self):
        batch = self.create(); self.config(); before = self.snapshot()
        with batches.observe() as queries:
            result = self.detail(batch['task_ids'][0])
        self.assertEqual(set(result), {'id','execution_scope','batch_id','config_id','config_revision','status',
            'version','reason_code','current_step','generation','cancel_requested','synthetic'})
        self.assertEqual(result['config_revision'], batch['config_revision'])
        task_lock = next(i for i,(_,q,_) in enumerate(queries) if 'FOR UPDATE OF t' in q)
        session_lock = next(i for i,(_,q,_) in enumerate(queries) if 'FROM operator_sessions' in q)
        self.assertLess(task_lock, session_lock); self.assertEqual(before, self.snapshot())

    def test_scope_locator_own_fixture_pool_and_tasks_only(self):
        fixture = self.task(); batch = self.create()
        self.change("UPDATE operators SET permissions=ARRAY['tasks:manage'] WHERE id=%s", (self.actor.operator_id,))
        for tid, scope in ((fixture['id'], 'fixture'), (batch['task_ids'][0], 'pool')):
            self.assertEqual(self.api().locate_scope(self.settings, self.actor, tid,
                policy=self.vault._policy, permission='tasks:manage'), scope)
            self.reject(lambda: self.api().locate_scope(self.settings, self.actor, tid,
                policy=self.vault._policy), ErrorCode.FORBIDDEN)

    def test_config_only_can_read_config_without_onboarding_read(self):
        self.config()
        self.change("UPDATE operators SET permissions=ARRAY['config:manage'] WHERE id=%s", (self.actor.operator_id,))
        self.assertIsNotNone(pool_config.get_current(self.settings, self.actor, policy=self.vault._policy,
                                                    permission='config:manage'))
        self.reject(lambda: pool_config.get_current(self.settings, self.actor, policy=self.vault._policy),
                    ErrorCode.FORBIDDEN)

    def test_page_keyset_filters_safe_fields_and_cursor_binding(self):
        fixture = self.task(); first = self.create(); second = self.create()
        page = self.page(limit=1); seen = [page.items[0]['id']]
        while page.next_cursor:
            page = self.page(limit=1, cursor=page.next_cursor); seen.extend(item['id'] for item in page.items)
        self.assertEqual(set(seen), {fixture['id'],first['task_ids'][0],second['task_ids'][0]})
        self.assertEqual(len(seen),3)
        self.assertEqual(len(self.page(scope='pool').items),2)
        self.assertEqual(len(self.page(scope='fixture').items),1)
        self.assertEqual(len(self.page(batch_id=first['batch_id']).items),1)
        token = self.page(limit=1).next_cursor
        self.reject(lambda:self.page(scope='pool',limit=1,cursor=token),ErrorCode.INVALID_INPUT)
        self.reject(lambda:self.page(limit=2,cursor=token),ErrorCode.INVALID_INPUT)
        for item in self.page().items:
            self.assertEqual(set(item), {'id','execution_scope','batch_id','config_id','config_revision','status',
                'version','reason_code','current_step','generation','cancel_requested','synthetic','created_at'})

    def test_scope_change_after_locator_not_retried_as_another_service(self):
        batch = self.create(); tid = batch['task_ids'][0]
        self.assertEqual(self.api().locate_scope(self.settings,self.actor,tid,policy=self.vault._policy),'pool')
        self.change("UPDATE onboarding_tasks SET execution_scope='fixture',mailbox_id=NULL,platform=NULL,"
                    "credential_version=NULL,mailbox_credential_ref=NULL,platform_plan='[]',platform_credential_pins='{}' "
                    'WHERE id=%s', (tid,))
        self.reject(lambda:self.detail(tid),ErrorCode.FORBIDDEN)


    def other_owner(self):
        import hashlib
        from onboarding import security
        oid, sid = str(uuid4()), str(uuid4())
        self.change("INSERT INTO operators(id,username_norm,password_hash,permissions) "
                    "VALUES(%s,%s,'fixture-only',ARRAY['onboarding:read','tasks:manage'])",(oid,'fixture-'+uuid4().hex))
        self.change('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                    "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                    (sid,oid,hashlib.sha256(uuid4().bytes).hexdigest(),hashlib.sha256(uuid4().bytes).hexdigest()))
        return security.Actor(oid,frozenset(('onboarding:read','tasks:manage')),sid,1)

    def test_other_owner_cannot_read_locate_filter_batch_or_use_cursor(self):
        first=self.create(); self.create(); token=self.page(limit=1).next_cursor
        other=self.other_owner(); api=self.api()
        self.reject(lambda:api.locate_scope(self.settings,other,first['task_ids'][0],policy=self.vault._policy),ErrorCode.FORBIDDEN)
        self.reject(lambda:api.read_pool(self.settings,other,first['task_ids'][0],policy=self.vault._policy),ErrorCode.FORBIDDEN)
        self.reject(lambda:api.list_page(self.settings,other,batch_id=first['batch_id'],policy=self.vault._policy,mac=self.mac),ErrorCode.FORBIDDEN)
        self.reject(lambda:api.list_page(self.settings,other,limit=1,cursor=token,policy=self.vault._policy,mac=self.mac),ErrorCode.INVALID_INPUT)
        self.assertEqual(api.list_page(self.settings,other,policy=self.vault._policy,mac=self.mac).items,())

    def test_config_permission_is_rechecked_after_observation(self):
        from onboarding import security
        self.config(); original=security.revalidate; calls=[]
        def revoke(conn,actor,permission):
            result=original(conn,actor,permission); calls.append(permission)
            if len(calls)==1:
                conn.execute("UPDATE operators SET permissions=ARRAY[]::text[] WHERE id=%s",(actor.operator_id,))
            return result
        with patch.object(security,'revalidate',revoke):
            self.reject(lambda:pool_config.get_current(self.settings,self.actor,policy=self.vault._policy,
                                                       permission='config:manage'),ErrorCode.FORBIDDEN)
        self.assertEqual(calls,['config:manage'])


class TaskCursorTests(unittest.TestCase):
    """Real MAC files with synthetic key bytes, never DB or credential reads."""
    def setUp(self):
        from test_onboarding_mailbox_cursor import MailboxCursorTests
        MailboxCursorTests.setUp(self)
        self.api = importlib.import_module('onboarding.task_read')

    def test_cursor_binds_owner_filters_instance_schema_directory_and_domain(self):
        from dataclasses import replace
        from test_onboarding_mailbox_cursor import OWNER, OTHER, STAMP
        from onboarding import mailbox_read
        api = self.api
        digest = api._list_filter_mac(self.settings,OWNER,mac=self.mac)
        token = api._encode_list_cursor(OWNER,digest,STAMP,OTHER,mac=self.mac)
        self.assertEqual(api._decode_list_cursor(token,OWNER,digest,mac=self.mac),(STAMP,OTHER))
        self.assertLessEqual(len(token),512)
        digests = [api._list_filter_mac(self.settings,OWNER,mac=self.mac,**changes)
                   for changes in ({'scope':'pool'},{'batch_id':OTHER},{'limit':1})]
        digests += [api._list_filter_mac(settings,OWNER,mac=self.mac) for settings in
                    (replace(self.settings,schema='rf_p1b_test_'+'2'*32),
                     replace(self.settings,instance_marker='rf-onboarding-p1b-v1:other'))]
        for changed in digests:
            with self.assertRaises(ServiceError) as caught:
                api._decode_list_cursor(token,OWNER,changed,mac=self.mac)
            self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)
        with self.assertRaises(ServiceError):
            api._decode_list_cursor(token,OTHER,digest,mac=self.mac)
        mailbox_token = mailbox_read._encode_list_cursor(OWNER,digest,STAMP,OTHER,mac=self.mac)
        with self.assertRaises(ServiceError):
            api._decode_list_cursor(mailbox_token,OWNER,digest,mac=self.mac)
        (self.directory/'request-mac.key').unlink()
        with self.assertRaises(ServiceError) as caught:
            api._decode_list_cursor(token,OWNER,digest,mac=self.mac)
        self.assertEqual(caught.exception.code,ErrorCode.SECRET_UNAVAILABLE)

    def test_cursor_rejects_signed_noncanonical_payloads_and_tampering(self):
        import base64
        import json
        from test_onboarding_mailbox_cursor import OWNER, OTHER, STAMP
        api=self.api; digest=api._list_filter_mac(self.settings,OWNER,mac=self.mac)
        good={'v':1,'f':digest,'t':'2026-09-24T01:02:03.123456Z','i':OTHER}
        for value in (good|{'v':True}, good|{'extra':1}, good|{'t':'infinity'},
                      good|{'i':'bad'}, good|{'f':'A'*64}):
            raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
            token=base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+self.mac.request_digest('task.list.cursor.v1',OWNER,raw)
            with self.assertRaises(ServiceError) as caught:
                api._decode_list_cursor(token,OWNER,digest,mac=self.mac)
            self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)
        token=api._encode_list_cursor(OWNER,digest,STAMP,OTHER,mac=self.mac)
        for bad in (None, True, '', token+'=', token+'.x', 'x'*513):
            with self.assertRaises(ServiceError) as caught:
                api._decode_list_cursor(bad,OWNER,digest,mac=self.mac)
            self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)


class TaskProjectionTests(unittest.TestCase):
    def test_batch_filter_owner_check_is_closed(self):
        from onboarding import security
        api=importlib.import_module('onboarding.task_read')
        self.assertTrue(callable(getattr(api,'_owner_batch',None)),'batch owner validation missing')
        bid,oid=uuid4(),uuid4()
        actor=security.Actor(str(oid),frozenset(),str(uuid4()),1)
        self.assertIsNone(api._owner_batch((bid,oid),str(bid),actor))
        for row, code in ((None,ErrorCode.FORBIDDEN),((bid,uuid4()),ErrorCode.FORBIDDEN),
                          ((uuid4(),oid),ErrorCode.DEPENDENCY_UNAVAILABLE),
                          ((str(bid),oid),ErrorCode.DEPENDENCY_UNAVAILABLE)):
            with self.assertRaises(ServiceError) as caught:
                api._owner_batch(row,str(bid),actor)
            self.assertEqual(caught.exception.code,code)

    def test_bad_list_rows_and_secret_like_fields_fail_closed(self):
        from datetime import datetime, timezone
        from uuid import UUID
        api=importlib.import_module('onboarding.task_read')
        tid,bid,cid,owner=[uuid4() for _ in range(4)]
        row=(tid,'pool',bid,cid,'pool-'+str(uuid4()),'QUEUED',1,None,None,1,False,
             datetime.now(timezone.utc),'pool',cid,owner)
        expected=api._list_dto(row,str(owner))
        self.assertEqual(set(expected),set(api._PUBLIC)|{'synthetic','created_at'})
        for index, value in ((0,str(tid)),(1,'real'),(4,'secret:ref'),(5,'BAD'),(6,True),
                             (7,'password=canary'),(8,'secret canary'),(9,0),(10,1),
                             (11,'infinity'),(12,'fixture'),(13,uuid4()),(14,uuid4())):
            values=list(row); values[index]=value
            with self.subTest(index=index), self.assertRaises(ServiceError) as caught:
                api._list_dto(tuple(values),str(owner))
            self.assertEqual(caught.exception.code,ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(str(caught.exception),'DEPENDENCY_UNAVAILABLE')
