"""Manifest-scoped real PostgreSQL; test-only made-up password material."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from onboarding import security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.migrate import apply_all
from onboarding.pool_secret_types import PoolResource
from onboarding.secret_store import aad_for
from onboarding_pool_support import PoolCase, LockHold, after_query, outcome_of


class LiveInputVaultDBTests(PoolCase):
    def _race_import(self, original, replay):
        # Imports now serialize at the shared OS boundary BEFORE taking PG
        # mailbox locks. Waiting for pg_blocking_pids would test the obsolete
        # lock order, not simultaneous imports under the actual contract.
        from contextlib import contextmanager
        import threading
        from common import managed_mailboxes
        hold, attempted, results = LockHold('INSERT INTO mailbox_registry'), threading.Event(), {}
        manager = managed_mailboxes.management_lock
        @contextmanager
        def tracked(root):
            if threading.current_thread().name == 'fixture-r1': attempted.set()
            with manager(root) as boundary: yield boundary
        threads = []
        with patch.object(managed_mailboxes, 'management_lock', tracked), after_query(hold.hook):
            try:
                first = threading.Thread(target=lambda: results.__setitem__('original', outcome_of(original)), name='fixture-r0')
                threads.append(first); first.start()
                self.assertTrue(hold.locked.wait(10), 'original never acquired its write boundary')
                second = threading.Thread(target=lambda: results.__setitem__('replay', outcome_of(replay)), name='fixture-r1')
                threads.append(second); second.start()
                self.assertTrue(attempted.wait(5), 'replay never reached shared ownership lock')
                self.assertNotIn('replay', results, 'replay bypassed the original ownership lock')
            finally:
                hold.release.set()
                for thread in threads: thread.join(15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return results['original'], results['replay']

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.live_input_vault'))
        super().setUp()
        self.m = importlib.import_module('onboarding.live_input_vault')
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=4)
        self.live_policy = self.m.LivePoolPolicy(self.settings)
        self.live_vault = self.m.LiveInputVault(self.keyring, self.live_policy)
        self.payload = self.m.LiveMailboxCredential('weifashi68@gmail.com','unit-test-password-canary')

    def put(self):
        resource = PoolResource('mailbox', str(uuid4()))
        with self.uow() as conn:
            ref = self.live_vault.put_mailbox_locked(conn, self.actor, resource.id, self.payload)
        return resource, ref

    def test_encrypts_real_typed_mailbox_in_distinct_namespace(self):
        resource, ref = self.put()
        row = self.read('SELECT kind,key_version,nonce,ciphertext,access_policy FROM secret_objects WHERE id=%s', (ref.id,))[0]
        self.assertEqual(row[0], 'mailbox_credential')
        self.assertEqual(row[4], f'pool-live-v1:{self.actor.operator_id}:mailbox:{resource.id}:v1')
        self.assertNotIn(b'unit-test-password-canary', bytes(row[3]))
        # Test-only decryption is not a production plaintext getter.
        body = AESGCM(self.keyring.key(row[1])).decrypt(bytes(row[2]),bytes(row[3]),
            aad_for(self.settings.schema,ref.id,row[0],1))
        self.assertEqual(json.loads(body)['password'], self.payload.password)

    def test_old_synthetic_vault_still_rejects_live_payload(self):
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid4())),self.payload)

    def test_revoked_session_cannot_store_material(self):
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s',(self.actor.session_id,))
        with self.assertRaises(ServiceError) as caught:
            self.put()
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE access_policy LIKE %s", ("pool-live-v1:%",)),[(0,)])

    def source(self):
        from onboarding.live_mailbox_source import OwnedMailboxSource
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name); root.chmod(0o700)
        path = root/'emails.txt'
        path.write_text('weifashi68@gmail.com----unit-test-password-canary\n'); path.chmod(0o600)
        return OwnedMailboxSource(root,self.actor.operator_id,'weifashi68@gmail.com'), path

    def preview(self, source):
        return self.m.preview_source(self.settings,self.actor,source,'owned',vault=self.live_vault,mac=self.mac)

    def import_source(self, source, digest, key='entry-test', actor=None):
        return self.m.import_source(self.settings,actor or self.actor,source,'owned',digest,key,vault=self.live_vault,mac=self.mac)

    def test_original_source_import_is_atomic_idempotent_and_not_qualified(self):
        source, path = self.source(); raw = path.read_bytes()
        preview = self.preview(source)
        self.assertNotIn('password',json.dumps(preview))
        result = self.import_source(source,preview['source_digest'])
        self.assertEqual(self.import_source(source,preview['source_digest']),result)
        skipped = self.import_source(source,preview['source_digest'],'another-key')
        self.assertFalse(skipped['created']); self.assertEqual(skipped['mailbox_id'],result['mailbox_id'])
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.read('SELECT health,source_type,credential_version FROM mailbox_registry'),[('UNKNOWN','other',1)])
        states = self.read('SELECT identity_status,usage_status,credential_ref FROM mailbox_platform_states')
        self.assertEqual(len(states),7)
        self.assertEqual(set(states),{('UNKNOWN','HISTORY_UNRECONCILED',None)})
        self.assertEqual(self.read('SELECT count(*) FROM onboarding_tasks'),[(0,)])
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases'),[(0,)])

    def test_source_change_after_writes_rolls_back_import(self):
        source, path = self.source(); preview = self.preview(source)
        original = self.m.LiveInputVault.put_mailbox_locked
        def changed(*args):
            value = original(*args)
            path.write_text('weifashi68@gmail.com----different-test-password\n')
            return value
        with patch.object(self.m.LiveInputVault,'put_mailbox_locked',changed):
            with self.assertRaises(ServiceError):
                self.import_source(source,preview['source_digest'])
        self.assertEqual(self.read('SELECT count(*) FROM mailbox_registry'),[(0,)])
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE access_policy LIKE %s", ("pool-live-v1:%",)),[(0,)])
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='live.mailbox.import'"),[(0,)])

    def test_credentials_do_not_silently_overwrite_existing_version(self):
        source,path=self.source(); first=self.preview(source)
        result=self.import_source(source,first['source_digest'])
        before=self.read('SELECT credential_ref,credential_version,version FROM mailbox_registry')
        path.write_text('weifashi68@gmail.com----different-test-password\n')
        newer=self.preview(source)
        with self.assertRaises(ServiceError) as caught:
            self.import_source(source,newer['source_digest'],'new-key')
        self.assertEqual(caught.exception.code,ErrorCode.VERSION_CONFLICT)
        self.assertEqual(before,self.read('SELECT credential_ref,credential_version,version FROM mailbox_registry'))

    def test_concurrent_first_import_same_key_returns_same_committed_receipt(self):
        source,path=self.source(); actor2=self.second_session()
        digest=self.preview(source)['source_digest']
        first,second=self._race_import(
            lambda:self.import_source(source,digest),
            lambda:self.import_source(source,digest,actor=actor2))
        self.assertEqual(first[0],'OK'); self.assertEqual(second[0],'OK')
        self.assertEqual(first[1],second[1]); self.assertTrue(first[1]['created'])
        self.assertEqual(self.read('SELECT count(*) FROM mailbox_registry'),[(1,)])
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE access_policy LIKE %s", ("pool-live-v1:%",)),[(1,)])

    def test_concurrent_first_import_different_keys_creates_only_one_secret(self):
        source,path=self.source(); actor2=self.second_session()
        digest=self.preview(source)['source_digest']
        first,second=self._race_import(
            lambda:self.import_source(source,digest),
            lambda:self.import_source(source,digest,'other-key',actor2))
        self.assertEqual(first[0],'OK'); self.assertEqual(second[0],'OK')
        self.assertEqual(first[1]['mailbox_id'],second[1]['mailbox_id'])
        self.assertTrue(first[1]['created']); self.assertFalse(second[1]['created'])
        self.assertEqual(self.read("SELECT count(*) FROM secret_objects WHERE access_policy LIKE %s", ("pool-live-v1:%",)),[(1,)])

    def test_postwrite_auth_and_mac_and_aes_failures_rollback(self):
        import secrets
        from onboarding import audit
        source,path=self.source(); original=audit.append
        for kind in ('auth','mac','aes'):
            with self.subTest(kind=kind):
                digest=self.preview(source)['source_digest']; before=self.business_snapshot()
                def fail(conn,*args,**kwargs):
                    result=original(conn,*args,**kwargs)
                    if args[2]=='mailbox.import':
                        if kind=='auth': conn.execute('UPDATE operators SET disabled=true WHERE id=%s',(self.actor.operator_id,))
                        if kind=='mac': (self.key_directory/'request-mac.key').write_bytes(secrets.token_bytes(32))
                        if kind=='aes': (self.key_directory/'v1.key').write_bytes(secrets.token_bytes(32))
                    return result
                with patch.object(audit,'append',fail),self.assertRaises(ServiceError):
                    self.import_source(source,digest)
                self.assertEqual(before,self.business_snapshot())

    def test_real_commit_lost_ack_has_no_success_return_and_same_key_replay_is_readonly(self):
        from contextlib import contextmanager
        import psycopg
        from psycopg.pq import TransactionStatus
        source,path=self.source(); digest=self.preview(source)['source_digest']
        original=psycopg.Connection.transaction; committed=[]
        @contextmanager
        def lost_ack(conn,*args,**kwargs):
            with original(conn,*args,**kwargs) as transaction:
                yield transaction
            if conn.info.transaction_status==TransactionStatus.IDLE:
                committed.append(True)
                raise psycopg.OperationalError('test-only-lost-ack')
        with patch.object(psycopg.Connection,'transaction',lost_ack):
            with self.assertRaises(ServiceError) as caught:
                self.import_source(source,digest)
        self.assertEqual(caught.exception.code,ErrorCode.COMMIT_UNKNOWN)
        self.assertFalse(caught.exception.not_committed); self.assertEqual(committed,[True])
        before=self.business_snapshot()
        result=self.import_source(source,digest)
        self.assertTrue(result['created']); self.assertEqual(before,self.business_snapshot())

    def test_current_secret_and_receipt_metadata_are_revalidated_on_replay(self):
        source,path=self.source(); digest=self.preview(source)['source_digest']
        self.import_source(source,digest)
        for mutation,restore,code in (
            ('UPDATE secret_objects SET revision=2','UPDATE secret_objects SET revision=1',ErrorCode.VERSION_CONFLICT),
            ('UPDATE secret_objects SET revoked_at=clock_timestamp()','UPDATE secret_objects SET revoked_at=NULL',ErrorCode.VERSION_CONFLICT),
            ("UPDATE operation_receipts SET generation=2 WHERE action='live.mailbox.import'",
             "UPDATE operation_receipts SET generation=1 WHERE action='live.mailbox.import'",ErrorCode.DEPENDENCY_UNAVAILABLE),
            ("UPDATE operation_receipts SET fence=1 WHERE action='live.mailbox.import'",
             "UPDATE operation_receipts SET fence=0 WHERE action='live.mailbox.import'",ErrorCode.DEPENDENCY_UNAVAILABLE),
            ("UPDATE operation_receipts SET external_ref='test-only-unexpected' WHERE action='live.mailbox.import'",
             "UPDATE operation_receipts SET external_ref=NULL WHERE action='live.mailbox.import'",ErrorCode.DEPENDENCY_UNAVAILABLE)):
            with self.uow() as conn: conn.execute(mutation)
            before=self.business_snapshot()
            with self.assertRaises(ServiceError) as caught:
                self.import_source(source,digest)
            self.assertEqual(caught.exception.code,code); self.assertEqual(before,self.business_snapshot())
            with self.uow() as conn: conn.execute(restore)

    def test_existing_mailbox_owned_by_another_operator_is_not_reused(self):
        source,path=self.source(); digest=self.preview(source)['source_digest']
        self.import_source(source,digest); owner=str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,%s)',
                         (owner,'unit-test-other-owner','not-a-real-password-hash'))
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s',(owner,))
        before=self.business_snapshot()
        with self.assertRaises(ServiceError) as caught:
            self.import_source(source,digest,'new-key')
        self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
        self.assertEqual(before,self.business_snapshot())

    def test_schema_drift_is_rejected_before_reading_original_mailbox(self):
        source,path=self.source()
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=4',('0'*64,))
        with patch.object(type(source),'snapshot',side_effect=AssertionError('source must not be read')) as reader:
            with self.assertRaises(ServiceError) as caught:
                self.preview(source)
        self.assertEqual(caught.exception.code,ErrorCode.VERSION_CONFLICT); reader.assert_not_called()
