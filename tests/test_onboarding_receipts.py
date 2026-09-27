"""Fixture-only durable intent, idempotency and fenced observation tests."""
import importlib.util
import unittest


class ReceiptBoundaryTests(unittest.TestCase):
    def test_receipt_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.receipts'),
                             'B1 durable intent boundary is absent')


class ReceiptAPITests(unittest.TestCase):
    def test_prepare_and_observation_contracts_exist(self):
        from onboarding import receipts
        for name in ('prepare', 'observe', 'record_late'):
            self.assertTrue(callable(getattr(receipts, name, None)), name)

from dataclasses import replace
import multiprocessing
from onboarding_b1_support import B1Case
from onboarding.contracts import Observation
from onboarding.errors import ErrorCode, ServiceError


def _consume_worker(schema, actor, token, args, barrier, output):
    from onboarding import receipts
    from onboarding.storage import unit_of_work
    from onboarding.settings import load_settings
    from onboarding_support import BASE
    settings = replace(load_settings(BASE / 'app.json', role='app'), schema=schema)
    try:
        barrier.wait(timeout=10)
        with unit_of_work(settings) as conn:
            result = receipts.prepare(conn, actor, token, **args)
        output.put(('ok', result.newly_consumed))
    except ServiceError as exc:
        output.put(('error', exc.code.value))
    except Exception as exc:
        output.put(('unexpected', type(exc).__name__))


def _expiry_worker(schema, actor, token, args, started, output, lease_checked=None):
    from onboarding import receipts
    from onboarding.settings import load_settings
    from onboarding.storage import unit_of_work
    from onboarding_support import BASE
    settings = replace(load_settings(BASE / 'app.json', role='app'), schema=schema)
    if lease_checked is not None:
        from onboarding import leases
        original = leases.assert_current
        def checked(conn, token):
            result = original(conn, token)
            lease_checked.set()
            return result
        leases.assert_current = checked
    try:
        with unit_of_work(settings) as conn:
            conn.execute('SELECT transaction_timestamp()')
            if lease_checked is not None:
                conn.execute("SET LOCAL lock_timeout='5s'")
            started.set()
            receipts.prepare(conn, actor, token, **args)
        output.put('UNEXPECTED_SUCCESS')
    except ServiceError as exc:
        output.put(exc.code.value)
    except Exception:
        output.put('UNEXPECTED_ERROR')


def _serialized_worker(schema, actor, token, args, operation, started, output):
    from onboarding import approvals, receipts, repository
    from onboarding.settings import load_settings
    from onboarding.storage import unit_of_work
    from onboarding_support import BASE
    settings = replace(load_settings(BASE / 'app.json', role='app'), schema=schema)
    try:
        with unit_of_work(settings) as conn:
            started.set()
            if operation == 'consume':
                result = receipts.prepare(conn, actor, token, **args)
                result = ('CONSUMED', result.newly_consumed)
            elif operation == 'revoke':
                approvals.revoke(conn, actor, args['approval_id'])
                result = ('MUTATED', None)
            else:
                task = repository.lock_task(conn, actor, token.task_id)
                repository.bump_task(conn, task, generation=task['generation'] + 1)
                result = ('MUTATED', None)
        output.put(result)
    except ServiceError as exc:
        output.put(('ERROR', exc.code.value))
    except Exception:
        output.put(('UNEXPECTED', None))


def _observe_expiry_worker(schema, actor, token, receipt_id, version, checked, output):
    from onboarding import leases, receipts
    from onboarding.settings import load_settings
    from onboarding.storage import unit_of_work
    from onboarding_support import BASE
    settings = replace(load_settings(BASE / 'app.json', role='app'), schema=schema)
    original = leases.assert_current
    def current(conn, token):
        result = original(conn, token)
        checked.set()
        return result
    leases.assert_current = current
    try:
        with unit_of_work(settings) as conn:
            conn.execute("SET LOCAL lock_timeout='5s'")
            receipts.observe(conn, actor, token, receipt_id,
                             Observation('SUCCEEDED', 'fixture:observed', None), version)
        output.put('UNEXPECTED_SUCCESS')
    except ServiceError as exc:
        output.put(exc.code.value)
    except Exception:
        output.put('UNEXPECTED_ERROR')


