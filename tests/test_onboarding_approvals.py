"""Fixture-only approval contract and PostgreSQL atomicity tests."""
import importlib.util
import unittest


class ApprovalBoundaryTests(unittest.TestCase):
    def test_approval_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.approvals'),
                             'B1 approval issuance boundary is absent')


class ApprovalAPITests(unittest.TestCase):
    def test_issue_and_revoke_contract_exists(self):
        from onboarding import approvals
        for name in ('issue', 'revoke'):
            self.assertTrue(callable(getattr(approvals, name, None)), name)

from onboarding_b1_support import B1Case
from onboarding.errors import ErrorCode, ServiceError


class ApprovalTests(B1Case):
    def issue(self, task, **overrides):
        from onboarding import approvals, repository
        args = dict(task_id=str(task['id']), action='test',
                    resource_revision=repository.resource_revision(task),
                    config_revision=task['config_revision'], ttl_seconds=60)
        args.update(overrides)
        with self.uow() as conn:
            return approvals.issue(conn, self.actor, **args)

    def test_issuance_replaces_unconsumed_approval_with_audit(self):
        task = self.task()
        first, second = self.issue(task), self.issue(task)
        rows = self.read('SELECT id, revoked_at, consumed_at FROM approvals ORDER BY created_at')
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0][0]), first)
        self.assertIsNotNone(rows[0][1])
        self.assertIsNone(rows[1][1])
        self.assertEqual(str(rows[1][0]), second)
        self.assertGreaterEqual(self.read('SELECT count(*) FROM audit_events')[0][0], 2)

    def test_invalid_ttl_action_and_resource_fail_closed(self):
        task = self.task()
        for override in ({'ttl_seconds': 59}, {'ttl_seconds': 601}, {'ttl_seconds': True},
                         {'action': 'billing'}, {'resource_revision': 'changed'},
                         {'config_revision': 'changed'}):
            with self.subTest(override=override), self.assertRaises(ServiceError):
                self.issue(task, **override)
        self.assertEqual(self.read('SELECT count(*) FROM approvals')[0][0], 0)

    def test_permissions_are_rechecked_from_database(self):
        task = self.task()
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',
                         (['tasks:manage'], self.actor.operator_id))
        with self.assertRaises(ServiceError) as caught:
            self.issue(task)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_revoke_prevents_consumption_and_is_audited(self):
        from onboarding import approvals
        task = self.task()
        approval = self.issue(task)
        with self.uow() as conn:
            approvals.revoke(conn, self.actor, approval)
        self.assertIsNotNone(self.read('SELECT revoked_at FROM approvals WHERE id=%s',
                                      (approval,))[0][0])

    def test_issue_new_generation_revokes_old_resource_approval(self):
        from onboarding import repository
        task = self.task()
        old = self.issue(task)
        with self.uow() as conn:
            locked = repository.lock_task(conn, self.actor, str(task['id']))
            task = repository.bump_task(conn, locked, generation=2)
        self.issue(task)
        self.assertIsNotNone(self.read('SELECT revoked_at FROM approvals WHERE id=%s',
                                      (old,))[0][0])

    def test_test_enable_download_approvals_are_separate_actions(self):
        task = self.task()
        for action in ('test', 'enable', 'download'):
            self.issue(task, action=action)
        self.assertEqual(self.read('SELECT count(*) FROM approvals WHERE revoked_at IS NULL')[0][0], 3)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',
                         (['tasks:manage', 'fees:approve'], self.actor.operator_id))
        for action in ('enable', 'download'):
            with self.assertRaises(ServiceError) as caught:
                self.issue(task, action=action)
            self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_audit_failure_rolls_back_replacement_and_issuance(self):
        from unittest.mock import patch
        from onboarding import audit
        task = self.task()
        original = self.issue(task)
        with patch.object(audit, 'append', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(ServiceError):
                self.issue(task)
        self.assertEqual(self.read('SELECT count(*) FROM approvals')[0][0], 1)
        self.assertIsNone(self.read('SELECT revoked_at FROM approvals WHERE id=%s', (original,))[0][0])
