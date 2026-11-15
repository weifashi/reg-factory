"""Session-bound worker tests. Only isolated fixture PG; never provider I/O."""
import copy
import importlib
import importlib.util
import os
import unittest
from uuid import UUID, uuid4
from unittest.mock import patch

from onboarding import live_commands, live_effects, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.migrate import apply_all
from onboarding_pool_support import PoolCase, after_query
import test_onboarding_pool_batches as batch_tests


class LiveRuntimeInputTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_runtime'),
                             'session-bound live worker boundary is missing')
        return importlib.import_module('onboarding.live_runtime')

    def test_runtime_boundary_exists(self):
        m = self.module()
        self.assertEqual(m.ACTION_SEQUENCE[0], 'project.create')
        self.assertEqual(m.ACTION_SEQUENCE[-1], 'sub2api.import_disabled')
        self.assertNotIn('google.login.password.submit', m.ACTION_SEQUENCE)

    def test_invalid_handoff_inputs_never_touch_connection(self):
        m = self.module()
        actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        args = [object(), actor, str(uuid4()), 2, 'test-worker']
        for slot, value, code in ((1, object(), ErrorCode.UNAUTHENTICATED),
                                  (2, 'bad', ErrorCode.INVALID_INPUT),
                                  (3, True, ErrorCode.INVALID_INPUT),
                                  (4, '', ErrorCode.INVALID_INPUT),
                                  (4, 'batch:reserved', ErrorCode.INVALID_INPUT)):
            with self.subTest(slot=slot, value=value):
                attempt = args.copy(); attempt[slot] = value
                with self.assertRaises(ServiceError) as caught:
                    m.handoff(*attempt)
                self.assertEqual(caught.exception.code, code)

    def test_foreign_handles_never_touch_connection(self):
        m = self.module()
        for call in (lambda: m.heartbeat(object(), object()),
                     lambda: m.effect_context(object(), object(), 'project.create', 'a'*64),
                     lambda: m.sending_guard(object(), object(), 'project.create', 'a'*64),
                     lambda: m.reconcile_guard(object(), object(), str(uuid4()), 'project.create', 'a'*64)):
            with self.assertRaises(ServiceError) as caught:
                call()
            self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_mutating_handle_type_cannot_reuse_registered_identity(self):
        m = self.module()
        actor = security.Actor(str(uuid4()), frozenset({'tasks:manage'}), str(uuid4()), 1)
        worker = m._register(m.LiveWorker(actor, str(uuid4()), 1, str(uuid4()), 'pool-'+str(uuid4()),
            str(uuid4()), (str(uuid4()), None, 1, 'UNKNOWN'),
            live_effects.FenceBinding('mailbox', str(uuid4()), 'worker:test', 2, 1), 'test-worker', 3))
        object.__setattr__(worker, 'generation', True)
        with patch.object(m, '_graph', side_effect=AssertionError('forged identity reached SQL')):
            with self.assertRaises(ServiceError) as caught:
                m.heartbeat(object(), worker)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)