class ReceiptTests(B1Case):
    def ready(self):
        from onboarding import leases, approvals, repository
        task = self.task()
        with self.uow() as conn:
            token = leases.claim(conn, self.actor, str(task['id']), 'fixture',
                                 'fixture:receipt-resource', 'fixture:worker', task['version'])
            task = repository.lock_task(conn, self.actor, str(task['id']))
            revision = repository.resource_revision(task)
            approval = approvals.issue(conn, self.actor, str(task['id']), 'test', revision,
                                       task['config_revision'], 60)
        args = dict(action='test', resource_revision=revision,
                    config_revision=task['config_revision'], request_key='fixture:request',
                    payload={'test_spec_revision': 'fixture-v1'}, approval_id=approval)
        return token, args

    def version(self, token):
        return self.read('SELECT version FROM onboarding_tasks WHERE id=%s', (token.task_id,))[0][0]

    def prepare(self, token, args):
        from onboarding import receipts
        with self.uow() as conn:
            return receipts.prepare(conn, self.actor, token, **args)

    def test_prepare_is_pending_not_dispatch_permit_and_atomic_consumption(self):
        token, args = self.ready()
        result = self.prepare(token, args)
        self.assertTrue(result.newly_consumed)
        self.assertEqual(result.phase, 'INTENT')
        self.assertFalse(hasattr(result, 'dispatch_permit'))
        row = self.read('SELECT consumed_at, receipt_id FROM approvals WHERE id=%s',
                        (args['approval_id'],))[0]
        self.assertIsNotNone(row[0])
        self.assertEqual(str(row[1]), result.receipt_id)

    def test_lost_reply_replay_returns_same_receipt_without_new_consumption(self):
        token, args = self.ready()
        original = self.prepare(token, args)
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='PAUSED' WHERE id=%s", (token.task_id,))
        replay = self.prepare(replace(token, owner_id='fixture:old', fence=0), args)
        self.assertEqual(replay.receipt_id, original.receipt_id)
        self.assertFalse(replay.newly_consumed)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 1)

    def test_same_key_changed_parameters_conflict_and_secret_payload_rejected(self):
        token, args = self.ready()
        self.prepare(token, args)
        with self.assertRaises(ServiceError) as caught:
            self.prepare(token, dict(args, resource_revision='changed'))
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        for payload in ({'secret': 'canary-fixture-secret'}, {'test_spec_revision': 'canary-fixture-secret'}, {}):
            with self.assertRaises(ServiceError) as caught:
                self.prepare(token, dict(args, payload=payload))
            self.assertNotIn('canary', str(caught.exception))

    def test_expired_revoked_paused_and_changed_version_reject(self):
        for scenario in ('expired', 'revoked', 'paused', 'generation'):
            with self.subTest(scenario=scenario):
                token, args = self.ready()
                with self.uow() as conn:
                    if scenario == 'expired':
                        conn.execute("UPDATE approvals SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (args['approval_id'],))
                    elif scenario == 'revoked':
                        conn.execute('UPDATE approvals SET revoked_at=clock_timestamp() WHERE id=%s', (args['approval_id'],))
                    elif scenario == 'paused':
                        conn.execute("UPDATE onboarding_tasks SET status='PAUSED' WHERE id=%s", (token.task_id,))
                    else:
                        conn.execute('UPDATE onboarding_tasks SET generation=generation+1 WHERE id=%s', (token.task_id,))
                with self.assertRaises(ServiceError):
                    self.prepare(token, args)
                # Keep resources independent across this test's subcases.
                with self.uow() as conn:
                    conn.execute('UPDATE resource_leases SET owner_id=NULL, task_id=NULL, hold_reason=NULL')
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)

    def test_two_processes_consume_one_approval_exactly_once(self):
        token, args = self.ready()
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_consume_worker,
                     args=(self.settings.schema, self.actor, token, dict(args, request_key=f'fixture:key-{i}'), barrier, output))
                     for i in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        results = [output.get(timeout=3) for _ in processes]
        self.assertCountEqual(results, [('ok', True), ('error', ErrorCode.RECONCILIATION_REQUIRED.value)])
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 1)
        self.assertEqual(self.read('SELECT count(*) FROM approvals WHERE consumed_at IS NOT NULL')[0][0], 1)

    def test_unknown_holds_and_never_returns_approval(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        with self.uow() as conn:
            receipts.observe(conn, self.actor, token, result.receipt_id,
                             Observation('UNKNOWN', 'fixture:evidence', None), self.version(token))
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'UNKNOWN')
        self.assertIsNotNone(self.read('SELECT consumed_at FROM approvals')[0][0])
        with self.assertRaises(ServiceError):
            self.prepare(token, dict(args, request_key='fixture:new'))

    def test_conflicting_terminal_observation_freezes_receipt(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        for version, code in ((1, 'FAILED_CONFIRMED'), (2, 'SUCCEEDED')):
            with self.uow() as conn:
                receipts.observe(conn, self.actor, token, result.receipt_id,
                                 Observation(code, 'fixture:evidence', None), self.version(token))
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'CONFLICT')
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks')[0][0], 'CONFLICT')

    def test_stale_fence_cannot_advance_but_late_evidence_is_audited(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        with self.uow() as conn:
            conn.execute('UPDATE resource_leases SET fence=fence+1')
        observation = Observation('SUCCEEDED', 'fixture:late-evidence', None)
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                receipts.observe(conn, self.actor, token, result.receipt_id, observation, self.version(token))
        self.assertEqual(caught.exception.code, ErrorCode.STALE_FENCE)
        with self.uow() as conn:
            receipts.record_late(conn, self.actor, result.receipt_id, observation)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'INTENT')
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='receipt.late'")[0][0], 1)

    def test_receipt_version_and_nonfixture_evidence_rejected(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        for obs, version in ((Observation('SUCCEEDED', 'fixture:ok', None), 99),
                             (Observation('SUCCEEDED', 'https://real-provider', None), self.version(token))):
            with self.assertRaises(ServiceError):
                with self.uow() as conn:
                    receipts.observe(conn, self.actor, token, result.receipt_id, obs, version)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'INTENT')

    def test_audit_failure_rolls_back_intent_and_approval_consumption(self):
        from unittest.mock import patch
        from onboarding import audit
        token, args = self.ready()
        with patch.object(audit, 'append', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(ServiceError):
                self.prepare(token, args)
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)
        self.assertIsNone(self.read('SELECT consumed_at FROM approvals')[0][0])

    def test_other_resource_same_task_and_fence_cannot_observe_receipt(self):
        from onboarding import leases, receipts, repository
        token, args = self.ready()
        result = self.prepare(token, args)
        with self.uow() as conn:
            task = repository.lock_task(conn, self.actor, token.task_id)
            other = leases.claim(conn, self.actor, token.task_id, 'fixture',
                                 'fixture:other-resource', 'fixture:worker', task['version'])
        version = self.read('SELECT version FROM onboarding_tasks WHERE id=%s', (token.task_id,))[0][0]
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                receipts.observe(conn, self.actor, other, result.receipt_id,
                                 Observation('SUCCEEDED', 'fixture:evidence', None), version)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'INTENT')

    def test_confirmed_observation_clears_hold_not_ownership(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        with self.uow() as conn:
            receipts.observe(conn, self.actor, token, result.receipt_id,
                             Observation('SUCCEEDED', 'fixture:evidence', None), self.version(token))
        hold, owner, task_id = self.read('SELECT hold_reason,owner_id,task_id FROM resource_leases')[0]
        self.assertIsNone(hold)
        self.assertEqual(owner, token.owner_id)
        self.assertEqual(str(task_id), token.task_id)

    def test_approval_expiry_is_checked_after_waiting_for_row_lock(self):
        token, args = self.ready()
        ctx = multiprocessing.get_context('spawn')
        started, output = ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_expiry_worker,
                              args=(self.settings.schema, self.actor, token, args, started, output))
        try:
            with self.uow() as conn:
                conn.execute('SELECT id FROM approvals WHERE id=%s FOR UPDATE', (args['approval_id'],))
                process.start()
                self.assertTrue(started.wait(5))
                # The other transaction began before this expiry. now() would
                # accept it; the required wall clock after the lock must not.
                conn.execute('UPDATE approvals SET expires_at=clock_timestamp() WHERE id=%s',
                             (args['approval_id'],))
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(output.get(timeout=3), ErrorCode.APPROVAL_INVALID.value)
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)

    def test_inconclusive_observation_cannot_overwrite_confirmed_result(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        for observation in (Observation('SUCCEEDED', 'fixture:confirmed', 'fixture:original'),
                            Observation('UNKNOWN', 'fixture:late', 'fixture:unconfirmed')):
            with self.uow() as conn:
                receipts.observe(conn, self.actor, token, result.receipt_id, observation, self.version(token))
        row = self.read('SELECT phase,result_code,external_ref FROM operation_receipts')[0]
        self.assertEqual(row, ('SUCCEEDED', 'SUCCEEDED', 'fixture:original'))

    def test_observe_requires_explicit_task_version(self):
        from onboarding import receipts
        token, args = self.ready()
        result = self.prepare(token, args)
        with self.assertRaises(ServiceError) as caught:
            with self.uow() as conn:
                receipts.observe(conn, self.actor, token, result.receipt_id,
                                 Observation('SUCCEEDED', 'fixture:evidence', None), None)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts')[0][0], 'INTENT')

    def test_lease_expiry_while_waiting_on_approval_prevents_intent(self):
        token, args = self.ready()
        with self.uow() as conn:
            expires = conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '2 seconds' RETURNING lease_until").fetchone()[0]
        ctx = multiprocessing.get_context('spawn')
        started, checked, output = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_expiry_worker,
                              args=(self.settings.schema, self.actor, token, args, started, output, checked))
        try:
            with self.uow() as conn:
                conn.execute('SELECT id FROM approvals WHERE id=%s FOR UPDATE', (args['approval_id'],))
                process.start()
                self.assertTrue(checked.wait(5))
                # Deterministically cross the known DB deadline while holding
                # approval, after the worker proved it passed the lease check.
                conn.execute('SELECT pg_sleep_until(%s)', (expires,))
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(output.get(timeout=3), ErrorCode.STALE_FENCE.value)
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(self.read('SELECT count(*) FROM operation_receipts')[0][0], 0)

    def test_other_receipt_cannot_hide_conflict_or_unknown(self):
        from onboarding import approvals, leases, receipts, repository
        for blocked_phase in ('CONFLICT', 'UNKNOWN'):
            with self.subTest(blocked_phase=blocked_phase):
                with self.uow() as conn:
                    conn.execute('UPDATE resource_leases SET owner_id=NULL,task_id=NULL,hold_reason=NULL')
                token, args = self.ready()
                first = self.prepare(token, args)
                with self.uow() as conn:
                    task = repository.lock_task(conn, self.actor, token.task_id)
                    other = leases.claim(conn, self.actor, token.task_id, 'fixture',
                                         'fixture:second-resource', 'fixture:second', task['version'])
                    approval = approvals.issue(conn, self.actor, token.task_id, 'enable',
                                               args['resource_revision'], args['config_revision'], 60)
                second = self.prepare(other, dict(args, action='enable', approval_id=approval))
                with self.uow() as conn:
                    conn.execute('UPDATE operation_receipts SET phase=%s WHERE id=%s', (blocked_phase, first.receipt_id))
                    conn.execute('UPDATE onboarding_tasks SET status=%s WHERE id=%s',
                                 ('CONFLICT' if blocked_phase == 'CONFLICT' else 'RECONCILING', token.task_id))
                with self.uow() as conn:
                    receipts.observe(conn, self.actor, other, second.receipt_id,
                                     Observation('FAILED_CONFIRMED', 'fixture:confirmed', None), self.version(token))
                status = self.read('SELECT status FROM onboarding_tasks WHERE id=%s', (token.task_id,))[0][0]
                self.assertEqual(status, 'CONFLICT' if blocked_phase == 'CONFLICT' else 'RECONCILING')
                with self.uow() as conn:
                    conn.execute('UPDATE resource_leases SET owner_id=NULL,task_id=NULL,hold_reason=NULL')

    def test_consume_serializes_with_revoke_and_generation_in_both_orders(self):
        from onboarding import approvals, receipts, repository
        ctx = multiprocessing.get_context('spawn')
        for mutation in ('revoke', 'generation'):
            for first in ('consume', 'mutation'):
                with self.subTest(mutation=mutation, first=first):
                    with self.uow() as conn:
                        conn.execute('UPDATE resource_leases SET owner_id=NULL,task_id=NULL,hold_reason=NULL')
                    token, args = self.ready()
                    started, output = ctx.Event(), ctx.Queue()
                    operation = mutation if first == 'consume' else 'consume'
                    process = ctx.Process(target=_serialized_worker,
                                          args=(self.settings.schema, self.actor, token, args, operation, started, output))
                    try:
                        # Both contenders need this same task row. Holding it
                        # before the other process enters fixes the order without
                        # guessing scheduler timing or sleeping.
                        with self.uow() as conn:
                            task = repository.lock_task(conn, self.actor, token.task_id)
                            if first == 'consume':
                                original = receipts.prepare(conn, self.actor, token, **args)
                            elif mutation == 'revoke':
                                approvals.revoke(conn, self.actor, args['approval_id'])
                            else:
                                repository.bump_task(conn, task, generation=task['generation'] + 1)
                            process.start()
                            self.assertTrue(started.wait(5))
                        process.join(10)
                        self.assertFalse(process.is_alive())
                        self.assertEqual(process.exitcode, 0)
                        expected = ('MUTATED', None) if first == 'consume' else ('ERROR', ErrorCode.APPROVAL_INVALID.value)
                        self.assertEqual(output.get(timeout=3), expected)
                    finally:
                        if process.is_alive():
                            process.terminate()
                            process.join()
                    count = self.read('SELECT count(*) FROM operation_receipts WHERE task_id=%s', (token.task_id,))[0][0]
                    self.assertEqual(count, 1 if first == 'consume' else 0)
                    if first == 'consume':
                        replay = self.prepare(token, args)
                        self.assertEqual(replay.receipt_id, original.receipt_id)
                        self.assertFalse(replay.newly_consumed)

    def test_lease_expiry_while_waiting_on_receipt_prevents_observation(self):
        token, args = self.ready()
        pending = self.prepare(token, args)
        version = self.version(token)
        with self.uow() as conn:
            expires = conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()+interval '2 seconds' RETURNING lease_until").fetchone()[0]
        ctx = multiprocessing.get_context('spawn')
        checked, output = ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_observe_expiry_worker,
                              args=(self.settings.schema, self.actor, token, pending.receipt_id, version, checked, output))
        try:
            with self.uow() as conn:
                conn.execute('SELECT id FROM operation_receipts WHERE id=%s FOR UPDATE', (pending.receipt_id,))
                process.start()
                self.assertTrue(checked.wait(5))
                conn.execute('SELECT pg_sleep_until(%s)', (expires,))
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(output.get(timeout=3), ErrorCode.STALE_FENCE.value)
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s', (pending.receipt_id,))[0][0], 'INTENT')

    def test_confirmed_resources_do_not_keep_each_others_unresolved_hold(self):
        from onboarding import approvals, leases, receipts, repository
        token, args = self.ready()
        first = self.prepare(token, args)
        with self.uow() as conn:
            task = repository.lock_task(conn, self.actor, token.task_id)
            other = leases.claim(conn, self.actor, token.task_id, 'fixture',
                                 'fixture:second-resource', 'fixture:second', task['version'])
            approval = approvals.issue(conn, self.actor, token.task_id, 'enable',
                                       args['resource_revision'], args['config_revision'], 60)
        second = self.prepare(other, dict(args, action='enable', approval_id=approval))
        for current_token, result in ((token, first), (other, second)):
            with self.uow() as conn:
                receipts.observe(conn, self.actor, current_token, result.receipt_id,
                                 Observation('SUCCEEDED', 'fixture:confirmed', None), self.version(token))
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases'), [(None,), (None,)])
        for current_token in (token, other):
            with self.uow() as conn:
                leases.release(conn, self.actor, current_token, self.version(token))
