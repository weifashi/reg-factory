"""Offline version-bound live graph/plan checks; fabricated decoded SQL rows only."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from onboarding import live_config, live_effects, live_run_store, live_runtime, pool_repository, security
from onboarding.errors import ErrorCode, ServiceError
from test_onboarding_live_config import FIELDS, LEGACY


class LiveV2GraphTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.mid, self.sid, self.tid, self.bid, self.cid, self.secret = (str(uuid4()) for _ in range(7))
        self.actor = security.Actor(self.owner, frozenset({'tasks:manage'}), str(uuid4()), 1)
        self.revision = 'pool-' + str(uuid4())
        self.fields = FIELDS.copy()
        self.pin = dict(state_id=self.sid, secret_ref=self.secret, revision=1, identity_status='UNKNOWN')
        self.now = datetime.now(timezone.utc)
        self.health = 'UNKNOWN'
        self.queries = []
        self.task = dict(id=UUID(self.tid), batch_id=UUID(self.bid), config_id=UUID(self.cid),
            execution_scope='pool', mailbox_id=UUID(self.mid), mailbox_ref=self.mid,
            platform='google', platform_plan=['google'], credential_version=1,
            mailbox_credential_ref=UUID(self.secret), platform_credential_pins={'google': self.pin},
            status='QUEUED', reason_code=None, current_step=None, generation=1, version=2,
            cancel_requested=False, created_at=self.now, updated_at=self.now)
        self.conn = Mock()
        self.conn.execute.side_effect = self.execute
        self.proof = Mock(return_value=([(), ()],
            {'resource_kind':'mailbox', 'version':2, 'fence':1, 'hold_reason':'HELD'}))

    def execute(self, query, args=()):
        self.queries.append((query, args))
        if 'FROM onboarding_tasks WHERE id=' in query:
            row = tuple(self.task[name] for name in pool_repository._TASK_FIELDS) + ('live',)
        elif 'FROM onboarding_batches' in query:
            row = (UUID(self.owner), UUID(self.cid), [self.mid], 1, 'specified')
        elif 'FROM global_configs' in query:
            self.assertEqual(args, (self.cid,))
            self.assertNotIn('ORDER BY', query)
            row = ((self.revision, 'pool') if query.startswith('SELECT revision,scope') else
                   (UUID(self.cid), self.revision, self.fields, {}, UUID(self.owner), self.now, 'pool'))
        elif 'FROM mailbox_registry' in query:
            row = (UUID(self.owner), UUID(self.secret), 1, False, self.health, 'AVAILABLE')
        elif 'FROM mailbox_platform_states' in query:
            row = (UUID(self.mid), 'google', None if self.pin['secret_ref'] is None else UUID(self.pin['secret_ref']),
                   self.pin['revision'], self.pin['identity_status'], 'RESERVED', UUID(self.tid))
        elif 'FROM resource_leases WHERE task_id=' in query:
            row = None
        elif 'FROM resource_leases' in query:
            row = ('mailbox', self.mid, UUID(self.tid), 'batch:' + self.bid, 1,
                   self.now + timedelta(seconds=30), 'HELD', 2, None)
        elif 'FROM secret_objects' in query:
            row = (UUID(self.secret), 'mailbox_credential', 1, 'v1',
                   f'pool-live-v1:{self.owner}:mailbox:{self.mid}:v1', None, None, 1)
        else:
            self.fail('unexpected SQL in isolated graph contract: ' + query)
        cursor = Mock(); cursor.fetchone.return_value = row
        return cursor

    def graph(self):
        with patch.object(live_runtime.repository, 'require_transaction'), \
             patch.object(live_runtime.security, 'revalidate'), \
             patch.object(live_runtime, '_creation_proof', self.proof, create=True), \
             patch.object(live_runtime.pool_config, '_current_locked', side_effect=AssertionError('never read head')):
            return live_runtime._graph(self.conn, self.actor, self.tid, sending=True)

    def test_v1_cannot_be_used_by_formal_live_graph(self):
        self.fields = LEGACY.copy(); self.health = 'HEALTHY'
        with self.assertRaises(ServiceError) as caught:
            self.graph()
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_v2_graph_uses_pinned_fields_unknown_health_and_one_shared_secret(self):
        try:
            task, lease, secrets = self.graph()
        except ServiceError as error:
            self.fail('approved v2 graph still rejects unchanged UNKNOWN health: ' + error.code.value)
        self.assertEqual(task['config_revision'], self.revision)
        self.assertEqual(task['nonsecret_config'], FIELDS)
        self.assertEqual(secrets, [(self.secret, 'mailbox_credential',
                                   f'pool-live-v1:{self.owner}:mailbox:{self.mid}:v1')])
        self.proof.assert_called_once()
        self.assertEqual(sum('FROM secret_objects' in q for q, _ in self.queries), 1)

    def test_independent_google_secret_missing_pin_or_changed_revision_rejected(self):
        self.health = 'HEALTHY'
        for change in ({'secret_ref':str(uuid4())}, {'secret_ref':None}, {'revision':2},
                       {'identity_status':'EXISTING'}, {'identity_status':'NEW_CONFIRMED'}):
            with self.subTest(change=change):
                old = self.pin.copy(); self.pin.update(change)
                try:
                    with self.assertRaises(ServiceError): self.graph()
                finally:
                    self.pin.clear(); self.pin.update(old)

    def test_sending_rejects_disabled_health_without_reclassifying_it(self):
        for health in ('NEEDS_REVIEW', 'DISABLED'):
            self.health = health
            with self.assertRaises(ServiceError): self.graph()

    def test_missing_new_creation_proof_cannot_be_read_as_authority(self):
        self.health = 'HEALTHY'
        self.proof.side_effect = ServiceError(ErrorCode.RECONCILIATION_REQUIRED)
        with self.assertRaises(ServiceError) as caught:
            self.graph()
        self.assertEqual(caught.exception.code, ErrorCode.RECONCILIATION_REQUIRED)


class LiveV2PlanTests(unittest.TestCase):
    def setUp(self):
        self.actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        self.task_id, self.mailbox, self.secret = (str(uuid4()) for _ in range(3))
        self.worker = live_runtime._register(live_runtime.LiveWorker(self.actor, self.task_id, 1,
            str(uuid4()), 'pool-'+str(uuid4()), self.secret, (str(uuid4()), self.secret, 1, 'UNKNOWN'),
            live_effects.FenceBinding('mailbox', self.mailbox, 'worker:test', 2, 1), 'test-worker', 3))
        self.task = {'nonsecret_config': FIELDS.copy()}
        self.plan = live_config.plan_for(FIELDS, self.actor.operator_id, self.task_id)

    def check(self, plan, fields=None):
        self.assertTrue(callable(getattr(live_run_store, '_plan_pinned', None)),
                        'saved plan is not yet compared against pinned v2 configuration')
        return live_run_store._plan_pinned(self.task if fields is None else {'nonsecret_config':fields},
                                          self.worker, plan)

    def test_exact_global_derived_plan_is_accepted(self):
        self.check(self.plan)

    def test_valid_but_per_task_overrides_are_rejected(self):
        for changes in ({'billing_account':'DDDDDD-EEEEEE-FFFFFF'}, {'region':'us-east1'},
                        {'project_id':'rf-other-project'}, {'service_account_id':'rf-other-sa'},
                        {'parent':'folders/123'},
                        {'approved_role':'projects/'+self.plan.project_id+'/roles/customVertex'}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.check(replace(self.plan, **changes))

    def test_missing_or_legacy_pinned_config_cannot_validate_plan(self):
        for fields in ({}, LEGACY, {**FIELDS, 'scheduling_enabled':True}):
            with self.assertRaises(ServiceError): self.check(self.plan, fields)
