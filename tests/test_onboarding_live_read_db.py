"""Formal-live historical observations in disposable PG; providers always mocked.

Tasks are made by real import/admission/create services, never mode promotion.
Adversarial SQL changes below test historical/resource drift, not production APIs.
"""
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg.types.json import Jsonb

from onboarding import (audit, live_commands, live_config, live_effects, live_runtime,
                        pool_batches, pool_config, security)
from onboarding.errors import ErrorCode, ServiceError
from onboarding_pool_support import PoolCase, after_query
from onboarding_live_support import populate
from test_onboarding_live_config import FIELDS
import test_onboarding_pool_batches as batch_tests
from test_onboarding_pool_batches import observe
import test_onboarding_task_read as read_tests


class LiveReadDBTests(PoolCase):
    change = batch_tests.PoolBatchEntryTests.change
    reject = batch_tests.PoolBatchEntryTests.reject
    other_owner = read_tests.TaskReadTests.other_owner

    def setUp(self):
        super().setUp()
        populate(self)
        self.api = importlib.import_module('onboarding.live_read')

    def detail(self, actor=None):
        return self.api.read_task(self.settings, actor or self.actor, self.tid, policy=self.live_policy)

    def events(self, actor=None):
        return self.api.events(self.settings, actor or self.actor, self.tid, policy=self.live_policy)

    def page(self, actor=None, **changes):
        return self.api.list_page(self.settings, actor or self.actor, policy=self.live_policy, mac=self.mac, **changes)

    def test_formal_live_all_read_apis_are_bounded_and_do_not_mutate_business_state(self):
        before=self.business_snapshot()
        with observe() as queries, patch.object(live_runtime,'_graph',side_effect=AssertionError('execution graph forbidden')):
            detail=self.detail(); page=self.page(); events=self.events()
            location=self.api.locate(self.settings,self.actor,self.tid,policy=self.live_policy)
        self.assertEqual(detail['execution_mode'],'live'); self.assertFalse(detail['synthetic'])
        self.assertEqual(location,dict(execution_scope='pool',execution_mode='live'))
        self.assertEqual(page.items[0]['id'],self.tid)
        self.assertEqual(set(detail),{'id','execution_scope','batch_id','config_id','config_revision','status',
            'version','reason_code','current_step','generation','cancel_requested','synthetic','execution_mode'})
        self.assertEqual(events['mode'],'live'); self.assertIs(events['execution_connected'],False)
        self.assertEqual(events['effect_events'],[])
        serialized=json.dumps([detail,list(page.items),events])
        for private in ('unit-test-owned-password',self.resource['secret_id'],self.resource['state_id'],
                        FIELDS['billing_account'],'source_fingerprint','source_digest','nonsecret_config'):
            self.assertNotIn(private,serialized)
        self.assertEqual(before,self.business_snapshot())
        for _,query,_ in queries:
            upper=query.lstrip().upper()
            self.assertFalse(upper.startswith(('INSERT ','UPDATE ','DELETE ')))
            self.assertNotIn('FROM secret_objects',query)
            self.assertNotIn('FROM resource_leases',query)
            self.assertNotIn('FROM mailbox_platform_states',query)
            if 'FOR UPDATE' in query: self.assertIn('operator_sessions',query)
            if 'FROM audit_events' in query: self.assertIn('LIMIT ',query)

    def test_read_only_role_reads_live_but_manage_locator_cannot_escape_permission(self):
        self.change("UPDATE operators SET permissions=ARRAY['onboarding:read'] WHERE id=%s",(self.actor.operator_id,))
        actor=replace(self.actor,permissions=frozenset({'onboarding:read'}))
        self.assertEqual(self.detail(actor)['id'],self.tid)
        self.assertEqual(self.events(actor)['task_id'],self.tid)
        self.assertEqual(len(self.page(actor).items),1)
        self.reject(lambda:self.api.locate(self.settings,actor,self.tid,policy=self.live_policy,permission='tasks:manage'),
                    ErrorCode.FORBIDDEN)

    def test_historical_reads_survive_terminal_release_expiry_revocation_and_disabled_mailbox(self):
        # No execution claim is inferred: only historical display survives drift.
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='CANCELLED_SAFE',cancel_requested=true,"
                         "reason_code='CANCELLED_SAFE_RELEASED',version=version+1 WHERE id=%s",(self.tid,))
            conn.execute("UPDATE resource_leases SET task_id=NULL,owner_id=NULL,lease_until=NULL,hold_reason=NULL,"
                         "stopped_evidence_ref=NULL,fence=fence+1,version=version+1 WHERE resource_id=%s",(self.resource['id'],))
            conn.execute("UPDATE mailbox_platform_states SET usage_status='HISTORY_UNRECONCILED',last_task_id=NULL,"
                         "version=version+1 WHERE id=%s",(self.resource['state_id'],))
            conn.execute("UPDATE mailbox_registry SET disabled=true,health='NEEDS_REVIEW',version=version+1 WHERE id=%s",
                         (self.resource['id'],))
            conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()-interval '1 second',"
                         "revoked_at=clock_timestamp(),version=version+1 WHERE id=%s",(self.resource['secret_id'],))
        before=self.business_snapshot()
        self.assertEqual(self.detail()['status'],'CANCELLED_SAFE')
        self.assertEqual(self.page().items[0]['status'],'CANCELLED_SAFE')
        self.assertEqual(self.events()['mode'],'live')
        self.assertEqual(before,self.business_snapshot())

    def test_new_head_does_not_replace_pinned_live_config(self):
        newer=live_config.replace(self.settings,self.actor,self.cfg['revision'],
            FIELDS|{'project_prefix':'new-prefix'},'read:new-head',policy=self.live_policy,mac=self.mac)
        self.assertNotEqual(newer['revision'],self.cfg['revision'])
        with patch.object(pool_config,'_current_locked',side_effect=AssertionError('head lookup forbidden')):
            self.assertEqual(self.detail()['config_revision'],self.cfg['revision'])
            self.assertEqual(self.page().items[0]['config_revision'],self.cfg['revision'])
            self.assertEqual(self.events()['mode'],'live')

    def test_owner_isolation_applies_to_detail_locator_events_batch_and_cursor(self):
        fixture=self.task()
        token=self.page(limit=1).next_cursor
        other=self.other_owner()
        for call in (lambda:self.detail(other),lambda:self.events(other),
            lambda:self.api.locate(self.settings,other,self.tid,policy=self.live_policy),
            lambda:self.page(other,batch_id=self.batch['batch_id'])):
            self.reject(call,ErrorCode.FORBIDDEN)
        self.assertEqual(self.page(other).items,())
        self.reject(lambda:self.page(other,limit=1,cursor=token),ErrorCode.INVALID_INPUT)

    def mixed(self):
        # Create an ordinary v1 task with its unmodified strict service while
        # leaving the preexisting formally created live task pinned to v2.
        cfg=batch_tests.PoolBatchEntryTests.config(self)
        seed=batch_tests.PoolBatchEntryTests.seed(self)
        synthetic=pool_batches.create(self.settings,self.actor,'specified',1,[seed['mailbox']],cfg['revision'],
            'read:old-synthetic',policy=self.vault._policy,mac=self.mac)
        fixture=self.task()
        return synthetic['task_ids'][0],fixture['id']

    def test_mixed_list_is_complete_paged_and_cursor_bound_without_mode_promotion(self):
        old,fixture=self.mixed(); before=self.business_snapshot()
        page=self.page(limit=1); items=list(page.items)
        token=page.next_cursor
        while page.next_cursor:
            page=self.page(limit=1,cursor=page.next_cursor); items.extend(page.items)
        self.assertEqual({item['id'] for item in items},{self.tid,old,fixture})
        self.assertEqual(len(items),3)
        self.assertEqual({item['id']:item['execution_mode'] for item in items},
                         {self.tid:'live',old:'synthetic',fixture:'synthetic'})
        for tid in (old,fixture):
            result=self.api.read_task(self.settings,self.actor,tid,policy=self.live_policy)
            self.assertTrue(result['synthetic'])
        self.assertEqual(len(self.page(scope='pool').items),2)
        self.assertEqual(len(self.page(scope='fixture').items),1)
        self.reject(lambda:self.page(scope='pool',limit=1,cursor=token),ErrorCode.INVALID_INPUT)
        self.reject(lambda:self.page(limit=2,cursor=token),ErrorCode.INVALID_INPUT)
        self.assertEqual(before,self.business_snapshot())

    def test_live_creation_proof_tampering_rejects_detail_events_and_even_list_sentinel(self):
        self.task()  # newer valid fixture; corrupt live row is the sentinel.
        self.change("UPDATE operation_receipts SET result_summary=jsonb_set(result_summary,'{original_usage_status}',"
                    "'\"UNUSED\"') WHERE id=%s",(self.batch['receipt_id'],))
        for call in (self.detail,self.events,lambda:self.page(limit=1)):
            self.reject(call,ErrorCode.RECONCILIATION_REQUIRED)

    def test_wrong_pinned_config_cannot_be_reported_as_valid_live_history(self):
        cfg=batch_tests.PoolBatchEntryTests.config(self)
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET config_id=%s WHERE id=%s',(cfg['config_id'],self.tid))
            conn.execute('UPDATE onboarding_batches SET config_id=%s WHERE id=%s',(cfg['config_id'],self.batch['batch_id']))
        for call in (self.detail,self.events,self.page): self.reject(call,ErrorCode.FORBIDDEN)

    def test_events_keep_fixed_live_effect_dto_and_management_outcomes(self):
        with self.uow() as conn:
            worker=live_runtime.handoff(conn,self.actor,self.tid,2,'read-events')
        with self.uow() as conn:
            context=live_runtime.effect_context(conn,worker,'project.create',self.plan.effect_digest('project.create'))
            live_effects.prepare(conn,context,'project.create','read-effect')
        live_commands.pause(self.settings,self.actor,self.tid,3,'read-pause',policy=self.live_policy,mac=self.mac)
        before=self.business_snapshot()
        value=self.events()
        self.assertEqual([row['action'] for row in value['events']],['task.create','task.pause'])
        self.assertEqual(value['events'][-1]['outcome'],'ACCEPTED')
        self.assertEqual(value['effect_events'][0]['phase'],'INTENT')
        self.assertEqual(set(value['effect_events'][0]),{'receipt_id','action','phase','result_code','version','updated_at'})
        self.assertEqual(before,self.business_snapshot())

    def test_event_history_is_bounded_and_unknown_public_outcomes_are_rejected(self):
        with self.uow() as conn:
            for _ in range(101):
                audit.append(conn,self.actor.operator_id,self.tid,'task.pause',self.tid,'ACCEPTED',str(uuid4()),
                             after_summary={'version':3})
        value=self.events(); self.assertEqual(len(value['events']),100); self.assertTrue(value['has_more'])
        with self.uow() as conn:
            audit.append(conn,self.actor.operator_id,self.tid,'task.pause',self.tid,'PRIVATE_CANARY',str(uuid4()))
        self.reject(self.events,ErrorCode.DEPENDENCY_UNAVAILABLE)

    def test_authority_is_rechecked_after_last_observation(self):
        original=security.revalidate; calls=[]
        def revoke(conn,actor,permission):
            result=original(conn,actor,permission); calls.append(permission)
            if len(calls)==1:
                conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(actor.session_id,))
            return result
        with patch.object(security,'revalidate',revoke): self.reject(self.detail,ErrorCode.UNAUTHENTICATED)
        self.assertIsNone(self.read('SELECT revoked_at FROM operator_sessions WHERE id=%s',(self.actor.session_id,))[0][0])

    def test_read_only_snapshot_is_not_returned_after_commit_unknown(self):
        from contextlib import contextmanager
        from onboarding import storage
        original=storage.unit_of_work
        @contextmanager
        def ambiguous(settings):
            with original(settings) as conn: yield conn
            raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage,'unit_of_work',ambiguous):
            self.reject(self.detail,ErrorCode.COMMIT_UNKNOWN)


def load_tests(loader,tests,pattern): return loader.loadTestsFromTestCase(LiveReadDBTests)
