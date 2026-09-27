"""Read-only preflight contracts; integration tests require root's PG window."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from uuid import uuid4

from onboarding import pool_batches, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.pool_vault import SyntheticPoolPolicy
from onboarding.request_mac import RequestMac
from onboarding.settings import Settings
import onboarding_pool_support
import test_onboarding_pool_batches as batches


def context():
    settings = Settings('unused', 1, 'unused', 'unused', 'unused', 'unused', 'unused')
    policy = object.__new__(SyntheticPoolPolicy)
    object.__setattr__(policy, 'settings', settings)
    actor = security.Actor(str(uuid4()), frozenset(), str(uuid4()), 1)
    return dict(settings=settings, actor=actor, policy=policy, mac=object.__new__(RequestMac))


class PreflightInputTests(unittest.TestCase):
    def test_entry_and_invalid_inputs_fail_before_dependencies(self):
        self.assertTrue(callable(getattr(pool_batches, 'preflight', None)), 'preflight missing')
        args = dict(context(), selection='specified', requested_count=1, mailbox_ids=[str(uuid4())])
        cases = {'selection': [None, 'all', True], 'requested_count': [True, 0, 101, '1', 1.0],
                 'mailbox_ids': [None, (), [], ['bad'], [uuid4().hex]], 'mac': [object()]}
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                     for owner, name in ((storage, 'open_app'), (SyntheticPoolPolicy, '_validate'),
                                         (RequestMac, 'request_digest'))]
            for field, values in cases.items():
                for value in values:
                    with self.subTest(field=field, kind=type(value).__name__):
                        with self.assertRaises(ServiceError) as caught:
                            pool_batches.preflight(**(args | {field: value}))
                        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            with self.assertRaises(ServiceError) as caught:
                pool_batches.preflight(**(args | {'selection': 'automatic'}))
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            for spy in spies:
                spy.assert_not_called()


class PreflightTests(onboarding_pool_support.PoolCase):
    seed = batches.PoolBatchEntryTests.seed
    config = batches.PoolBatchEntryTests.config
    snapshot = batches.PoolBatchEntryTests.snapshot
    change = batches.PoolBatchEntryTests.change
    reject = batches.PoolBatchEntryTests.reject

    def preflight(self, ids=(), *, count=None, automatic=False):
        return pool_batches.preflight(self.settings, self.actor,
            'automatic' if automatic else 'specified', len(ids) if count is None else count,
            [] if automatic else list(ids), policy=self.vault._policy, mac=self.mac)

    def test_observation_no_business_write_no_lease_and_exact_projection(self):
        config = self.config(); seed = self.seed()
        before = self.snapshot()
        with batches.observe() as queries, batches.no_consumption():
            result = self.preflight([seed['mailbox']])
        self.assertEqual(set(result), {'selection','requested_count','eligible_count','config_revision',
                                      'can_create','reason_codes','observation_only'})
        self.assertEqual(result, dict(selection='specified', requested_count=1, eligible_count=1,
            config_revision=config['revision'], can_create=True, reason_codes=[], observation_only=True))
        self.assertEqual(before, self.snapshot())
        for _, sql, _ in queries:
            self.assertFalse(sql.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')))
            if 'FOR UPDATE' in sql:
                self.assertIn('operator_sessions', sql)
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases'), [(0,)])

    def test_no_config_unknown_history_and_short_inventory_are_observations(self):
        seed = self.seed(health='UNKNOWN', usage='HISTORY_UNRECONCILED')
        result = self.preflight([seed['mailbox']])
        self.assertFalse(result['can_create']); self.assertEqual(result['eligible_count'], 0)
        self.assertIsNone(result['config_revision']); self.assertIn('CONFIG_MISSING', result['reason_codes'])
        self.config(); self.seed()
        result = self.preflight(count=2, automatic=True)
        self.assertEqual(result['eligible_count'], 1); self.assertFalse(result['can_create'])
        self.assertIn('INSUFFICIENT_ELIGIBLE', result['reason_codes'])

    def test_automatic_count_is_capped_and_create_still_rechecks(self):
        config = self.config(); seed = self.seed(); self.seed()
        result = self.preflight(count=1, automatic=True)
        self.assertTrue(result['can_create']); self.assertEqual(result['eligible_count'], 1)
        self.change('UPDATE mailbox_registry SET disabled=true WHERE id=%s', (seed['mailbox'],))
        self.reject(lambda: pool_batches.create(self.settings, self.actor, 'specified', 1,
            [seed['mailbox']], config['revision'], 'fixture:changed', policy=self.vault._policy, mac=self.mac),
            ErrorCode.RECONCILIATION_REQUIRED)

    def test_all_specified_owner_checks_precede_eligibility(self):
        seed = self.seed(health='UNKNOWN')
        before = self.snapshot()
        self.reject(lambda: self.preflight([seed['mailbox'], str(uuid4())]), ErrorCode.FORBIDDEN)
        self.assertEqual(before, self.snapshot())

    def test_tasks_only_permission_and_revocation(self):
        self.config(); seed = self.seed()
        self.change("UPDATE operators SET permissions=ARRAY['tasks:manage'] WHERE id=%s", (self.actor.operator_id,))
        self.assertTrue(self.preflight([seed['mailbox']])['can_create'])
        self.change("UPDATE operators SET permissions=ARRAY[]::text[] WHERE id=%s", (self.actor.operator_id,))
        self.reject(lambda: self.preflight([seed['mailbox']]), ErrorCode.FORBIDDEN)

    def test_occupied_resource_observation_preserves_hold(self):
        config = self.config(); seed = self.seed()
        pool_batches.create(self.settings, self.actor, 'specified', 1, [seed['mailbox']], config['revision'],
                            'fixture:held', policy=self.vault._policy, mac=self.mac)
        before = self.snapshot(); result = self.preflight([seed['mailbox']])
        self.assertFalse(result['can_create']); self.assertEqual(result['eligible_count'], 0)
        self.assertIn('INSUFFICIENT_ELIGIBLE', result['reason_codes']); self.assertEqual(before, self.snapshot())