class LiveRuntimeDBTests(PoolCase):
    reject = batch_tests.PoolBatchEntryTests.reject

    def setUp(self):
        super().setUp()
        from onboarding_live_support import seed_live_runtime
        self.m = importlib.import_module('onboarding.live_runtime')
        seed_live_runtime(self)

    def admit(self, config=None):
        from onboarding_live_support import admit
        return admit(self, config)

    def call(self, config, seeds, *, request_key='runtime:create'):
        from onboarding_live_support import create_batch
        return create_batch(self, config, seeds, request_key=request_key)

    def change(self, query, args=()):
        with self.uow() as conn:
            conn.execute(query, args)

    def handoff(self, actor=None, version=2):
        with self.uow() as conn:
            return self.m.handoff(conn, actor or self.actor, self.tid, version, 'test-worker')

    def heartbeat(self, worker):
        with self.uow() as conn:
            return self.m.heartbeat(conn, worker)

    def context(self, worker, action='project.create', digest='a'*64):
        with self.uow() as conn:
            return self.m.effect_context(conn, worker, action, digest)

    def sending(self, worker, action='project.create', digest='a'*64):
        with self.uow() as conn:
            return self.m.sending_guard(conn, worker, action, digest)

    def reconcile(self, worker, record, action='project.create', digest='a'*64):
        with self.uow() as conn:
            return self.m.reconcile_guard(conn, worker, record.receipt_id, action, digest)

    def prepare(self, worker):
        ctx = self.context(worker)
        with self.uow() as conn:
            record = live_effects.prepare(conn, ctx, ctx.step_key, 'first')
        return ctx, record

    def test_handoff_transfers_exact_batch_hold_atomically_without_effect(self):
        before = self.read('SELECT owner_id,fence,version FROM resource_leases')[0]
        with patch('onboarding.pool_leases.claim', side_effect=AssertionError('UNUSED claim forbidden')):
            worker = self.handoff()
        after = self.read('SELECT owner_id,fence,version,hold_reason FROM resource_leases')[0]
        self.assertTrue(before[0].startswith('batch:'))
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(after[1:], (before[1]+1, before[2]+1, 'HELD'))
        self.assertEqual(worker.binding.owner_id, after[0])
        self.assertEqual(self.read('SELECT status,current_step,version FROM onboarding_tasks')[0],
                         ('RUNNING', 'project.create', 3))
        self.assertEqual(self.read("SELECT usage_status,last_task_id FROM mailbox_platform_states WHERE platform='google'")[0],
                         ('RESERVED', UUID(self.tid)))
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal')[0][0], 0)
        self.assertFalse(hasattr(worker, 'consume'))

    def test_formal_create_keeps_google_identity_and_one_shared_mailbox_secret(self):
        row=self.read("SELECT m.health,s.identity_status,s.usage_status,m.credential_ref,s.credential_ref,"
            "m.credential_version,s.credential_version FROM mailbox_registry m JOIN mailbox_platform_states s "
            "ON s.mailbox_id=m.id WHERE s.platform='google'")[0]
        self.assertEqual(row[:3],('UNKNOWN','UNKNOWN','RESERVED'))
        self.assertEqual(row[3],row[4]); self.assertEqual(row[5:],(1,1))
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE kind='mailbox_credential'"),[(1,)])
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE kind='platform_credential'"),[(0,)])
        self.assertEqual(self.read("SELECT DISTINCT usage_status FROM mailbox_platform_states WHERE platform<>'google'"),
                         [('HISTORY_UNRECONCILED',)])

    def test_relabelled_v1_config_or_old_receipt_cannot_start_formal_live_task(self):
        from psycopg.types.json import Jsonb
        from test_onboarding_live_config import LEGACY
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                         (Jsonb(LEGACY),self.cfg['config_id']))
        self.reject(self.handoff,ErrorCode.FORBIDDEN)
        from test_onboarding_live_config import FIELDS
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE global_configs SET nonsecret_config=%s WHERE id=%s',
                         (Jsonb(FIELDS),self.cfg['config_id']))
            conn.execute("UPDATE operation_receipts SET action='pool.batch.create' WHERE id=%s",
                         (self.batch['receipt_id'],))
        self.reject(self.handoff,ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.read('SELECT status,version FROM onboarding_tasks'),[('QUEUED',2)])

    def test_superseding_head_does_not_change_the_task_plan(self):
        from onboarding import live_config,live_run_store
        from test_onboarding_live_config import FIELDS
        live_config.replace(self.settings,self.actor,self.cfg['revision'],
            {**FIELDS,'project_prefix':'new-head'},'runtime:new-head',policy=self.live_policy,mac=self.mac)
        worker=self.handoff()
        with self.uow() as conn:
            self.assertEqual(live_run_store.bind_plan(conn,worker,self.plan),self.plan)
            self.assertEqual(live_run_store.read_plan(conn,worker),self.plan)

    def test_first_plan_bind_and_persisted_plan_reject_arbitrary_targets(self):
        from dataclasses import asdict,replace
        from psycopg.types.json import Jsonb
        from onboarding import live_run_store
        worker=self.handoff()
        changed=replace(self.plan,project_id='rf-unapproved-project')
        def bind(plan):
            with self.uow() as conn:
                return live_run_store.bind_plan(conn,worker,plan)
        self.reject(lambda:bind(changed),ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.read('SELECT count(*) FROM live_run_plans'),[(0,)])
        bind(self.plan)
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE live_run_plans SET plan=%s,plan_digest=%s WHERE task_id=%s',
                         (Jsonb(asdict(changed)),changed.effect_digest('project.create'),self.tid))
        def read():
            with self.uow() as conn:
                return live_run_store.read_plan(conn,worker)
        self.reject(read,ErrorCode.RECONCILIATION_REQUIRED)

    def test_handoff_does_not_promote_synthetic_or_adopt_started_task(self):
        self.change("UPDATE onboarding_tasks SET execution_mode='synthetic'")
        self.reject(self.handoff, ErrorCode.FORBIDDEN)
        self.change("UPDATE onboarding_tasks SET execution_mode='live'")
        self.handoff()
        self.reject(lambda: self.handoff(version=3), ErrorCode.RECONCILIATION_REQUIRED)

    def test_handoff_rejects_stale_version_and_any_step_history(self):
        self.reject(lambda: self.handoff(version=1), ErrorCode.VERSION_CONFLICT)
        self.change("INSERT INTO task_steps(id,task_id,step_key,generation,state) VALUES(%s,%s,'old',1,'SUCCEEDED')",
                    (str(uuid4()), self.tid))
        self.reject(self.handoff, ErrorCode.RECONCILIATION_REQUIRED)

    def test_handoff_rejects_any_live_journal_history(self):
        self.change("INSERT INTO live_effect_journal(id,task_id,step_key,action,generation,config_revision,"
                    "resource_revision,effect_digest,request_hash,request_key,fence_bindings,phase) "
                    "VALUES(%s,%s,'project.create','project.create',1,%s,%s,%s,%s,'old','[]','FAILED_CONFIRMED')",
                    (str(uuid4()), self.tid, self.cfg['revision'], 'a'*64, 'b'*64, 'c'*64))
        self.reject(self.handoff, ErrorCode.RECONCILIATION_REQUIRED)

    def test_batch_owner_fence_or_version_change_cannot_be_adopted(self):
        for query in ("UPDATE resource_leases SET owner_id='worker:other'",
                      'UPDATE resource_leases SET fence=fence+1',
                      'UPDATE resource_leases SET version=version+1'):
            with self.subTest(query=query), self.uow() as conn:
                with self.assertRaises(ServiceError):
                    with conn.transaction():
                        conn.execute(query)
                        self.m.handoff(conn, self.actor, self.tid, 2, 'test-worker')

    def test_expired_batch_hold_is_not_a_takeover_opportunity(self):
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        self.reject(self.handoff, ErrorCode.STALE_FENCE)

    def test_handoff_rollback_cannot_authorize_or_replay(self):
        with self.assertRaises(ServiceError) as rolled_back:
            with self.uow() as conn:
                worker = self.m.handoff(conn, self.actor, self.tid, 2, 'test-worker')
                raise RuntimeError('fixture rollback')
        self.assertEqual(rolled_back.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.reject(lambda: self.heartbeat(worker), ErrorCode.STALE_FENCE)
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks')[0][0], 'QUEUED')

    def test_heartbeat_renews_only_current_owner_without_changing_fence(self):
        worker = self.handoff()
        before = self.read('SELECT fence,version,lease_until FROM resource_leases')[0]
        self.heartbeat(worker)
        after = self.read('SELECT fence,version,lease_until FROM resource_leases')[0]
        self.assertEqual(after[:2], (before[0], before[1]+1))
        self.assertGreaterEqual(after[2], before[2])

    def test_heartbeat_cannot_resurrect_expiry_or_new_fence(self):
        worker = self.handoff()
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        self.reject(lambda: self.heartbeat(worker), ErrorCode.STALE_FENCE)
        self.change("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '60 seconds',fence=fence+1")
        self.reject(lambda: self.heartbeat(worker), ErrorCode.STALE_FENCE)

    def test_handle_is_bound_to_process_object_and_original_session(self):
        worker = self.handoff()
        self.reject(lambda: self.heartbeat(copy.copy(worker)), ErrorCode.FORBIDDEN)
        with patch('onboarding.live_runtime.os.getpid', return_value=os.getpid()+1):
            self.reject(lambda: self.heartbeat(worker), ErrorCode.FORBIDDEN)
        self.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                    (self.actor.session_id,))
        self.reject(lambda: self.heartbeat(worker), ErrorCode.UNAUTHENTICATED)
        self.reject(lambda: self.handoff(self.second_session(), 3), ErrorCode.RECONCILIATION_REQUIRED)

    def test_current_mailbox_and_platform_pins_required_each_time(self):
        worker = self.handoff()
        for query in ('UPDATE mailbox_registry SET credential_version=credential_version+1',
                      'UPDATE mailbox_platform_states SET credential_version=credential_version+1'):
            with self.subTest(query=query), self.uow() as conn:
                with self.assertRaises(ServiceError) as caught:
                    with conn.transaction():
                        conn.execute(query)
                        self.m.heartbeat(conn, worker)
                self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)

    def test_current_config_generation_and_owner_required_each_time(self):
        worker = self.handoff()
        for query, args, code in (
            ('UPDATE onboarding_tasks SET generation=generation+1', (), ErrorCode.VERSION_CONFLICT),
            ('UPDATE onboarding_tasks SET config_id=%s', (self.config_id,), ErrorCode.VERSION_CONFLICT),
            ('UPDATE resource_leases SET owner_id=%s', ('worker:another',), ErrorCode.STALE_FENCE),
        ):
            with self.subTest(query=query), self.uow() as conn:
                with self.assertRaises(ServiceError) as caught:
                    with conn.transaction():
                        conn.execute(query, args)
                        self.m.heartbeat(conn, worker)
                self.assertEqual(caught.exception.code, code)

    def test_cancel_or_pause_blocks_send_but_reconcile_remains_readonly(self):
        worker = self.handoff(); ctx, record = self.prepare(worker)
        permits = len(live_effects._permits)
        live_commands.cancel(self.settings, self.actor, self.tid, 3, 'runtime:cancel',
                             policy=self.live_policy, mac=self.mac)
        self.reject(lambda: self.sending(worker), ErrorCode.RECONCILIATION_REQUIRED)
        before = self.read('SELECT * FROM resource_leases')
        self.assertIs(self.reconcile(worker, record), True)
        self.assertEqual(self.read('SELECT * FROM resource_leases'), before)
        self.assertEqual(len(live_effects._permits), permits)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0], 'INTENT')
        self.heartbeat(worker)  # Retaining a hold is not sending authorization.

    def test_reconcile_checks_action_target_receipt_and_original_binding(self):
        worker = self.handoff(); _, record = self.prepare(worker)
        for action, digest in (('billing.link', 'a'*64), ('project.create', 'b'*64)):
            self.reject(lambda: self.reconcile(worker, record, action, digest), ErrorCode.RECONCILIATION_REQUIRED)
        self.change('UPDATE resource_leases SET fence=fence+1')
        self.reject(lambda: self.reconcile(worker, record), ErrorCode.STALE_FENCE)

    def test_context_is_not_permit_and_future_step_cannot_send(self):
        worker = self.handoff()
        self.assertFalse(hasattr(self.context(worker), 'consume'))
        self.assertIs(self.sending(worker), True)
        self.reject(lambda: self.sending(worker, 'billing.link'), ErrorCode.RECONCILIATION_REQUIRED)

    def test_different_target_cannot_guard_existing_intent(self):
        worker = self.handoff(); self.prepare(worker)
        self.reject(lambda: self.sending(worker, digest='b'*64), ErrorCode.RECONCILIATION_REQUIRED)

    def test_advance_requires_success_and_preserves_task_and_resource_hold(self):
        worker = self.handoff(); ctx, record = self.prepare(worker)
        with self.uow() as conn:
            with self.assertRaises(ServiceError):
                self.m.advance(conn, worker, record.receipt_id, ctx.effect_digest)
        with self.uow() as conn:
            live_effects.observe(conn, ctx, record.receipt_id, 'SUCCEEDED', 'CONFIRMED', 'evidence:'+str(uuid4()))
        with self.uow() as conn:
            self.assertEqual(self.m.advance(conn, worker, record.receipt_id, ctx.effect_digest), 'billing.link')
        self.assertEqual(self.read('SELECT status,current_step FROM onboarding_tasks')[0], ('RUNNING', 'billing.link'))
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0], 'HELD')

    def test_fixed_sequence_never_marks_task_success_or_releases_hold(self):
        worker = self.handoff()
        for index, action in enumerate(self.m.ACTION_SEQUENCE):
            digest = str(index+1)*64
            ctx = self.context(worker, action, digest)
            with self.uow() as conn:
                record = live_effects.prepare(conn, ctx, action, 'step-'+str(index))
                live_effects.observe(conn, ctx, record.receipt_id, 'SUCCEEDED', 'CONFIRMED', 'evidence:'+str(uuid4()))
            with self.uow() as conn:
                next_step = self.m.advance(conn, worker, record.receipt_id, digest)
            expected = self.m.ACTION_SEQUENCE[index+1] if index+1 < len(self.m.ACTION_SEQUENCE) else None
            self.assertEqual(next_step, expected)
        self.assertEqual(self.read('SELECT status,current_step FROM onboarding_tasks')[0],
                         ('RUNNING', 'sub2api.import_disabled'))
        self.assertEqual(self.read('SELECT task_id,hold_reason FROM resource_leases')[0],
                         (UUID(self.tid), 'HELD'))

    def test_reconcile_has_no_database_writes_and_cannot_bypass_session_revocation(self):
        worker = self.handoff(); _, record = self.prepare(worker)
        queries = []
        with after_query(lambda cursor, query: queries.append(query)):
            self.assertIs(self.reconcile(worker, record), True)
        self.assertFalse(any(query.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE') for query in queries))
        self.change('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.actor.session_id,))
        self.reject(lambda: self.reconcile(worker, record), ErrorCode.UNAUTHENTICATED)

    def test_final_auth_clock_after_blocking_audit_rolls_back_heartbeat(self):
        worker = self.handoff()
        before = self.read('SELECT * FROM resource_leases')
        fired = []
        def expire(cursor, query):
            if 'INSERT INTO audit_events' in query and not fired:
                fired.append(True)
                cursor.connection.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()-interval '1 second',"
                                          "idle_expires_at=clock_timestamp()-interval '2 seconds'")
        with after_query(expire):
            self.reject(lambda: self.heartbeat(worker), ErrorCode.UNAUTHENTICATED)
        self.assertTrue(fired)
        self.assertEqual(self.read('SELECT * FROM resource_leases'), before)

    def test_final_lease_clock_after_blocking_audit_rolls_back_handoff(self):
        before = self.read('SELECT * FROM resource_leases')
        fired = []
        def expire(cursor, query):
            if 'INSERT INTO audit_events' in query and not fired:
                fired.append(True)
                cursor.connection.execute("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        with after_query(expire):
            self.reject(self.handoff, ErrorCode.STALE_FENCE)
        self.assertTrue(fired)
        self.assertEqual(self.read('SELECT * FROM resource_leases'), before)
