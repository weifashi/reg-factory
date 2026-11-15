"""Disposable PostgreSQL evidence: lookup never reclaims or restarts a task."""
from unittest.mock import patch
from uuid import uuid4

from onboarding import live_batches, live_config
from onboarding.errors import ErrorCode, ServiceError
from onboarding_live_support import populate, admit
from onboarding_pool_support import PoolCase
from test_onboarding_live_config import FIELDS


class LiveObserveDBTests(PoolCase):
    def setUp(self):
        super().setUp()
        populate(self, create=False)
        self.mid = self.resource['id']

    def observe(self, *, mid=None, revision=None, key='lookup'):
        return live_batches.observe_request(self.settings, self.actor, mid or self.mid,
            revision or self.cfg['revision'], key, policy=self.live_policy, mac=self.mac)

    def create(self):
        return live_batches.create_one(self.settings, self.actor, self.mid, self.cfg['revision'],
            'lookup', policy=self.live_policy, mac=self.mac, admission=admit(self))

    def test_absence_and_exact_replay_are_read_only_and_do_not_prepare(self):
        before = self.business_snapshot()
        with patch('onboarding.live_admission.prepare', side_effect=AssertionError('provider lookup')):
            self.assertIsNone(self.observe())
        self.assertEqual(before, self.business_snapshot())
        created = self.create()
        before = self.business_snapshot()
        with patch('onboarding.live_admission.prepare', side_effect=AssertionError('provider lookup')):
            replay = self.observe()
        self.assertEqual(replay, {**created, 'replayed': True})
        self.assertEqual(before, self.business_snapshot())

    def test_same_key_different_request_is_conflict_not_a_new_permission(self):
        self.create()
        before = self.business_snapshot()
        for values in ({'mid': str(uuid4())}, {'revision': 'pool-' + str(uuid4())}):
            with self.subTest(values=values), self.assertRaises(ServiceError) as caught:
                self.observe(**values)
            self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(before, self.business_snapshot())

    def test_historical_lookup_does_not_require_source_head_or_live_lease(self):
        created = self.create()
        live_config.replace(self.settings, self.actor, self.cfg['revision'], FIELDS.copy(), 'newhead',
                            policy=self.live_policy, mac=self.mac)
        (self.source.data_root / 'emails.txt').unlink()
        with self.uow() as conn:
            conn.execute('UPDATE resource_leases SET lease_until=clock_timestamp()-interval \'1 second\'')
            conn.execute('UPDATE mailbox_registry SET disabled=true')
            conn.execute('UPDATE secret_objects SET expires_at=clock_timestamp()-interval \'1 second\'')
        before = self.business_snapshot()
        self.assertEqual(self.observe()['task_ids'], created['task_ids'])
        self.assertEqual(before, self.business_snapshot())

    def test_altered_receipt_or_revoked_session_never_returns_observation(self):
        self.create()
        with self.uow() as conn:
            conn.execute('UPDATE operation_receipts SET request_hash=%s WHERE action=%s',
                         ('f' * 64, live_batches.ACTION))
        with self.assertRaises(ServiceError):
            self.observe()
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                         (self.actor.session_id,))
        with self.assertRaises(ServiceError) as caught:
            self.observe(key='absent')
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_old_synthetic_key_is_not_treated_as_absence(self):
        with self.uow() as conn:
            conn.execute('INSERT INTO operation_receipts(id,scope_operator_id,action,resource_revision,generation,fence,'
                "phase,idempotency_key,request_hash,result_code,result_summary) VALUES(%s,%s,'pool.batch.create',%s,1,0,"
                "'SUCCEEDED','lookup',%s,'BATCH_ACCEPTED','{}')", (str(uuid4()), self.actor.operator_id,
                'pool-admin:' + self.actor.operator_id, 'a' * 64))
        with self.assertRaises(ServiceError) as caught:
            self.observe()
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_changed_batch_config_cannot_be_reported_as_original_request(self):
        created = self.create()
        newer = live_config.replace(self.settings, self.actor, self.cfg['revision'], FIELDS.copy(), 'newhead',
                                    policy=self.live_policy, mac=self.mac)
        with self.uow() as conn:
            conn.execute('UPDATE onboarding_batches SET config_id=%s WHERE id=%s',
                         (newer['config_id'], created['batch_id']))
        with self.assertRaises(ServiceError):
            self.observe()
