"""Durable plan/evidence contracts in real disposable PostgreSQL; no providers."""
from dataclasses import replace
import importlib
import importlib.util
from uuid import uuid4
import unittest
from unittest.mock import Mock, patch

import psycopg

from onboarding import live_effects, migrate, migration_catalog
from onboarding.errors import ServiceError, ErrorCode
from onboarding.google_cloud import GooglePlan, GoogleObservation
from onboarding.sub2api_import import ImportObservation
from onboarding_pool_support import PoolCase
import test_onboarding_live_runtime as runtime_tests


class LiveRunStoreCatalogTests(unittest.TestCase):
    def test_reviewed_append_only_plan_and_evidence_migration(self):
        versions = migration_catalog.expected_schema_versions(4)
        self.assertEqual(versions[:3], migration_catalog.expected_schema_versions(3))
        text = migration_catalog.load_migration(4)
        self.assertIn('CREATE TABLE live_run_plans', text)
        self.assertIn('CREATE TABLE live_effect_observations', text)
        self.assertNotIn("execution_mode='live'", text)


class LiveRunStoreProjectionTests(unittest.TestCase):
    def test_import_persisted_extra_fields_are_not_projected(self):
        from types import SimpleNamespace
        from onboarding import live_run_store as m
        rid, tid, eid = (str(uuid4()) for _ in range(3))
        context = SimpleNamespace(step_key='sub2api.import_disabled', effect_digest='a'*64)
        worker = SimpleNamespace(task_id=tid)
        base = [eid, 'SUCCEEDED', 'DISABLED_IMPORT_VERIFIED', None, None, None, 31,
                context.step_key, tid, 'PASSED', 'DISABLED', 'NOT_SENT']
        for column, value in ((3,'private-secret-canary'), (4,'operations/stray'), (5,str(uuid4()))):
            row = base.copy(); row[column] = value
            conn = Mock(); conn.execute.return_value.fetchone.return_value = tuple(row)
            with self.subTest(column=column), patch.object(m, '_context', return_value=object()), \
                 patch.object(m.live_effects, '_row', return_value={'phase':'SUCCEEDED','evidence_ref':'evidence:'+eid}), \
                 patch.object(m.live_runtime, 'reconcile_guard', return_value=True):
                with self.assertRaises(ServiceError) as caught:
                    m.last_observation(conn, worker, context, rid)
                self.assertNotIn('canary', str(caught.exception))


