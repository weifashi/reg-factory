"""Real disposable PG/source/receipts; only Google auth and HTTP are test doubles."""
import asyncio
from contextlib import contextmanager
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from onboarding import live_admission, live_batches, live_config, live_entry, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.google_auth_runtime import GoogleAuthConfig
from onboarding_pool_support import PoolCase
from test_onboarding_live_config import FIELDS
from onboarding_live_support import populate


class LiveEntryDBTests(PoolCase):
    def setUp(self):
        super().setUp()
        populate(self, create=False)
        self.path = self.source.data_root / 'emails.txt'
        self.original_source = self.path.read_text()
        self.mid = self.resource['id']
        self.requests = []

    def admit(self, *, open=True):
        def respond(request):
            self.requests.append((request.method, str(request.url)))
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'cloudbilling.googleapis.com')
            return httpx.Response(200, json={'name': 'billingAccounts/' + FIELDS['billing_account'], 'open': open})
        with patch('onboarding.google_auth_runtime._verify_paths'), \
                patch('onboarding.live_admission.GoogleTokenProvider.__call__', new=AsyncMock(return_value='unit-test-token')):
            return asyncio.run(live_admission.prepare(self.settings, self.actor, self.source, self.mid,
                self.cfg['revision'], policy=self.live_policy, mac=self.mac,
                google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'), google_transport=httpx.MockTransport(respond)))

    def create(self, grant, key='entry'):
        return live_batches.create_one(self.settings, self.actor, self.mid, self.cfg['revision'], key,
                                        policy=self.live_policy, mac=self.mac, admission=grant)

    def start(self, grant, key='entry'):
        from onboarding.live_key_store import ServiceAccountKeyStore
        from onboarding.sub2api_probe import ProbeConfig
        return live_entry.start_committed(self.settings, self.actor, self.mid, self.cfg['revision'], key,
            policy=self.live_policy, mac=self.mac, admission=grant, key_store=ServiceAccountKeyStore(self.settings, self.keyring),
            google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
            sub2api_config=ProbeConfig('http://127.0.0.1:8800', 'admin@fixture.invalid', 'fixture-password'))

    def test_explicit_live_insert_same_secret_preserves_unknown_history_and_replay(self):
        result = self.create(self.admit())
        self.assertFalse(result['replayed'])
        self.assertEqual(self.read('SELECT execution_mode,status,version FROM onboarding_tasks'), [('live', 'QUEUED', 2)])
        self.assertEqual(self.read('SELECT health FROM mailbox_registry'), [('UNKNOWN',)])
        states = self.read("SELECT identity_status,usage_status,credential_ref FROM mailbox_platform_states WHERE platform='google'")
        self.assertEqual(states[0][:2], ('UNKNOWN', 'RESERVED'))
        self.assertEqual(states[0][2], self.read('SELECT credential_ref FROM mailbox_registry')[0][0])
        before = self.business_snapshot()
        replay = self.create(None)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['task_ids'], result['task_ids'])
        self.assertEqual(before, self.business_snapshot())
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects'), [(1,)])
        self.assertNotIn('unit-test-password', json.dumps(result))
        self.assertEqual(result['original_usage_status'], 'HISTORY_UNRECONCILED')

    def test_billing_visibility_failure_has_no_batch_or_qualification_side_effect(self):
        before = self.business_snapshot()
        with self.assertRaises(ServiceError): self.admit(open=False)
        self.assertEqual(before, self.business_snapshot())

    def test_source_or_config_change_invalidates_grant(self):
        grant = self.admit()
        self.path.write_text('weifashi68@gmail.com----another-unit-test-password\n')
        with self.assertRaises(ServiceError): self.create(grant)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])
        self.path.write_text(self.original_source)
        grant = self.admit()
        live_config.replace(self.settings, self.actor, self.cfg['revision'], FIELDS.copy(), 'newconfig',
                            policy=self.live_policy, mac=self.mac)
        with self.assertRaises(ServiceError): self.create(grant)
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks'), [(0,)])

    def test_opaque_grant_copy_reuse_and_expiry_are_rejected(self):
        grant = self.admit()
        with self.assertRaises(ServiceError): self.create(copy.copy(grant))
        with patch('onboarding.live_admission.time.monotonic', return_value=grant._deadline + 1):
            with self.assertRaises(ServiceError): self.create(grant)
        self.create(grant)
        with self.assertRaises(ServiceError): self.create(grant, 'different-key')

    def test_constructor_only_after_two_confirmed_commits(self):
        grant = self.admit()
        original = storage.unit_of_work
        commits = []
        @contextmanager
        def counted(settings):
            with original(settings) as conn: yield conn
            commits.append(True)
        def construct(*args, **kwargs):
            self.assertEqual(len(commits), 2)
            return object()
        with patch.object(storage, 'unit_of_work', counted), patch.object(live_entry, 'LiveExecutor', side_effect=construct):
            prepared = self.start(grant)
        self.assertIsNotNone(prepared.executor)
        self.assertEqual(self.read('SELECT status,current_step FROM onboarding_tasks'), [('RUNNING', 'project.create')])
        self.assertEqual(self.read('SELECT count(*) FROM live_run_plans'), [(1,)])
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        with patch.object(live_entry, 'LiveExecutor', side_effect=AssertionError('replay started')):
            self.assertIsNone(self.start(None).executor)

    def test_invalid_server_target_is_rejected_before_creating_or_consuming_grant(self):
        from onboarding.live_key_store import ServiceAccountKeyStore
        from onboarding.sub2api_probe import ProbeConfig
        grant = self.admit()
        before = self.business_snapshot()
        with self.assertRaises(ValueError):
            live_entry.start_committed(self.settings, self.actor, self.mid, self.cfg['revision'], 'entry',
                policy=self.live_policy, mac=self.mac, admission=grant,
                key_store=ServiceAccountKeyStore(self.settings, self.keyring),
                google_auth_config=GoogleAuthConfig('weifashi68@gmail.com'),
                sub2api_config=ProbeConfig('https://unapproved.invalid', 'admin@fixture.invalid', 'fixture-password'))
        self.assertEqual(before, self.business_snapshot())
        self.assertFalse(self.create(grant)['replayed'])

    def _unknown(self, at):
        grant = self.admit()
        original = storage.unit_of_work
        commits = []
        @contextmanager
        def lost_ack(settings):
            with original(settings) as conn: yield conn
            commits.append(True)
            if len(commits) == at:
                raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
        with patch.object(storage, 'unit_of_work', lost_ack), patch.object(live_entry, 'LiveExecutor') as constructor:
            with self.assertRaises(ServiceError) as caught: self.start(grant)
            self.assertEqual(caught.exception.code, ErrorCode.COMMIT_UNKNOWN)
            constructor.assert_not_called()
        self.assertEqual(self.read('SELECT count(*) FROM live_effect_journal'), [(0,)])
        with patch.object(live_entry, 'LiveExecutor') as constructor:
            replay = self.start(None)
            self.assertIsNone(replay.executor)
            constructor.assert_not_called()

    def test_first_commit_unknown_cannot_start_or_restart_on_replay(self):
        self._unknown(1)

    def test_second_commit_unknown_cannot_start_or_restart_on_replay(self):
        self._unknown(2)

    def test_old_key_and_cross_session_cannot_grant_start(self):
        grant = self.admit()
        with self.uow() as conn:
            conn.execute('INSERT INTO operation_receipts(id,scope_operator_id,action,resource_revision,generation,fence,'
                "phase,idempotency_key,request_hash,result_code,result_summary) VALUES(%s,%s,'pool.batch.create',%s,1,0,"
                "'SUCCEEDED','old-key',%s,'BATCH_ACCEPTED','{}')", (str(uuid4()), self.actor.operator_id,
                'pool-admin:' + self.actor.operator_id, 'a' * 64))
        with self.assertRaises(ServiceError) as caught: self.create(grant, 'old-key')
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        with self.assertRaises(ServiceError):
            live_batches.create_one(self.settings, self.second_session(), self.mid, self.cfg['revision'], 'other-session',
                                     policy=self.live_policy, mac=self.mac, admission=grant)

    def test_source_validation_delay_cannot_outlive_current_authority(self):
        from onboarding.live_mailbox_source import OwnedMailboxSource
        original = OwnedMailboxSource.validate
        with self.uow() as conn:
            def revoked(source, *args):
                result = original(source, *args)
                conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                             (self.actor.session_id,))
                return result
            with patch.object(OwnedMailboxSource, 'validate', revoked):
                with self.assertRaises(ServiceError) as caught:
                    live_admission.inspect(conn, self.settings, self.actor, self.source, self.mid,
                                           self.cfg['revision'], self.live_policy, self.mac)
                self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_tx2_policy_delay_revocation_rolls_back_handoff_without_constructor(self):
        from onboarding.live_input_vault import LivePoolPolicy
        grant = self.admit()
        original = LivePoolPolicy._connection
        def late_revoke(policy, conn):
            result = original(policy, conn)
            if conn.execute("SELECT 1 FROM onboarding_tasks WHERE status='RUNNING'").fetchone():
                conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',
                             (self.actor.session_id,))
            return result
        with patch.object(LivePoolPolicy, '_connection', late_revoke), \
                patch.object(live_entry, 'LiveExecutor') as constructor:
            with self.assertRaises(ServiceError) as caught: self.start(grant)
            self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
            constructor.assert_not_called()
        self.assertEqual(self.read('SELECT status FROM onboarding_tasks'), [('QUEUED',)])
        self.assertEqual(self.read('SELECT count(*) FROM live_run_plans'), [(0,)])

    def test_create_owns_file_boundary_before_taking_mailbox_related_locks(self):
        from onboarding import pool_leases
        grant = self.admit()
        management = live_batches.management_lock
        read_lease = pool_leases._read_lease
        held = []
        @contextmanager
        def tracked(root):
            with management(root) as boundary:
                held.append(True)
                try: yield boundary
                finally: held.pop()
        def lease(conn, *args, **kwargs):
            self.assertTrue(held, 'create acquired PG resource locks before the shared file boundary')
            return read_lease(conn, *args, **kwargs)
        with patch.object(live_batches, 'management_lock', tracked), \
                patch.object(pool_leases, '_read_lease', lease):
            self.create(grant)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LiveEntryDBTests)
