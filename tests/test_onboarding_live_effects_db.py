"""Real PostgreSQL, fresh manifest-scoped schema only; no provider execution."""
import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import unittest
import psycopg
from psycopg.types.json import Jsonb

from onboarding_b2_support import B2Case
from onboarding.errors import ErrorCode, ServiceError
from onboarding.migrate import apply_all


class LiveEffectsDBTests(B2Case):
    def setUp(self):
        super().setUp()
        migration = Path('onboarding/migrations/003_live_effects.sql')
        self.assertTrue(migration.exists(), 'live journal migration missing')
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=3)
        from onboarding import live_effects
        self.m = live_effects
        task = self.task()
        self.tid = task['id']
        mid, sid, secret = (str(uuid4()) for _ in range(3))
        with self.uow() as conn:
            conn.execute("INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) "
                         "VALUES(%s,'mailbox_credential','test',%s,%s,%s)",
                         (secret, bytes(12), bytes(16), f'pool:{self.actor.operator_id}:mailbox:{mid}:v1'))
            conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref,health) "
                         "VALUES(%s,%s,%s,'other',%s,'HEALTHY')", (mid,self.actor.operator_id,'test@fixture.invalid',secret))
            conn.execute("INSERT INTO mailbox_platform_states(id,mailbox_id,platform,usage_status,last_task_id) "
                         "VALUES(%s,%s,'google','RESERVED',%s)", (sid,mid,self.tid))
            pins = {'google':dict(state_id=sid,secret_ref=None,revision=1,identity_status='UNKNOWN')}
            conn.execute("UPDATE onboarding_tasks SET execution_scope='pool',execution_mode='live',mailbox_id=%s,"
                         "mailbox_ref=%s,platform='google',platform_plan=%s,credential_version=1,mailbox_credential_ref=%s,"
                         "platform_credential_pins=%s,status='RUNNING',current_step='google.login.email.submit' WHERE id=%s",
                         (mid,mid,Jsonb(['google']),secret,Jsonb(pins),self.tid))
            conn.execute("INSERT INTO resource_leases(resource_kind,resource_id,task_id,owner_id,fence,lease_until,hold_reason) "
                         "VALUES('mailbox',%s,%s,'worker:test',1,clock_timestamp()+interval '60 seconds','HELD')",(mid,self.tid))
        self.ctx = self.m.EffectContext(self.actor,self.tid,1,self.config_revision,'google.login.email.submit',
            (self.m.FenceBinding('mailbox',mid,'worker:test',1,1),), 'a'*64)

    def prepare(self,key='test-request',ctx=None):
        context = ctx or self.ctx
        with self.uow() as conn:
            return self.m.prepare(conn,context,context.step_key,key)

    def reject(self,call,code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code,code)

    def test_intent_is_durable_idempotent_and_success_not_task_success(self):
        first=self.prepare(); again=self.prepare()
        self.assertEqual(first.receipt_id,again.receipt_id)
        self.assertTrue(first.newly_created); self.assertFalse(again.newly_created)
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks WHERE id=%s',(self.tid,))[0][0],'RUNNING')
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0],'SUCCEEDED')

    def test_new_key_cannot_repeat_step_even_after_success(self):
        first=self.prepare()
        self.reject(lambda:self.prepare('other-key'),ErrorCode.RECONCILIATION_REQUIRED)
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
        self.reject(lambda:self.prepare('third-key'),ErrorCode.RECONCILIATION_REQUIRED)

    def test_stale_fence_expired_lease_and_cancel_deny(self):
        self.reject(lambda:self.prepare(ctx=replace(self.ctx,bindings=(replace(self.ctx.bindings[0],fence=2),))),ErrorCode.STALE_FENCE)
        with self.uow() as conn:
            conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second'")
        self.reject(self.prepare,ErrorCode.STALE_FENCE)
        with self.uow() as conn:
            conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '60 seconds'")
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s',(self.tid,))
        self.reject(self.prepare,ErrorCode.RECONCILIATION_REQUIRED)

    def test_synthetic_task_rejected_at_service_and_database(self):
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET execution_mode='synthetic' WHERE id=%s",(self.tid,))
        self.reject(self.prepare,ErrorCode.FORBIDDEN)
        with self.fixture.migrator() as conn:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute("INSERT INTO live_effect_journal(id,task_id,step_key,action,generation,config_revision,"
                    "resource_revision,effect_digest,request_hash,request_key,fence_bindings) VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,'x','[]')",
                    (str(uuid4()),self.tid,self.ctx.step_key,self.ctx.step_key,self.config_revision,'a'*64,'a'*64,'b'*64))

    def test_read_recovery_has_no_permit_and_changed_revision_conflicts(self):
        first=self.prepare()
        with self.uow() as conn:
            record=self.m.read_record(conn,self.ctx,first.receipt_id)
        self.assertFalse(hasattr(record,'consume'))
        self.reject(lambda:self.prepare(ctx=replace(self.ctx,config_revision='changed')),ErrorCode.VERSION_CONFLICT)

    def test_final_guard_checks_cancel_after_confirmed_commit(self):
        permit=self.m.prepare_committed(self.settings,self.ctx,self.ctx.step_key,'permit')
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s',(self.tid,))
        self.reject(lambda:asyncio.run(permit.consume(self.ctx.step_key)),ErrorCode.RECONCILIATION_REQUIRED)
        self.reject(lambda:asyncio.run(permit.consume(self.ctx.step_key)),ErrorCode.FORBIDDEN)

    def test_unknown_keeps_hold_and_conflicting_confirmation_is_conflict(self):
        first=self.prepare()
        for phase in ('UNKNOWN','SUCCEEDED','FAILED_CONFIRMED'):
            with self.uow() as conn:
                self.m.observe(conn,self.ctx,first.receipt_id,phase,'CONFIRMED','evidence:'+str(uuid4()))
            if phase=='UNKNOWN':
                self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0],'UNKNOWN')
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0],'CONFLICT')
        self.assertEqual(self.read('SELECT task_id FROM resource_leases')[0][0].hex,self.tid.replace('-',''))

    def test_confirmed_evidence_survives_inconclusive_late_result(self):
        first=self.prepare(); evidence='evidence:'+str(uuid4())
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED',evidence)
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'UNKNOWN','TIMEOUT','evidence:'+str(uuid4()))
        self.assertEqual(self.read('SELECT phase,result_code,evidence_ref FROM live_effect_journal')[0],
                         ('SUCCEEDED','CONFIRMED',evidence))

    def test_prepare_and_observe_have_appendonly_audit(self):
        first=self.prepare()
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'UNKNOWN','TIMEOUT','evidence:'+str(uuid4()))
        self.assertEqual(self.read("SELECT action FROM audit_events WHERE correlation_id=%s ORDER BY id",
                                  (first.receipt_id,)),[('receipt.prepare',),('receipt.observe',)])

    def test_real_guard_can_consume_and_revoked_session_cannot(self):
        permit=self.m.prepare_committed(self.settings,self.ctx,self.ctx.step_key,'real-consume')
        self.assertIs(asyncio.run(permit.consume(self.ctx.step_key)),True)
        first=self.prepare('real-consume')
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
        ctx=replace(self.ctx,step_key='google.login.password.submit')
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET current_step=%s WHERE id=%s',(ctx.step_key,self.tid))
        permit=self.m.prepare_committed(self.settings,ctx,ctx.step_key,'revoked')
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
        self.reject(lambda:asyncio.run(permit.consume(ctx.step_key)),ErrorCode.UNAUTHENTICATED)

    def test_wrong_current_step_cannot_send(self):
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET current_step='other.step' WHERE id=%s",(self.tid,))
        self.reject(self.prepare,ErrorCode.RECONCILIATION_REQUIRED)

    def test_effect_target_digest_cannot_change_under_same_request_key(self):
        self.prepare()
        self.reject(lambda:self.prepare(ctx=replace(self.ctx,effect_digest='b'*64)),ErrorCode.IDEMPOTENCY_CONFLICT)

    def next_effect(self):
        first=self.prepare()
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
            conn.execute("UPDATE onboarding_tasks SET current_step='google.login.password.submit' WHERE id=%s",(self.tid,))
        next_context=replace(self.ctx,step_key='google.login.password.submit')
        permit=self.m.prepare_committed(self.settings,next_context,next_context.step_key,'next-effect')
        return first,next_context,permit

    def test_late_success_cannot_downgrade_other_intent_hold(self):
        first,_,_=self.next_effect()
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0],'INTENT')

    def test_late_success_cannot_downgrade_other_unknown_or_conflict_hold(self):
        first,context,permit=self.next_effect()
        for phase,expected in (('UNKNOWN','UNKNOWN'),('SUCCEEDED','HELD'),('FAILED_CONFIRMED','CONFLICT')):
            with self.uow() as conn:
                self.m.observe(conn,context,permit.receipt_id,phase,'CONFIRMED','evidence:'+str(uuid4()))
                self.m.observe(conn,self.ctx,first.receipt_id,'SUCCEEDED','CONFIRMED','evidence:'+str(uuid4()))
            self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0],expected)

    def test_prior_conflict_blocks_prepared_next_effect_at_dispatch(self):
        first,context,permit=self.next_effect()
        with self.uow() as conn:
            self.m.observe(conn,self.ctx,first.receipt_id,'FAILED_CONFIRMED','CONTRADICTED','evidence:'+str(uuid4()))
        self.reject(lambda:asyncio.run(permit.consume(context.step_key)),ErrorCode.RECONCILIATION_REQUIRED)
