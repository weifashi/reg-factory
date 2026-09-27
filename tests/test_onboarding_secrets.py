"""Synthetic AES-GCM, owner/session-bound use and commit-gated disclosure."""
import importlib
import importlib.util
import unittest
from unittest.mock import patch

from onboarding.errors import ErrorCode, ServiceError


class SecretBoundaryTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.secret_store'),
                             'secret storage boundary is missing')
        return importlib.import_module('onboarding.secret_store')

    def test_service_and_safe_consumer_contract(self):
        api = self.api()
        self.assertTrue(callable(getattr(api, 'SecretStore', None)))
        self.assertTrue(callable(getattr(api, 'FixtureConsumer', None)))

    def test_aad_is_canonical_and_binds_all_identity_parts(self):
        api = self.api()
        self.assertEqual(api.aad_for('schema', 'id', 'fixture', 1), b'["schema","id","fixture",1]')
        original = api.aad_for('schema', 'id', 'fixture', 1)
        for args in (('other', 'id', 'fixture', 1), ('schema', 'other', 'fixture', 1),
                     ('schema', 'id', 'other', 1), ('schema', 'id', 'fixture', 2)):
            self.assertNotEqual(api.aad_for(*args), original)

    def test_only_explicit_keyring_dependency_is_allowed(self):
        api = self.api()
        with self.assertRaises(ServiceError) as caught:
            api.SecretStore({'v1': b'not a safe keyring'})
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)

    def test_consumer_repr_does_not_reveal_content_and_has_no_callback_slot(self):
        api = self.api()
        consumer = api.FixtureConsumer()
        with self.assertRaises(AttributeError):
            consumer.consume = lambda value: value
        self.assertNotIn('secret', repr(consumer).lower())

from contextlib import contextmanager
from dataclasses import replace
import os
import uuid
from onboarding_b2_support import B2Case


