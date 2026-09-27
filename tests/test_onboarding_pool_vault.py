"""Write-only synthetic pool vault: real app transactions, no disclosure API."""
from dataclasses import asdict, replace
import importlib
import json
import os
import secrets
import unittest
import uuid
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from onboarding.errors import ErrorCode, ServiceError
from onboarding import audit, security
from onboarding.keyring import Keyring
from onboarding.pool_secret_types import (
    MailboxCredential, PlatformCredential, Pan, BillingHolder, BillingAddress,
    CardExpiry, PoolResource, SecretRef,
)
from onboarding.secret_store import aad_for
from onboarding_b2_support import B2Case


class PoolVaultBoundaryTests(unittest.TestCase):
    def test_write_only_contract_exists_without_disclosure_capability(self):
        api = importlib.import_module('onboarding.pool_vault')
        self.assertTrue(callable(api.PoolVault.put_locked))
        self.assertTrue(callable(api.SyntheticPoolPolicy.from_settings))
        for name in ('use', 'read', 'decrypt', '_decrypt', 'read_for_download', 'export'):
            self.assertFalse(hasattr(api.PoolVault, name))


class PoolVaultTests(B2Case):
    def setUp(self):
        self.api = importlib.import_module('onboarding.pool_vault')
        super().setUp()
        from onboarding.migrate import apply_all
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=2)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=permissions||ARRAY['cards:manage','mailboxes:manage'] WHERE id=%s",
                         (self.actor.operator_id,))
        self.old_store = self.make_store()
        self.mac_path = self.key_directory / 'request-mac.key'
        self.write_key(self.mac_path, secrets.token_bytes(32))
        self.keyring = Keyring(self.key_directory, 'v1')
        self.policy = self.api.SyntheticPoolPolicy.from_settings(self.settings)
        self.vault = self.api.PoolVault(self.keyring, self.policy)
        self.payload = MailboxCredential('one@fixture.invalid', password='fixture:mailbox-canary')

    def write_key(self, path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)

    def put(self, payload=None, resource=None, actor=None, vault=None):
        with self.uow() as conn:
            return (vault or self.vault).put_locked(conn, actor or self.actor,
                resource or PoolResource('mailbox', str(uuid.uuid4())), payload or self.payload)

    def mailbox(self, *, owner=None, email='one@fixture.invalid'):
        identity = str(uuid.uuid4())
        secret = self.put(resource=PoolResource('mailbox', identity))
        with self.uow() as conn:
            conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref) VALUES(%s,%s,%s,'outlook',%s)",
                         (identity, owner or self.actor.operator_id, email, secret.id))
        return identity

    def other_operator(self):
        identity = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,%s,'fixture-hash')", (identity, 'fixture-' + uuid.uuid4().hex))
        return identity

    def test_six_exact_payloads_encrypt_with_actual_kind_aad_and_audit(self):
        payloads = ((self.payload, 'mailbox', 'mailbox_credential'),
                    (PlatformCredential('one@fixture.invalid', 'google', 'fixture:platform-canary'), 'platform', 'platform_credential'),
                    (Pan('4111111111111111'), 'card', 'pan'), (BillingHolder('fixture:holder'), 'billing', 'billing_holder'),
                    (BillingAddress('US','fixture:line','','fixture:city','fixture:region','fixture:postal'), 'billing', 'billing_address'),
                    (CardExpiry(12,2099), 'card', 'card_expiry'))
        for payload, resource_kind, kind in payloads:
            resource = PoolResource(resource_kind, str(uuid.uuid4()))
            result = self.put(payload, resource)
            self.assertIs(type(result), SecretRef)
            self.assertEqual(result.revision, 1)
            row = self.read('SELECT kind,key_version,nonce,ciphertext,access_policy FROM secret_objects WHERE id=%s', (result.id,))[0]
            self.assertEqual(row[:2], (kind, 'v1'))
            self.assertEqual(row[4], f'pool:{self.actor.operator_id}:{resource_kind}:{resource.id}:v1')
            # Test-only decryption, never a public or private vault read method.
            clear = AESGCM(self.keyring.key('v1')).decrypt(row[2], row[3], aad_for(self.fixture.schema,result.id,kind,1))
            self.assertEqual(json.loads(clear), asdict(payload))
            with self.assertRaises(InvalidTag):
                AESGCM(self.keyring.key('v1')).decrypt(row[2],row[3],aad_for(self.fixture.schema,result.id,'fixture',1))
            self.assertNotIn(clear, row[3])
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='secret.put'")[0][0], 6)
        snapshot = repr(self.read('SELECT row_to_json(secret_objects) FROM secret_objects') + self.read('SELECT row_to_json(audit_events) FROM audit_events'))
        for canary in ('fixture:mailbox-canary', 'fixture:platform-canary', '4111111111111111'):
            self.assertNotIn(canary, snapshot)
            self.assertNotIn(canary, repr(self.vault) + repr(self.policy))

    def test_nonce_and_aad_identity_change_for_identical_payload(self):
        first, second = self.put(), self.put()
        rows = self.read('SELECT id,nonce,ciphertext FROM secret_objects ORDER BY created_at')
        self.assertNotEqual(rows[0][1], rows[1][1])
        self.assertNotEqual(rows[0][2], rows[1][2])
        with self.assertRaises(InvalidTag):
            AESGCM(self.keyring.key('v1')).decrypt(rows[0][1],rows[0][2],aad_for(self.fixture.schema,second.id,'mailbox_credential',1))
        self.assertNotEqual(first.id, second.id)

    def test_invalid_payload_and_subclass_never_reach_encryption_or_sql(self):
        class SubPan(Pan):
            pass
        subclass = object.__new__(SubPan)
        object.__setattr__(subclass, 'value', '4111111111111111')
        forged = Pan('4111111111111111')
        object.__setattr__(forged, 'value', '4000000000000002')
        for payload in ({'kind':'pan'}, object(), lambda: None, subclass, forged):
            with patch.object(self.api, 'AESGCM') as encrypt, self.assertRaises(ServiceError):
                self.vault.put_locked(None, self.actor, PoolResource('card', str(uuid.uuid4())), payload)
            encrypt.assert_not_called()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 0)

    def test_dependency_types_reserved_key_name_and_equal_material_are_rejected(self):
        for keyring, policy in ((object(), self.policy), (self.keyring, object())):
            with self.assertRaises(ServiceError):
                self.api.PoolVault(keyring, policy)
        with self.assertRaises(ServiceError):
            self.api.PoolVault(Keyring(self.key_directory, 'request-mac'), self.policy)
        self.write_key(self.mac_path, self.keyring.key('v1'))
        with self.assertRaises(ServiceError):
            self.put()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 0)

    def test_key_files_rechecked_no_fallback_hardlink_or_replacement(self):
        self.mac_path.unlink()
        with self.assertRaises(ServiceError):
            self.put()
        self.assertFalse(self.mac_path.exists())
        os.link(self.key_directory/'v1.key', self.mac_path)
        with self.assertRaises(ServiceError):
            self.put()
        self.mac_path.unlink()
        self.write_key(self.mac_path, secrets.token_bytes(32))
        os.chmod(self.key_directory/'v1.key',0o644)
        with self.assertRaises(ServiceError):
            self.put()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 0)

    def test_permission_is_reloaded_and_fixture_actor_is_never_authorized(self):
        with self.assertRaises(ServiceError):
            self.put(actor=self.fixture_actor)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'mailboxes:manage') WHERE id=%s", (self.actor.operator_id,))
        with self.assertRaises(ServiceError) as caught:
            self.put()
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        self.put(Pan('4111111111111111'), PoolResource('card', str(uuid.uuid4())))

    def test_owner_mailbox_identity_and_resource_kind_are_checked(self):
        own = self.mailbox()
        self.put(resource=PoolResource('mailbox', own))
        with self.assertRaises(ServiceError):
            self.put(MailboxCredential('another@fixture.invalid',password='fixture:pass'), PoolResource('mailbox',own))
        other = self.mailbox(owner=self.other_operator(),email='other@fixture.invalid')
        with self.assertRaises(ServiceError):
            self.put(resource=PoolResource('mailbox',other))
        with self.assertRaises(ServiceError):
            self.put(self.payload,PoolResource('card',str(uuid.uuid4())))

    def test_platform_checks_parent_owner_email_and_platform(self):
        mailbox = self.mailbox()
        platform = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute("INSERT INTO mailbox_platform_states(id,mailbox_id,platform) VALUES(%s,%s,'google')", (platform,mailbox))
        resource = PoolResource('platform',platform)
        self.put(PlatformCredential('one@fixture.invalid','google','fixture:p'),resource)
        for payload in (PlatformCredential('one@fixture.invalid','grok','fixture:p'),
                        PlatformCredential('different@fixture.invalid','google','fixture:p')):
            with self.assertRaises(ServiceError):
                self.put(payload,resource)
        with self.uow() as conn:
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s', (self.other_operator(),mailbox))
        with self.assertRaises(ServiceError):
            self.put(PlatformCredential('one@fixture.invalid','google','fixture:p'),resource)

    def test_policy_requires_private_manifest_and_dedicated_settings(self):
        for changes in ({'schema':'rf_onboarding'}, {'schema':'public'}, {'port':5432}, {'dbname':'production'}):
            with self.assertRaises(ServiceError):
                self.api.SyntheticPoolPolicy.from_settings(replace(self.settings,**changes))
        foreign = replace(self.settings,schema='rf_p1b_test_'+uuid.uuid4().hex)
        with self.assertRaises(ServiceError):
            self.api.SyntheticPoolPolicy.from_settings(foreign)

    def test_actual_role_schema_and_path_must_match_policy_before_business_sql(self):
        with self.fixture.migrator() as conn, conn.transaction():
            with self.assertRaises(ServiceError):
                self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        for path in ('pg_catalog', f'public,{self.fixture.schema},pg_catalog', f'{self.fixture.schema},pg_catalog,public'):
            with self.fixture.app() as conn, conn.transaction():
                conn.execute('SET LOCAL search_path TO '+path)
                with self.assertRaises(ServiceError):
                    self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError):
                self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],0)

    def test_audit_failure_rolls_back_secret_and_never_returns_success(self):
        with patch('onboarding.audit.append',side_effect=RuntimeError('fixture:failure-canary')) as fail:
            with self.assertRaises(ServiceError):
                self.put()
        fail.assert_called_once()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],0)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events')[0][0],0)

    def test_caller_resource_failure_rolls_back_secret_and_audit(self):
        import psycopg
        resource = PoolResource('mailbox',str(uuid.uuid4()))
        insert_reached = False
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                secret = self.vault.put_locked(conn,self.actor,resource,self.payload)
                try:
                    conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref) "
                                 "VALUES(%s,%s,'one@fixture.invalid','invalid-source',%s)",
                                 (resource.id,self.actor.operator_id,secret.id))
                except psycopg.errors.CheckViolation:
                    insert_reached = True
                    raise
        self.assertTrue(insert_reached,'must actually reach resource INSERT CHECK failure')
        for table in ('secret_objects','audit_events','mailbox_registry'):
            self.assertEqual(self.read('SELECT count(*) FROM '+table)[0][0],0)

    def test_caller_can_create_resource_and_secret_in_one_transaction(self):
        resource = PoolResource('mailbox',str(uuid.uuid4()))
        with self.uow() as conn:
            secret = self.vault.put_locked(conn,self.actor,resource,self.payload)
            conn.execute("INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,credential_ref) "
                         "VALUES(%s,%s,'one@fixture.invalid','outlook',%s)",
                         (resource.id,self.actor.operator_id,secret.id))
        self.assertEqual(self.read('SELECT id::text,credential_ref::text FROM mailbox_registry'),[(resource.id,secret.id)])
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],1)
        self.assertEqual(self.read('SELECT count(*) FROM audit_events')[0][0],1)

    def test_session_revoke_epoch_disabled_and_expiry_are_rechecked(self):
        statements = ("UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s",
                      "UPDATE operator_sessions SET auth_epoch=auth_epoch+1 WHERE id=%s",
                      "UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s")
        for statement in statements:
            with self.fixture.app() as conn, conn.transaction(force_rollback=True):
                conn.execute(statement,(self.actor.session_id,))
                with self.assertRaises(ServiceError):
                    self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        with self.fixture.app() as conn, conn.transaction(force_rollback=True):
            conn.execute('UPDATE operators SET disabled=true WHERE id=%s',(self.actor.operator_id,))
            with self.assertRaises(ServiceError):
                self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],0)

    def test_old_fixture_store_cannot_write_or_read_pool_types(self):
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                self.old_store.put(conn,self.actor,'mailbox_credential',b'fixture:mailbox', 'operator:'+self.actor.operator_id)
        secret = self.put()
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                self.old_store.read_for_download(conn,self.actor,secret.id,1)

    def test_final_key_recheck_cannot_extend_session_past_its_deadline(self):
        import time
        original = self.api.PoolVault._keys
        calls = []
        def delayed_keys(vault):
            calls.append(1)
            if len(calls) == 2:
                time.sleep(0.35)
            return original(vault)
        with self.uow() as conn:
            conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '0.2 seconds' WHERE id=%s", (self.actor.session_id,))
        with patch.object(self.api.PoolVault,'_keys',delayed_keys):
            with self.assertRaises(ServiceError) as caught:
                self.put()
        self.assertEqual(caught.exception.code,ErrorCode.UNAUTHENTICATED)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],0)

    def _assert_real_lock_expiry(self, boundary):
        import threading
        import time
        from psycopg import sql
        resource = PoolResource('mailbox',self.mailbox())
        before = self.read('SELECT count(*) FROM secret_objects')[0][0]
        entered, results, pids = threading.Event(), [], []
        if boundary == 'resource':
            original = self.api.PoolVault._resource
            def observed(vault,conn,*args):
                pids.append(conn.info.backend_pid)
                entered.set()
                return original(vault,conn,*args)
            watcher = patch.object(self.api.PoolVault,'_resource',observed)
        else:
            original = audit.append
            def observed(conn,*args,**kwargs):
                pids.append(conn.info.backend_pid)
                entered.set()
                return original(conn,*args,**kwargs)
            watcher = patch('onboarding.audit.append',observed)
        def worker():
            try:
                self.put(resource=resource)
                results.append('UNEXPECTED_SUCCESS')
            except ServiceError as exc:
                results.append(exc.code.value)
        with self.fixture.migrator() as blocker:
            blocker.execute('BEGIN')
            if boundary == 'resource':
                blocker.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE',(resource.id,))
            else:
                blocker.execute('LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE')
            with self.uow() as conn:
                conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()+interval '0.6 seconds' WHERE id=%s",(self.actor.session_id,))
            thread = threading.Thread(target=worker)
            with watcher:
                thread.start()
                try:
                    self.assertTrue(entered.wait(2),'write did not reach lock boundary')
                    waiting = False
                    deadline = time.monotonic()+1
                    while time.monotonic() < deadline:
                        row = blocker.execute('SELECT pg_blocking_pids(%s)',(pids[0],)).fetchone()
                        if row and blocker.info.backend_pid in row[0]:
                            waiting = True
                            break
                        time.sleep(0.01)
                    self.assertTrue(waiting,'expected actual PostgreSQL lock wait')
                    time.sleep(0.7)
                finally:
                    blocker.rollback()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(results,[ErrorCode.UNAUTHENTICATED.value])
        self.assertEqual(len(pids),1)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],before)

    def test_real_resource_lock_wait_rechecks_session_deadline(self):
        self._assert_real_lock_expiry('resource')

    def test_real_audit_lock_wait_rechecks_session_deadline(self):
        self._assert_real_lock_expiry('audit')

    def test_existing_card_and_billing_owners_are_checked_without_billing_update_grant(self):
        owner = self.other_operator()
        billing,card = str(uuid.uuid4()),str(uuid.uuid4())
        holder = self.put(BillingHolder('fixture:holder'),PoolResource('billing',billing))
        address = self.put(BillingAddress('US','fixture:line','','fixture:city','fixture:region','fixture:postal'),PoolResource('billing',billing))
        pan = self.put(Pan('4111111111111111'),PoolResource('card',card))
        expiry = self.put(CardExpiry(12,2099),PoolResource('card',card))
        with self.uow() as conn:
            conn.execute("INSERT INTO billing_identities(id,owner_operator_id,holder_secret_ref,address_secret_ref,country) VALUES(%s,%s,%s,%s,'US')",(billing,owner,holder.id,address.id))
            conn.execute("INSERT INTO payment_cards(id,owner_operator_id,alias,brand,last4,expiry_ref,pan_secret_ref,billing_identity_ref,pan_fingerprint,account_limit) VALUES(%s,%s,'fixture:card','visa','1111',%s,%s,%s,%s,1)",(card,owner,expiry.id,pan.id,billing,'c'*64))
        for resource,payload in ((PoolResource('card',card),CardExpiry(12,2099)),(PoolResource('billing',billing),BillingHolder('fixture:holder'))):
            with self.assertRaises(ServiceError) as caught:
                self.put(payload,resource)
            self.assertEqual(caught.exception.code,ErrorCode.FORBIDDEN)
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE billing_identities SET owner_operator_id=%s WHERE id=%s',(self.actor.operator_id,billing))
        self.put(BillingHolder('fixture:holder'),PoolResource('billing',billing))
        self.assertFalse(self.read("SELECT has_table_privilege(current_user,'billing_identities','UPDATE')")[0][0])

    def test_policy_rechecks_manifest_and_actual_different_schema(self):
        from onboarding_support import SchemaFixture
        other = SchemaFixture()
        self.addCleanup(other.close)
        with other.app() as conn, conn.transaction():
            with self.assertRaises(ServiceError):
                self.vault.put_locked(conn,self.actor,PoolResource('mailbox',str(uuid.uuid4())),self.payload)
        original = self.fixture.manifest.read_bytes()
        try:
            changed = json.loads(original)
            changed['schema_token'] = '0'*32
            self.fixture.manifest.write_text(json.dumps(changed))
            with self.assertRaises(ServiceError):
                self.put()
        finally:
            self.fixture.manifest.write_bytes(original)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0],0)
