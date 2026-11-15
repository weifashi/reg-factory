"""Live mailbox observations: offline boundaries and explicit disposable PG tests.

Offline tests patch only DB/policy/file boundaries; DTO/cursors/MAC framing are
real. PG tests use formal source import, never relabel a synthetic task as live.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from onboarding import live_runtime, mailbox_read, mailboxes, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_input_vault import LivePoolPolicy
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.settings import BASE, Settings
import onboarding_pool_support
import test_onboarding_task_read as task_read_tests
from onboarding_live_support import populate
from test_onboarding_pool_batches import observe

STAMP = datetime(2026, 9, 30, tzinfo=timezone.utc)
PUBLIC = {'id','email','source_type','group_ref','health','disabled','ever_registration_attempted',
          'sale_eligibility','pool_status','occupied','version','created_at','updated_at','last_used_at','platforms'}


def api(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.live_mailbox_read'),
                         'explicit LivePoolPolicy mailbox reader is missing')
    return importlib.import_module('onboarding.live_mailbox_read')


def row(identity=None, *, state='HISTORY_UNRECONCILED'):
    return (UUID(identity or str(uuid4())), 'owned@example.org', 'other', 'owned', 'UNKNOWN',
            False, False, 'UNVERIFIED', 'AVAILABLE', 1, STAMP, STAMP, None, False,
            [dict(platform='google', identity_status='UNKNOWN', usage_status=state, version=1, checked_at=None)])


class LiveMailboxReadOfflineTests(unittest.TestCase):
    def setUp(self):
        self.api = api(self)
        self.settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
        self.policy = object.__new__(LivePoolPolicy)
        object.__setattr__(self.policy, 'settings', self.settings)
        self.actor = security.Actor(str(uuid4()), frozenset({'onboarding:read'}), str(uuid4()), 1)
        self.mac = object.__new__(RequestMac)
        object.__setattr__(self.mac, 'directory', BASE / 'keyrings' / 'fixture-live-read')
        self.key = b'a' * 32
        self.rows, self.sql, self.checks, self.authority = [row()], [], [], []
        self.after_data = self.after_connection = None
        self.commit_error = None

    @contextmanager
    def dependencies(self):
        def execute(query, params):
            self.sql.append((query, params))
            if self.after_data:
                self.after_data()
            return SimpleNamespace(fetchall=lambda: self.rows)
        conn = SimpleNamespace(execute=execute)
        @contextmanager
        def uow(settings):
            self.assertIs(settings, self.settings)
            yield conn
            if self.commit_error:
                raise self.commit_error
        def connection(policy, current):
            self.assertIs(policy, self.policy); self.assertIs(current, conn)
            self.checks.append('policy')
            if self.after_connection:
                self.after_connection()
        def auth(current, actor, permission):
            self.authority.append(permission)
            self.assertEqual(permission, 'onboarding:read')
            if getattr(self, 'auth_error', False):
                raise ServiceError(ErrorCode.UNAUTHENTICATED)
            return actor
        with ExitStack() as stack:
            for owner, name, value in ((storage,'unit_of_work',uow),
                (LivePoolPolicy,'_validate',lambda *args: None),
                (LivePoolPolicy,'_connection',connection),
                (RequestMac,'_key',lambda *args: self.key),
                (security,'revalidate',auth),
                (live_runtime,'_graph',lambda *args,**kwargs: self.fail('execution graph reached'))):
                stack.enter_context(patch.object(owner,name,value))
            yield

    def page(self, **changes):
        values=dict(settings=self.settings,actor=self.actor,policy=self.policy,mac=self.mac)
        values.update(changes)
        with self.dependencies():
            return self.api.list_page(**values)

    def reject(self, call, code=ErrorCode.INVALID_INPUT):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code,code)
        self.assertFalse(caught.exception.not_committed)
        self.assertNotIn('PRIVATE_CANARY',str(caught.exception))

    def test_exact_context_and_invalid_filters_fail_before_any_dependencies(self):
        bad=[{'settings':object()},{'policy':object()},{'mac':object()},
             {'platform':'unknown'},{'search':None},{'search':'x'*321},{'search':'a\n'},
             {'group_ref':3},{'health':'healthy'},{'occupied':1},{'limit':True},
             {'limit':0},{'limit':101},{'cursor':True},{'cursor':'x'*513}]
        with ExitStack() as stack:
            for owner,name in ((storage,'unit_of_work'),(LivePoolPolicy,'_validate'),(RequestMac,'request_digest')):
                stack.enter_context(patch.object(owner,name,side_effect=AssertionError(name)))
            for changes in bad:
                values=dict(settings=self.settings,actor=self.actor,policy=self.policy,mac=self.mac)|changes
                self.reject(lambda:self.api.list_page(**values))
            self.reject(lambda:self.api.list_page(self.settings,object(),policy=self.policy,mac=self.mac),
                        ErrorCode.UNAUTHENTICATED)
            bad_actor=replace(self.actor,session_id='bad')
            self.reject(lambda:self.api.list_page(self.settings,bad_actor,policy=self.policy,mac=self.mac),
                        ErrorCode.UNAUTHENTICATED)

    def test_owner_filters_safe_dto_and_unknown_history_are_observations_only(self):
        page=self.page(platform='google',search='%_A',group_ref='owned',health='UNKNOWN',occupied=False)
        self.assertIs(type(page),mailbox_read.Page)
        self.assertEqual(set(page.items[0]),PUBLIC)
        self.assertEqual(page.items[0]['platforms'][0]['identity_status'],'UNKNOWN')
        self.assertEqual(page.items[0]['platforms'][0]['usage_status'],'HISTORY_UNRECONCILED')
        self.assertEqual(page.items[0]['health'],'UNKNOWN')
        self.assertEqual(self.authority,['onboarding:read','onboarding:read'])
        self.assertEqual(self.checks,['policy','policy'])
        self.assertEqual(len(self.sql),1)
        query,params=self.sql[0]
        self.assertEqual(query,mailbox_read._DATA_SQL)
        self.assertEqual(params,dict(owner=self.actor.operator_id,platform='google',search='%_a',
            group_ref='owned',health='UNKNOWN',occupied=False,limit=50,fetch_limit=51,anchor_time=None,anchor_id=None))
        for field in ('credential_ref','secret_objects','source_fingerprint','evidence_ref'):
            self.assertNotIn(field,query)
        self.assertFalse(query.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')))

    def test_legacy_valid_usage_is_displayed_without_admission_or_upgrade(self):
        self.rows=[row(state='SUCCEEDED'),row(state='UNUSED')]
        result=self.page()
        self.assertEqual([item['platforms'][0]['usage_status'] for item in result.items],['SUCCEEDED','UNUSED'])
        self.assertTrue(all(set(item)==PUBLIC for item in result.items))

    def test_keyset_cursor_is_bound_to_owner_filters_and_limit(self):
        self.rows=[row(),row()]
        token=self.page(limit=1).next_cursor
        self.assertIsNotNone(token)
        first_id=str(self.rows[0][0])
        self.rows=self.rows[1:]
        self.page(limit=1,cursor=token)
        self.assertEqual((self.sql[-1][1]['anchor_time'],self.sql[-1][1]['anchor_id']),(STAMP,first_id))
        for changes in ({'limit':2},{'platform':'google'},{'search':'x'},
                        {'actor':replace(self.actor,operator_id=str(uuid4()))}):
            self.reject(lambda:self.page(**(dict(limit=1,cursor=token)|changes)))

    def test_corrupt_sentinel_is_not_omitted_or_leaked(self):
        broken=list(row()); broken[14][0]['extra']='PRIVATE_CANARY'
        self.rows=[row(),tuple(broken)]
        self.reject(lambda:self.page(limit=1),ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_final_policy_key_check_detects_rotation(self):
        def rotate():
            if len(self.checks)==2:
                self.key=b'b'*32
        self.after_connection=rotate
        self.reject(self.page,ErrorCode.SECRET_UNAVAILABLE)
        self.assertEqual(self.authority,['onboarding:read'])

    def test_authority_revoked_during_data_read_is_checked_at_tail(self):
        self.after_data=lambda:setattr(self,'auth_error',True)
        self.reject(self.page,ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.authority,['onboarding:read','onboarding:read'])

    def test_commit_unknown_never_returns_a_page(self):
        self.commit_error=ServiceError(ErrorCode.COMMIT_UNKNOWN)
        self.reject(self.page,ErrorCode.COMMIT_UNKNOWN)
        self.assertEqual(len(self.sql),1)

    def test_unicode_lowercase_expansion_preserves_original_filter_contract(self):
        self.rows=[]
        self.assertEqual(self.page(search='İ'*320).items,())
        self.assertEqual(self.sql[-1][1]['search'],'i\u0307'*320)


class LiveMailboxReadDBTests(onboarding_pool_support.PoolCase):
    """Run only in the root-assigned serial disposable PostgreSQL window."""
    other_owner=task_read_tests.TaskReadTests.other_owner

    def setUp(self):
        super().setUp()
        self.api=api(self)
        populate(self,create=False)

    def change(self, query, args=()):
        with self.uow() as conn:
            conn.execute(query, args)

    def page(self, **changes):
        values=dict(settings=self.settings,actor=self.actor,policy=self.live_policy,mac=self.mac)|changes
        return self.api.list_page(**values)

    def reject(self,call,code):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code,code)
        self.assertFalse(caught.exception.not_committed)

    def legacy(self):
        text=json.dumps([{'email':'fixture-history@fixture.invalid','password':'fixture:private-canary','provider':'outlook'}])
        preview=mailboxes.preview_import(self.settings,self.actor,text,'history',vault=self.vault,mac=self.mac)
        return mailboxes.import_text(self.settings,self.actor,text,'history',preview['preview_digest'],
            'read:legacy',vault=self.vault,mac=self.mac).created_ids[0]

    def test_formal_import_unknown_history_safe_fields_and_zero_writes(self):
        before=self.business_snapshot()
        session=self.read('SELECT version,idle_expires_at,updated_at FROM operator_sessions')
        with observe() as queries, patch.object(live_runtime,'_graph',side_effect=AssertionError('no graph')):
            page=self.page()
        value=page.items[0]
        self.assertEqual(set(value),PUBLIC)
        self.assertEqual(value['id'],self.resource['id'])
        self.assertEqual((value['health'],value['sale_eligibility'],value['occupied']),('UNKNOWN','UNVERIFIED',False))
        self.assertEqual({(p['identity_status'],p['usage_status']) for p in value['platforms']},
                         {('UNKNOWN','HISTORY_UNRECONCILED')})
        self.assertEqual(len(value['platforms']),7)
        self.assertEqual(before,self.business_snapshot())
        self.assertEqual(session,self.read('SELECT version,idle_expires_at,updated_at FROM operator_sessions'))
        for _,query,_ in queries:
            self.assertFalse(query.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')))
            self.assertNotIn('secret_objects',query)
        rendered=json.dumps(list(page.items))
        for secret in ('unit-test-owned-password',self.resource['secret_id'],'source_fingerprint','source_digest'):
            self.assertNotIn(secret,rendered)

    def test_read_only_permissions_and_cross_owner_empty_page(self):
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read'] WHERE id=%s",(self.actor.operator_id,))
        self.assertEqual(len(self.page(actor=replace(self.actor,permissions=frozenset())).items),1)
        self.assertEqual(self.page(actor=self.other_owner()).items,())

    def test_mixed_legacy_live_paging_and_filter_bound_cursors(self):
        old=self.legacy()
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET created_at='2026-09-30 00:00:00+00'")
        first=self.page(limit=1); second=self.page(limit=1,cursor=first.next_cursor)
        self.assertEqual([first.items[0]['id'],second.items[0]['id']],sorted([old,self.resource['id']],reverse=True))
        self.assertIsNone(second.next_cursor)
        self.assertEqual([item['id'] for item in self.page(group_ref='history').items],[old])
        self.assertEqual([item['id'] for item in self.page(search='WEIFASHI68',platform='google').items],[self.resource['id']])
        self.assertEqual(self.page(search='%_').items,())
        self.reject(lambda:self.page(limit=2,cursor=first.next_cursor),ErrorCode.INVALID_INPUT)
        self.reject(lambda:self.page(actor=self.other_owner(),limit=1,cursor=first.next_cursor),ErrorCode.INVALID_INPUT)

    def test_expired_live_hold_is_still_occupied_not_admitted(self):
        from onboarding_live_support import create_batch
        batch=create_batch(self,self.cfg,[self.resource])
        with self.uow() as conn:
            conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 hour' WHERE task_id=%s",
                         (batch['task_ids'][0],))
        before=self.business_snapshot()
        page=self.page(occupied=True,platform='google')
        self.assertTrue(page.items[0]['occupied'])
        self.assertEqual(page.items[0]['platforms'][0]['usage_status'],'RESERVED')
        self.assertEqual(self.page(occupied=False).items,())
        self.assertEqual(before,self.business_snapshot())

    def test_history_read_does_not_require_live_aes_or_source_files(self):
        (self.key_directory/'v1.key').unlink()
        (self.source.data_root/'emails.txt').unlink()
        self.assertEqual(self.page().items[0]['id'],self.resource['id'])
        with self.assertRaises(ServiceError) as caught:
            mailbox_read.list_page(self.settings,self.actor,policy=self.live_policy,mac=self.mac)
        self.assertEqual(caught.exception.code,ErrorCode.INVALID_INPUT)

    def test_current_database_revocation_is_not_actor_cache(self):
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
        self.reject(self.page,ErrorCode.UNAUTHENTICATED)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls)
        for cls in (LiveMailboxReadOfflineTests,LiveMailboxReadDBTests))


if __name__=='__main__': unittest.main()