class SecretTests(B2Case):
    CLEAR = b'fixture:secret-canary-not-for-logs'

    def setUp(self):
        from onboarding.secret_store import SecretStore
        for name in ('put', 'use', 'read_for_download', 'rotate', 'revoke'):
            self.assertTrue(callable(getattr(SecretStore, name, None)), 'missing secret service: ' + name)
        super().setUp()
        self.store = self.make_store()
        self.policy = 'operator:' + self.actor.operator_id

    def put(self, **changes):
        args = dict(kind='fixture', clear_bytes=self.CLEAR, access_policy=self.policy)
        args.update(changes)
        with self.uow() as conn:
            return self.store.put(conn, self.actor, **args)

    def consume(self, secret_id, store=None):
        from onboarding.secret_store import FixtureConsumer
        consumer = FixtureConsumer()
        result = (store or self.store).use(self.settings, self.actor, secret_id, 'fixture', consumer)
        self.assertEqual(consumer.calls, 1)
        self.assertEqual(result, self.CLEAR)
        self.assertNotIn(self.CLEAR.decode(), repr(consumer))
        return result

    def unavailable(self, call):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, ErrorCode.SECRET_UNAVAILABLE)
        self.assertNotIn(self.CLEAR.decode(), str(caught.exception))

    def test_put_encrypts_and_audits_without_persisting_cleartext(self):
        secret_id = self.put()
        row = self.read('SELECT kind,revision,key_version,nonce,ciphertext,access_policy,version FROM secret_objects')[0]
        self.assertEqual(row[:3], ('fixture', 1, 'v1'))
        self.assertEqual(len(row[3]), 12)
        self.assertGreaterEqual(len(row[4]), 16)
        self.assertNotIn(self.CLEAR, row[4])
        self.assertEqual(row[5:], (self.policy, 1))
        snapshot = repr(self.read('SELECT row_to_json(secret_objects) FROM secret_objects') +
                        self.read('SELECT row_to_json(audit_events) FROM audit_events'))
        self.assertNotIn(self.CLEAR.decode(), snapshot)
        self.assertNotIn(self.CLEAR.decode(), repr(self.store))
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='secret.put'")[0][0], 1)
        self.consume(secret_id)

    def test_only_synthetic_bytes_and_own_policy_are_accepted(self):
        for changes in ({'kind': 'pan'}, {'kind': 'otp'}, {'kind': 'cvv'},
                        {'kind': 'google_credential'}, {'clear_bytes': b'not-fixture'},
                        {'clear_bytes': 'fixture:text-not-bytes'}, {'clear_bytes': b'fixture:'},
                        {'clear_bytes': b'fixture:' + b'x' * 65536},
                        {'access_policy': 'public'}, {'access_policy': 'operator:' + str(uuid.uuid4())}):
            with self.subTest(changes=list(changes)), self.assertRaises(ServiceError) as caught:
                self.put(**changes)
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 0)

    def test_fixture_actor_or_permission_snapshot_cannot_bypass_session(self):
        with self.assertRaises(ServiceError):
            with self.uow() as conn:
                self.store.put(conn, self.fixture_actor, 'fixture', self.CLEAR, self.policy)
        secret_id = self.put()
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'keys:download') WHERE id=%s", (self.actor.operator_id,))
        with self.assertRaises(ServiceError) as caught:
            self.consume(secret_id)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_internal_download_read_requires_transaction_and_exact_revision(self):
        secret_id = self.put()
        with self.uow() as conn:
            self.assertEqual(self.store.read_for_download(conn, self.actor, secret_id, 1), self.CLEAR)
        for revision in (2, None, True, '1'):
            with self.subTest(revision=revision):
                self.unavailable(lambda: self.read_revision(secret_id, revision))
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError):
                self.store.read_for_download(conn, self.actor, secret_id, 1)

    def read_revision(self, secret_id, revision):
        with self.uow() as conn:
            return self.store.read_for_download(conn, self.actor, secret_id, revision)

    def test_ciphertext_tamper_and_changed_revision_aad_fail_closed(self):
        for mutation in ('ciphertext', 'revision'):
            secret_id = self.put()
            with self.uow() as conn:
                if mutation == 'ciphertext':
                    cipher = conn.execute('SELECT ciphertext FROM secret_objects WHERE id=%s', (secret_id,)).fetchone()[0]
                    conn.execute('UPDATE secret_objects SET ciphertext=%s WHERE id=%s', (bytes([cipher[0] ^ 1]) + cipher[1:], secret_id))
                else:
                    conn.execute('UPDATE secret_objects SET revision=revision+1 WHERE id=%s', (secret_id,))
            self.unavailable(lambda: self.consume(secret_id))

    def test_ciphertext_is_bound_to_schema_and_identity(self):
        from onboarding import secret_store
        secret_id = self.put()
        real_aad = secret_store.aad_for
        for part in ('schema', 'identity'):
            def wrong_aad(schema, identity, kind, revision):
                return real_aad('wrong-schema' if part == 'schema' else schema,
                                str(uuid.uuid4()) if part == 'identity' else identity, kind, revision)
            with patch.object(secret_store, 'aad_for', wrong_aad):
                self.unavailable(lambda: self.consume(secret_id))

    def test_missing_key_never_falls_back_to_cleartext_or_new_key(self):
        secret_id = self.put()
        (self.key_directory / 'v1.key').unlink()
        self.unavailable(lambda: self.consume(secret_id))
        self.assertFalse((self.key_directory / 'v1.key').exists())

    def test_owner_expiry_revoke_and_missing_objects_are_indistinguishable(self):
        self.unavailable(lambda: self.consume(str(uuid.uuid4())))
        for mutation in ('owner', 'expiry', 'revoke'):
            secret_id = self.put()
            with self.uow() as conn:
                if mutation == 'owner':
                    conn.execute('UPDATE secret_objects SET access_policy=%s WHERE id=%s', ('operator:' + str(uuid.uuid4()), secret_id))
                elif mutation == 'expiry':
                    conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (secret_id,))
                else:
                    self.store.revoke(conn, self.actor, secret_id, 1)
            self.unavailable(lambda: self.consume(secret_id))

    def test_session_revoked_or_expired_after_actor_snapshot_blocks_use(self):
        secret_id = self.put()
        for mutation in ('revoked_at', 'expires_at', 'idle_expires_at'):
            with self.subTest(mutation=mutation):
                with self.uow() as conn:
                    if mutation == 'revoked_at':
                        conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.session_id,))
                    else:
                        conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()-interval '1 second', idle_expires_at=clock_timestamp()-interval '2 seconds' WHERE id=%s", (self.session_id,))
                with self.assertRaises(ServiceError):
                    self.consume(secret_id)
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET revoked_at=NULL,expires_at=clock_timestamp()+interval '8 hours',idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.session_id,))

    def test_fixed_consumer_only_and_purpose_is_not_a_provider_hook(self):
        from onboarding.secret_store import FixtureConsumer
        secret_id = self.put()
        for consumer in (lambda value: value, object()):
            with self.assertRaises(ServiceError):
                self.store.use(self.settings, self.actor, secret_id, 'fixture', consumer)
        class Derived(FixtureConsumer):
            pass
        with self.assertRaises(ServiceError):
            self.store.use(self.settings, self.actor, secret_id, 'fixture', Derived())
        consumer = FixtureConsumer()
        with self.assertRaises(ServiceError):
            self.store.use(self.settings, self.actor, secret_id, 'google', consumer)
        self.assertEqual(consumer.calls, 0)

    def test_commit_unknown_and_failure_never_disclose_to_consumer(self):
        from onboarding import storage
        from onboarding.secret_store import FixtureConsumer
        secret_id = self.put()
        original = storage.unit_of_work
        for committed in (False, True):
            @contextmanager
            def failed(settings):
                if committed:
                    with original(settings) as conn:
                        yield conn
                    raise ServiceError(ErrorCode.COMMIT_UNKNOWN)
                with original(settings) as conn:
                    yield conn
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            consumer = FixtureConsumer()
            with patch.object(storage, 'unit_of_work', failed):
                with self.assertRaises(ServiceError):
                    self.store.use(self.settings, self.actor, secret_id, 'fixture', consumer)
            self.assertEqual(consumer.calls, 0)

    def test_audit_failure_rolls_back_put_and_prevents_use_disclosure(self):
        from onboarding import audit
        from onboarding.secret_store import FixtureConsumer
        with patch.object(audit, 'append', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(ServiceError):
                self.put()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 0)
        secret_id = self.put()
        consumer = FixtureConsumer()
        with patch.object(audit, 'append', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(ServiceError):
                self.store.use(self.settings, self.actor, secret_id, 'fixture', consumer)
        self.assertEqual(consumer.calls, 0)

    def new_store(self):
        from onboarding.keyring import Keyring
        from onboarding.secret_store import SecretStore
        fd = os.open(self.key_directory / 'v2.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(os.urandom(32))
        return SecretStore(Keyring(self.key_directory, 'v2'))

    def test_rotation_changes_envelope_not_business_revision_and_keeps_old_key(self):
        secret_id = self.put()
        before = self.read('SELECT nonce,ciphertext FROM secret_objects WHERE id=%s', (secret_id,))[0]
        store = self.new_store()
        with self.uow() as conn:
            version = store.rotate(conn, self.actor, secret_id, 1)
        self.assertEqual(version, 2)
        row = self.read('SELECT key_version,revision,version,nonce,ciphertext FROM secret_objects WHERE id=%s', (secret_id,))[0]
        self.assertEqual(row[:3], ('v2', 1, 2))
        self.assertNotEqual(row[3:], before)
        self.assertTrue((self.key_directory / 'v1.key').exists())
        self.consume(secret_id, store)
        self.consume(secret_id, self.store)  # Old active writers can still read known v2 files.

    def test_rotation_cas_missing_old_key_and_audit_failure_do_not_modify_envelope(self):
        from onboarding import audit
        secret_id = self.put()
        store = self.new_store()
        before = self.read('SELECT key_version,nonce,ciphertext,revision,version FROM secret_objects WHERE id=%s', (secret_id,))
        for expected_version in (99, None, True):
            with self.assertRaises(ServiceError):
                with self.uow() as conn:
                    store.rotate(conn, self.actor, secret_id, expected_version)
        with patch.object(audit, 'append', side_effect=RuntimeError('fixture audit failure')):
            with self.assertRaises(ServiceError):
                with self.uow() as conn:
                    store.rotate(conn, self.actor, secret_id, 1)
        (self.key_directory / 'v1.key').unlink()
        self.unavailable(lambda: self.rotate(store, secret_id, 1))
        self.assertEqual(self.read('SELECT key_version,nonce,ciphertext,revision,version FROM secret_objects WHERE id=%s', (secret_id,)), before)

    def rotate(self, store, secret_id, version):
        with self.uow() as conn:
            return store.rotate(conn, self.actor, secret_id, version)

    def test_nonce_collision_rolls_back_without_plaintext_fallback(self):
        from onboarding import secret_store
        with patch.object(secret_store.secrets, 'token_bytes', return_value=b'fixed-nonce!'):
            first = self.put()
            with self.assertRaises(ServiceError):
                self.put()
        self.assertEqual(self.read('SELECT count(*) FROM secret_objects')[0][0], 1)
        self.consume(first)

    def test_rotated_object_can_be_read_after_explicit_store_restart(self):
        from onboarding.keyring import Keyring
        from onboarding.secret_store import SecretStore
        secret_id = self.put()
        store = self.new_store()
        self.rotate(store, secret_id, 1)
        restarted = SecretStore(Keyring(self.key_directory, 'v2'))
        self.consume(secret_id, restarted)
        self.assertTrue((self.key_directory / 'v1.key').exists())

    def test_session_and_secret_deadlines_are_checked_after_waiting_for_secret_lock(self):
        import threading
        from onboarding import security
        from onboarding.secret_store import FixtureConsumer
        for deadline_kind in ('session', 'secret'):
            secret_id = self.put()
            consumer = FixtureConsumer()
            with self.uow() as conn:
                if deadline_kind == 'session':
                    deadline = conn.execute("UPDATE operator_sessions SET expires_at=statement_timestamp()+interval '1 second', idle_expires_at=statement_timestamp()+interval '1 second' WHERE id=%s RETURNING expires_at", (self.session_id,)).fetchone()[0]
                else:
                    deadline = conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()+interval '1 second' WHERE id=%s RETURNING expires_at", (secret_id,)).fetchone()[0]
            checked, results = threading.Event(), []
            original = security.revalidate
            def signal_checked(conn, actor, permission):
                result = original(conn, actor, permission)
                checked.set()
                return result
            def use_separate_connection():
                try:
                    self.store.use(self.settings, self.actor, secret_id, 'fixture', consumer)
                    results.append('UNEXPECTED_USE')
                except ServiceError as exc:
                    results.append(exc.code)
            worker = threading.Thread(target=use_separate_connection, daemon=True)
            with patch.object(security, 'revalidate', signal_checked):
                try:
                    with self.uow() as conn:
                        conn.execute('SELECT id FROM secret_objects WHERE id=%s FOR UPDATE', (secret_id,))
                        worker.start()
                        self.assertTrue(checked.wait(5), 'first valid session check missing')
                        conn.execute('SELECT pg_sleep_until(%s)', (deadline,))
                    worker.join(10)
                    self.assertFalse(worker.is_alive())
                finally:
                    if worker.ident is not None:
                        worker.join(10)
            self.assertEqual(consumer.calls, 0)
            self.assertEqual(results, [ErrorCode.UNAUTHENTICATED if deadline_kind == 'session' else ErrorCode.SECRET_UNAVAILABLE])
            with self.uow() as conn:
                conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '8 hours',idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.session_id,))

    def test_audit_lock_crossing_session_or_secret_deadline_never_discloses(self):
        import threading
        from onboarding import audit
        from onboarding.secret_store import FixtureConsumer
        for deadline_kind in ('session', 'secret'):
            with self.subTest(deadline_kind=deadline_kind):
                secret_id = self.put()
                consumer = FixtureConsumer()
                with self.uow() as conn:
                    if deadline_kind == 'session':
                        deadline = conn.execute("UPDATE operator_sessions SET expires_at=statement_timestamp()+interval '1 second', idle_expires_at=statement_timestamp()+interval '1 second' WHERE id=%s RETURNING expires_at", (self.session_id,)).fetchone()[0]
                    else:
                        deadline = conn.execute("UPDATE secret_objects SET expires_at=clock_timestamp()+interval '1 second' WHERE id=%s RETURNING expires_at", (secret_id,)).fetchone()[0]
                reached, results = threading.Event(), []
                original = audit.append
                def signal_audit(*args, **kwargs):
                    reached.set()
                    return original(*args, **kwargs)
                def use_separate_connection():
                    try:
                        self.store.use(self.settings, self.actor, secret_id, 'fixture', consumer)
                        results.append('UNEXPECTED_USE')
                    except ServiceError as exc:
                        results.append(exc.code)
                worker = threading.Thread(target=use_separate_connection, daemon=True)
                with patch.object(audit, 'append', signal_audit):
                    try:
                        with self.fixture.migrator() as conn, conn.transaction():
                            conn.execute('LOCK TABLE audit_events IN SHARE MODE')
                            worker.start()
                            self.assertTrue(reached.wait(5), 'secret.use audit was not reached')
                            conn.execute('SELECT pg_sleep_until(%s)', (deadline,))
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                    finally:
                        if worker.ident is not None:
                            worker.join(10)
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '8 hours',idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.session_id,))
                self.assertEqual(consumer.calls, 0)
                self.assertEqual(results, [ErrorCode.UNAUTHENTICATED if deadline_kind == 'session' else ErrorCode.SECRET_UNAVAILABLE])

    def test_mutation_audit_lock_crossing_session_deadline_rolls_back(self):
        import threading
        from onboarding import audit
        for operation in ('put', 'rotate', 'revoke'):
            with self.subTest(operation=operation):
                secret_id = self.put() if operation != 'put' else None
                before = self.read('SELECT id,key_version,version,revoked_at FROM secret_objects ORDER BY id')
                reached, results = threading.Event(), []
                original = audit.append
                with self.uow() as conn:
                    deadline = conn.execute("UPDATE operator_sessions SET expires_at=statement_timestamp()+interval '1 second',idle_expires_at=statement_timestamp()+interval '1 second' WHERE id=%s RETURNING expires_at", (self.session_id,)).fetchone()[0]
                def signal_audit(*args, **kwargs):
                    reached.set()
                    return original(*args, **kwargs)
                def change_separate_connection():
                    try:
                        with self.uow() as conn:
                            if operation == 'put':
                                self.store.put(conn, self.actor, 'fixture', self.CLEAR, self.policy)
                            elif operation == 'rotate':
                                self.store.rotate(conn, self.actor, secret_id, 1)
                            else:
                                self.store.revoke(conn, self.actor, secret_id, 1)
                        results.append('UNEXPECTED_COMMIT')
                    except ServiceError as exc:
                        results.append(exc.code)
                worker = threading.Thread(target=change_separate_connection, daemon=True)
                with patch.object(audit, 'append', signal_audit):
                    try:
                        with self.fixture.migrator() as conn, conn.transaction():
                            conn.execute('LOCK TABLE audit_events IN SHARE MODE')
                            worker.start()
                            self.assertTrue(reached.wait(5))
                            conn.execute('SELECT pg_sleep_until(%s)', (deadline,))
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                    finally:
                        if worker.ident is not None:
                            worker.join(10)
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '8 hours',idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.session_id,))
                self.assertEqual(results, [ErrorCode.UNAUTHENTICATED])
                self.assertEqual(self.read('SELECT id,key_version,version,revoked_at FROM secret_objects ORDER BY id'), before)
