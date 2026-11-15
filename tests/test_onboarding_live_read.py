"""Offline read contracts; no PG, credential files, or cloud operations."""
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib
import importlib.util
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from onboarding import pool_repository, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_input_vault import LivePoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.settings import Settings
from test_onboarding_live_config import FIELDS, LEGACY


def api(case):
    case.assertIsNotNone(importlib.util.find_spec('onboarding.live_read'),
                         'formal live historical observation service is missing')
    return importlib.import_module('onboarding.live_read')


def context():
    settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
    policy = object.__new__(LivePoolPolicy)
    object.__setattr__(policy, 'settings', settings)
    actor = security.Actor(str(uuid4()), frozenset({'onboarding:read'}), str(uuid4()), 1)
    return dict(settings=settings, actor=actor, policy=policy)


class LiveReadInputTests(unittest.TestCase):
    def test_malformed_context_and_identifiers_never_touch_dependencies(self):
        m = api(self); ctx = context(); tid = str(uuid4())
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                     for owner, name in ((storage, 'open_app'), (LivePoolPolicy, '_validate'),
                                         (RequestMac, 'request_digest'))]
            for name in ('locate', 'read_task', 'events'):
                for bad in (None, True, UUID(tid), 'bad', tid.upper()):
                    with self.subTest(name=name, bad=bad), self.assertRaises(ServiceError) as caught:
                        getattr(m, name)(**ctx, task_id=bad)
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
                with self.assertRaises(ServiceError) as caught:
                    getattr(m, name)(**(ctx | {'actor': object()}), task_id=tid)
                self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
                with self.assertRaises(ServiceError) as caught:
                    getattr(m, name)(**(ctx | {'policy': object()}), task_id=tid)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for permission in ('config:manage', '', True, None):
                with self.assertRaises(ServiceError) as caught:
                    m.locate(**ctx, task_id=tid, permission=permission)
                self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
            mac = object.__new__(RequestMac)
            for changes in ({'scope':'live'}, {'scope':True}, {'limit':True}, {'limit':0}, {'limit':101},
                            {'batch_id':'bad'}, {'cursor':'x'*513}, {'mac':object()}):
                with self.subTest(changes=changes), self.assertRaises(ServiceError) as caught:
                    m.list_page(**ctx, **({'mac':mac} | changes))
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for spy in spies: spy.assert_not_called()


class LiveReadProjectionTests(unittest.TestCase):
    def setUp(self):
        self.m = api(self)
        self.owner,self.tid,self.bid,self.cid,self.mid,self.sid,self.secret = (str(uuid4()) for _ in range(7))
        self.actor = security.Actor(self.owner,frozenset({'onboarding:read'}),str(uuid4()),1)
        self.now = datetime(2026,9,29,tzinfo=timezone.utc)
        self.mode = 'live'
        self.task = dict(id=UUID(self.tid),batch_id=UUID(self.bid),config_id=UUID(self.cid),execution_scope='pool',
            mailbox_id=UUID(self.mid),mailbox_ref=self.mid,platform='google',platform_plan=['google'],
            credential_version=1,mailbox_credential_ref=UUID(self.secret),
            platform_credential_pins={'google':dict(state_id=self.sid,secret_ref=self.secret,revision=1,identity_status='UNKNOWN')},
            status='CANCELLED_SAFE',reason_code='CANCELLED_SAFE_RELEASED',current_step='project.create',
            generation=1,version=7,cancel_requested=True,created_at=self.now,updated_at=self.now)
        self.batch = [UUID(self.owner),UUID(self.cid),[self.mid],1,'specified']
        self.config = [UUID(self.cid),'pool-'+self.cid,FIELDS.copy(),{},UUID(self.owner),self.now,'pool']

    def row(self):
        return tuple(self.task[k] for k in pool_repository._TASK_FIELDS)+(self.mode,)+tuple(self.batch)+tuple(self.config)

    def decode(self):
        return self.m._decode(self.row(),self.actor,self.tid)

    def test_live_terminal_projection_preserves_pin_but_never_exposes_it(self):
        task, mode = self.decode()
        self.assertEqual(mode,'live'); self.assertEqual(task['config_revision'],self.config[1])
        public=self.m.task_read._safe(task,mode)
        self.assertEqual(set(public),set(self.m.task_read._PUBLIC)|{'synthetic','execution_mode'})
        self.assertEqual(public['status'],'CANCELLED_SAFE'); self.assertFalse(public['synthetic'])
        self.assertNotIn(self.secret,str(public)); self.assertNotIn(self.config[2]['billing_account'],str(public))

    def test_modes_require_matching_complete_config_versions(self):
        self.config[2]=LEGACY.copy()
        with self.assertRaises(ServiceError): self.decode()
        self.mode='synthetic'
        self.assertEqual(self.decode()[1],'synthetic')
        self.config[2]=FIELDS.copy()
        with self.assertRaises(ServiceError): self.decode()
        self.mode='arbitrary-mode'
        with self.assertRaises(ServiceError): self.decode()

    def test_owner_and_batch_config_mismatches_are_rejected(self):
        self.batch[0]=uuid4()
        with self.assertRaises(ServiceError) as caught: self.decode()
        self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
        self.batch[0]=UUID(self.owner); self.batch[1]=uuid4()
        with self.assertRaises(ServiceError): self.decode()

    def test_fixture_remains_synthetic_and_complete_legacy_config_only(self):
        self.mode='synthetic'; self.task.update(execution_scope='fixture',mailbox_id=None,
            mailbox_ref='fixture:mailbox',platform=None,platform_plan=[],credential_version=None,
            mailbox_credential_ref=None,platform_credential_pins={})
        self.batch[2]=['fixture:mailbox']; self.config[1]='fixture-'+uuid4().hex; self.config[-1]='fixture'
        self.config[2]={k:LEGACY[k] for k in self.m.repository._CONFIG_FIELDS}
        self.assertEqual(self.decode()[1],'synthetic')
        self.mode='live'
        with self.assertRaises(ServiceError): self.decode()

    def test_history_requires_new_proof_but_reads_no_resource_liveness_or_head(self):
        conn=Mock(); queries=[]
        def execute(query,params=()):
            queries.append(query); cursor=Mock()
            if 'FROM onboarding_tasks t' in query: cursor.fetchone.return_value=self.row()
            elif 'FROM audit_events' in query:
                self.assertIn('LIMIT 2',query); cursor.fetchall.return_value=[(),()]
            else: self.fail('history performed an unexpected read: '+query)
            return cursor
        conn.execute.side_effect=execute
        with patch.object(self.m.live_batches,'creation_details',return_value={'proof':'present'}) as proof:
            result,mode=self.m._history(conn,self.actor,self.tid)
            self.assertEqual(result['id'],self.tid); self.assertEqual(mode,'live'); proof.assert_called_once()
        with patch.object(self.m.live_batches,'creation_details',return_value=None):
            with self.assertRaises(ServiceError) as caught: self.m._history(conn,self.actor,self.tid)
            self.assertEqual(caught.exception.code,ErrorCode.RECONCILIATION_REQUIRED)
        for query in queries:
            self.assertNotIn('FOR UPDATE',query)
            self.assertNotIn('secret_objects',query); self.assertNotIn('resource_leases',query)
            self.assertNotIn('mailbox_platform_states',query)
            self.assertNotIn('ORDER BY created_at',query)


if __name__ == '__main__': unittest.main()
