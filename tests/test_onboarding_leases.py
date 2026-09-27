"""Real PostgreSQL lease invariants; spawn processes never share connections."""
import importlib.util
import unittest


class LeaseInterfaceTests(unittest.TestCase):
    def test_lease_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.leases'))

    def test_frozen_stop_evidence_contract_exists(self):
        self.assertTrue(hasattr(leases, 'FixtureStopEvidence'))
        from dataclasses import FrozenInstanceError
        evidence = leases.FixtureStopEvidence('task', 'fixture', 'fixture:x', 'owner', 1)
        with self.assertRaises(FrozenInstanceError):
            evidence.fence = 2


# Added as a separate integration layer once the shared B1 fixture is present.
import multiprocessing
from dataclasses import replace
import uuid

from onboarding import leases
from onboarding.errors import ErrorCode, ServiceError
from onboarding_b1_support import B1Case


def _claim_worker(schema, actor_id, task_id, barrier, queue):
    """Only synthetic IDs and barriers cross spawn, never DB credentials."""
    from onboarding.repository import FixtureActor
    from onboarding.settings import BASE, load_settings
    from onboarding.storage import unit_of_work
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    try:
        with unit_of_work(settings) as conn:
            barrier.wait(timeout=15)
            token = leases.claim(conn, FixtureActor(actor_id), task_id,
                                 'mailbox', 'fixture:shared-mailbox',
                                 'fixture:worker-' + task_id, 1)
        queue.put(('CLAIMED', token.fence))
    except ServiceError as exc:
        queue.put((exc.code.value, None))
    except Exception:
        queue.put(('UNEXPECTED', None))


def _deadlock_worker(schema, actor_id, first_task, target_task, barrier, queue):
    from onboarding.repository import FixtureActor
    from onboarding.settings import BASE, load_settings
    from onboarding.storage import unit_of_work
    import psycopg
    settings = replace(load_settings(BASE / 'app.json'), schema=schema)
    saw_deadlock = False
    try:
        with unit_of_work(settings) as conn:
            conn.execute('SELECT id FROM onboarding_tasks WHERE id=%s FOR UPDATE', (first_task,))
            barrier.wait(timeout=15)
            try:
                leases.claim(conn, FixtureActor(actor_id), target_task, 'fixture',
                             'fixture:deadlock-' + target_task, 'fixture:worker', 1)
            except psycopg.errors.DeadlockDetected:
                saw_deadlock = True
                raise
        queue.put(('CLAIMED', target_task))
    except ServiceError as exc:
        queue.put(('DEADLOCK_ROLLED_BACK' if saw_deadlock else exc.code.value, target_task))
    except Exception:
        queue.put(('UNEXPECTED', target_task))