class LiveRunStoreTests(PoolCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_run_store'), 'durable plan/evidence store missing')
        case = runtime_tests.LiveRuntimeDBTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        for name in ('fixture', 'settings', 'actor', 'keyring', 'tid', 'cfg'):
            setattr(self, name, getattr(case, name))
        self.runtime = case.m
        with self.fixture.migrator() as conn:
            migrate.apply_all(conn, self.fixture.schema, target_version=4)
        self.worker = case.handoff()
        self.m = importlib.import_module('onboarding.live_run_store')
        self.plan = case.plan

    def bind(self, plan=None):
        with self.uow() as conn:
            return self.m.bind_plan(conn, self.worker, plan or self.plan)

    def context(self, action='project.create'):
        with self.uow() as conn:
            return self.runtime.effect_context(conn, self.worker, action, self.plan.effect_digest(action))

    def prepare(self):
        self.bind()
        ctx = self.context()
        receipt = live_effects.prepare_committed(self.settings, ctx, ctx.step_key, 'run-step')
        return ctx, receipt

    def record(self, ctx, receipt, observation):
        with self.uow() as conn:
            return self.m.record(conn, self.worker, ctx, receipt.receipt_id, observation)

    def test_plan_is_persistent_immutable_and_matches_owner_task_generation(self):
        self.bind()
        self.bind()
        with self.uow() as conn:
            self.assertEqual(self.m.read_plan(conn, self.worker), self.plan)
        self.assertEqual(self.read('SELECT count(*) FROM live_run_plans')[0][0], 1)
        for changed in (replace(self.plan, billing_account='DDDDDD-EEEEEE-FFFFFF'),
                        replace(self.plan, owner_id=str(uuid4())), replace(self.plan, task_id=str(uuid4()))):
            with self.subTest(plan=changed), self.assertRaises(ServiceError):
                self.bind(changed)

    def test_plan_bind_after_first_intent_is_forbidden(self):
        ctx = self.context()
        live_effects.prepare_committed(self.settings, ctx, ctx.step_key, 'no-plan')
        with self.assertRaises(ServiceError):
            self.bind()
        self.assertEqual(self.read('SELECT count(*) FROM live_run_plans')[0][0], 0)

    def test_missing_plan_or_wrong_digest_does_not_accept_observation(self):
        ctx = self.context()
        receipt = live_effects.prepare_committed(self.settings, ctx, ctx.step_key, 'no-plan')
        with self.assertRaises(ServiceError):
            self.record(ctx, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 0)

    def test_pending_stores_operation_and_unknown_atomically_then_readback_advances(self):
        ctx, receipt = self.prepare()
        item = self.record(ctx, receipt, GoogleObservation('PENDING', 'READBACK_REQUIRED', operation='operations/run-1'))
        self.assertEqual((item.phase, item.operation), ('UNKNOWN', 'operations/run-1'))
        self.assertEqual(self.read('SELECT phase,evidence_ref FROM live_effect_journal')[0], ('UNKNOWN', item.evidence_ref))
        with self.uow() as conn:
            self.assertEqual(self.m.last_observation(conn, self.worker, ctx, receipt.receipt_id), item)
            self.assertFalse(hasattr(item, 'consume'))
        success = self.record(ctx, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        with self.uow() as conn:
            self.assertEqual(self.runtime.advance(conn, self.worker, receipt.receipt_id, ctx.effect_digest), 'billing.link')
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 2)
        self.assertNotEqual(success.evidence_ref, item.evidence_ref)

    def test_cancelled_task_allows_evidence_only_and_blocks_advance(self):
        ctx, receipt = self.prepare()
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET cancel_requested=true WHERE id=%s', (self.tid,))
        self.record(ctx, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        with self.assertRaises(ServiceError), self.uow() as conn:
            self.runtime.advance(conn, self.worker, receipt.receipt_id, ctx.effect_digest)
        self.assertIsNotNone(self.read('SELECT task_id FROM resource_leases')[0][0])

    def test_failed_or_blocked_unknown_conservatively_preserves_hold(self):
        ctx, receipt = self.prepare()
        item = self.record(ctx, receipt, GoogleObservation('BLOCKED', 'TOKEN_UNAVAILABLE'))
        self.assertEqual(item.phase, 'UNKNOWN')
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0], 'UNKNOWN')
        item = self.record(ctx, receipt, GoogleObservation('FAILED_CONFIRMED', 'PROVIDER_REJECTED'))
        self.assertEqual(item.phase, 'FAILED_CONFIRMED')
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks')[0][0], 'RUNNING')

    def test_malformed_provider_text_fields_rejected_without_leaking_to_evidence(self):
        ctx, receipt = self.prepare()
        invalid = [GoogleObservation('SUCCEEDED', 'private token canary'),
                   GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'Bearer-secret-canary'),
                   GoogleObservation('PENDING', 'READBACK_REQUIRED', operation='https://evil.invalid/token'),
                   GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', secret_ref=str(uuid4())),
                   GoogleObservation('SUCCEEDED', 'KEY_SAVED', 'projects/123'),
                   ImportObservation('SUCCEEDED', 'DISABLED_IMPORT_VERIFIED', account_id=1),
                   {'phase': 'SUCCEEDED'}]
        for value in invalid:
            with self.subTest(shape=type(value).__name__), self.assertRaises(ServiceError):
                self.record(ctx, receipt, value)
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 0)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0], 'INTENT')

    def test_rollback_cannot_leave_evidence_or_advance_receipt(self):
        ctx, receipt = self.prepare()
        with self.assertRaises(ServiceError), self.uow() as conn:
            self.m.record(conn, self.worker, ctx, receipt.receipt_id,
                          GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
            raise RuntimeError('rollback fixture')
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 0)
        self.assertEqual(self.read('SELECT phase FROM live_effect_journal')[0][0], 'INTENT')

    def test_wrong_context_worker_or_receipt_never_records(self):
        ctx, receipt = self.prepare()
        for altered in (replace(ctx, effect_digest='b'*64), replace(ctx, generation=2),
                        replace(ctx, task_id=str(uuid4()))):
            with self.assertRaises(ServiceError):
                self.record(altered, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        with self.assertRaises(ServiceError), self.uow() as conn:
            self.m.record(conn, self.worker, ctx, str(uuid4()), GoogleObservation('SUCCEEDED','PROJECT_MATCHED','projects/123'))
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 0)

    def test_import_without_verified_source_key_cannot_record_success(self):
        self.bind()
        action = 'sub2api.import_disabled'
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_tasks SET current_step=%s WHERE id=%s', (action, self.tid))
            ctx = self.runtime.effect_context(conn, self.worker, action, 'a'*64)
        permit = live_effects.prepare_committed(self.settings, ctx, action, 'import-without-source')
        with self.assertRaises(ServiceError):
            self.record(ctx, permit, ImportObservation('SUCCEEDED', 'DISABLED_IMPORT_VERIFIED',
                                                       account_id=3, scheduling='DISABLED'))
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 0)

    def test_actual_terminal_conflict_is_not_reported_as_incoming_success(self):
        ctx, receipt = self.prepare()
        self.record(ctx, receipt, GoogleObservation('FAILED_CONFIRMED', 'PROVIDER_REJECTED'))
        item = self.record(ctx, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        self.assertEqual((item.phase, item.incoming_phase), ('CONFLICT', 'FAILED_CONFIRMED'))
        with self.uow() as conn:
            self.assertEqual(self.m.last_observation(conn, self.worker, ctx, receipt.receipt_id).phase, 'CONFLICT')

    def test_retained_success_never_returns_new_external_target(self):
        ctx, receipt = self.prepare()
        first = self.record(ctx, receipt, GoogleObservation('SUCCEEDED','PROJECT_MATCHED','projects/123'))
        for observation in (GoogleObservation('SUCCEEDED','PROJECT_MATCHED','projects/999'),
                            GoogleObservation('CONFLICT','PROJECT_IDENTITY_MISMATCH','projects/999')):
            result = self.record(ctx, receipt, observation)
            self.assertEqual(result, first)
            with self.uow() as conn:
                self.assertEqual(self.m.last_observation(conn, self.worker, ctx, receipt.receipt_id), first)
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_observations')[0][0], 3)

    def test_latest_evidence_not_reordered_by_backwards_clock(self):
        ctx, receipt = self.prepare()
        first = self.record(ctx, receipt, GoogleObservation('PENDING','READBACK_REQUIRED',operation='operations/run-1'))
        second = self.record(ctx, receipt, GoogleObservation('PENDING','OPERATION_PENDING',operation='operations/run-1'))
        # Trusted fixture migration connection models clock skew in metadata only.
        with self.fixture.migrator() as conn:
            conn.execute("UPDATE live_effect_observations SET observed_at='2020-01-01 UTC' WHERE id=%s",
                         (second.evidence_ref[9:],))
        with self.uow() as conn:
            actual = self.m.last_observation(conn, self.worker, ctx, receipt.receipt_id)
        self.assertEqual(actual.evidence_ref, second.evidence_ref)

    def test_corrupt_persisted_evidence_cannot_return_raw_provider_text(self):
        ctx, receipt = self.prepare()
        item = self.record(ctx, receipt, GoogleObservation('SUCCEEDED', 'PROJECT_MATCHED', 'projects/123'))
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE live_effect_observations SET external_ref=%s WHERE id=%s',
                         ('private-secret-canary', item.evidence_ref[9:]))
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            self.m.last_observation(conn, self.worker, ctx, receipt.receipt_id)
        self.assertNotIn('canary', str(caught.exception))

    def test_app_cannot_update_or_delete_plan_or_evidence(self):
        ctx, receipt = self.prepare()
        self.record(ctx, receipt, GoogleObservation('PENDING', 'READBACK_REQUIRED', operation='operations/run-1'))
        with self.fixture.app() as conn:
            for table in ('live_run_plans', 'live_effect_observations'):
                for sql in ('UPDATE '+table+' SET task_id=task_id', 'DELETE FROM '+table):
                    with self.subTest(sql=sql), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        conn.execute(sql)


if __name__ == '__main__':
    unittest.main()