class LeaseTests(B1Case):
    def claim(self, task=None, resource='fixture:mailbox', owner='fixture:worker-a'):
        task = task or self.task()
        with self.uow() as conn:
            token = leases.claim(conn, self.actor, str(task['id']), 'mailbox',
                                 resource, owner, task['version'])
        return task, token

    def version(self, task_id):
        return self.read('SELECT version FROM onboarding_tasks WHERE id=%s', (task_id,))[0][0]

    def expire(self, token):
        with self.uow() as conn:
            conn.execute("UPDATE resource_leases SET lease_until=clock_timestamp()-interval '1 second' "
                         'WHERE resource_kind=%s AND resource_id=%s',
                         (token.resource_kind, token.resource_id))

    def stop_evidence(self, token):
        return leases.FixtureStopEvidence(token.task_id, token.resource_kind,
                                          token.resource_id, token.owner_id, token.fence)

    def recover(self, token):
        with self.uow() as conn:
            return leases.recover(conn, self.actor, token.task_id, token.resource_kind,
                                  token.resource_id, 'fixture:worker-b', self.stop_evidence(token))

    def test_claim_persists_monotonic_token_and_audit(self):
        task, token = self.claim()
        self.assertEqual(token.fence, 1)
        self.assertEqual(token.task_id, str(task['id']))
        self.assertEqual(self.version(token.task_id), 2)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events WHERE task_id=%s '
                                  "AND action='lease.claim'", (token.task_id,))[0][0], 1)
        with self.uow() as conn:
            row = leases.assert_current(conn, token)
            self.assertEqual(row['fence'], 1)

    def test_two_processes_only_one_claims(self):
        tasks = [self.task(), self.task()]
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(2)
        queue = context.Queue()
        children = [context.Process(target=_claim_worker, args=(
            self.settings.schema, self.actor.operator_id, str(task['id']), barrier, queue))
                    for task in tasks]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=25) for _ in children]
            for child in children:
                child.join(timeout=15)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sorted(item[0] for item in results), ['CLAIMED', 'RESOURCE_HELD'])
            self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 1)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            queue.close()
            queue.join_thread()
        self.claim(resource='fixture:independent-mailbox')

    def test_expired_lease_keeps_logical_hold(self):
        _, token = self.claim()
        self.expire(token)
        other = self.task()
        with self.assertRaises(ServiceError) as raised:
            self.claim(other, resource=token.resource_id)
        self.assertEqual(raised.exception.code, ErrorCode.RESOURCE_HELD)
        self.assertEqual(str(self.read('SELECT task_id FROM resource_leases')[0][0]), token.task_id)
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.renew(conn, token)
        self.assertEqual(raised.exception.code, ErrorCode.STALE_FENCE)

    def test_old_fence_cannot_write_or_release(self):
        _, old = self.claim()
        self.expire(old)
        new = self.recover(old)
        self.assertEqual(new.fence, old.fence + 1)
        for operation in ('assert', 'renew', 'release'):
            with self.subTest(operation=operation), self.assertRaises(ServiceError) as raised:
                with self.uow() as conn:
                    if operation == 'assert':
                        leases.assert_current(conn, old)
                    elif operation == 'renew':
                        leases.renew(conn, old)
                    else:
                        leases.release(conn, self.actor, old, self.version(old.task_id))
            self.assertEqual(raised.exception.code, ErrorCode.STALE_FENCE)
        with self.uow() as conn:
            leases.assert_current(conn, new)

    def test_recovery_requires_exact_typed_stopping_evidence(self):
        _, token = self.claim()
        self.expire(token)
        invalid = [None, 'worker-stopped', replace(self.stop_evidence(token), fence=99),
                   replace(self.stop_evidence(token), owner_id='fixture:wrong-owner')]
        for evidence in invalid:
            with self.subTest(evidence=type(evidence).__name__), self.assertRaises(ServiceError):
                with self.uow() as conn:
                    leases.recover(conn, self.actor, token.task_id, token.resource_kind,
                                   token.resource_id, 'fixture:worker-b', evidence)
        self.assertEqual(self.read('SELECT fence FROM resource_leases')[0][0], token.fence)

    def test_recovery_cannot_move_held_resource_to_another_task(self):
        _, token = self.claim()
        self.expire(token)
        other = self.task()
        with self.assertRaises(ServiceError), self.uow() as conn:
            leases.recover(conn, self.actor, str(other['id']), token.resource_kind,
                           token.resource_id, 'fixture:worker-b', self.stop_evidence(token))

    def test_renew_uses_db_clock_and_bounded_ttl(self):
        _, token = self.claim()
        for ttl in (4, 61, True, 5.5, '30'):
            with self.subTest(ttl=ttl), self.assertRaises(ServiceError), self.uow() as conn:
                leases.renew(conn, token, ttl_seconds=ttl)
        with self.uow() as conn:
            leases.renew(conn, token, ttl_seconds=60)
            remaining = conn.execute('SELECT extract(epoch FROM lease_until-clock_timestamp()) '
                                     'FROM resource_leases').fetchone()[0]
            self.assertGreater(remaining, 58)
            self.assertLessEqual(remaining, 60)

    def test_wait_human_and_unknown_preserve_hold_on_recovery(self):
        _, token = self.claim()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='WAIT_HUMAN' WHERE id=%s", (token.task_id,))
            conn.execute("UPDATE resource_leases SET hold_reason='UNKNOWN' WHERE resource_id=%s",
                         (token.resource_id,))
        self.expire(token)
        new = self.recover(token)
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0], 'UNKNOWN')
        with self.assertRaises(ServiceError), self.uow() as conn:
            leases.release(conn, self.actor, new, self.version(new.task_id))

    def test_release_requires_safe_terminal_and_preserves_fence_row(self):
        _, token = self.claim()
        with self.assertRaises(ServiceError), self.uow() as conn:
            leases.release(conn, self.actor, token, self.version(token.task_id))
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='CANCELLED_SAFE' WHERE id=%s", (token.task_id,))
            leases.release(conn, self.actor, token, self.version(token.task_id))
        row = self.read('SELECT task_id,owner_id,fence FROM resource_leases')[0]
        self.assertEqual(row, (None, None, token.fence))
        _, replacement = self.claim(resource=token.resource_id)
        self.assertEqual(replacement.fence, token.fence + 1)

    def test_unresolved_receipts_prevent_release_even_if_terminal(self):
        _, token = self.claim()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='SUCCEEDED' WHERE id=%s", (token.task_id,))
            conn.execute('INSERT INTO operation_receipts(id,task_id,action,resource_revision,'
                         'idempotency_key,request_hash,phase,fence,generation) '
                         "VALUES (%s,%s,'test','fixture:v1','fixture:pending',%s,'UNKNOWN',%s,1)",
                         (uuid.uuid4(), token.task_id, '0' * 64, token.fence))
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.release(conn, self.actor, token, self.version(token.task_id))
        self.assertEqual(raised.exception.code, ErrorCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.read('SELECT fence FROM resource_leases')[0][0], token.fence)

    def test_raw_real_resource_and_autocommit_rejected(self):
        task = self.task()
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.claim(conn, self.actor, str(task['id']), 'card', 'real-resource',
                         'fixture:worker-a', task['version'])
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError):
                leases.claim(conn, self.actor, str(task['id']), 'fixture', 'fixture:x',
                             'fixture:worker-a', task['version'])
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)

    def test_paused_task_cannot_claim(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='PAUSED' WHERE id=%s", (task['id'],))
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.claim(conn, self.actor, str(task['id']), 'mailbox', 'fixture:paused',
                         'fixture:worker', task['version'])
        self.assertEqual(raised.exception.code, ErrorCode.RESOURCE_HELD)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)

    def test_disconnected_claim_rolls_back_and_can_be_safely_retried(self):
        task = self.task()
        with self.assertRaises(ServiceError) as raised:
            with self.uow() as conn:
                leases.claim(conn, self.actor, str(task['id']), 'fixture', 'fixture:disconnect',
                             'fixture:worker', task['version'])
                conn.close()
                # This is pre-COMMIT failure, not an ambiguous COMMIT retry.
                conn.execute('SELECT 1')
        self.assertEqual(raised.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)
        self.assertEqual(self.version(str(task['id'])), task['version'])
        self.claim(task, resource='fixture:disconnect')

    def test_audit_failure_rolls_back_claim_and_task_version(self):
        from unittest.mock import patch
        task = self.task()
        with patch('onboarding.audit.append', side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)) as fail:
            with self.assertRaises(ServiceError), self.uow() as conn:
                leases.claim(conn, self.actor, str(task['id']), 'fixture', 'fixture:audit-fail',
                             'fixture:worker', task['version'])
            fail.assert_called_once()
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)
        self.assertEqual(self.version(str(task['id'])), task['version'])

    def test_fence_overflow_never_wraps_or_claims(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute('INSERT INTO resource_leases(resource_kind,resource_id,fence) '
                         "VALUES ('fixture','fixture:max',9223372036854775807)")
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.claim(conn, self.actor, str(task['id']), 'fixture', 'fixture:max',
                         'fixture:worker', task['version'])
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.read('SELECT task_id,fence FROM resource_leases')[0],
                         (None, 9223372036854775807))

    def test_unknown_hold_cannot_be_cleared_by_terminal_task_alone(self):
        _, token = self.claim()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='CANCELLED_SAFE' WHERE id=%s", (token.task_id,))
            conn.execute("UPDATE resource_leases SET hold_reason='UNKNOWN' WHERE resource_id=%s",
                         (token.resource_id,))
        with self.assertRaises(ServiceError), self.uow() as conn:
            leases.release(conn, self.actor, token, self.version(token.task_id))
        self.assertEqual(self.read('SELECT hold_reason FROM resource_leases')[0][0], 'UNKNOWN')

    def test_forged_owner_token_cannot_renew(self):
        _, token = self.claim()
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.renew(conn, replace(token, owner_id='fixture:imposter'))
        self.assertEqual(raised.exception.code, ErrorCode.STALE_FENCE)

    def test_claim_stale_task_version_changes_nothing(self):
        task = self.task()
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.claim(conn, self.actor, str(task['id']), 'fixture', 'fixture:stale',
                         'fixture:worker', task['version'] + 1)
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)


    def test_real_deadlock_rolls_back_then_allows_one_explicit_retry(self):
        tasks = [self.task(), self.task()]
        context = multiprocessing.get_context('spawn')
        barrier, queue = context.Barrier(2), context.Queue()
        children = [context.Process(target=_deadlock_worker, args=(
            self.settings.schema, self.actor.operator_id, str(tasks[index]['id']),
            str(tasks[1-index]['id']), barrier, queue)) for index in range(2)]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=25) for _ in children]
            for child in children:
                child.join(timeout=15)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sorted(item[0] for item in results),
                             ['CLAIMED', 'DEADLOCK_ROLLED_BACK'])
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            queue.close()
            queue.join_thread()
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 1)
        loser_id = next(task_id for state, task_id in results if state == 'DEADLOCK_ROLLED_BACK')
        self.assertEqual(self.version(loser_id), 1)
        with self.uow() as conn:
            token = leases.claim(conn, self.actor, loser_id, 'fixture',
                                 'fixture:deadlock-' + loser_id, 'fixture:retry-worker', 1)
        self.assertEqual(token.fence, 1)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 2)

    def test_maximum_length_resource_id_can_be_claimed_and_audited(self):
        resource_id = 'fixture:' + ('x' * 248)
        _, token = self.claim(resource=resource_id)
        self.assertEqual(token.resource_id, resource_id)
        self.assertEqual(self.read("SELECT after_summary->>'resource_kind' FROM audit_events "
                                  "WHERE action='lease.claim'")[0][0], 'mailbox')

    def test_untyped_resource_kind_returns_invalid_input(self):
        task = self.task()
        with self.assertRaises(ServiceError) as raised, self.uow() as conn:
            leases.claim(conn, self.actor, str(task['id']), [], 'fixture:invalid',
                         'fixture:worker', task['version'])
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)

    def test_claim_requires_explicit_positive_task_version(self):
        task = self.task()
        for version in (None, True, 0, -1, '1'):
            with self.subTest(version=version):
                with self.assertRaises(ServiceError) as raised, self.uow() as conn:
                    leases.claim(conn, self.actor, str(task['id']), 'fixture',
                                 'fixture:no-cas', 'fixture:worker', version)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases')[0][0], 0)

    def test_release_requires_explicit_positive_task_version(self):
        _, token = self.claim()
        with self.uow() as conn:
            conn.execute("UPDATE onboarding_tasks SET status='CANCELLED_SAFE' WHERE id=%s", (token.task_id,))
        for version in (None, True, 0, -1, '1'):
            with self.subTest(version=version):
                with self.assertRaises(ServiceError) as raised, self.uow() as conn:
                    leases.release(conn, self.actor, token, version)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(str(self.read('SELECT task_id FROM resource_leases')[0][0]), token.task_id)
